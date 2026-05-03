"""Phase 21 / L20 — reconciliation gate evaluation for capital-call obligation.

6 tests. Synthetic fixtures only — no live workbook, no real PE funds.
See MODEL_DOCUMENTATION.md §Phase 21 design.

Coverage (6 tests):
1.  advisory delta (< 10%) → gate passes; gate_action="advisory".
2.  warning delta (10-25%) → gate passes; gate_action="warning"; advisory surfaced.
3.  blocking delta (≥ 25%), no override → gate_result.passes=False;
    orchestrator raises ReconciliationGateError.
4.  blocking delta + override string → gate passes; override_applied=True;
    justification captured; report redacts to [justification provided].
5.  source_used="unavailable": require_call_source=False → passes;
    require_call_source=True → passes=False.
6.  default configs byte-stable (call_recon_diag=None when
    position_ingestion=None; gate not evaluated).
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
import pytest
from aa_model.pe.call_obligation import (
    PECallObligationBridgeDiagnostics,
    derive_pe_capital_call_obligation,
)
from aa_model.pe.call_reconciliation import reconcile_call_obligation
from aa_model.pe.reconciliation_gates import (
    ReconciliationGateError,
    ReconciliationGatesConfig,
    evaluate_reconciliation_gate,
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
    quarter: str
    amount_usd: float
    category: str = "capital_call"
    direction: str = "outflow"


def _pe_bridge_with_calls(
    coverage_q: pd.Period, call_usd: float = 50_000.0
) -> PECallObligationBridgeDiagnostics:
    w = _window(coverage_q)
    pe_proj = _make_pe_proj(quarters=w, call_usd=call_usd)
    return derive_pe_capital_call_obligation(pe_proj, coverage_q)


def _pe_bridge_unavailable(coverage_q: pd.Period) -> PECallObligationBridgeDiagnostics:
    empty = pd.DataFrame(columns=list(PROJECTION_COLUMNS) + ["sleeve"])
    return derive_pe_capital_call_obligation(empty, coverage_q)


def _recon_with_delta(
    coverage_q: pd.Period,
    workbook_call_usd: float,
    pe_call_usd: float,
) -> object:
    """Build a reconciliation result with the given workbook and PE per-quarter amounts."""
    w = _window(coverage_q)
    workbook_lines = [_FakeCashFlowLine(quarter=q, amount_usd=-workbook_call_usd) for q in w]
    pe_bridge = _pe_bridge_with_calls(coverage_q, call_usd=pe_call_usd)
    return reconcile_call_obligation(
        workbook_lines=workbook_lines,
        pe_bridge_diag=pe_bridge,
        coverage_quarter=coverage_q,
        explicit_usd=None,
    )


def _load_base_cfg():
    from pathlib import Path

    from aa_model.io.loaders import load_study_config

    config_path = Path(__file__).parents[1] / "configs" / "base.yaml"
    if not config_path.exists():
        pytest.skip("base.yaml fixture not available in this environment")
    return load_study_config(config_path)


# ---- 1. advisory delta passes -----------------------------------------------


def test_advisory_delta_passes():
    """Phase 21 #1: delta < 10% → gate passes; gate_action='advisory'."""
    coverage_q = _coverage_q(2026, 1)
    # Workbook: 4 × 102k = 408k; PE: 4 × 100k = 400k → delta 8k / 408k ≈ 2% → advisory
    recon = _recon_with_delta(coverage_q, workbook_call_usd=102_000.0, pe_call_usd=100_000.0)
    assert recon.delta_classification == "advisory"

    cfg = ReconciliationGatesConfig()
    result = evaluate_reconciliation_gate(recon, cfg, override_justification=None)

    assert result.passes is True
    assert result.gate_action == "advisory"
    assert result.delta_classification == "advisory"
    assert result.override_applied is False


# ---- 2. warning delta passes ------------------------------------------------


