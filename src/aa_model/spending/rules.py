"""Spending rules — flat-real and smoothing.

Per SPEC §6 Phase 1, only ``flat_real`` and ``smoothing`` rules are
implemented. Both return a non-negative quarterly outflow series; the
orchestrator translates each value into a ``spend`` ledger row with the sign
flipped (cash bucket loses the dollars).
"""

from __future__ import annotations

import pandas as pd

from aa_model.integration.ledger import QuarterlyLedger
from aa_model.spending.base import SpendingParams, SpendingRule


class FlatRealRule(SpendingRule):
    """Constant real-dollar spend; nominal grows by ``inflation_pct`` per year.

    Within a calendar year (every four quarters from start) the nominal
    spending is constant. At each year boundary it scales by ``(1+inflation)``.
    Floor and ceiling clip the per-quarter value.
    """

    def quarterly_outflows(
        self, ledger: QuarterlyLedger, params: SpendingParams
    ) -> pd.Series:
        cfg = params.config
        idx = [params.start_quarter + i for i in range(params.num_quarters)]
        values: list[float] = []
        for i in range(params.num_quarters):
            year_index = i // 4
            inflated_annual = cfg.annual_spend_usd * (1.0 + cfg.inflation_pct) ** year_index
            quarterly = inflated_annual / 4.0
            quarterly = max(cfg.floor_usd, min(cfg.ceiling_usd, quarterly))
            values.append(quarterly)
        return pd.Series(values, index=idx, dtype=float, name="quarterly_outflow_usd")


class SmoothingRule(SpendingRule):
    """Smoothed spending policy.

    Phase 1 supports only ``smoothing.weight == 0``, in which case spending
    collapses exactly to :class:`FlatRealRule`. Non-zero weights require
    rolling-NAV smoothing, which depends on a NAV trajectory the orchestrator
    only knows quarter-by-quarter — that wiring is deferred to a later phase.
    """

    def quarterly_outflows(
        self, ledger: QuarterlyLedger, params: SpendingParams
    ) -> pd.Series:
        if params.config.smoothing.weight != 0.0:
            raise NotImplementedError(
                "smoothing.weight > 0 requires NAV-trajectory smoothing — "
                "deferred past Phase 1"
            )
        return FlatRealRule().quarterly_outflows(ledger, params)


def make_rule(name: str) -> SpendingRule:
    if name == "flat_real":
        return FlatRealRule()
    if name == "smoothing":
        return SmoothingRule()
    raise ValueError(f"unknown spending rule {name!r}")
