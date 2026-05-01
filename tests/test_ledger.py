"""Ledger arithmetic and §5.1 invariant tests."""

from __future__ import annotations

import pandas as pd
import pytest
from aa_model.integration.ledger import FLOW_ORDER, QuarterlyLedger


def _q(s: str) -> pd.Period:
    return pd.Period(s, freq="Q-DEC")


def test_basic_chain_consistency():
    L = QuarterlyLedger("r1", initial_nav={"a": 100.0}, start_quarter=_q("2026Q1"))
    L.add(quarter=_q("2026Q1"), bucket="a", flow_type="return", amount_usd=10.0, source="cma")
    L.add(quarter=_q("2026Q2"), bucket="a", flow_type="return", amount_usd=5.0, source="cma")
    df = L.finalize()
    assert df.iloc[0]["nav_start_usd"] == 100.0
    assert df.iloc[0]["nav_end_usd"] == 110.0
    assert df.iloc[1]["nav_start_usd"] == 110.0
    assert df.iloc[1]["nav_end_usd"] == 115.0
    L.validate()


def test_canonical_intra_quarter_ordering():
    q = _q("2026Q1")
    L = QuarterlyLedger("r", initial_nav={"a": 0.0}, start_quarter=q)
    # Insert in reverse order; finalize should re-sort.
    L.add(quarter=q, bucket="a", flow_type="transaction_cost", amount_usd=0.0, source="cvx")
    L.add(quarter=q, bucket="a", flow_type="rebalance", amount_usd=0.0, source="z")
    L.add(quarter=q, bucket="a", flow_type="spend", amount_usd=0.0, source="s")
    L.add(quarter=q, bucket="a", flow_type="pe_nav_mark", amount_usd=0.0, source="p")
    L.add(quarter=q, bucket="a", flow_type="pe_distribution", amount_usd=0.0, source="p")
    L.add(quarter=q, bucket="a", flow_type="pe_call", amount_usd=0.0, source="p")
    L.add(quarter=q, bucket="a", flow_type="return", amount_usd=0.0, source="cma")
    L.add(quarter=q, bucket="a", flow_type="inflow", amount_usd=0.0, source="ext")
    df = L.finalize()
    assert df["flow_type"].tolist() == list(FLOW_ORDER)


def test_per_row_consistency_holds_by_construction():
    q = _q("2026Q1")
    L = QuarterlyLedger("r", initial_nav={"a": 100.0}, start_quarter=q)
    L.add(quarter=q, bucket="a", flow_type="return", amount_usd=2.5, source="cma")
    df = L.finalize()
    diff = (df["nav_end_usd"] - df["nav_start_usd"] - df["amount_usd"]).abs().max()
    assert diff < 1e-12


def test_rebalance_zero_sum_violation_detected():
    q = _q("2026Q1")
    L = QuarterlyLedger("r", initial_nav={"a": 100.0, "b": 50.0}, start_quarter=q)
    L.add(quarter=q, bucket="a", flow_type="rebalance", amount_usd=-10.0, source="r")
    L.add(quarter=q, bucket="b", flow_type="rebalance", amount_usd=+9.0, source="r")
    with pytest.raises(AssertionError, match="rebalance not zero-sum"):
        L.validate()


def test_pe_call_zero_sum_violation_detected():
    q = _q("2026Q1")
    L = QuarterlyLedger("r", initial_nav={"cash": 100.0, "pe_buyout": 0.0}, start_quarter=q)
    L.add(quarter=q, bucket="pe_buyout", flow_type="pe_call", amount_usd=10.0, source="p")
    L.add(quarter=q, bucket="cash", flow_type="pe_call", amount_usd=-9.0, source="p")
    with pytest.raises(AssertionError, match="pe_call not zero-sum"):
        L.validate()


def test_external_cash_flow_tie_out():
    q = _q("2026Q1")
    L = QuarterlyLedger("r", initial_nav={"cash": 100.0}, start_quarter=q)
    L.add(quarter=q, bucket="cash", flow_type="inflow", amount_usd=50.0, source="ext")
    L.add(quarter=q, bucket="cash", flow_type="spend", amount_usd=-30.0, source="sp")
    # Net external this quarter = +20. Mismatch should raise.
    L.validate(expected_externals_by_quarter={q: 20.0})  # pass case
    with pytest.raises(AssertionError, match="external cash flow tie-out"):
        L.validate(expected_externals_by_quarter={q: 99.0})


