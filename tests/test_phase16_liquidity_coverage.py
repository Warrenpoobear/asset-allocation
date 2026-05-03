"""Phase 16 / L20 — Liquidity coverage diagnostics tests.

12 tests. Synthetic fixtures only — no live workbook, no real positions,
no manager names. See MODEL_DOCUMENTATION.md §Phase 16 design.

Coverage (12 tests):
1.  Tier NAV sums: positions with known buckets → correct aggregates.
2.  Coverage ratio: liquid=200k, spend=100k → 2.0x.
3.  BREACH: liquid < 1× annual spend → breach emitted.
4.  WARNING: liquid between 1× and 2× annual spend → warning, no breach.
5.  OK: liquid >= 2× annual spend → no breach, no warning.
6.  Zero total NAV → ratios are None; no ZeroDivisionError.
7.  Missing annual_spend_usd → advisory emitted; ratio is None.
8.  T4: unfunded exists but next_12m_capital_calls_usd is None → advisory.
9.  T5: spending_base_is_flow=True → liquid_nav_to_annual_income_estimate
        set; liquid_to_spending_base is None.
10. T3: semi-liquid NAV excluded from runway and breach coverage.
11. T6: total_unfunded_commitments_usd correctly aggregated.
12. Byte-stable: same inputs → identical LiquidityCoverageResult.
"""

from __future__ import annotations

import datetime

import pytest
from aa_model.ingestion.schemas_position import PositionRecord
from aa_model.liquidity.coverage import (
    LiquidityCoverageConfig,
    LiquidityObligationConfig,
    compute_liquidity_coverage,
)

# ---- synthetic position builder --------------------------------------------


def _pos(
    bucket: str,
    nav: float,
    *,
    unfunded: float | None = None,
    manager_id: str | None = None,
    position_id: str | None = None,
) -> PositionRecord:
    return PositionRecord(
        position_id=position_id or f"p_{bucket}_{int(nav)}",
        account_id="acct_synthetic",
        manager_id=manager_id,
        market_value_usd=nav,
        unfunded_commitment_usd=unfunded,
        liquidity_bucket=bucket,
        valuation_date=datetime.date(2026, 3, 31),
        source_row=1,
    )


def _obligations(**kwargs) -> LiquidityObligationConfig:
    return LiquidityObligationConfig(**kwargs)


# ---- 1. Tier NAV sums -------------------------------------------------------


def test_tier_nav_sums():
    """Phase 16 #1: positions with known buckets produce correct tier aggregates."""
    positions = [
        _pos("cash_equivalent", 100_000),
        _pos("daily_liquid", 200_000),
        _pos("semi_liquid", 50_000),
        _pos("illiquid", 300_000),
        _pos("locked_strategic", 150_000),
        _pos("re_stabilized", 80_000),
        _pos("re_development", 40_000),
        _pos("re_land", 30_000),
        _pos("opco_strategic", 50_000),
    ]
    result = compute_liquidity_coverage(positions, _obligations())

    # cash_equivalent + daily_liquid → liquid tier
    assert result.liquid_nav == pytest.approx(300_000)
    assert result.semi_liquid_nav == pytest.approx(50_000)
    # illiquid tier: illiquid only (re_stabilized defaults to illiquid)
    assert result.illiquid_nav == pytest.approx(300_000 + 80_000)
    # locked_strategic: locked_strategic + re_development + re_land + opco_strategic
    assert result.locked_strategic_nav == pytest.approx(150_000 + 40_000 + 30_000 + 50_000)
    assert result.total_position_nav == pytest.approx(1_000_000)


# ---- 2. Coverage ratio -----------------------------------------------------


def test_coverage_ratio_exact():
    """Phase 16 #2: liquid=200k, annual_spend=100k → ratio 2.0."""
    positions = [_pos("daily_liquid", 200_000)]
    obs = _obligations(annual_spend_usd=100_000)
    result = compute_liquidity_coverage(positions, obs)

    assert result.liquid_to_annual_spend == pytest.approx(2.0)


# ---- 3. BREACH -------------------------------------------------------------


