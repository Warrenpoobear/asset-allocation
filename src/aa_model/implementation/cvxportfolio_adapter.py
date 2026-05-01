"""Cvxportfolio implementation adapter (SPEC §6 Phase 3b).

A reference *non-stub* implementation of :class:`ImplementationAdapter`
backed by ``cvxportfolio``. The adapter computes trades to bring the
portfolio from ``current`` to ``target`` and applies a linear transaction
cost consistent with cvxportfolio's
:class:`cvxportfolio.costs.StocksTransactionCost(a=bps/1e4)` linear term.

Contract / discipline (Phase 3 guardrails)
==========================================

* **Pure function from inputs → outputs.** No module-level state, no
  hidden caches, no ledger access. ``rebalance(current, target, costs)``
  is deterministic in its arguments.
* **Stub-parity contract at zero cost.** With ``bps_per_trade == 0`` the
  adapter MUST produce trades bit-equal to :class:`StubImplementation`.
  This is the numerical anchor case — see
  ``tests/test_cvxportfolio_adapter.py``. Tested both ways (zero-cost
  parity and a fixed-bps closed-form anchor at ε = 1e-9 USD).
* **No path dependence.** Trades depend only on the current and target
  vectors handed in for *this* quarter; no state from prior calls
  influences the result. Determinism of the orchestrator is therefore
  not affected by the choice of implementation engine — see L13.

Cost model
==========

For trade vector ``t = target - current`` and bps coefficient
``a = costs.bps_per_trade / 1e4``::

    cost_usd = a · ∑ |t_i|        (linear, all-bucket)

This matches the linear term of cvxportfolio's
``StocksTransactionCost(a=a)``. Quadratic / market-impact / per-share
terms are intentionally NOT modeled — Phase 3b minimum.

Wheel availability (probed 2026-05-01)
======================================

cvxportfolio 1.5.1 is ``py3-none-any`` (pure Python on top of cvxpy).
Installs in ~2 s on top of an existing cvxpy install. Same opt-in extra
group as riskfolio:
``[project.optional-dependencies] cvxportfolio = ["cvxportfolio==1.5.1"]``.
"""

from __future__ import annotations

import pandas as pd

from aa_model.implementation.base import (
    CostModel,
    ImplementationAdapter,
    RebalanceResult,
)


class CvxportfolioImplementation(ImplementationAdapter):
    """Linear-cost rebalancer.

    Trades exactly the ``target - current`` gap (no optimization), then
    applies a linear cost in basis points consistent with cvxportfolio's
    transaction-cost model. The optimizer is intentionally *not* invoked
    for Phase 3b — see *Cost model* above.
    """

    def __init__(self) -> None:
        # Lazy backend import: keeps cvxportfolio optional. The constructor
        # validates the import early so failures surface at run start, not
        # mid-rebalance.
        import cvxportfolio  # noqa: F401  (import-only conformance check)

        self._cvxportfolio_version = cvxportfolio.__version__
        self._diagnostics: dict = {
            "engine": "cvxportfolio",
            "cvxportfolio_version": self._cvxportfolio_version,
            "cost_model": "linear (cvxportfolio.costs.StocksTransactionCost.a only)",
        }

    def rebalance(
        self,
        current: pd.Series,
        target: pd.Series,
        costs: CostModel,
    ) -> RebalanceResult:
        idx = current.index.union(target.index)
        cur = current.reindex(idx).fillna(0.0).astype(float)
        tgt = target.reindex(idx).fillna(0.0).astype(float)
        trades = (tgt - cur).astype(float)
        trades.name = "trade_usd"

        a = costs.bps_per_trade / 1e4  # bps → fraction
        cost_usd = float(trades.abs().sum() * a)

        return RebalanceResult(trades=trades, cost_usd=cost_usd)

    def diagnostics(self) -> dict:
        return dict(self._diagnostics)