def test_transaction_cost_counts_toward_external_tie_out():
    """Phase 3b extension: transaction_cost behaves like a household-external
    outflow. It must be summed with inflow / spend in the external tie-out
    check, and it must be included in the total NAV conservation contribution
    set so the ledger still balances when costs are non-zero.
    """
    q = _q("2026Q1")
    L = QuarterlyLedger("r", initial_nav={"cash": 100.0, "bond": 50.0}, start_quarter=q)
    L.add(quarter=q, bucket="cash", flow_type="return", amount_usd=2.0, source="cma")
    L.add(quarter=q, bucket="bond", flow_type="return", amount_usd=1.0, source="cma")
    L.add(quarter=q, bucket="cash", flow_type="rebalance", amount_usd=-5.0, source="r")
    L.add(quarter=q, bucket="bond", flow_type="rebalance", amount_usd=+5.0, source="r")
    L.add(quarter=q, bucket="cash", flow_type="transaction_cost", amount_usd=-0.5, source="cvx")
    # Net external this quarter = inflow(0) + spend(0) + transaction_cost(-0.5).
    L.validate(expected_externals_by_quarter={q: -0.5})
    # Total NAV: (51 + 96.5) - (50 + 100) = -2.5;
    # contributing flows: return(+1+2) + transaction_cost(-0.5) = 2.5 — passes
    # the conservation check. Mismatch on the external truth raises.
    with pytest.raises(AssertionError, match="external cash flow tie-out"):
        L.validate(expected_externals_by_quarter={q: 0.0})


def test_transaction_cost_only_on_cash_keeps_buckets_consistent():
    """transaction_cost is NOT zero-sum across buckets — it's an external
    outflow on cash with no offset elsewhere. Other invariants still hold.
    """
    q = _q("2026Q1")
    L = QuarterlyLedger("r", initial_nav={"cash": 100.0}, start_quarter=q)
    L.add(quarter=q, bucket="cash", flow_type="transaction_cost", amount_usd=-3.0, source="cvx")
    L.validate(expected_externals_by_quarter={q: -3.0})
    df = L.finalize()
    assert df.iloc[0]["nav_end_usd"] == 97.0


def test_total_nav_conservation():
    q1 = _q("2026Q1")
    q2 = _q("2026Q2")
    L = QuarterlyLedger("r", initial_nav={"a": 100.0, "b": 50.0}, start_quarter=q1)
    # P&L only: a +10, b +5 in q1; rebalance moves 5 from a to b.
    L.add(quarter=q1, bucket="a", flow_type="return", amount_usd=10.0, source="cma")
    L.add(quarter=q1, bucket="b", flow_type="return", amount_usd=5.0, source="cma")
    L.add(quarter=q1, bucket="a", flow_type="rebalance", amount_usd=-5.0, source="r")
    L.add(quarter=q1, bucket="b", flow_type="rebalance", amount_usd=+5.0, source="r")
    # q2 P&L
    L.add(quarter=q2, bucket="a", flow_type="return", amount_usd=10.0, source="cma")
    L.add(quarter=q2, bucket="b", flow_type="return", amount_usd=5.0, source="cma")
    L.validate()


def test_nan_amount_rejected_at_add_time():
    q = _q("2026Q1")
    L = QuarterlyLedger("r", initial_nav={"a": 100.0}, start_quarter=q)
    with pytest.raises(ValueError, match="NaN"):
        L.add(quarter=q, bucket="a", flow_type="return", amount_usd=float("nan"), source="cma")


def test_unknown_flow_type_rejected():
    q = _q("2026Q1")
    L = QuarterlyLedger("r", initial_nav={"a": 100.0}, start_quarter=q)
    with pytest.raises(ValueError, match="unknown flow_type"):
        L.add(quarter=q, bucket="a", flow_type="bogus", amount_usd=1.0, source="x")


def test_finalize_idempotent_and_locks():
    q = _q("2026Q1")
    L = QuarterlyLedger("r", initial_nav={"a": 100.0}, start_quarter=q)
    L.add(quarter=q, bucket="a", flow_type="return", amount_usd=5.0, source="cma")
    df1 = L.finalize()
    df2 = L.finalize()
    assert df1 is df2
    with pytest.raises(RuntimeError, match="already finalized"):
        L.add(quarter=q, bucket="a", flow_type="return", amount_usd=1.0, source="cma")


