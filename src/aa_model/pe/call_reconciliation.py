"""Phase 20/21 / L20 — PE call-obligation reconciliation to the cash-flow worksheet.

Reconciles next_12m_capital_calls_usd from three sources in precedence order:

  1. explicit_config   — user-provided via liquidity_obligations.next_12m_capital_calls_usd
  2. cashflow_workbook — CashFlowLineRecord rows with category="capital_call", direction="outflow"
  3. pe_pacing_model   — derive_pe_capital_call_obligation result (Phase 19)
  4. unavailable       — neither source populated

The cash-flow worksheet is the operating forecast spine. PE pacing is a
deterministic cross-check. When both workbook and PE pacing are available,
a per-quarter reconciliation delta is computed regardless of which source wins.

T4 boundary preserved: calls are never inferred from unfunded_commitment_usd
× heuristic.

Phase 21 adds WorkbookCallReconciliationDiagnostics.gate_result for carrying
the ReconciliationGateResult from evaluate_reconciliation_gate into the report.
Gate evaluation and enforcement live in reconciliation_gates.py and the
orchestrator respectively — not here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pandas as pd

from aa_model.pe.call_obligation import PECallObligationBridgeDiagnostics

if TYPE_CHECKING:
    from aa_model.pe.reconciliation_gates import ReconciliationGateResult


@dataclass
class WorkbookCallReconciliationDiagnostics:
    """Phase 20 / L20 — reconciliation diagnostics for the PE call-obligation bridge.

    Supersedes PECallObligationBridgeDiagnostics as the primary report artifact.
    The Phase 19 PE bridge result is embedded as ``pe_bridge`` for per-fund
    breakdown and detailed PE pacing diagnostics.
    """

    # Resolved obligation — the final answer passed to LiquidityObligationConfig
    next_12m_capital_calls_usd: float | None
    source_used: str  # "explicit_config"|"cashflow_workbook"|"pe_pacing_model"|"unavailable"

    # Measurement point
    coverage_quarter: str
    quarters_in_window: list[str]  # next-4-quarter window queried

    # Explicit side
    explicit_usd: float | None

    # Workbook side
    workbook_calls_by_quarter: dict[str, float]  # abs USD per quarter in window
    workbook_total_usd: float | None  # None when no lines in window

    # PE pacing side (embedded Phase 19 result)
    pe_bridge: PECallObligationBridgeDiagnostics

    # Reconciliation delta (workbook − pe_pacing), only when both available
    delta_by_quarter: dict[str, float]  # empty when < 2 sources available
    total_delta_usd: float | None  # workbook_total − pe_total
    total_delta_pct: float | None  # pct of max(workbook, pe) — avoids distortion
    delta_classification: str  # "advisory" | "warning" | "blocking" | "n/a"

    advisories: list[str] = field(default_factory=list)

    # Phase 21: gate evaluation result — set by the orchestrator after calling
    # evaluate_reconciliation_gate. None until gate evaluation runs.
    gate_result: ReconciliationGateResult | None = field(default=None)


def aggregate_workbook_capital_calls(
    cash_flow_lines: list,
    coverage_quarter: pd.Period,
) -> tuple[dict[str, float], list[str]]:
    """Extract and aggregate capital-call outflow lines from workbook ingestion.

    Filters CashFlowLineRecord rows where category=="capital_call" and
    direction=="outflow". The sign convention requires amount_usd < 0 for
    outflows; returns abs values per quarter.

    Window: coverage_quarter+1 through coverage_quarter+4 (same as Phase 19).

    No entity-type filter: category="capital_call" is the classification
    boundary (Q5 reviewer answer).

    Returns (calls_by_quarter, advisories).
    """
    window = {str(coverage_quarter + i) for i in range(1, 5)}
    advisories: list[str] = []

    qualifying = [
        line
        for line in cash_flow_lines
        if getattr(line, "category", None) == "capital_call"
        and getattr(line, "direction", None) == "outflow"
    ]

    if not qualifying:
        return {}, advisories

    in_window = [line for line in qualifying if line.quarter in window]
    if not in_window:
        # Q3 reviewer answer: outside-window lines do not win precedence.
        advisories.append(
            "workbook capital-call lines present but none fall in next-12m window — "
            "falling through to pe_pacing_model"
        )
        return {}, advisories

    calls_by_quarter: dict[str, float] = {}
    for line in in_window:
        q = line.quarter
        calls_by_quarter[q] = calls_by_quarter.get(q, 0.0) + abs(line.amount_usd)

    return calls_by_quarter, advisories


def _classify_delta(total_delta_usd: float, workbook_total: float, pe_total: float) -> str:
    """Classify reconciliation delta. Denominator = max(workbook, pe) to avoid distortion."""
    denom = max(workbook_total, pe_total)
    if denom == 0.0:
        return "n/a"
    pct = abs(total_delta_usd) / denom
    if pct < 0.10:
        return "advisory"
    if pct < 0.25:
        return "warning"
    return "blocking"


def reconcile_call_obligation(
    workbook_lines: list,
    pe_bridge_diag: PECallObligationBridgeDiagnostics,
    coverage_quarter: pd.Period,
    explicit_usd: float | None,
) -> WorkbookCallReconciliationDiagnostics:
    """Reconcile next-12m capital-call obligation from all available sources.

    Precedence: explicit_config > cashflow_workbook > pe_pacing_model > unavailable.

    When both workbook and PE pacing are available, a per-quarter delta is
    computed regardless of which source wins the obligation value. Blocking
    delta classification does not halt execution in Phase 20 (Q4 reviewer answer).

    Parameters
    ----------
    workbook_lines:
        list[CashFlowLineRecord] from IngestionResult.cash_flow_lines,
        or empty list when workbook ingestion was not run.
    pe_bridge_diag:
        Phase 19 PECallObligationBridgeDiagnostics from
        derive_pe_capital_call_obligation.
    coverage_quarter:
        pd.Period enclosing the position snapshot as_of_date.
    explicit_usd:
        User-provided override from cfg.liquidity_obligations. None when not set.

    Returns
    -------
    WorkbookCallReconciliationDiagnostics with all fields populated.
    """
    window = [str(coverage_quarter + i) for i in range(1, 5)]
    advisories: list[str] = []

    workbook_calls_by_quarter, wb_advisories = aggregate_workbook_capital_calls(
        workbook_lines, coverage_quarter
    )
    advisories.extend(wb_advisories)

    workbook_total = sum(workbook_calls_by_quarter.values()) if workbook_calls_by_quarter else None
    pe_total = pe_bridge_diag.next_12m_capital_calls_usd

    # Reconciliation delta — compute whenever both sides are available,
    # regardless of which source wins the obligation value.
    delta_by_quarter: dict[str, float] = {}
    total_delta_usd: float | None = None
    total_delta_pct: float | None = None
    delta_classification = "n/a"

    if workbook_total is not None and pe_total is not None:
        for q in window:
            wb_q = workbook_calls_by_quarter.get(q, 0.0)
            pe_q = pe_bridge_diag.calls_by_quarter.get(q, 0.0)
            delta = wb_q - pe_q
            if delta != 0.0:
                delta_by_quarter[q] = delta
        total_delta_usd = workbook_total - pe_total
        delta_classification = _classify_delta(total_delta_usd, workbook_total, pe_total)
        denom = max(workbook_total, pe_total)
        # Both sources present and both zero is a legitimate state (no calls
        # forecast either way); treat the percentage as 0 rather than dividing.
        total_delta_pct = (abs(total_delta_usd) / denom * 100.0) if denom > 0.0 else 0.0
        if delta_classification == "warning":
            advisories.append(
                f"workbook vs PE pacing delta WARNING: "
                f"${total_delta_usd:+,.0f} ({total_delta_pct:.1f}% of max) — "
                f"review recommended"
            )
        elif delta_classification == "blocking":
            advisories.append(
                f"workbook vs PE pacing delta BLOCKING: "
                f"${total_delta_usd:+,.0f} ({total_delta_pct:.1f}% of max) — "
                f"obligation source uncertain; manual reconciliation required"
            )

    # Source precedence resolution.
    if explicit_usd is not None:
        source_used = "explicit_config"
        final_usd: float | None = float(explicit_usd)
    elif workbook_total is not None:
        source_used = "cashflow_workbook"
        final_usd = workbook_total
    elif pe_total is not None:
        source_used = "pe_pacing_model"
        final_usd = pe_total
    else:
        source_used = "unavailable"
        final_usd = None
        advisories.append(
            "no capital-call obligation available — "
            "neither workbook lines nor PE pacing produced next-12m calls; "
            "capital_call_coverage n/a"
        )

    return WorkbookCallReconciliationDiagnostics(
        next_12m_capital_calls_usd=final_usd,
        source_used=source_used,
        coverage_quarter=str(coverage_quarter),
        quarters_in_window=window,
        explicit_usd=explicit_usd,
        workbook_calls_by_quarter=workbook_calls_by_quarter,
        workbook_total_usd=workbook_total,
        pe_bridge=pe_bridge_diag,
        delta_by_quarter=delta_by_quarter,
        total_delta_usd=total_delta_usd,
        total_delta_pct=total_delta_pct,
        delta_classification=delta_classification,
        advisories=advisories,
    )
