"""Spending rules — flat-real and smoothing.

Per Phase 4a, both rules implement the per-quarter
:meth:`SpendingRule.quarterly_outflow_at` API. ``FlatRealRule`` derives
each quarter's spend from config alone and ignores the ledger;
``SmoothingRule`` recovers ``spend_{t-1}`` from its own prior ``spend``
row in the closed ledger view, threading the EWMA recursion through
the ledger rather than across cached state.
"""

from __future__ import annotations

import pandas as pd

from aa_model.integration.ledger import QuarterlyLedger
from aa_model.spending.base import SpendingParams, SpendingRule


def _quarter_offset(quarter: pd.Period, start: pd.Period) -> int:
    return int((quarter - start).n)


def _read_own_prior_spend(
    ledger: QuarterlyLedger, source_id: str, prior_quarter: pd.Period
) -> float:
    """Phase 4 design / prior-spend-row source filter: a path-dependent rule
    may only read prior ``spend`` rows where ``source == its own rule source``.
    Returns the absolute (positive) dollar amount of the most recent matching
    row at ``prior_quarter``; raises if none found.
    """
    view = ledger.closed_through(prior_quarter)
    if view.empty:
        raise RuntimeError(
            f"prior spend row not found for source={source_id!r} at "
            f"quarter={prior_quarter}; ledger view is empty"
        )
    own = view[
        (view["flow_type"] == "spend")
        & (view["source"] == source_id)
        & (view["quarter"] == prior_quarter)
    ]
    if own.empty:
        raise RuntimeError(
            f"prior spend row not found for source={source_id!r} at " f"quarter={prior_quarter}"
        )
    return float(-own["amount_usd"].iloc[-1])


class FlatRealRule(SpendingRule):
    """Constant real-dollar spend; nominal grows by ``inflation_pct`` per year.

    Path-independent: each quarter's value is a deterministic function of
    config alone. The per-quarter method ignores ``ledger``.
    """

    SOURCE_ID = "spending:flat_real"

    def quarterly_outflow_at(
        self,
        ledger: QuarterlyLedger,
        params: SpendingParams,
        quarter: pd.Period,
    ) -> float:
        cfg = params.config
        offset = _quarter_offset(quarter, params.start_quarter)
        year_index = offset // 4  # 0 for q0..q3, 1 for q4..q7, ...
        inflated_annual = cfg.annual_spend_usd * (1.0 + cfg.inflation_pct) ** year_index
        quarterly = inflated_annual / 4.0
        return max(cfg.floor_usd, min(cfg.ceiling_usd, quarterly))


class SmoothingRule(SpendingRule):
    """Exponentially-weighted spending smoother.

    Per-quarter recursion ``spend_t = w · target_t + (1 - w) · spend_{t-1}``;
    ``spend_{t-1}`` is recovered from this rule's own prior ``spend`` row in
    the closed ledger view. q0 returns ``annual_spend_usd / 4`` with no
    smoothing (Phase 4 design / q0 initialization).
    """

    SOURCE_ID = "spending:smoothing"

    def quarterly_outflow_at(
        self,
        ledger: QuarterlyLedger,
        params: SpendingParams,
        quarter: pd.Period,
    ) -> float:
        cfg = params.config
        if quarter == params.start_quarter:
            quarterly = cfg.annual_spend_usd / 4.0
            return max(cfg.floor_usd, min(cfg.ceiling_usd, quarterly))

        offset = _quarter_offset(quarter, params.start_quarter)
        year_index = offset // 4
        inflated_annual = cfg.annual_spend_usd * (1.0 + cfg.inflation_pct) ** year_index
        target = inflated_annual / 4.0

        prior_q = quarter - 1
        prior_spend = _read_own_prior_spend(ledger, self.SOURCE_ID, prior_q)

        w = cfg.smoothing.weight
        cur = w * target + (1.0 - w) * prior_spend
        return max(cfg.floor_usd, min(cfg.ceiling_usd, cur))


def make_rule(name: str) -> SpendingRule:
    if name == "flat_real":
        return FlatRealRule()
    if name == "smoothing":
        return SmoothingRule()
    if name == "owl":
        # Local import to avoid a circular dependency: owl_adapter imports
        # SpendingRule from .base; this module also imports from .base.
        from aa_model.spending.owl_adapter import OwlRule

        return OwlRule()
    raise ValueError(f"unknown spending rule {name!r}")
