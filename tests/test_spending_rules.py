"""Spending rule tests."""

from __future__ import annotations

import pandas as pd
import pytest
from aa_model.integration.ledger import QuarterlyLedger
from aa_model.io.schemas import SmoothingConfig, SpendingConfig
from aa_model.spending.base import SpendingParams
from aa_model.spending.rules import FlatRealRule, SmoothingRule, make_rule


def _params(
    rule: str = "flat_real",
    weight: float = 0.0,
    inflation: float = 0.025,
    annual: float = 4_000_000,
) -> SpendingParams:
    cfg = SpendingConfig(
        rule=rule,
        annual_spend_usd=annual,
        inflation_pct=inflation,
        smoothing=SmoothingConfig(window_quarters=12, weight=weight),
        floor_usd=0.0,
        ceiling_usd=1e12,
    )
    return SpendingParams(
        config=cfg,
        start_quarter=pd.Period("2026Q1", freq="Q-DEC"),
        num_quarters=8,
    )


def _empty_ledger(p: SpendingParams) -> QuarterlyLedger:
    return QuarterlyLedger("test", initial_nav={"cash": 0.0}, start_quarter=p.start_quarter)


def test_flat_real_first_year_constant():
    p = _params()
    out = FlatRealRule().quarterly_outflows(_empty_ledger(p), p)
    assert all(v == 1_000_000.0 for v in out.iloc[:4])


def test_flat_real_inflation_steps_up_at_year_boundary():
    p = _params()
    out = FlatRealRule().quarterly_outflows(_empty_ledger(p), p)
    assert pytest.approx(out.iloc[4]) == 1_025_000.0
    assert pytest.approx(out.iloc[7]) == 1_025_000.0


def test_smoothing_full_weight_equals_flat_real():
    p = _params(rule="smoothing", weight=1.0)
    L = _empty_ledger(p)
    pd.testing.assert_series_equal(
        FlatRealRule().quarterly_outflows(L, p),
        SmoothingRule().quarterly_outflows(L, p),
    )


def test_smoothing_zero_weight_freezes_at_initial_target():
    p = _params(rule="smoothing", weight=0.0)
    out = SmoothingRule().quarterly_outflows(_empty_ledger(p), p)
    # spend_0 = target_0 = annual/4; subsequent quarters never re-anchor.
    assert (out == 1_000_000.0).all()


def test_smoothing_intermediate_weight_lies_between():
    p_flat = _params(rule="flat_real")
    p_smooth = _params(rule="smoothing", weight=0.5)
    L = _empty_ledger(p_flat)
    flat = FlatRealRule().quarterly_outflows(L, p_flat)
    smooth = SmoothingRule().quarterly_outflows(L, p_smooth)
    # First quarter: equal (both anchor on target_0).
    assert smooth.iloc[0] == flat.iloc[0]
    # After the first inflation step (q4), smoothed value sits strictly between
    # the prior smoothed value and the new target.
    assert flat.iloc[3] < smooth.iloc[4] < flat.iloc[4]


def test_smoothing_recursion_matches_closed_form():
    """spend_t = w * target_t + (1 - w) * spend_{t-1}; verify against hand math."""
    w = 0.4
    p = _params(rule="smoothing", weight=w, annual=4_000_000, inflation=0.025)
    out = SmoothingRule().quarterly_outflows(_empty_ledger(p), p)
    target = [1_000_000.0] * 4 + [1_025_000.0] * 4
    expected = [target[0]]
    for i in range(1, 8):
        expected.append(w * target[i] + (1.0 - w) * expected[-1])
    for i in range(8):
        assert out.iloc[i] == pytest.approx(expected[i], rel=1e-12)


def test_make_rule_factory():
    assert isinstance(make_rule("flat_real"), FlatRealRule)
    assert isinstance(make_rule("smoothing"), SmoothingRule)
    with pytest.raises(ValueError):
        make_rule("unknown")


def test_floor_clip():
    p = _params(annual=0.0)  # zero spend; floor should be 0 by default and not clip
    out = FlatRealRule().quarterly_outflows(_empty_ledger(p), p)
    assert (out == 0.0).all()