def test_closed_through_filters_to_quarter_inclusive():
    """Phase 4a primitive: closed_through(q) returns rows with
    quarter <= q with the chain computed; appends after the call still work.
    """
    q1 = _q("2026Q1")
    q2 = _q("2026Q2")
    L = QuarterlyLedger("r", initial_nav={"a": 100.0}, start_quarter=q1)
    L.add(quarter=q1, bucket="a", flow_type="return", amount_usd=10.0, source="cma")
    L.add(quarter=q2, bucket="a", flow_type="return", amount_usd=5.0, source="cma")
    view_q1 = L.closed_through(q1)
    assert len(view_q1) == 1
    assert view_q1.iloc[0]["nav_end_usd"] == 110.0
    # Append after closed_through must not be locked.
    q3 = _q("2026Q3")
    L.add(quarter=q3, bucket="a", flow_type="return", amount_usd=2.0, source="cma")
    full = L.finalize()
    assert len(full) == 3


def test_closed_through_with_quarter_before_start_returns_empty():
    q1 = _q("2026Q1")
    L = QuarterlyLedger("r", initial_nav={"a": 100.0}, start_quarter=q1)
    L.add(quarter=q1, bucket="a", flow_type="return", amount_usd=10.0, source="cma")
    view = L.closed_through(q1 - 1)
    assert view.empty


def test_end_nav_through_returns_initial_when_no_rows_for_bucket():
    q1 = _q("2026Q1")
    L = QuarterlyLedger("r", initial_nav={"a": 100.0, "b": 50.0}, start_quarter=q1)
    L.add(quarter=q1, bucket="a", flow_type="return", amount_usd=10.0, source="cma")
    nav = L.end_nav_through(q1)
    assert nav["a"] == 110.0
    assert nav["b"] == 50.0  # unchanged from initial


def test_end_nav_through_pre_start_returns_initial():
    q1 = _q("2026Q1")
    L = QuarterlyLedger("r", initial_nav={"a": 100.0}, start_quarter=q1)
    nav = L.end_nav_through(q1 - 1)
    assert nav["a"] == 100.0


def test_spend_uniqueness_passes_for_one_row_per_source_per_quarter():
    """Phase 4a hardening: a single spend row per (run_id, quarter, source) is
    the orchestrator's emission pattern. Multiple distinct sources in the same
    quarter remain valid (e.g. spending rule + ad-hoc external withdrawal).
    """
    q1 = _q("2026Q1")
    q2 = _q("2026Q2")
    L = QuarterlyLedger("r", initial_nav={"cash": 100.0}, start_quarter=q1)
    L.add(quarter=q1, bucket="cash", flow_type="spend", amount_usd=-2.0, source="spending:flat")
    L.add(quarter=q1, bucket="cash", flow_type="spend", amount_usd=-1.0, source="spending:other")
    L.add(quarter=q2, bucket="cash", flow_type="spend", amount_usd=-2.0, source="spending:flat")
    L.validate()


def test_spend_uniqueness_violation_detected():
    """Two spend rows at the same (run_id, quarter, source) would silently
    double-count under path-dependent prior-spend recovery. Invariant must
    catch it.
    """
    q = _q("2026Q1")
    L = QuarterlyLedger("r", initial_nav={"cash": 100.0}, start_quarter=q)
    L.add(quarter=q, bucket="cash", flow_type="spend", amount_usd=-1.0, source="spending:flat")
    L.add(quarter=q, bucket="cash", flow_type="spend", amount_usd=-1.0, source="spending:flat")
    with pytest.raises(AssertionError, match="duplicate spend row"):
        L.validate()


def test_end_nav_by_quarter_includes_all_buckets():
    q1 = _q("2026Q1")
    q2 = _q("2026Q2")
    L = QuarterlyLedger("r", initial_nav={"a": 100.0, "b": 50.0}, start_quarter=q1)
    # Only `a` has flows; `b` should still appear with carried NAV.
    L.add(quarter=q1, bucket="a", flow_type="return", amount_usd=10.0, source="cma")
    L.add(quarter=q2, bucket="a", flow_type="return", amount_usd=5.0, source="cma")
    grid = L.end_nav_by_quarter()
    assert "b" in grid.columns
    # b had no rows so its end NAV stays at initial_nav across quarters.
    assert grid.loc[q1, "b"] == 50.0
    assert grid.loc[q2, "b"] == 50.0
