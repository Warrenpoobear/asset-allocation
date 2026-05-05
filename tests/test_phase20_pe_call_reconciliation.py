"""Phase 20 / L20 — PE call-obligation reconciliation to the cash-flow worksheet.

6 tests. Synthetic fixtures only — no live workbook, no real PE funds.
See MODEL_DOCUMENTATION.md §Phase 20 design.

Coverage (6 tests):
1.  explicit_config overrides both workbook and PE pacing.
2.  workbook capital-call lines present → source_used="cashflow_workbook";
    delta computed against PE pacing.
3.  workbook lines absent, PE present → source_used="pe_pacing_model";
    delta_classification="n/a".
4.  both present, delta > 25% → delta_classification="blocking";
    advisory surfaced; workbook value still used.
5.  both absent → source_used="unavailable", next_12m_capital_calls_usd=None,
    advisory.
6.  default configs byte-stable (call_recon_diag=None when
    position_ingestion=None).
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
import pytest
from aa_model.pe.call_obligation import (
    PECallObligationBridgeDiagnostics,
    derive_pe_capital_call_obligation,
)
from aa_model.pe.call_reconciliation import (
    reconcile_call_obligation,
)
from aa_model.pe.ta_model import PROJECTION_COLUMNS

# ---- shared synthetic helpers -----------------------------------------------


def _coverage_q(year: int = 2026, quarter: int = 1) -> pd.Period:
    return pd.Period(f"{year}Q{quarter}", freq="Q-DEC")


def _window(coverage_q: pd.Period) -> list[str]:
    return [str(coverage_q + i) for i in range(1, 5)]


def _make_pe_proj(
    *,
    fund_name: str = "fund_a",
    quarters: list[str],
    call_usd: float = 50_000.0,
) -> pd.DataFrame:
    rows = []
    for q in quarters:
        rows.append(
            {
                "fund_name": fund_name,
                "vintage": "2023Q1",
                "quarter_index": 0,
                "quarter": q,
                "age_years": 1.0,
                "nav_start_usd": 1_000_000.0,
                "call_usd": call_usd,
                "distribution_usd": 0.0,
                "nav_mark_usd": 0.0,
                "nav_end_usd": 1_000_000.0,
                "sleeve": "pe_buyout",
            }
        )
    return pd.DataFrame(rows, columns=list(PROJECTION_COLUMNS) + ["sleeve"])


@dataclass
class _FakeCashFlowLine:
    """Minimal stand-in for CashFlowLineRecord — only fields used by the reconciler."""

    quarter: str
    amount_usd: float
    category: str = "capital_call"
    direction: str = "outflow"


def _pe_bridge_unavailable(coverage_q: pd.Period) -> PECallObligationBridgeDiagnostics:
    """Return a Phase 19 diag with source='unavailable' and no calls."""
    empty = pd.DataFrame(columns=list(PROJECTION_COLUMNS) + ["sleeve"])
    return derive_pe_capital_call_obligation(empty, coverage_q)


def _pe_bridge_with_calls(
    coverage_q: pd.Period, call_usd: float = 50_000.0
) -> PECallObligationBridgeDiagnostics:
    """Return a Phase 19 diag with calls in next-4-quarter window."""
    w = _window(coverage_q)
    pe_proj = _make_pe_proj(quarters=w, call_usd=call_usd)
    return derive_pe_capital_call_obligation(pe_proj, coverage_q)


def _load_base_cfg():
    from pathlib import Path

    from aa_model.io.loaders import load_study_config

    config_path = Path(__file__).parents[1] / "configs" / "base.yaml"
    if not config_path.exists():
        pytest.skip("base.yaml fixture not available in this environment")
    return load_study_config(config_path)


# ---- 1. explicit_config overrides workbook + PE -----------------------------


def test_explicit_config_overrides_all():
    """Phase 20 #1: explicit_config wins regardless of workbook or PE pacing."""
    coverage_q = _coverage_q(2026, 1)
    w = _window(coverage_q)

    # Workbook has calls in window
    workbook_lines = [
        _FakeCashFlowLine(quarter=w[0], amount_usd=-80_000.0),
        _FakeCashFlowLine(quarter=w[1], amount_usd=-80_000.0),
    ]
    pe_bridge = _pe_bridge_with_calls(coverage_q, call_usd=50_000.0)

    result = reconcile_call_obligation(
        workbook_lines=workbook_lines,
        pe_bridge_diag=pe_bridge,
        coverage_quarter=coverage_q,
        explicit_usd=999_000.0,
    )

    assert result.source_used == "explicit_config"
    assert result.next_12m_capital_calls_usd == pytest.approx(999_000.0)
    assert result.explicit_usd == pytest.approx(999_000.0)


# ---- 2. workbook overrides PE pacing; delta computed ------------------------


