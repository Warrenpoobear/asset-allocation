"""Phase 19 / L20 — PE pacing → next-12m capital-call obligation bridge.

Derives ``next_12m_capital_calls_usd`` from the deterministic PE pacing
projection so ``LiquidityCoverageResult.capital_call_coverage`` is populated
without inventing calls from static unfunded commitments (Phase 16 T4).

Pure function: ``derive_pe_capital_call_obligation`` takes ``pe_proj`` +
a coverage quarter and returns a ``PECallObligationBridgeDiagnostics``
object. No ledger reads. No side effects. No module state.

Override precedence (resolved in orchestrator, not here):
  1. Explicit ``liquidity_obligations.next_12m_capital_calls_usd`` set by
     user → source = "explicit"
  2. PE-derived sum of call_usd over next-4-quarter window → source =
     "pe_pacing"
  3. PE pacing unavailable (empty pe_proj) → source = "unavailable"

T4 boundary preserved: calls are derived only from the forward pacing
model, never from ``unfunded_commitment_usd × heuristic_pct``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd


@dataclass
class PECallObligationBridgeDiagnostics:
    """Phase 19 / L20 — diagnostics for the PE call obligation bridge.

    Returned by ``derive_pe_capital_call_obligation`` and carried through
    ``_build_ledger`` into ``write_markdown_report``.
    """

    next_12m_capital_calls_usd: float | None
    source: str  # "explicit" | "pe_pacing" | "unavailable"
    coverage_quarter: str  # str(pd.Period) — the measurement point
    quarters_included: list[str]  # next-4-quarter window queried
    quarters_in_horizon: list[str]  # subset that appeared in pe_proj
    fund_count: int  # funds with call_usd > 0 in window
    calls_by_quarter: dict[str, float]
    top_contributors: list[tuple[str, float]]  # (fund_name, sum_call_usd), top 5 desc
    advisories: list[str] = field(default_factory=list)


def derive_pe_capital_call_obligation(
    pe_proj: pd.DataFrame,
    coverage_quarter: pd.Period,
) -> PECallObligationBridgeDiagnostics:
    """Derive next-12m capital-call obligation from PE pacing projections.

    Parameters
    ----------
    pe_proj:
        Tidy PE projection frame (PROJECTION_COLUMNS + sleeve) from
        ``PEAdapter.project_horizon``. One row per (fund, quarter) in
        the run horizon. ``quarter`` column is a string (e.g. "2026Q1").
    coverage_quarter:
        The quarter enclosing the position snapshot as_of_date. The
        next-12m window is coverage_quarter+1 through coverage_quarter+4.

    Returns
    -------
    ``PECallObligationBridgeDiagnostics`` with all fields populated.
    ``next_12m_capital_calls_usd`` is ``None`` when no calls are
    projected (preserves T4: a zero-denominator obligation is not a
    useful coverage input; the advisory explains why).
    """
    window = [str(coverage_quarter + i) for i in range(1, 5)]
    advisories: list[str] = []

    if pe_proj.empty:
        advisories.append(
            "PE pacing produced no projections — " "next_12m_capital_calls_usd unavailable (T4)"
        )
        return PECallObligationBridgeDiagnostics(
            next_12m_capital_calls_usd=None,
            source="unavailable",
            coverage_quarter=str(coverage_quarter),
            quarters_included=window,
            quarters_in_horizon=[],
            fund_count=0,
            calls_by_quarter={},
            top_contributors=[],
            advisories=advisories,
        )

    in_window = pe_proj[pe_proj["quarter"].isin(window)]
    quarters_in_horizon = sorted(in_window["quarter"].unique().tolist())

    if len(quarters_in_horizon) < 4:
        missing = [q for q in window if q not in quarters_in_horizon]
        advisories.append(
            f"next-12m window partially outside run horizon — "
            f"call projection covers {len(quarters_in_horizon)} of 4 quarters "
            f"(missing: {', '.join(missing)})"
        )

    calls_by_quarter: dict[str, float] = {}
    if not in_window.empty:
        for q_val, grp in in_window.groupby("quarter"):
            total = float(grp["call_usd"].sum())
            if total > 0.0:
                calls_by_quarter[str(q_val)] = total

    if not in_window.empty:
        fund_totals = in_window.groupby("fund_name")["call_usd"].sum().sort_values(ascending=False)
        top_contributors = [
            (str(name), float(val)) for name, val in fund_totals.head(5).items() if float(val) > 0.0
        ]
    else:
        top_contributors = []

    fund_count = len(top_contributors)
    total_calls = sum(calls_by_quarter.values())

    if total_calls == 0.0:
        advisories.append(
            "no PE calls projected in next-12m window — "
            "funds may be past commitment period; "
            "capital_call_coverage n/a is expected"
        )
        return PECallObligationBridgeDiagnostics(
            next_12m_capital_calls_usd=None,
            source="pe_pacing",
            coverage_quarter=str(coverage_quarter),
            quarters_included=window,
            quarters_in_horizon=quarters_in_horizon,
            fund_count=fund_count,
            calls_by_quarter=calls_by_quarter,
            top_contributors=top_contributors,
            advisories=advisories,
        )

    return PECallObligationBridgeDiagnostics(
        next_12m_capital_calls_usd=total_calls,
        source="pe_pacing",
        coverage_quarter=str(coverage_quarter),
        quarters_included=window,
        quarters_in_horizon=quarters_in_horizon,
        fund_count=fund_count,
        calls_by_quarter=calls_by_quarter,
        top_contributors=top_contributors,
        advisories=advisories,
    )
