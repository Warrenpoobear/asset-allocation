"""Liquidity / drawdown metric tests against hand-worked examples."""

from __future__ import annotations

import math

import pandas as pd
import pytest
from aa_model.spending.liquidity import (
    compute_liquidity_metrics,
    coverage_months_per_quarter,
    max_drawdown,
    shortfall_frequency,
)


def _idx(n: int) -> pd.PeriodIndex:
    return pd.period_range("2026Q1", periods=n, freq="Q-DEC")


def test_coverage_hand_worked_example():
    """liquid = $5M cash + $20M bond = $25M; annual spend = $4M;
    monthly spend = $4M/12; coverage = $25M / ($4M/12) = 75 months.
    """
    nav = pd.DataFrame(
        {
            "cash": [5_000_000.0] * 4,
            "public_bond": [20_000_000.0] * 4,
            "pe_buyout": [25_000_000.0] * 4,
        },
        index=_idx(4),
    )
    annual = pd.Series([4_000_000.0] * 4, index=nav.index)
    cov = coverage_months_per_quarter(nav, annual, liquid_buckets=("cash", "public_bond"))
    for v in cov.tolist():
        assert v == pytest.approx(75.0)


def test_coverage_excludes_pe_bucket():
    nav = pd.DataFrame(
        {
            "cash": [10_000_000.0],
            "pe_buyout": [50_000_000.0],
        },
        index=_idx(1),
    )
    annual = pd.Series([12_000_000.0], index=nav.index)  # $1M/month
    cov = coverage_months_per_quarter(nav, annual, liquid_buckets=("cash", "public_bond"))
    # Only cash counts; public_bond absent. Coverage = $10M / $1M = 10 months.
    assert cov.iloc[0] == pytest.approx(10.0)


def test_coverage_zero_spend_is_infinite():
    nav = pd.DataFrame({"cash": [5_000_000.0]}, index=_idx(1))
    annual = pd.Series([0.0], index=nav.index)
    cov = coverage_months_per_quarter(nav, annual, liquid_buckets=("cash",))
    assert math.isinf(cov.iloc[0])


def test_shortfall_frequency_threshold():
    cov = pd.Series([24.0, 17.0, 12.0, 30.0])
    assert shortfall_frequency(cov, 18.0) == 0.5  # 17, 12 below 18


def test_max_drawdown_on_simple_path():
    """100 → 110 → 90 → 95: peak 110, trough 90, dd = 90/110 - 1 = -2/11 ≈ -18.18%, length 1q."""
    total = pd.Series([100.0, 110.0, 90.0, 95.0])
    dd, n = max_drawdown(total)
    assert dd == pytest.approx(-2.0 / 11.0, rel=1e-9)
    assert n == 1


def test_max_drawdown_monotone_path_returns_zero():
    total = pd.Series([100.0, 110.0, 120.0, 130.0])
    dd, n = max_drawdown(total)
    assert dd == 0.0
    assert n == 0


def test_compute_liquidity_metrics_aggregates_correctly():
    nav = pd.DataFrame(
        {
            "cash": [5_000_000.0, 5_000_000.0, 5_000_000.0, 5_000_000.0],
            "public_bond": [20_000_000.0, 20_000_000.0, 20_000_000.0, 20_000_000.0],
            "pe_buyout": [25_000_000.0, 30_000_000.0, 28_000_000.0, 32_000_000.0],
        },
        index=_idx(4),
    )
    annual = pd.Series([4_000_000.0] * 4, index=nav.index)
    initial = 50_000_000.0
    m = compute_liquidity_metrics(
        nav,
        annual,
        floor_months=18.0,
        initial_nav_usd=initial,
        liquid_buckets=("cash", "public_bond"),
    )
    # Final NAV: $5M + $20M + $32M = $57M; vs $50M → +14%
    assert m.final_nav_usd == 57_000_000.0
    assert m.cumulative_return_pct == pytest.approx(14.0)
    # Coverage uniform 75 months → never below 18.
    assert m.min_coverage_months == pytest.approx(75.0)
    assert m.mean_coverage_months == pytest.approx(75.0)
    assert m.shortfall_frequency == 0.0
    # Total NAV path: 50M → 55M → 53M → 57M. Peak 55M at q2, trough 53M at q3.
    # max_dd = 53/55 - 1 = -3.636%
    assert m.max_drawdown_pct == pytest.approx((53.0 / 55.0 - 1.0) * 100.0, rel=1e-9)
    # Length: peak q2 → trough q3 = 1 quarter.
    assert m.drawdown_quarters == 1
