"""ImplementationAdapter ABC + cost / result containers. SPEC §9."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class CostModel:
    """Trading cost model. Phase 1 stub uses zero cost."""

    bps_per_trade: float = 0.0


@dataclass(frozen=True)
class RebalanceResult:
    """Per-bucket signed dollar trades plus aggregate cost.

    ``trades`` indexed by bucket; positive = buy, negative = sell. By
    construction ``trades.sum() == 0`` for zero-cost rebalances.
    """

    trades: pd.Series
    cost_usd: float = 0.0


class ImplementationAdapter(ABC):
    @abstractmethod
    def rebalance(
        self,
        current: pd.Series,
        target: pd.Series,
        costs: CostModel,
    ) -> RebalanceResult:
        """Compute trades to move from ``current`` to ``target`` dollar allocations."""
