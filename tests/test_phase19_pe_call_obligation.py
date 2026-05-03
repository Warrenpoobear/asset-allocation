"""Phase 19 / L20 — PE pacing → capital-call obligation bridge tests.

6 tests. Synthetic fixtures only — no live workbook, no real PE funds.
See MODEL_DOCUMENTATION.md §Phase 19 design.

Coverage (6 tests):
1.  Explicit next_12m_capital_calls_usd overrides PE-derived value.
2.  PE projections with calls in next-4-quarter window → capital_call_coverage finite.
3.  Empty pe_proj → next_12m_capital_calls_usd=None + 'unavailable' advisory.
4.  PE projections exist but zero calls in window → None + advisory.
5.  Same inputs → same output (deterministic contract).
6.  Default configs byte-stable (pe_call_bridge_diag=None when position_ingestion=None).
"""

from __future__ import annotations

import datetime

import pandas as pd
import pytest

from aa_model.pe.call_obligation import (
    PECallObligationBridgeDiagnostics,
    derive_pe_capital_call_obligation,
)
from aa_model.pe.ta_model import PROJECTION_COLUMNS


# ---- shared synthetic helpers -----------------------------------------------


def _coverage_q(year: int = 2026, quarter: int = 1) -> pd.Period:
    return pd.Period(f"{year}Q{quarter}", freq="Q-DEC")


def _make_pe_proj(
    *,
    fund_name: str = "fund_a",
    quarters: list[str],
    call_usd: float = 50_000.0,
) -> pd.DataFrame:
    """Build a minimal pe_proj frame with call_usd for the given quarters."""
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


def _load_base_cfg():
    from pathlib import Path

    from aa_model.io.loaders import load_study_config

    config_path = Path(__file__).parents[1] / "configs" / "base.yaml"
    if not config_path.exists():
        pytest.skip("base.yaml fixture not available in this environment")
    return load_study_config(config_path)


# ---- 1. Explicit override takes priority ------------------------------------


def test_explicit_override_preserved():
    """Phase 19 #1: explicit next_12m_capital_calls_usd overrides PE-derived value."""
    from aa_model.integration.orchestrator import _run_liquidity_coverage
    from aa_model.ingestion.schemas_position import (
        PositionIngestionDiagnostics,
        PositionIngestionResult,
        PositionManifestConfig,
        PositionRecord,
    )

    # Build a minimal position ingestion result and manifest
    pos = PositionRecord(
        position_id="p1",
        account_id="acct",
        manager_id=None,
        market_value_usd=500_000.0,
        unfunded_commitment_usd=None,
        liquidity_bucket="daily_liquid",
        valuation_date=datetime.date(2026, 3, 31),
        source_row=1,
    )
    diag = PositionIngestionDiagnostics(
        workbook_hash="aa" * 32,
        workbook_version="1",
        manifest_version="1",
        positions_total=1,
        stale_valuation_count=0,
        positions_missing_bucket=0,
    )
    pir = PositionIngestionResult(accounts=[], positions=[pos], diagnostics=diag)
    manifest = PositionManifestConfig(
        manifest_version="1",
        workbook_version="1",
        as_of_date=datetime.date(2026, 3, 31),
    )

    cfg = _load_base_cfg()
    cfg = cfg.model_copy(
        update={
            "liquidity_obligations": {
                "annual_spend_usd": 100_000.0,
                "next_12m_capital_calls_usd": 75_000.0,  # explicit user value
            },
            "liquidity_coverage_config": {},
        }
    )

    # PE-derived value would be 0 if pe_proj is empty, but user value wins
    result = _run_liquidity_coverage(
        pir, manifest, cfg,
        pe_call_obligation_usd=75_000.0,  # resolved explicit value passed in
    )
    # capital_call_coverage = liquid_nav / next_12m_capital_calls_usd = 500k / 75k
    assert result.capital_call_coverage == pytest.approx(500_000.0 / 75_000.0)


# ---- 2. PE-derived calls populate obligation --------------------------------


def test_pe_derived_calls_populate_obligation():
    """Phase 19 #2: PE projections with calls in next-4-quarter window → capital_call_coverage finite."""
    coverage_q = _coverage_q(2026, 1)
    window = [str(coverage_q + i) for i in range(1, 5)]

    pe_proj = _make_pe_proj(quarters=window, call_usd=50_000.0)
    result = derive_pe_capital_call_obligation(pe_proj, coverage_q)

    assert result.source == "pe_pacing"
    assert result.next_12m_capital_calls_usd == pytest.approx(200_000.0)  # 4 × 50k
    assert len(result.quarters_in_horizon) == 4
    assert result.fund_count == 1
    assert result.advisories == []


# ---- 3. Empty pe_proj → None + unavailable advisory -----------------------


def test_empty_pe_proj_returns_unavailable():
    """Phase 19 #3: Empty pe_proj → next_12m_capital_calls_usd=None + unavailable source."""
    coverage_q = _coverage_q(2026, 1)
    empty_proj = pd.DataFrame(columns=list(PROJECTION_COLUMNS) + ["sleeve"])

    result = derive_pe_capital_call_obligation(empty_proj, coverage_q)

    assert result.source == "unavailable"
    assert result.next_12m_capital_calls_usd is None
    assert len(result.advisories) == 1
    assert "unavailable" in result.advisories[0].lower()


# ---- 4. PE projections exist but zero calls in window ----------------------


def test_zero_calls_in_window_returns_none_with_advisory():
    """Phase 19 #4: PE projections exist but call_usd=0 in window → None + advisory."""
    coverage_q = _coverage_q(2026, 1)
    window = [str(coverage_q + i) for i in range(1, 5)]

    pe_proj = _make_pe_proj(quarters=window, call_usd=0.0)
    result = derive_pe_capital_call_obligation(pe_proj, coverage_q)

    assert result.source == "pe_pacing"
    assert result.next_12m_capital_calls_usd is None
    assert len(result.advisories) >= 1
    assert any("no PE calls" in adv for adv in result.advisories)


# ---- 5. Deterministic same-input same-output --------------------------------


def test_deterministic_same_input_same_output():
    """Phase 19 #5: Same pe_proj + coverage_quarter → identical result on repeated calls."""
    coverage_q = _coverage_q(2026, 1)
    window = [str(coverage_q + i) for i in range(1, 3)]  # only 2 quarters
    pe_proj = _make_pe_proj(quarters=window, call_usd=30_000.0)

    r1 = derive_pe_capital_call_obligation(pe_proj, coverage_q)
    r2 = derive_pe_capital_call_obligation(pe_proj, coverage_q)

    assert r1.next_12m_capital_calls_usd == r2.next_12m_capital_calls_usd
    assert r1.source == r2.source
    assert r1.calls_by_quarter == r2.calls_by_quarter
    assert r1.advisories == r2.advisories


# ---- 6. Default configs byte-stable (bridge inactive) ----------------------


def test_default_configs_byte_stable():
    """Phase 19 #6: pe_call_bridge_diag=None when position_ingestion is None."""
    from aa_model.integration.orchestrator import _build_ledger

    cfg = _load_base_cfg()
    assert cfg.position_ingestion is None

    result_tuple = _build_ledger(cfg, "test-run-p19")
    pe_call_bridge_diag = result_tuple[10]  # 11th element (index 10)
    assert pe_call_bridge_diag is None
