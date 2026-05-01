"""Zero-cost rebalancer: trades exactly the gap, ignores ``costs``.

Reference implementation of ``ImplementationAdapter``. Phase 3 will replace
this with a cost-aware adapter (cvxportfolio).
"""

from __future__ import annotations

import pandas as pd

from aa_model.implementation.base import (
    CostModel,
    ImplementationAdapter,
    RebalanceResult,
)


class StubImplementation(ImplementationAdapter):
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
        return RebalanceResult(trades=trades, cost_usd=0.0)
