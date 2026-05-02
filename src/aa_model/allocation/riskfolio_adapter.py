"""Riskfolio-Lib allocation adapter (SPEC §6 Phase 3a).

A reference *non-stub* implementation of :class:`AllocationAdapter` backed by
``riskfolio-lib``. The adapter solves a minimum-variance optimization with
sum-to-one + non-negativity + caller-provided box bounds, using the CMA
passed to :meth:`fit`.

Contract / discipline (Phase 3 guardrails)
==========================================

* **Pure function from inputs → outputs.** No module-level state, no
  hidden caches, no ledger access. Two ``RiskfolioAdapter`` instances
  fitted on identical inputs produce identical weights (within solver
  tolerance).
* **Stub-parity contract.** Stub and riskfolio outputs are not bit-equal
  — they solve different problems (return config weights verbatim vs.
  minimize portfolio variance). What they DO share, by contract, is
  the structural surface tested in ``tests/test_riskfolio_adapter.py``:

    1. Same bucket index, same dtype.
    2. Weights sum to ``1.0`` within ``1e-6``.
    3. All weights in ``[0, 1]`` (no shorts under default constraints).
    4. No NaN / inf.
    5. Under a *binding* equality constraint (``min == max`` for a bucket),
       both adapters produce that exact weight within ``1e-6``.

* **Default CMA fallback.** When called with an empty :class:`CMA`
  (Phase 1's default), the adapter synthesizes annualized vols from a
  hard-coded per-bucket table and an identity correlation matrix. This
  exists so the orchestrator can run ``engine=riskfolio`` against the
  Phase 1 fixtures without first growing a real CMA pipeline. Real users
  pass in a populated CMA.

Wheel availability (probed 2026-05-01)
======================================

riskfolio-lib 7.2.1 + cvxpy 1.8.2 + clarabel + scs + osqp all resolve to
binary wheels on ``manylinux_2_24_aarch64`` / ``manylinux_2_27_aarch64``.
No source compilation required on WSL2 aarch64.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from aa_model.allocation.base import AllocationAdapter
from aa_model.allocation.constraints import Constraints
from aa_model.assumptions.cma import CMA
from aa_model.io.schemas import PublicAllocationConfig

# TEST-ONLY fallback. Reachable only when the caller passes an empty
# CMA() default-constructed sentinel. **Must not be used by
# orchestrator-loaded production configs** — those load explicit CMA via
# configs/cma.yaml (Phase 5; see MODEL_DOCUMENTATION.md §Phase 5 design
# / decision B and the L4 [RESOLVED] callout). The values are
# conservative round numbers chosen to keep Phase 1 fixture optimizations
# sensible, not realistic CMA inputs. **Remove this table (and the
# fallback branch in `_build_inputs`) once every test path passes an
# explicit CMA fixture** — at that point the empty-CMA sentinel becomes
# unreachable and the placeholder lineage closes completely.
_DEFAULT_VOL_ANNUAL: dict[str, float] = {
    "cash": 0.005,
    "public_bond": 0.04,
    "public_equity": 0.16,
    "pe_buyout": 0.20,
    "pe_venture": 0.30,
    "pe_growth": 0.22,
    "pe_infra": 0.12,
    "pe_re": 0.14,
    "pe_pc": 0.10,
}
_DEFAULT_VOL_FALLBACK: float = 0.15


@dataclass(frozen=True)
class _OptimizationInputs:
    buckets: list[str]
    expected_returns: pd.Series  # annualized
    cov: pd.DataFrame  # annualized
    lower: pd.Series
    upper: pd.Series


class RiskfolioAdapter(AllocationAdapter):
    """Minimum-variance allocator backed by ``riskfolio-lib``.

    Parameters
    ----------
    config:
        The :class:`PublicAllocationConfig`. ``stub_weights`` defines the
        bucket universe; ``stub_weights`` values themselves are NOT used by
        the optimizer (they are the stub's domain).
    objective:
        Riskfolio objective ('MinRisk' minimum variance, 'Sharpe' max
        Sharpe). Phase 3a default is ``MinRisk``.
    risk_measure:
        Riskfolio risk measure (`rm` argument). ``MV`` (variance) is the
        only Phase 3a default.
    """

    def __init__(
        self,
        config: PublicAllocationConfig,
        *,
        objective: str = "MinRisk",
        risk_measure: str = "MV",
    ) -> None:
        self._buckets: list[str] = sorted(config.stub_weights.keys())
        self._objective = objective
        self._risk_measure = risk_measure
        self._weights: pd.Series | None = None
        self._diagnostics: dict = {
            "engine": "riskfolio",
            "objective": objective,
            "risk_measure": risk_measure,
        }

    # ---- AllocationAdapter ABC --------------------------------------------

    def fit(self, returns: pd.DataFrame, cma: CMA, constraints: Constraints) -> None:
        inputs = self._build_inputs(returns, cma, constraints)
        self._weights = self._solve(inputs)
        self._diagnostics["fit_inputs"] = {
            "returns_shape": tuple(returns.shape) if returns is not None else None,
            "n_buckets": len(inputs.buckets),
            "n_constraints": (
                (len(constraints.min_weights) + len(constraints.max_weights))
                if constraints is not None
                else 0
            ),
        }

    def weights(self) -> pd.Series:
        if self._weights is None:
            raise RuntimeError("call fit() before weights()")
        return self._weights.copy()

    def diagnostics(self) -> dict:
        return dict(self._diagnostics)

    # ---- internal ---------------------------------------------------------

    def _build_inputs(
        self, returns: pd.DataFrame, cma: CMA, constraints: Constraints
    ) -> _OptimizationInputs:
        buckets = list(self._buckets)
        n = len(buckets)

        # Expected returns: prefer CMA; fall back to zeros (MinRisk doesn't use them anyway).
        if cma.expected_returns_annual is not None and not cma.expected_returns_annual.empty:
            er = cma.expected_returns_annual.reindex(buckets).fillna(0.0).astype(float)
        else:
            er = pd.Series(0.0, index=buckets, dtype=float)

        # Covariance: prefer CMA vol+corr; fall back to default vols + identity corr.
        if (
            cma.vol_annual is not None
            and not cma.vol_annual.empty
            and cma.corr is not None
            and not cma.corr.empty
        ):
            vol = cma.vol_annual.reindex(buckets).astype(float)
            corr = cma.corr.reindex(index=buckets, columns=buckets).astype(float)
        else:
            vol = pd.Series(
                [_DEFAULT_VOL_ANNUAL.get(b, _DEFAULT_VOL_FALLBACK) for b in buckets],
                index=buckets,
                dtype=float,
            )
            corr = pd.DataFrame(np.eye(n), index=buckets, columns=buckets)
        cov = pd.DataFrame(
            np.outer(vol.to_numpy(), vol.to_numpy()) * corr.to_numpy(),
            index=buckets,
            columns=buckets,
        )

        # Box bounds.
        lower = pd.Series(
            [constraints.min_weights.get(b, 0.0) for b in buckets], index=buckets, dtype=float
        )
        upper = pd.Series(
            [constraints.max_weights.get(b, 1.0) for b in buckets], index=buckets, dtype=float
        )

        return _OptimizationInputs(
            buckets=buckets,
            expected_returns=er,
            cov=cov,
            lower=lower,
            upper=upper,
        )

    def _solve(self, inputs: _OptimizationInputs) -> pd.Series:
        # Lazy import so the package can be imported without riskfolio-lib
        # installed (the wider package supports a stub-only build).
        import riskfolio as rp  # type: ignore

        port = rp.Portfolio(returns=self._synthetic_returns(inputs))
        port.assets_stats(method_mu="hist", method_cov="hist")
        # Override stats with our analytic CMA — riskfolio expects mu as a
        # row-DataFrame and cov as a DataFrame indexed by asset names.
        port.mu = pd.DataFrame(
            inputs.expected_returns.to_numpy().reshape(1, -1),
            columns=inputs.buckets,
        )
        port.cov = inputs.cov

        # Box bounds. riskfolio reads these from port.lowerlng / port.upperlng
        # for long-only problems.
        port.lowerlng = inputs.lower.to_numpy().reshape(-1, 1)
        port.upperlng = inputs.upper.to_numpy().reshape(-1, 1)

        w = port.optimization(
            model="Classic",
            rm=self._risk_measure,
            obj=self._objective,
            rf=0.0,
            l=0.0,
            hist=False,
        )
        if w is None or w.empty:
            raise RuntimeError("riskfolio optimization returned no solution")

        # riskfolio returns a DataFrame with column 'weights' indexed by asset.
        col = w.columns[0]
        weights = w[col].reindex(inputs.buckets).astype(float)
        # Final sum-to-1 normalization to absorb floating-point drift.
        total = weights.sum()
        if abs(total - 1.0) > 1e-9 and total > 0:
            weights = weights / total
        weights.name = "weight"

        self._diagnostics["weights_sum_pre_normalize"] = float(total)
        return weights

    def _synthetic_returns(self, inputs: _OptimizationInputs) -> pd.DataFrame:
        """Riskfolio's Portfolio constructor wants a returns frame for shape /
        index inference even when we override mu/cov afterwards. We produce a
        deterministic 2-row dummy keyed off the bucket list; the actual
        statistics are replaced before optimization.
        """
        return pd.DataFrame(
            np.zeros((2, len(inputs.buckets))),
            columns=inputs.buckets,
            index=pd.RangeIndex(start=0, stop=2),
        )