def test_breach_liquid_below_annual_spend():
    """Phase 16 #3: liquid < 1× annual spend → breach emitted."""
    positions = [_pos("daily_liquid", 80_000)]
    obs = _obligations(annual_spend_usd=100_000)
    result = compute_liquidity_coverage(positions, obs)

    assert result.liquid_to_annual_spend == pytest.approx(0.8)
    assert len(result.diagnostics.breaches) >= 1
    assert any("liquid_to_annual_spend" in b for b in result.diagnostics.breaches)


# ---- 4. WARNING (no breach) ------------------------------------------------


def test_warning_liquid_between_1x_and_2x():
    """Phase 16 #4: liquid between 1× and 2× annual spend → warning, no breach."""
    positions = [_pos("daily_liquid", 150_000)]
    obs = _obligations(annual_spend_usd=100_000)
    result = compute_liquidity_coverage(positions, obs)

    assert result.liquid_to_annual_spend == pytest.approx(1.5)
    assert len(result.diagnostics.breaches) == 0
    assert any("liquid_to_annual_spend" in w for w in result.diagnostics.warnings)


# ---- 5. OK -----------------------------------------------------------------


def test_ok_liquid_above_warning_threshold():
    """Phase 16 #5: liquid >= 2× annual spend → no breach, no spend warning."""
    positions = [_pos("daily_liquid", 300_000)]
    obs = _obligations(annual_spend_usd=100_000)
    result = compute_liquidity_coverage(positions, obs)

    assert result.liquid_to_annual_spend == pytest.approx(3.0)
    assert not any("liquid_to_annual_spend" in b for b in result.diagnostics.breaches)
    assert not any("liquid_to_annual_spend" in w for w in result.diagnostics.warnings)


# ---- 6. Zero total NAV ------------------------------------------------------


def test_zero_total_nav_no_division_error():
    """Phase 16 #6: zero positions → ratios are None; no ZeroDivisionError."""
    result = compute_liquidity_coverage([], _obligations(annual_spend_usd=100_000))
    assert result.total_position_nav == 0.0
    assert result.liquid_nav == 0.0
    assert result.liquid_to_annual_spend == pytest.approx(0.0)
    assert result.liquid_fraction_of_nav is None
    assert result.illiquid_fraction_of_nav is None


# ---- 7. Missing annual_spend -----------------------------------------------


def test_missing_annual_spend_advisory():
    """Phase 16 #7: annual_spend_usd=None → advisory emitted; ratio is None."""
    positions = [_pos("daily_liquid", 200_000)]
    obs = _obligations()  # all None
    result = compute_liquidity_coverage(positions, obs)

    assert result.liquid_to_annual_spend is None
    assert result.liquidity_runway_quarters is None
    assert any("annual_spend_usd" in a for a in result.diagnostics.advisories)
    assert "annual_spend_usd" in result.diagnostics.missing_obligation_inputs


# ---- 8. T4: unfunded without next-12m calls --------------------------------


def test_t4_unfunded_without_capital_calls_advisory():
    """Phase 16 #8: T4 — unfunded exists, next_12m_capital_calls_usd=None → advisory."""
    positions = [
        _pos("illiquid", 500_000, unfunded=200_000),
    ]
    obs = _obligations(annual_spend_usd=100_000)
    result = compute_liquidity_coverage(positions, obs)

    assert result.total_unfunded_commitments_usd == pytest.approx(200_000)
    assert result.capital_call_coverage is None
    assert any(
        "next_12m_capital_calls_usd" in a or "unfunded" in a for a in result.diagnostics.advisories
    )


# ---- 9. T5: distributable_income mode labeling -----------------------------


def test_t5_distributable_income_labeling():
    """Phase 16 #9: T5 — flow mode sets income estimate; spending_base ratio is None."""
    from aa_model.spending.spending_base import SpendingBaseBreakdown

    positions = [_pos("daily_liquid", 400_000)]
    obs = _obligations()

    spending_base = SpendingBaseBreakdown(
        base_usd=100_000,  # annual distributable income estimate
        excluded_by_tier_usd={},
        excluded_by_income_flag_usd={},
        distributable_income_by_source_usd={"re_entity_01": 100_000.0},
        is_bootstrap=False,
    )

    result = compute_liquidity_coverage(
        positions,
        obs,
        spending_base=spending_base,
        spending_base_is_flow=True,
    )

    # T5: liquid_to_spending_base must be None for flow-type base
    assert result.liquid_to_spending_base is None
    # T5: income estimate ratio set
    assert result.liquid_nav_to_annual_income_estimate == pytest.approx(4.0)


