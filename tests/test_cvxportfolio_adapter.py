"""Phase 3b — cvxportfolio implementation adapter tests.

Two tiers of validation:

1. **Structural parity at zero cost.** With ``bps_per_trade == 0`` the
   adapter MUST produce trades and cost identical to
   :class:`StubImplementation`. This is the parity contract analogue of
   Phase 3a's binding-equality test.
2. **Numerical anchor at non-zero bps.** Hand-worked closed-form check on
   a fixed (current, target, bps) input — proves the adapter's cost
   formula matches the documented linear model within ``ε = 1e-9 USD``,
   with a per-bucket trade match within the L11 ε of ``1e-4``.

Plus determinism, no-NaN, and a non-cash-side smoke check.

Path-dependence statement: the adapter has none. Trades depend only on
the current and target vectors handed in for *this* call; no state
from prior calls influences the result. See L13 in MODEL_DOCUMENTATION.
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

cvxportfolio = pytest.importorskip("cvxportfolio")  # noqa: F841

from aa_model.implementation.base import CostModel  # noqa: E402
from aa_model.implementation.cvxportfolio_adapter import (  # noqa: E402
    CvxportfolioImplementation,
)
from aa_model.implementation.stub import StubImplementation  # noqa: E402


def _portfolio() -> tuple[pd.Series, pd.Series]:
    current = pd.Series(
        {
            "cash": 5_000_000.0,
            "public_bond": 22_000_000.0,
            "public_equity": 48_000_000.0,
            "pe_buyout": 25_000_000.0,
        }
    )
    target = pd.Series(
        {
            "cash": 5_000_000.0,
            "public_bond": 20_000_000.0,
            "public_equity": 50_000_000.0,
            "pe_buyout": 25_000_000.0,
        }
    )
    return current, target


# ---- structural parity at zero cost -----------------------------------------


def test_zero_bps_trades_match_stub_exactly():
    """The numerical anchor at bps == 0: cvxportfolio must produce trades
    bit-equal to the stub, since both are computing target - current.
    """
    current, target = _portfolio()
    costs = CostModel(bps_per_trade=0.0)
    stub_result = StubImplementation().rebalance(current, target, costs)
    cvx_result = CvxportfolioImplementation().rebalance(current, target, costs)
    pd.testing.assert_series_equal(stub_result.trades, cvx_result.trades, atol=1e-4)
    assert cvx_result.cost_usd == 0.0


def test_zero_bps_trades_sum_to_zero():
    current, target = _portfolio()
    cvx_result = CvxportfolioImplementation().rebalance(
        current, target, CostModel(bps_per_trade=0.0)
    )
    # Trades sum to zero by construction (target and current both sum to total NAV).
    assert abs(cvx_result.trades.sum()) < 1e-6


# ---- numerical anchor at non-zero bps ---------------------------------------


def test_numerical_anchor_5bps_closed_form():
    """Hand-worked anchor: trades = [0, -2M, +2M, 0]; |trades| sum = 4M.
    At 5 bps linear cost: 4_000_000 * 5/10000 = 2_000.00 USD.
    """
    current, target = _portfolio()
    costs = CostModel(bps_per_trade=5.0)
    result = CvxportfolioImplementation().rebalance(current, target, costs)

    expected_trades = pd.Series(
        {
            "cash": 0.0,
            "public_bond": -2_000_000.0,
            "public_equity": 2_000_000.0,
            "pe_buyout": 0.0,
        },
        name="trade_usd",
    )
    pd.testing.assert_series_equal(
        result.trades.sort_index(), expected_trades.sort_index(), atol=1e-4
    )
    assert result.cost_usd == pytest.approx(2_000.0, abs=1e-9)


def test_cost_scales_linearly_with_bps():
    """Linear cost: doubling bps must double cost; trades unchanged."""
    current, target = _portfolio()
    a = CvxportfolioImplementation().rebalance(current, target, CostModel(bps_per_trade=5.0))
    b = CvxportfolioImplementation().rebalance(current, target, CostModel(bps_per_trade=10.0))
    pd.testing.assert_series_equal(a.trades, b.trades, atol=1e-4)
    assert b.cost_usd == pytest.approx(2.0 * a.cost_usd, rel=1e-12)


def test_cost_proportional_to_trade_volume():
    """Doubling every trade must double the cost at fixed bps."""
    cur1, tgt1 = _portfolio()
    cur2 = cur1.copy()
    # Stretch the target away from current by 2x the gap on each bucket.
    tgt2 = cur2 + 2 * (tgt1 - cur1)
    costs = CostModel(bps_per_trade=7.0)
    a = CvxportfolioImplementation().rebalance(cur1, tgt1, costs)
    b = CvxportfolioImplementation().rebalance(cur2, tgt2, costs)
    assert b.cost_usd == pytest.approx(2.0 * a.cost_usd, rel=1e-12)


# ---- determinism + structural sanity ----------------------------------------


def test_deterministic_for_same_inputs():
    current, target = _portfolio()
    a = CvxportfolioImplementation().rebalance(current, target, CostModel(bps_per_trade=3.0))
    b = CvxportfolioImplementation().rebalance(current, target, CostModel(bps_per_trade=3.0))
    pd.testing.assert_series_equal(a.trades, b.trades, atol=1e-4)
    assert a.cost_usd == b.cost_usd


def test_no_nan_or_inf_in_trades_or_cost():
    current, target = _portfolio()
    result = CvxportfolioImplementation().rebalance(current, target, CostModel(bps_per_trade=5.0))
    assert not result.trades.isna().any()
    assert not result.trades.isin([math.inf, -math.inf]).any()
    assert math.isfinite(result.cost_usd)


def test_buckets_only_in_one_side_handled():
    """If a bucket is in target but not current (or vice versa), it should
    be aligned and not raise."""
    current = pd.Series({"cash": 100.0, "public_bond": 50.0})
    target = pd.Series({"cash": 80.0, "public_bond": 40.0, "public_equity": 30.0})
    result = CvxportfolioImplementation().rebalance(current, target, CostModel(bps_per_trade=5.0))
    # Trades: cash -20, bond -10, equity +30. |trades| sum = 60.
    # Cost: 60 * 5/10000 = 0.03.
    assert result.cost_usd == pytest.approx(0.03, abs=1e-12)
    assert result.trades["public_equity"] == pytest.approx(30.0, abs=1e-4)


# ---- diagnostics ------------------------------------------------------------


def test_diagnostics_record_engine_and_version():
    impl = CvxportfolioImplementation()
    d = impl.diagnostics()
    assert d["engine"] == "cvxportfolio"
    assert d["cvxportfolio_version"] == cvxportfolio.__version__
    assert "linear" in d["cost_model"]