def test_workbook_overrides_pe_delta_computed():
    """Phase 20 #2: workbook lines present → source_used=cashflow_workbook; delta computed."""
    coverage_q = _coverage_q(2026, 1)
    w = _window(coverage_q)

    # 4 workbook calls × 60k each = 240k
    workbook_lines = [_FakeCashFlowLine(quarter=q, amount_usd=-60_000.0) for q in w]
    # PE pacing: 4 × 50k = 200k
    pe_bridge = _pe_bridge_with_calls(coverage_q, call_usd=50_000.0)

    result = reconcile_call_obligation(
        workbook_lines=workbook_lines,
        pe_bridge_diag=pe_bridge,
        coverage_quarter=coverage_q,
        explicit_usd=None,
    )

    assert result.source_used == "cashflow_workbook"
    assert result.next_12m_capital_calls_usd == pytest.approx(240_000.0)
    assert result.workbook_total_usd == pytest.approx(240_000.0)
    # Delta = 240k - 200k = +40k; pct = 40/240 ≈ 16.7% → "warning"
    assert result.total_delta_usd == pytest.approx(40_000.0)
    assert result.delta_classification == "warning"
    assert result.total_delta_pct == pytest.approx(40_000.0 / 240_000.0 * 100.0)


# ---- 3. workbook absent → PE pacing; delta n/a ------------------------------


def test_workbook_absent_pe_present():
    """Phase 20 #3: no workbook lines → source_used=pe_pacing_model; delta_classification=n/a."""
    coverage_q = _coverage_q(2026, 1)
    pe_bridge = _pe_bridge_with_calls(coverage_q, call_usd=50_000.0)

    result = reconcile_call_obligation(
        workbook_lines=[],
        pe_bridge_diag=pe_bridge,
        coverage_quarter=coverage_q,
        explicit_usd=None,
    )

    assert result.source_used == "pe_pacing_model"
    assert result.next_12m_capital_calls_usd == pytest.approx(200_000.0)  # 4 × 50k
    assert result.workbook_total_usd is None
    assert result.delta_classification == "n/a"
    assert result.total_delta_usd is None


# ---- 4. blocking delta (> 25%) — workbook value still used ------------------


def test_blocking_delta_workbook_wins():
    """Phase 20 #4: delta > 25% → blocking advisory; workbook value used (no halt)."""
    coverage_q = _coverage_q(2026, 1)
    w = _window(coverage_q)

    # Workbook: 4 × 100k = 400k; PE: 4 × 50k = 200k → delta 200k / 400k = 50% → blocking
    workbook_lines = [_FakeCashFlowLine(quarter=q, amount_usd=-100_000.0) for q in w]
    pe_bridge = _pe_bridge_with_calls(coverage_q, call_usd=50_000.0)

    result = reconcile_call_obligation(
        workbook_lines=workbook_lines,
        pe_bridge_diag=pe_bridge,
        coverage_quarter=coverage_q,
        explicit_usd=None,
    )

    assert result.delta_classification == "blocking"
    assert result.source_used == "cashflow_workbook"
    assert result.next_12m_capital_calls_usd == pytest.approx(400_000.0)
    # Advisory surfaced for blocking
    assert any("BLOCKING" in adv for adv in result.advisories)


# ---- 4b. zero-denominator guard — both totals zero --------------------------


def test_both_workbook_and_pe_totals_zero_no_division_error():
    """Both workbook total and PE total present and each equal to zero
    must not divide by zero. Reachable when a caller constructs a bridge
    diag with next_12m_capital_calls_usd=0.0 directly (the dataclass does
    not validate that field) and the workbook reports a zero-amount line."""
    coverage_q = _coverage_q(2026, 1)
    w = _window(coverage_q)

    # Workbook line with zero amount in the window. This produces a
    # workbook_total of 0.0 (not None) once aggregated.
    workbook_lines = [_FakeCashFlowLine(quarter=w[0], amount_usd=0.0)]

    pe_bridge = PECallObligationBridgeDiagnostics(
        next_12m_capital_calls_usd=0.0,
        source="pe_pacing",
        coverage_quarter=str(coverage_q),
        quarters_included=w,
        quarters_in_horizon=w,
        fund_count=0,
        calls_by_quarter={},
        top_contributors=[],
        advisories=[],
    )

    result = reconcile_call_obligation(
        workbook_lines=workbook_lines,
        pe_bridge_diag=pe_bridge,
        coverage_quarter=coverage_q,
        explicit_usd=None,
    )

    # Both totals are 0.0 — denominator guard kicks in.
    assert result.workbook_total_usd == pytest.approx(0.0)
    assert result.total_delta_usd == pytest.approx(0.0)
    assert result.total_delta_pct == pytest.approx(0.0)
    assert result.delta_classification == "n/a"


# ---- 5. both absent → unavailable + advisory --------------------------------


def test_both_absent_unavailable():
    """Phase 20 #5: neither workbook nor PE pacing → unavailable + advisory."""
    coverage_q = _coverage_q(2026, 1)
    pe_bridge = _pe_bridge_unavailable(coverage_q)

    result = reconcile_call_obligation(
        workbook_lines=[],
        pe_bridge_diag=pe_bridge,
        coverage_quarter=coverage_q,
        explicit_usd=None,
    )

    assert result.source_used == "unavailable"
    assert result.next_12m_capital_calls_usd is None
    assert len(result.advisories) >= 1
    assert any("no capital-call obligation" in adv for adv in result.advisories)


# ---- 6. default configs byte-stable (call_recon_diag=None) ------------------


def test_default_configs_byte_stable():
    """Phase 20 #6: call_recon_diag=None when position_ingestion=None (11th element)."""
    from aa_model.integration.orchestrator import _build_ledger

    cfg = _load_base_cfg()
    assert cfg.position_ingestion is None

    result_tuple = _build_ledger(cfg, "test-run-p20")
    call_recon_diag = result_tuple[10]  # 11th element (index 10)
    assert call_recon_diag is None
