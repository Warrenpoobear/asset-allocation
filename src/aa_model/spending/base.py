"""SpendingRule ABC + parameter container. SPEC §9."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import pandas as pd

from aa_model.integration.ledger import QuarterlyLedger
from aa_model.io.schemas import SpendingConfig


@dataclass(frozen=True)
class SpendingParams:
    """Inputs the rule needs beyond the ledger itself."""

    config: SpendingConfig
    start_quarter: pd.Period
    num_quarters: int


class SpendingRule(ABC):
    @abstractmethod
    def quarterly_outflows(
        self, ledger: QuarterlyLedger, params: SpendingParams
    ) -> pd.Series:
        """Return a Series indexed by quarter Period, values = spending USD ≥ 0."""
