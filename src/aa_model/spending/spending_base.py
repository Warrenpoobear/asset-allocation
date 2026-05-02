"""Phase 12 / L19 — spending-base computation.

Pure helper that converts an end-of-quarter NAV-by-bucket series and
static CMA tags (liquidity tier, income_producing flag) into the dollar
denominator Owl uses for its withdrawal-rate trigger.

The helper is consumed only by ``OwlRule`` (Owl is the only spending
rule with a rate concept). flat_real and smoothing have no rate concept
and never call into this module.

Phase 4a state-flow contract preservation
=========================================

For NAV-side modes, this module reads no ledger state. The caller
(``OwlRule``) passes in ``ledger.end_nav_through(prior_q)`` — the same
closed-prior-quarter view Owl already consumes. CMA tags are static
config, not ledger state, so the closed-prior-quarter contract is
unaffected.

For the Phase 12.5 ``distributable_income`` mode, the helper reads
``ledger.closed_through(prior_q)`` (which only exposes rows where
``quarter <= prior_q``). The closed-prior-quarter contract is
preserved; no current-quarter or future-quarter rows are consulted.

Phase 12 ships four NAV-side modes; Phase 12.5 adds the flow-side mode
=====================================================================

* ``"total_nav"`` (default) — sum of every bucket's NAV. Backward-
  compatible with Phase 11.
* ``"liquid_nav"`` — sum of buckets tagged ``liquidity == "liquid"``.
* ``"liquid_plus_income_producing_nav"`` — buckets tagged ``"liquid"``
  OR ``income_producing == True``. **Includes the NAV of income-
  producing buckets; does NOT measure actual distributable income.**
  Stabilized real estate tagged ``income_producing=True`` contributes
  its appraised NAV — overstating spending capacity vs. true
  distributable yield. The structurally correct fix is Phase 12.5.
* ``"custom_policy"`` — per-bucket inclusion-weight blend. Weights are
  bucket-keyed; unspecified buckets default to weight 0; weights are
  inclusion fractions, not allocation weights, so they do not sum to 1.
* ``"distributable_income"`` (Phase 12.5) — trailing-window sum of
  ``distribution_inflow`` ledger rows. Bootstrap fallback for
  q0 / insufficient history. **Phase 12.5 does NOT determine legal /
  tax / entity-governance distributability** — it consumes rows
  already classified upstream by a Phase 13/14 producer as
  family-office-distributable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    from aa_model.integration.ledger import QuarterlyLedger


@dataclass(frozen=True)
class SpendingBaseBreakdown:
    """Pure data carrier surfaced into ``OwlRule.diagnostics()``.

    Phase 12 fields:
      base_usd, excluded_by_tier_usd, excluded_by_income_flag_usd

    Phase 12.5 additions (default neutral so all Phase 12 callers
    remain byte-identical):
      distributable_income_by_source_usd, is_bootstrap
    """

    base_usd: float
    excluded_by_tier_usd: dict[str, float]
    excluded_by_income_flag_usd: dict[bool, float]
    # Phase 12.5 / L19 flow-side additions:
    distributable_income_by_source_usd: dict[str, float] = field(default_factory=dict)
    is_bootstrap: bool = False


def _rollup_by_tier(
    excluded_per_bucket: pd.Series, cma_liquidity: pd.Series | None
) -> dict[str, float]:
    """Sum excluded dollars by liquidity tier. Drops zero-tier groups."""
    if cma_liquidity is None or cma_liquidity.empty:
        return {}
    aligned = cma_liquidity.reindex(excluded_per_bucket.index)
    grouped = excluded_per_bucket.groupby(aligned, dropna=True).sum()
    return {str(k): float(v) for k, v in grouped.items() if float(v) != 0.0}


def _rollup_by_income_flag(
    excluded_per_bucket: pd.Series, cma_income_producing: pd.Series | None
) -> dict[bool, float]:
    """Sum excluded dollars by income_producing flag. Drops zero-flag groups."""
    if cma_income_producing is None or cma_income_producing.empty:
        return {}
    aligned = cma_income_producing.reindex(excluded_per_bucket.index).fillna(False)
    aligned = aligned.astype(bool)
    grouped = excluded_per_bucket.groupby(aligned).sum()
    return {bool(k): float(v) for k, v in grouped.items() if float(v) != 0.0}


def compute_distributable_income_base(
    ledger: QuarterlyLedger,
    prior_quarter: pd.Period,
    *,
    window_quarters: int,
    bootstrap_usd: float,
) -> tuple[float, dict[str, float], bool]:
    """Phase 12.5 / L19 flow-side — trailing distributable-income base.

    Sums ``distribution_inflow`` rows where
    ``prior_quarter - window_quarters < quarter <= prior_quarter``.
    The trailing sum is a literal dollar figure for the trailing N
    quarters; for ``window_quarters=4`` (the recommended default)
    this IS the trailing-12-month figure directly comparable to
    ``annual_spend_usd``.

    Returns ``(base_usd, by_source_usd, is_bootstrap)``.

    Bootstrap rule:
      - The realized window covers quarters in
        ``[prior_quarter - window_quarters + 1, prior_quarter]``.
      - The "first realized quarter" of the run is
        ``ledger.start_quarter`` (the orchestrator's q0). The realized
        window is "complete" when its earliest quarter is ≥ q0.
      - When the realized window is complete, return the trailing sum
        with ``is_bootstrap=False``.
      - When the realized window extends earlier than q0 (run age too
        short), return ``bootstrap_usd`` with ``is_bootstrap=True``.

    Phase 4a state-flow contract: reads only
    ``ledger.closed_through(prior_quarter)``. No current/future-quarter
    reads. No mutation. No state.

    Args:
        ledger: The running ``QuarterlyLedger`` Owl already consumes.
        prior_quarter: ``quarter - 1`` from the OwlRule call site.
        window_quarters: trailing-window length; from
            ``GuardrailConfig.distribution_window_quarters``.
        bootstrap_usd: static fallback; from
            ``GuardrailConfig.bootstrap_distributable_income_usd``.

    Returns:
        (base_usd, by_source_usd, is_bootstrap):
            base_usd: trailing realized sum OR bootstrap value
            by_source_usd: per-source rollup (empty when bootstrap)
            is_bootstrap: True iff bootstrap value was used
    """
    earliest_realized = prior_quarter - (window_quarters - 1)
    # Bootstrap when the realized window extends before the run's
    # actual start quarter — i.e., we don't have enough closed history
    # to populate the full window.
    if earliest_realized < ledger._start_quarter:
        return float(bootstrap_usd), {}, True

    view = ledger.closed_through(prior_quarter)
    if view.empty:
        return float(bootstrap_usd), {}, True

    di = view[
        (view["flow_type"] == "distribution_inflow")
        & (view["quarter"] >= earliest_realized)
        & (view["quarter"] <= prior_quarter)
    ]
    base = float(di["amount_usd"].sum())
    by_source: dict[str, float] = {}
    if not di.empty:
        grouped = di.groupby("source", sort=True)["amount_usd"].sum()
        by_source = {str(k): float(v) for k, v in grouped.items()}
    return base, by_source, False


def compute_spending_base(
    nav_by_bucket: pd.Series,
    cma_liquidity: pd.Series | None,
    cma_income_producing: pd.Series | None,
    spending_base: str | None,
    spending_base_weights: dict[str, float] | None,
    *,
    # Phase 12.5 / L19 flow-side additions — only consumed for the
    # distributable_income branch. All other branches ignore them.
    ledger: QuarterlyLedger | None = None,
    prior_quarter: pd.Period | None = None,
    distribution_window_quarters: int | None = None,
    bootstrap_distributable_income_usd: float | None = None,
) -> SpendingBaseBreakdown:
    """Pure function. No ledger mutation. No state. No CMA mutation.

    For NAV-side modes (``total_nav`` / ``liquid_nav`` /
    ``liquid_plus_income_producing_nav`` / ``custom_policy``) the
    function reads only ``nav_by_bucket`` + CMA tags.

    For the Phase 12.5 ``distributable_income`` mode the function
    additionally reads ``ledger.closed_through(prior_quarter)``.

    Args:
        nav_by_bucket: index = bucket, value = USD. Caller passes in
            either ``ledger.end_nav_through(prior_q)`` (current-rate
            denominator) or the initial-NAV series (initial-rate
            denominator). Ignored for ``distributable_income`` mode.
        cma_liquidity: index = bucket, value = liquidity tier. Required
            for any non-``"total_nav"`` NAV-side mode.
        cma_income_producing: index = bucket, value = bool. Required
            for ``"liquid_plus_income_producing_nav"``.
        spending_base: GuardrailConfig.spending_base. ``None`` is
            treated as ``"total_nav"``.
        spending_base_weights: bucket-keyed inclusion fractions. Only
            consumed when ``spending_base == "custom_policy"``.
        ledger / prior_quarter: required for
            ``"distributable_income"``; ignored otherwise.
        distribution_window_quarters / bootstrap_distributable_income_usd:
            required for ``"distributable_income"``; ignored otherwise.

    Returns:
        SpendingBaseBreakdown carrying the dollar base plus the
        diagnostic rollups.

    Raises:
        ValueError: on missing required inputs for a non-default mode.
    """
    if spending_base is None or spending_base == "total_nav":
        return SpendingBaseBreakdown(
            base_usd=float(nav_by_bucket.sum()),
            excluded_by_tier_usd={},
            excluded_by_income_flag_usd={},
        )

    if spending_base == "distributable_income":
        if ledger is None or prior_quarter is None:
            raise ValueError(
                "spending_base='distributable_income' requires ledger "
                "and prior_quarter at the OwlRule call site"
            )
        if (
            distribution_window_quarters is None
            or bootstrap_distributable_income_usd is None
        ):
            raise ValueError(
                "spending_base='distributable_income' requires "
                "distribution_window_quarters and "
                "bootstrap_distributable_income_usd on GuardrailConfig"
            )
        base, by_source, is_bootstrap = compute_distributable_income_base(
            ledger,
            prior_quarter,
            window_quarters=distribution_window_quarters,
            bootstrap_usd=bootstrap_distributable_income_usd,
        )
        return SpendingBaseBreakdown(
            base_usd=base,
            excluded_by_tier_usd={},
            excluded_by_income_flag_usd={},
            distributable_income_by_source_usd=by_source,
            is_bootstrap=is_bootstrap,
        )

    if cma_liquidity is None or cma_liquidity.empty:
        raise ValueError(
            f"spending_base={spending_base!r} requires cma.liquidity to be "
            f"populated; got empty/None"
        )

    if spending_base == "liquid_nav":
        weights_per_bucket = (cma_liquidity == "liquid").astype(float)
    elif spending_base == "liquid_plus_income_producing_nav":
        if cma_income_producing is None or cma_income_producing.empty:
            raise ValueError(
                "spending_base='liquid_plus_income_producing_nav' requires "
                "cma.income_producing to be populated"
            )
        liquid_mask = (cma_liquidity == "liquid").astype(bool)
        income_mask = cma_income_producing.reindex(nav_by_bucket.index).fillna(False)
        income_mask = income_mask.astype(bool)
        liquid_aligned = liquid_mask.reindex(nav_by_bucket.index).fillna(False).astype(bool)
        weights_per_bucket = (liquid_aligned | income_mask).astype(float)
    elif spending_base == "custom_policy":
        if spending_base_weights is None:
            raise ValueError("spending_base='custom_policy' requires weights")
        weights_per_bucket = pd.Series(
            {b: float(spending_base_weights.get(b, 0.0)) for b in nav_by_bucket.index},
            dtype=float,
        )
    else:
        raise ValueError(f"unknown spending_base {spending_base!r}")

    weights_per_bucket = weights_per_bucket.reindex(nav_by_bucket.index).fillna(0.0)
    included_per_bucket = nav_by_bucket * weights_per_bucket
    excluded_per_bucket = nav_by_bucket * (1.0 - weights_per_bucket)
    return SpendingBaseBreakdown(
        base_usd=float(included_per_bucket.sum()),
        excluded_by_tier_usd=_rollup_by_tier(excluded_per_bucket, cma_liquidity),
        excluded_by_income_flag_usd=_rollup_by_income_flag(
            excluded_per_bucket, cma_income_producing
        ),
    )
