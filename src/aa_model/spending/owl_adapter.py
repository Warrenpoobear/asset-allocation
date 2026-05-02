"""Owl spending rule (SPEC §6 Phase 3c, Phase 4a fix for L15 / L18,
Phase 11 fix for L16).

"Owl" is the project codename for a Guyton-Klinger–style guardrail
spending policy. There is no external "Owl" Python library.

Phase 4a behavior — realized-NAV feedback
=========================================

For each quarter ``t = 0..N-1``:

* **q0 (initialization)**: return ``cfg.annual_spend_usd / 4``. No
  guardrail check, no inflation step, no special ledger event. Per
  Phase 4 design: *q0 is initialization, not a guardrail decision.*
* **mid-year (t % 4 != 0)**: return the same quarterly amount this
  rule emitted at ``t - 1`` (within-year constancy).
* **year boundary (t > 0, t % 4 == 0)**:

      annual_spend_t = annual_spend_{year_t-1} · (1 + inflation_pct)
      nav_realized   = ledger.end_nav_through(t - 1).sum()
      current_rate   = annual_spend_t / nav_realized
      initial_rate   = cfg.annual_spend_usd / sum(ledger.initial_nav.values())

      if current_rate < initial_rate · (1 - lower_band_pct):
          annual_spend_t *= (1 + raise_pct)
      elif current_rate > initial_rate · (1 + upper_band_pct):
          annual_spend_t *= (1 - cut_pct)

      # Phase 11 / L16 — optional absolute-dollar clamps:
      if gr.absolute_min_annual_usd is not None:
          annual_spend_t = max(annual_spend_t, gr.absolute_min_annual_usd)
      if gr.absolute_max_annual_usd is not None:
          annual_spend_t = min(annual_spend_t, gr.absolute_max_annual_usd)

  Both ``annual_spend_{year_t-1}`` and ``nav_realized`` are read
  from the closed ledger via :meth:`QuarterlyLedger.closed_through`
  and :meth:`QuarterlyLedger.end_nav_through` — no shared state, no
  forecast assumption.

Phase 11 / L16 — absolute-dollar clamps
=======================================

When ``gr.absolute_min_annual_usd`` and / or ``absolute_max_annual_usd``
are set, the trigger output is clamped to a dollar floor / ceiling
that does NOT scale with initial NAV. This breaks the rate-based
scale-invariance documented as L16: under proportional setup
(``annual_spend ∝ initial_nav``) the rate-band test cancels NAV
algebraically, so a $100M and $1B household with the same
proportional setup get identical Owl trajectories. Adding a
dollar-denominated decision (the absolute clamps) breaks that.

**Phase 11 fixes scale-invariance only. It does NOT resolve
spending-base realism (L19).** Owl still measures rate against
**total modeled NAV**, including illiquid private real estate, opco
equity, etc. For a Gen3-Gen5 SFO this may overstate spending
capacity. See MODEL_DOCUMENTATION.md §Use-case context + §Phase 11
design.

Phase 4 discipline
==================

* **Pure (with one allowance)**: no module-level state. The Owl rule
  retains a per-instance counter of clamp activations
  (``_min_clamp_activations`` and ``_max_clamp_activations``) so the
  report can surface them; this state is reset on each call to
  :meth:`quarterly_outflow_at` for a fresh ``params.start_quarter``
  and accumulates across quarters within a single run. The state
  does NOT influence the trigger output and is purely diagnostic.
* **No ledger mutation.** May read the ledger passed into the
  ``SpendingRule`` interface (per Phase 3+ adapter discipline) but
  may not retain, mutate, or access global state.
* **Source filter.** Reads only its own prior spend rows
  (``source == "spending:owl"``); other rules' history is ignored
  even if a config switch left it in the ledger.
* **Closed-prior-quarter view only.** Sees ``ledger[quarter <= q-1]``;
  never reads the current quarter's flows.

Resolves L15 (forecast-only NAV), L18 (Owl misreads inflation
shock as headroom), and L16 (scale-invariance, Phase 11; partial —
spending-base realism L19 remains open).
"""

