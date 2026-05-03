"""STAIRS PE adapter (Phase 7).

Deterministic single-path PE projection that replaces the TA model's
constant ``growth_pct`` with a CMA-driven, public-equity-coupled growth
term. Same call schedule, same distribution curve, same per-row
schema — only the per-quarter NAV-mark term changes.

Per-quarter recursion (per fund with sleeve ``s``)::

    expected_quarterly_pu = cma.expected_returns_annual["public_equity"] / 4
    realized_quarterly_pu = public_equity_path.get(quarter_t, expected_quarterly_pu)
    excess                = realized_quarterly_pu - expected_quarterly_pu

    drift                 = stairs_defaults.per_sleeve[s].idiosyncratic_drift_pct / 4
    beta                  = stairs_defaults.per_sleeve[s].beta_to_public_equity

    growth_pct_q          = drift + beta * excess
    growth_pct_q          = max(growth_pct_q, _GROWTH_FLOOR)   # required clip

    nav_mark_t            = nav_after_dist * growth_pct_q

The clip is a **domain constraint**: ``growth_pct_q ≥ -0.99`` keeps
NAV strictly non-negative. The count of quarters where the clip
activated is surfaced via :meth:`diagnostics` so the user sees when it
is biting. Upside is unbounded.

See MODEL_DOCUMENTATION.md §Phase 7 design.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pandas as pd

from aa_model.pe.base import PEAdapter
from aa_model.pe.ta_model import PROJECTION_COLUMNS

if TYPE_CHECKING:
    from aa_model.assumptions.cma import CMA
    from aa_model.io.schemas import (
        FundConfig,
        PEPacingConfig,
        TADefaultsConfig,
    )


_GROWTH_FLOOR: float = -0.99
"""Domain constraint: ``growth_pct_q`` cannot push NAV below zero. The
clip prevents floating-point edge cases at exactly -1.0."""


class STAIRSAdapter(PEAdapter):
    """Public-equity-coupled deterministic single-path adapter."""

    def __init__(self) -> None:
        self._clipped_quarters: int = 0

    def project_horizon(
        self,
        pacing: PEPacingConfig,
        horizon_start: pd.Period,
        num_quarters: int,
        *,
        cma: CMA,
        public_equity_path: pd.Series,
    ) -> pd.DataFrame:
        if pacing.stairs_defaults is None:
            raise ValueError(
                "STAIRSAdapter.project_horizon: pacing.stairs_defaults is "
                "required (cross-config validation should have caught this)"
            )
        # Reset diagnostics so a single adapter instance reused across runs
        # doesn't accumulate counts.
        self._clipped_quarters = 0

        if not pacing.funds:
            return pd.DataFrame(columns=list(PROJECTION_COLUMNS) + ["sleeve"])

        if "public_equity" not in cma.expected_returns_annual.index:
            raise ValueError(
                "STAIRSAdapter requires cma.expected_returns_annual to include "
                "'public_equity'; missing in this CMA"
            )
        expected_quarterly_pu = float(cma.expected_returns_annual.loc["public_equity"]) / 4.0

        # Project every fund over its full lifetime (TA's behavior), then
        # filter to horizon at the end. Per-fund full-lifetime projection
        # avoids horizon-edge discontinuities for vintages that pre-date
        # the run.
        parts: list[pd.DataFrame] = []
        for fund in pacing.funds:
            sleeve_params = pacing.stairs_defaults.per_sleeve.get(fund.sleeve)
            if sleeve_params is None:
                raise ValueError(
                    f"STAIRSAdapter: fund {fund.name!r} sleeve "
                    f"{fund.sleeve!r} has no entry in "
                    "stairs_defaults.per_sleeve (cross-config validation "
                    "should have caught this)"
                )
            df = self._project_fund(
                fund,
                pacing.ta_defaults,
                idiosyncratic_drift_pct=float(sleeve_params.idiosyncratic_drift_pct),
                beta=float(sleeve_params.beta_to_public_equity),
                expected_quarterly_pu=expected_quarterly_pu,
                public_equity_path=public_equity_path,
            )
            parts.append(df)

        proj = pd.concat(parts, ignore_index=True)
        # Filter to the run horizon, mirroring pacing.project_horizon.
        horizon_strs = {str(horizon_start + i) for i in range(num_quarters)}
        proj = proj[proj["quarter"].isin(horizon_strs)].copy()
        fund_to_sleeve = {f.name: f.sleeve for f in pacing.funds}
        proj["sleeve"] = proj["fund_name"].map(fund_to_sleeve)
        return proj.reset_index(drop=True)

    def diagnostics(self) -> dict:
        return {
            "engine": "STAIRSAdapter",
            "clipped_quarters": self._clipped_quarters,
            "growth_floor": _GROWTH_FLOOR,
        }

    def _project_fund(
        self,
        fund: FundConfig,
        defaults: TADefaultsConfig,
        *,
        idiosyncratic_drift_pct: float,
        beta: float,
        expected_quarterly_pu: float,
        public_equity_path: pd.Series,
    ) -> pd.DataFrame:
        """Per-fund full-lifetime projection with the STAIRS NAV-mark
        term. Identical to ``ta_model.project_fund`` except for the
        per-quarter ``growth_pct_q`` computation; preserved verbatim
        otherwise to keep the linear-commitment property and the
        per-quarter ordering.
        """
        L = defaults.lifetime_years
        P = defaults.commitment_period_years
        rc = defaults.rate_of_contribution
        B = defaults.bow
        Y = defaults.yield_pct
        K = fund.commitment_usd
        vintage = pd.Period(fund.vintage, freq="Q-DEC")

        n_quarters = 4 * L
        rows: list[dict] = []
        nav = 0.0
        drift_quarterly = idiosyncratic_drift_pct / 4.0

        for t in range(n_quarters):
            year_index = t // 4
            age_years = (t + 1) / 4.0
            quarter = vintage + t

            call = (rc[year_index] * K) / 4.0 if year_index < P else 0.0
            nav_after_call = nav + call

            annual_dist_rate = max(Y, (age_years / L) ** B)
            quarterly_dist_rate = min(annual_dist_rate / 4.0, 1.0)
            distribution = quarterly_dist_rate * nav_after_call
            nav_after_dist = nav_after_call - distribution

            # STAIRS coupling: realized public_equity excess vs CMA
            # expectation. Quarters outside the supplied path are
            # treated as ``excess = 0`` (CMA-expectation default).
            try:
                realized_quarterly_pu = float(public_equity_path.loc[quarter])
            except KeyError:
                realized_quarterly_pu = expected_quarterly_pu
            excess = realized_quarterly_pu - expected_quarterly_pu

            growth_pct_q = drift_quarterly + beta * excess
            if growth_pct_q < _GROWTH_FLOOR:
                growth_pct_q = _GROWTH_FLOOR
                self._clipped_quarters += 1

            nav_mark = nav_after_dist * growth_pct_q
            nav_end = nav_after_dist + nav_mark

            rows.append(
                {
                    "fund_name": fund.name,
                    "vintage": str(vintage),
                    "quarter_index": t,
                    "quarter": str(quarter),
                    "age_years": age_years,
                    "nav_start_usd": nav,
                    "call_usd": call,
                    "distribution_usd": distribution,
                    "nav_mark_usd": nav_mark,
                    "nav_end_usd": nav_end,
                }
            )
            nav = nav_end

        return pd.DataFrame(rows, columns=list(PROJECTION_COLUMNS))
