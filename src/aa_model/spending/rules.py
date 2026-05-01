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

    def quarterly_outflows(self, ledger: QuarterlyLedger, params: SpendingParams) -> pd.Series:
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
    """Exponentially-weighted spending smoother.

    The "target" path is the same inflated quarterly series :class:`FlatRealRule`
    would emit. The smoothed series is::

        spend_0 = target_0
        spend_t = w * target_t + (1 - w) * spend_{t-1}    (t > 0)

    where ``w = config.smoothing.weight`` and is in ``[0, 1]``. ``w = 1``
    tracks ``target`` exactly (equivalent to flat-real). ``w = 0`` freezes
    spending at the initial target and never re-anchors. Floor / ceiling
    clip each smoothed value.
    """

    def quarterly_outflows(self, ledger: QuarterlyLedger, params: SpendingParams) -> pd.Series:
        cfg = params.config
        w = cfg.smoothing.weight
        idx = [params.start_quarter + i for i in range(params.num_quarters)]

        targets: list[float] = []
        for i in range(params.num_quarters):
            year_index = i // 4
            inflated_annual = cfg.annual_spend_usd * (1.0 + cfg.inflation_pct) ** year_index
            targets.append(inflated_annual / 4.0)

        out: list[float] = []
        prev = targets[0] if targets else 0.0
        for i, tgt in enumerate(targets):
            cur = tgt if i == 0 else (w * tgt + (1.0 - w) * prev)
            cur = max(cfg.floor_usd, min(cfg.ceiling_usd, cur))
            out.append(cur)
            prev = cur
        return pd.Series(out, index=idx, dtype=float, name="quarterly_outflow_usd")


def make_rule(name: str) -> SpendingRule:
    if name == "flat_real":
        return FlatRealRule()
    if name == "smoothing":
        return SmoothingRule()
    raise ValueError(f"unknown spending rule {name!r}")