from __future__ import annotations

import pandas as pd

from aa_model.integration.ledger import QuarterlyLedger
from aa_model.spending.base import SpendingParams, SpendingRule
from aa_model.spending.rules import _quarter_offset, _read_own_prior_spend
from aa_model.spending.spending_base import (
    SpendingBaseBreakdown,
    compute_spending_base,
)


class OwlRule(SpendingRule):
    """Guyton-Klinger guardrail spending against realized prior-quarter NAV."""

    SOURCE_ID = "spending:owl"

    def __init__(self) -> None:
        # Phase 11 / L16 diagnostic counters: how many year-boundary
        # quarters had the absolute floor / ceiling clamp activate.
        # Surfaced via diagnostics() for the report. Does NOT influence
        # the trigger output.
        self._min_clamp_activations: int = 0
        self._max_clamp_activations: int = 0
        self._last_start_quarter: pd.Period | None = None
        # Phase 12 / L19 diagnostics — populated at the year-boundary
        # path each year so the report can surface base-side realism.
        # These are last-evaluation snapshots, not run-aggregates: the
        # report uses the most recent value (the "run end" snapshot).
        self._last_spending_base_mode: str | None = None
        self._last_spending_base_realized: SpendingBaseBreakdown | None = None
        self._last_spending_base_initial: SpendingBaseBreakdown | None = None
        self._last_total_nav_realized: float = 0.0
        self._last_annual_spend: float = 0.0

    def diagnostics(self) -> dict:
        """Return Phase 11 clamp-activation counts plus Phase 12
        spending-base snapshots from the most recent year-boundary
        evaluation. Resets when the rule sees a new
        ``params.start_quarter`` (i.e., a new run)."""
        out: dict = {
            "engine": "OwlRule",
            "min_clamp_activations": self._min_clamp_activations,
            "max_clamp_activations": self._max_clamp_activations,
            "spending_base_mode": self._last_spending_base_mode,
            "spending_base_run_end_usd": (
                self._last_spending_base_realized.base_usd
                if self._last_spending_base_realized is not None
                else 0.0
            ),
            "spending_base_initial_usd": (
                self._last_spending_base_initial.base_usd
                if self._last_spending_base_initial is not None
                else 0.0
            ),
            "total_nav_run_end_usd": self._last_total_nav_realized,
            "excluded_nav_by_tier_usd": (
                dict(self._last_spending_base_realized.excluded_by_tier_usd)
                if self._last_spending_base_realized is not None
                else {}
            ),
            "excluded_nav_by_income_flag_usd": (
                dict(self._last_spending_base_realized.excluded_by_income_flag_usd)
                if self._last_spending_base_realized is not None
                else {}
            ),
        }
        # Withdrawal-rate comparison snapshots. Guarded for divide-by-zero.
        annual = self._last_annual_spend
        total_nav = self._last_total_nav_realized
        base_usd = out["spending_base_run_end_usd"]
        out["withdrawal_rate_vs_total_nav"] = (
            (annual / total_nav) if total_nav > 0.0 else 0.0
        )
        out["withdrawal_rate_vs_spending_base"] = (
            (annual / base_usd) if base_usd > 0.0 else 0.0
        )
        # (illiquid + locked_strategic) / total_nav at run end —
        # consumed by the report's default-base material-illiquid
        # warning (reviewer tightening: alert when default mode is
        # used but the SFO has material non-spendable NAV).
        excluded_by_tier = out["excluded_nav_by_tier_usd"]
        if total_nav > 0.0 and excluded_by_tier:
            material = float(
                excluded_by_tier.get("illiquid", 0.0)
                + excluded_by_tier.get("locked_strategic", 0.0)
            )
            out["material_illiquid_share"] = material / total_nav
        else:
            out["material_illiquid_share"] = 0.0

        # Phase 12.5 / L19 flow-side diagnostics. Populated when the
        # selected mode is distributable_income; empty / neutral
        # otherwise. The report renderer keys off these fields plus
        # spending_base_mode to choose the third render mode.
        last_realized = self._last_spending_base_realized
        if (
            self._last_spending_base_mode == "distributable_income"
            and last_realized is not None
        ):
            out["trailing_distributable_income_usd"] = float(
                last_realized.base_usd
            )
            out["distributable_income_by_source_usd"] = dict(
                last_realized.distributable_income_by_source_usd
            )
            out["used_bootstrap_at_run_end"] = bool(last_realized.is_bootstrap)
        else:
            out["trailing_distributable_income_usd"] = 0.0
            out["distributable_income_by_source_usd"] = {}
            out["used_bootstrap_at_run_end"] = False
        return out

    def quarterly_outflow_at(
        self,
        ledger: QuarterlyLedger,
        params: SpendingParams,
        quarter: pd.Period,
    ) -> float:
        cfg = params.config
        if cfg.guardrail is None:
            raise ValueError("OwlRule requires spending.guardrail config")
        gr = cfg.guardrail

        initial_nav_total = float(sum(ledger.initial_nav.values()))
        if initial_nav_total <= 0.0:
            raise ValueError(f"OwlRule requires positive initial NAV; got {initial_nav_total}")

        # Reset clamp + base diagnostics on a new run (new start_quarter).
        if self._last_start_quarter != params.start_quarter:
            self._min_clamp_activations = 0
            self._max_clamp_activations = 0
            self._last_start_quarter = params.start_quarter
            self._last_spending_base_mode = None
            self._last_spending_base_realized = None
            self._last_spending_base_initial = None
            self._last_total_nav_realized = 0.0
            self._last_annual_spend = 0.0

        # q0 initialization — no guardrail, no inflation, no ledger read.
        # Floor / ceiling clip is applied so a rule emitting a clipped value
        # at q0 still satisfies its config bounds and the wrapper's prior-row
        # recovery sees the clipped value at q1.
        if quarter == params.start_quarter:
            quarterly = cfg.annual_spend_usd / 4.0
            return max(cfg.floor_usd, min(cfg.ceiling_usd, quarterly))

        offset = _quarter_offset(quarter, params.start_quarter)
        quarter_in_year = offset % 4
        prior_q = quarter - 1
        prior_quarterly = _read_own_prior_spend(ledger, self.SOURCE_ID, prior_q)

        # Mid-year: within-year constancy. Same quarterly as prior.
        if quarter_in_year != 0:
            return max(cfg.floor_usd, min(cfg.ceiling_usd, prior_quarterly))

        # Year boundary: inflate, then guardrail-check vs realized NAV.
        prior_annual = prior_quarterly * 4.0
        annual_spend = prior_annual * (1.0 + cfg.inflation_pct)

        # Phase 12 / L19: replace the rate-band denominators with the
        # configured spending base on both sides. Default
        # (gr.spending_base in {None, "total_nav"}) is byte-identical
        # to the Phase 11 path (compute_spending_base short-circuits
        # to nav.sum()). Phase 12.5 adds the distributable_income
        # branch via the new ledger / prior_quarter / window /
        # bootstrap kwargs (additive, ignored by NAV-side modes).
        nav_realized_series = ledger.end_nav_through(prior_q)
        nav_realized_total = float(nav_realized_series.sum())
        realized = compute_spending_base(
            nav_realized_series,
            params.cma_liquidity,
            params.cma_income_producing,
            gr.spending_base,
            gr.spending_base_weights,
            ledger=ledger,
            prior_quarter=prior_q,
            distribution_window_quarters=gr.distribution_window_quarters,
            bootstrap_distributable_income_usd=gr.bootstrap_distributable_income_usd,
        )
        initial_nav_series = pd.Series(ledger.initial_nav, dtype=float)
        # Phase 12.5 / L19 flow-side: the initial-rate denominator for
        # distributable_income is the household's STARTING income figure —
        # the bootstrap value the user provides. We force the helper down
        # the bootstrap branch by passing a prior_quarter that predates
        # the run (earliest_realized < start_quarter ⇒ bootstrap fires).
        # NAV-side modes ignore prior_quarter and read initial_nav_series
        # directly, so this is a no-op for them.
        initial_prior_q = (
            params.start_quarter - 1
            if gr.spending_base == "distributable_income"
            else prior_q
        )
        initial = compute_spending_base(
            initial_nav_series,
            params.cma_liquidity,
            params.cma_income_producing,
            gr.spending_base,
            gr.spending_base_weights,
            ledger=ledger,
            prior_quarter=initial_prior_q,
            distribution_window_quarters=gr.distribution_window_quarters,
            bootstrap_distributable_income_usd=gr.bootstrap_distributable_income_usd,
        )

        # Phase 12 reviewer tightening 3 runtime guard: the initial
        # base must be > 0 whenever Owl needs the rate denominator.
        # Detects pathological configs (every weight zeros every
        # bucket the household actually owns) before the rate-band
        # math goes near a divide-by-zero.
        if initial.base_usd <= 0.0:
            raise ValueError(
                f"OwlRule: initial spending base is {initial.base_usd}; "
                f"selected mode={gr.spending_base!r}; "
                f"weights={gr.spending_base_weights!r}; "
                f"initial_nav_by_bucket={dict(initial_nav_series)}"
            )

        # Phase 12.5 / L19 flow-side runtime guard: when the realized
        # window has elapsed (no longer using bootstrap) but the
        # trailing distributable income is zero, the household has
        # no realized spendable income this period. Asserting a
        # withdrawal rate against zero is meaningless — fail loudly
        # with the named remediation paths. Reuses the Phase 12
        # base>0 pattern.
        if (
            gr.spending_base == "distributable_income"
            and not realized.is_bootstrap
            and realized.base_usd <= 0.0
        ):
            raise ValueError(
                f"OwlRule: realized trailing distributable income is "
                f"{realized.base_usd}; window="
                f"{gr.distribution_window_quarters}q ending at {prior_q}; "
                f"by_source={realized.distributable_income_by_source_usd}. "
                "The household has no realized distributable income in the "
                "closed window. Either wait for a producer-feed quarter, "
                "configure a wider window, or switch to a non-flow-side "
                "spending base."
            )

        if realized.base_usd > 0.0:
            initial_rate = cfg.annual_spend_usd / initial.base_usd
            current_rate = annual_spend / realized.base_usd
            if current_rate < initial_rate * (1.0 - gr.lower_band_pct):
                annual_spend *= 1.0 + gr.raise_pct
            elif current_rate > initial_rate * (1.0 + gr.upper_band_pct):
                annual_spend *= 1.0 - gr.cut_pct

        # Phase 11 / L16: optional absolute-dollar clamps. Break the
        # rate-based scale-invariance by introducing dollar-denominated
        # decisions in the trigger output. Activations are tracked for
        # the report diagnostic.
        if gr.absolute_min_annual_usd is not None:
            if annual_spend < gr.absolute_min_annual_usd:
                annual_spend = float(gr.absolute_min_annual_usd)
                self._min_clamp_activations += 1
        if gr.absolute_max_annual_usd is not None:
            if annual_spend > gr.absolute_max_annual_usd:
                annual_spend = float(gr.absolute_max_annual_usd)
                self._max_clamp_activations += 1

        # Phase 12 / L19: snapshot the most recent year-boundary
        # spending-base evaluation for diagnostics(). The report
        # surfaces the run-end snapshot (last call wins).
        self._last_spending_base_mode = gr.spending_base
        self._last_spending_base_realized = realized
        self._last_spending_base_initial = initial
        self._last_total_nav_realized = nav_realized_total
        self._last_annual_spend = float(annual_spend)

        quarterly = annual_spend / 4.0
        return max(cfg.floor_usd, min(cfg.ceiling_usd, quarterly))