def test_t5_nav_mode_spending_base():
    """Phase 16 #9b: NAV-denominator mode → liquid_to_spending_base set; income est None."""
    from aa_model.spending.spending_base import SpendingBaseBreakdown

    positions = [_pos("daily_liquid", 200_000)]
    obs = _obligations()

    spending_base = SpendingBaseBreakdown(
        base_usd=400_000,
        excluded_by_tier_usd={},
        excluded_by_income_flag_usd={},
    )

    result = compute_liquidity_coverage(
        positions,
        obs,
        spending_base=spending_base,
        spending_base_is_flow=False,
    )

    assert result.liquid_to_spending_base == pytest.approx(0.5)
    assert result.liquid_nav_to_annual_income_estimate is None


# ---- 10. T3: semi-liquid excluded from runway and breach -------------------


def test_t3_semi_liquid_excluded_from_runway_and_breach():
    """Phase 16 #10: T3 — semi-liquid NAV not in runway or breach coverage."""
    positions = [
        _pos("daily_liquid", 80_000),  # liquid
        _pos("semi_liquid", 500_000),  # semi-liquid: advisory only
    ]
    obs = _obligations(annual_spend_usd=100_000)
    result = compute_liquidity_coverage(positions, obs)

    # Liquid is 80k, spend is 100k → BREACH (liquid only; semi excluded)
    assert result.liquid_to_annual_spend == pytest.approx(0.8)
    assert any("liquid_to_annual_spend" in b for b in result.diagnostics.breaches)

    # Runway uses liquid-only: 80k / (100k/4) = 3.2 → floor = 3
    assert result.liquidity_runway_quarters == 3

    # semi_liquid_nav captured in result but not in breach
    assert result.semi_liquid_nav == pytest.approx(500_000)


# ---- 11. T6: total_unfunded_commitments_usd --------------------------------


def test_t6_total_unfunded_commitments():
    """Phase 16 #11: T6 — total_unfunded_commitments_usd correctly aggregated."""
    positions = [
        _pos("illiquid", 1_000_000, unfunded=150_000),
        _pos("illiquid", 500_000, unfunded=75_000),
        _pos("locked_strategic", 200_000, unfunded=None),
    ]
    obs = _obligations()
    result = compute_liquidity_coverage(positions, obs)

    assert result.total_unfunded_commitments_usd == pytest.approx(225_000)


# ---- 12. Byte-stable -------------------------------------------------------


def test_byte_stable():
    """Phase 16 #12: same inputs produce identical LiquidityCoverageResult."""
    positions = [
        _pos("cash_equivalent", 100_000),
        _pos("semi_liquid", 50_000),
        _pos("illiquid", 300_000, unfunded=80_000),
    ]
    obs = _obligations(
        annual_spend_usd=120_000,
        next_12m_capital_calls_usd=40_000,
        next_12m_tax_obligations_usd=20_000,
    )
    cfg = LiquidityCoverageConfig(
        liquid_coverage_breach_threshold=1.0,
        liquid_coverage_warning_threshold=2.0,
    )

    r1 = compute_liquidity_coverage(positions, obs, config=cfg)
    r2 = compute_liquidity_coverage(positions, obs, config=cfg)

    assert r1.liquid_nav == r2.liquid_nav
    assert r1.liquid_to_annual_spend == r2.liquid_to_annual_spend
    assert r1.liquidity_runway_quarters == r2.liquidity_runway_quarters
    assert r1.total_unfunded_commitments_usd == r2.total_unfunded_commitments_usd
    assert r1.diagnostics.breaches == r2.diagnostics.breaches
    assert r1.diagnostics.warnings == r2.diagnostics.warnings
    assert r1.diagnostics.advisories == r2.diagnostics.advisories
