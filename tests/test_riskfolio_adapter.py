"""Phase 3a — riskfolio adapter contract + stub parity tests.

The parity contract here is **structural**, not numerical. The stub returns
configured weights verbatim; the riskfolio adapter solves a min-variance
optimization. They produce different vectors by design. What they MUST share
is the surface every downstream consumer (orchestrator, ledger, report)
relies on:

* same bucket index
* sums to 1 within 1e-6
* all weights in [0, 1] (no shorts)
* no NaN / inf
* binding equality constraints (min == max for a bucket) collapse both
  adapters to that exact weight within 1e-6
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

riskfolio = pytest.importorskip("riskfolio")  # noqa: F841

from aa_model.allocation.constraints import Constraints  # noqa: E402
from aa_model.allocation.riskfolio_adapter import RiskfolioAdapter  # noqa: E402
from aa_model.allocation.stub import StubAllocator  # noqa: E402
from aa_model.assumptions.cma import CMA  # noqa: E402
from aa_model.io.schemas import PublicAllocationConfig  # noqa: E402

_BUCKETS = ("cash", "public_bond", "public_equity", "pe_buyout")


def _config() -> PublicAllocationConfig:
    return PublicAllocationConfig(
        stub_weights={"cash": 0.05, "public_bond": 0.20, "public_equity": 0.50, "pe_buyout": 0.25}
    )


def _fit(adapter, *, constraints: Constraints | None = None) -> pd.Series:
    adapter.fit(pd.DataFrame(), CMA(), constraints or Constraints())
    return adapter.weights()


# ---- single-adapter sanity --------------------------------------------------


def test_riskfolio_weights_sum_to_one_no_nan():
    w = _fit(RiskfolioAdapter(_config()))
    assert abs(w.sum() - 1.0) < 1e-6
    assert not w.isna().any()
    assert not w.isin([math.inf, -math.inf]).any()


def test_riskfolio_weights_all_non_negative():
    w = _fit(RiskfolioAdapter(_config()))
    assert (w >= -1e-9).all(), f"negative weight detected: {w[w < -1e-9].to_dict()}"


def test_riskfolio_weights_all_at_most_one():
    w = _fit(RiskfolioAdapter(_config()))
    assert (w <= 1.0 + 1e-9).all()


def test_riskfolio_diagnostics_records_engine():
    a = RiskfolioAdapter(_config())
    _fit(a)
    d = a.diagnostics()
    assert d["engine"] == "riskfolio"
    assert "fit_inputs" in d


def test_riskfolio_must_fit_before_weights():
    a = RiskfolioAdapter(_config())
    with pytest.raises(RuntimeError, match="fit"):
        a.weights()


# ---- determinism ------------------------------------------------------------


def test_riskfolio_is_deterministic():
    """Two adapters fit on identical inputs return identical weights (within
    solver tolerance — different runs of the same convex problem must converge
    to the same point).
    """
    a = RiskfolioAdapter(_config())
    b = RiskfolioAdapter(_config())
    wa = _fit(a)
    wb = _fit(b)
    pd.testing.assert_series_equal(wa, wb, atol=1e-9)


# ---- stub parity contract ---------------------------------------------------


def test_stub_and_riskfolio_share_index_and_dtype():
    cfg = _config()
    stub = StubAllocator(cfg)
    rf = RiskfolioAdapter(cfg)
    ws = _fit(stub)
    wr = _fit(rf)
    assert sorted(ws.index.tolist()) == sorted(wr.index.tolist())
    assert ws.dtype == wr.dtype


def test_stub_and_riskfolio_both_sum_to_one():
    cfg = _config()
    ws = _fit(StubAllocator(cfg))
    wr = _fit(RiskfolioAdapter(cfg))
    assert abs(ws.sum() - 1.0) < 1e-6
    assert abs(wr.sum() - 1.0) < 1e-6


def test_binding_equality_constraint_pins_both_adapters():
    """min == max for a bucket → both adapters must produce that exact weight.

    With 4 buckets, fix two of them at 0.05 and 0.25; the remaining two
    can take any non-negative values summing to 0.70. Both adapters must
    produce 0.05 for cash and 0.25 for pe_buyout.
    """
    cfg = _config()
    cons = Constraints(
        min_weights={"cash": 0.05, "pe_buyout": 0.25},
        max_weights={"cash": 0.05, "pe_buyout": 0.25},
    )
    ws = _fit(StubAllocator(cfg), constraints=cons)
    wr = _fit(RiskfolioAdapter(cfg), constraints=cons)
    assert ws["cash"] == pytest.approx(0.05, abs=1e-6)
    assert ws["pe_buyout"] == pytest.approx(0.25, abs=1e-6)
    assert wr["cash"] == pytest.approx(0.05, abs=1e-6)
    assert wr["pe_buyout"] == pytest.approx(0.25, abs=1e-6)


# ---- structural sanity that defends downstream consumers --------------------


def test_riskfolio_consumes_explicit_cma_not_fallback():
    """Phase 5: with an explicit CMA passed, the adapter must reflect those
    values (not the hard-coded fallback). Two different vol vectors that
    differ only in the public_equity entry must produce different
    minimum-variance weights — proves the adapter is reading the CMA.
    """
    import numpy as np

    cfg = _config()
    buckets = list(cfg.stub_weights.keys())

    def _cma_with(vol_equity: float) -> CMA:
        idx = sorted(buckets)
        # Same vols as the fallback table for everything except equity.
        vol_table = {"cash": 0.005, "public_bond": 0.04, "public_equity": vol_equity, "pe_buyout": 0.20}
        vol = pd.Series([vol_table[b] for b in idx], index=idx, dtype=float)
        er = pd.Series(0.0, index=idx, dtype=float)
        corr = pd.DataFrame(np.eye(len(idx)), index=idx, columns=idx)
        return CMA(expected_returns_annual=er, vol_annual=vol, corr=corr)

    adapter_low = RiskfolioAdapter(_config())
    adapter_high = RiskfolioAdapter(_config())
    adapter_low.fit(pd.DataFrame(), _cma_with(0.10), Constraints())
    adapter_high.fit(pd.DataFrame(), _cma_with(0.30), Constraints())
    w_low = adapter_low.weights()
    w_high = adapter_high.weights()

    # Higher equity vol → MinRisk shifts weight away from public_equity.
    assert w_high["public_equity"] < w_low["public_equity"] - 1e-6, (
        f"adapter ignored explicit CMA: w_low={w_low.to_dict()}, w_high={w_high.to_dict()}"
    )


def test_weights_returned_are_a_copy():
    a = RiskfolioAdapter(_config())
    _fit(a)
    w = a.weights()
    w["cash"] = 999.0
    w2 = a.weights()
    assert w2["cash"] != 999.0