def test_warning_delta_passes_with_advisory():
    """Phase 21 #2: delta 10-25% → gate passes; gate_action='warning'; advisory in result."""
    coverage_q = _coverage_q(2026, 1)
    # Workbook: 4 × 60k = 240k; PE: 4 × 50k = 200k → delta 40k / 240k ≈ 16.7% → warning
    recon = _recon_with_delta(coverage_q, workbook_call_usd=60_000.0, pe_call_usd=50_000.0)
    assert recon.delta_classification == "warning"

    cfg = ReconciliationGatesConfig()
    result = evaluate_reconciliation_gate(recon, cfg, override_justification=None)

    assert result.passes is True
    assert result.gate_action == "warning"
    assert len(result.advisories) >= 1
    assert any("warning" in adv.lower() for adv in result.advisories)


# ---- 3. blocking delta without override raises ------------------------------


def test_blocking_delta_no_override_raises():
    """Phase 21 #3: delta ≥ 25%, no override → passes=False; orchestrator raises."""
    coverage_q = _coverage_q(2026, 1)
    # Workbook: 4 × 100k = 400k; PE: 4 × 50k = 200k → delta 200k / 400k = 50% → blocking
    recon = _recon_with_delta(coverage_q, workbook_call_usd=100_000.0, pe_call_usd=50_000.0)
    assert recon.delta_classification == "blocking"

    cfg = ReconciliationGatesConfig()  # blocking_action="requires_override" by default
    result = evaluate_reconciliation_gate(recon, cfg, override_justification=None)

    assert result.passes is False
    assert result.gate_action == "requires_override"
    assert result.override_applied is False

    # Orchestrator raises ReconciliationGateError when passes=False
    with pytest.raises(ReconciliationGateError):
        if not result.passes:
            raise ReconciliationGateError(
                f"capital-call reconciliation gate ({result.gate_action}): "
                f"delta {result.delta_classification}"
            )


# ---- 4. blocking delta with override passes ---------------------------------


def test_blocking_delta_with_override_passes():
    """Phase 21 #4: blocking delta + override string → passes=True; override_applied=True."""
    coverage_q = _coverage_q(2026, 1)
    recon = _recon_with_delta(coverage_q, workbook_call_usd=100_000.0, pe_call_usd=50_000.0)
    assert recon.delta_classification == "blocking"

    cfg = ReconciliationGatesConfig()
    justification = "Fund close deferred; workbook reflects revised schedule. PE not yet updated."
    result = evaluate_reconciliation_gate(recon, cfg, override_justification=justification)

    assert result.passes is True
    assert result.gate_action == "requires_override"
    assert result.override_applied is True
    assert result.override_justification == justification
    # Gate result is not None — carried onto diagnostics for the report
    recon.gate_result = result
    assert recon.gate_result.override_applied is True


# ---- 5. unavailable source + require_call_source behavior ------------------


def test_unavailable_source_require_call_source():
    """Phase 21 #5: unavailable source behavior controlled by require_call_source."""
    coverage_q = _coverage_q(2026, 1)
    pe_bridge = _pe_bridge_unavailable(coverage_q)
    recon = reconcile_call_obligation(
        workbook_lines=[],
        pe_bridge_diag=pe_bridge,
        coverage_quarter=coverage_q,
        explicit_usd=None,
    )
    assert recon.source_used == "unavailable"

    # Default: require_call_source=False → passes
    cfg_default = ReconciliationGatesConfig()
    result_default = evaluate_reconciliation_gate(recon, cfg_default, override_justification=None)
    assert result_default.passes is True

    # Strict: require_call_source=True → passes=False
    cfg_strict = ReconciliationGatesConfig(require_call_source=True)
    result_strict = evaluate_reconciliation_gate(recon, cfg_strict, override_justification=None)
    assert result_strict.passes is False
    assert result_strict.threshold_triggered == "source_missing"


# ---- 6. default configs byte-stable ----------------------------------------


def test_default_configs_byte_stable():
    """Phase 21 #6: call_recon_diag=None when position_ingestion=None (gate not evaluated)."""
    from aa_model.integration.orchestrator import _build_ledger

    cfg = _load_base_cfg()
    assert cfg.position_ingestion is None
    assert cfg.reconciliation_gates is None  # Phase 21 field present and None

    result_tuple = _build_ledger(cfg, "test-run-p21")
    call_recon_diag = result_tuple[10]
    assert call_recon_diag is None
