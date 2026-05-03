"""Phase 8 (L8) — illiquidity overlay between policy target and rebalance.

Pure-function transformation of allocation weights that locks every
bucket tagged ``illiquid`` at its current dollars and renormalises the
liquid-set policy weights over the residual liquid NAV. PE exposure
under the default overlay can change only through ``pe_call`` /
``pe_distribution`` / ``pe_nav_mark`` flows — never through generic
rebalance trades.

The overlay is **generic over CMA liquidity tags**, not PE-specific.
Any bucket tagged ``illiquid`` is locked; ``liquid`` and
``semi_liquid`` are both treated as part of the rebalanceable set.

See MODEL_DOCUMENTATION.md §Phase 8 design for the load-bearing rules,
edge cases, and invariant.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

_TINY_DOLLAR: float = 1.0
"""Threshold for the ``clipped_to_zero_liquid_count`` diagnostic. A
liquid bucket whose post-overlay execution dollar amount rounds to
``≤ $1`` is considered "clipped to zero" — analog to the STAIRS
``clipped_quarters`` and the cost-aware allocator's advisory
diagnostics. The value is small enough that genuine partial
allocations don't trip it."""


@dataclass(frozen=True)
class LiquidityOverlayDiagnostics:
    """Per-quarter diagnostics produced by :func:`apply_liquidity_overlay`.

    Aggregated across quarters by the orchestrator and surfaced in
    ``report.md`` under a new ``## Illiquidity overlay`` section.

    * ``illiquid_buckets``: the bucket names locked this quarter.
    * ``policy_weight_per_illiquid``: bucket → policy weight (input).
    * ``current_weight_per_illiquid``: bucket → current_dollars / V.
    * ``drift_per_illiquid``: bucket → current − policy.
    * ``max_abs_illiquid_drift``: max |drift| across illiquid buckets.
    * ``sum_abs_illiquid_drift``: sum |drift| across illiquid buckets.
    * ``clipped_to_zero_liquid_count``: liquid buckets where the
      post-overlay execution dollar amount rounded to ``≤ $1``.
    """

    illiquid_buckets: tuple[str, ...]
    policy_weight_per_illiquid: dict[str, float]
    current_weight_per_illiquid: dict[str, float]
    drift_per_illiquid: dict[str, float]
    max_abs_illiquid_drift: float
    sum_abs_illiquid_drift: float
    clipped_to_zero_liquid_count: int


def apply_liquidity_overlay(
    *,
    policy_weights: pd.Series,
    current_dollars: pd.Series,
    liquidity: pd.Series,
) -> tuple[pd.Series, LiquidityOverlayDiagnostics]:
    """Return execution weights + diagnostics.

    Args:
      policy_weights: bucket → strategic policy weight. Must sum to
        approximately 1.0 (caller's responsibility; not enforced
        here).
      current_dollars: bucket → pre-rebalance dollar holdings. The
        overlay locks illiquid buckets at these values.
      liquidity: bucket → "liquid" / "semi_liquid" / "illiquid".
        Source of truth for which buckets are tradable.

    Returns:
      Tuple of (execution_weights, diagnostics). ``execution_weights``
      has the same index as the union of inputs, sums to 1.0 within
      ``1e-12``, and assigns ``current_dollars[i] / V`` to every
      illiquid bucket ``i``.

    Raises:
      ValueError if ``liquid_nav < 0`` (illiquid current dollars
        exceed total NAV — pathological leveraged-via-PE state),
        with a per-bucket breakdown.
      ValueError if ``liquid_nav == 0`` and any liquid bucket has
        non-zero current dollars (would imply selling those liquid
        positions to zero, which is almost certainly wrong).
    """
    idx = policy_weights.index.union(current_dollars.index).union(liquidity.index)
    idx = idx.sort_values()
    pol = policy_weights.reindex(idx).fillna(0.0).astype(float)
    cur = current_dollars.reindex(idx).fillna(0.0).astype(float)
    liq = liquidity.reindex(idx).astype(object)

    illiquid_mask = liq == "illiquid"
    liquid_mask = liq.isin(("liquid", "semi_liquid"))

    V_total = float(cur.sum())
    illiquid_current = float(cur[illiquid_mask].sum())
    liquid_nav = V_total - illiquid_current

    # liquid_nav < 0 is a pathological leveraged-via-PE state; fail
    # loudly with a per-bucket breakdown so the user can see which
    # illiquid bucket pushed total above NAV.
    if liquid_nav < -1e-6:
        breakdown = {b: float(cur[b]) for b in idx if illiquid_mask[b]}
        raise ValueError(
            f"apply_liquidity_overlay: liquid_nav = {liquid_nav:,.2f} < 0; "
            f"V_total = {V_total:,.2f}, illiquid_current = "
            f"{illiquid_current:,.2f}. Per-illiquid-bucket: {breakdown}. "
            "This indicates a leveraged-via-PE state that the model does "
            "not auto-resolve — fix the upstream commitment / call schedule."
        )

    # liquid_nav == 0 only valid as genuine no-op (every liquid
    # bucket already at zero). Otherwise we'd be implicitly asking
    # the rebalancer to sell every liquid position to zero — almost
    # certainly wrong.
    if abs(liquid_nav) <= 1e-6:
        liquid_current_nonzero = [
            b for b in idx if liquid_mask[b] and abs(float(cur[b])) > _TINY_DOLLAR
        ]
        if liquid_current_nonzero:
            raise ValueError(
                f"apply_liquidity_overlay: liquid_nav ≈ 0 but liquid buckets "
                f"have non-zero current dollars: "
                f"{[(b, float(cur[b])) for b in liquid_current_nonzero]}. "
                "This would imply selling those positions to zero — "
                "indicates an upstream pacing/coverage error."
            )
        # Genuine no-op: execution_weights = current_weights (which
        # are zero on the liquid side and possibly non-zero on the
        # illiquid side). Skip the renormalisation branch.
        if V_total <= 0.0:
            execution_weights = pd.Series(0.0, index=idx, dtype=float)
        else:
            execution_weights = (cur / V_total).astype(float)
        diag = _build_diagnostics(
            idx=idx,
            illiquid_mask=illiquid_mask,
            policy=pol,
            current=cur,
            V_total=V_total,
            execution_dollars=cur.copy(),
            liquid_mask=liquid_mask,
        )
        return execution_weights, diag

    # Standard branch: liquid_nav > 0. Renormalise policy weights
    # over the liquid set and distribute liquid_nav across them.
    liquid_policy_weight_sum = float(pol[liquid_mask].sum())
    if liquid_policy_weight_sum <= 0.0:
        # Cross-config validation should have caught this; defence-
        # in-depth in case the overlay is invoked outside the
        # orchestrator (e.g. unit tests with a custom policy).
        raise ValueError(
            "apply_liquidity_overlay: aggregate policy weight across the "
            f"liquid set is {liquid_policy_weight_sum} (must be > 0); "
            "renormalisation w_j / Σ w_L is undefined"
        )

    execution_dollars = cur.copy()
    # Illiquid buckets: lock at current.
    # (cur already equals the locked value; assignment is a no-op
    # but kept for symmetry and to make the contract explicit.)
    for b in idx:
        if illiquid_mask[b]:
            execution_dollars[b] = float(cur[b])
        elif liquid_mask[b]:
            renorm_weight = float(pol[b]) / liquid_policy_weight_sum
            execution_dollars[b] = liquid_nav * renorm_weight
        else:
            # Buckets not tagged at all: treat as liquid for safety,
            # matching the "liquid" branch above. Cross-config
            # validator already requires liquidity to cover every
            # allocation bucket, so this branch is unreachable from
            # the orchestrator path. Keeping it defensive for
            # off-orchestrator callers.
            renorm_weight = float(pol[b]) / liquid_policy_weight_sum
            execution_dollars[b] = liquid_nav * renorm_weight

    if V_total > 0.0:
        execution_weights = (execution_dollars / V_total).astype(float)
    else:
        execution_weights = pd.Series(0.0, index=idx, dtype=float)

    diag = _build_diagnostics(
        idx=idx,
        illiquid_mask=illiquid_mask,
        policy=pol,
        current=cur,
        V_total=V_total,
        execution_dollars=execution_dollars,
        liquid_mask=liquid_mask,
    )
    return execution_weights, diag


def _build_diagnostics(
    *,
    idx: pd.Index,
    illiquid_mask: pd.Series,
    policy: pd.Series,
    current: pd.Series,
    V_total: float,
    execution_dollars: pd.Series,
    liquid_mask: pd.Series,
) -> LiquidityOverlayDiagnostics:
    """Build the per-quarter diagnostic record."""
    illiquid_buckets = tuple(b for b in idx if illiquid_mask[b])
    if V_total > 0.0:
        cur_w = (current / V_total).astype(float)
    else:
        cur_w = pd.Series(0.0, index=idx, dtype=float)

    pol_per = {b: float(policy[b]) for b in illiquid_buckets}
    cur_per = {b: float(cur_w[b]) for b in illiquid_buckets}
    drift_per = {b: cur_per[b] - pol_per[b] for b in illiquid_buckets}
    max_abs = max((abs(d) for d in drift_per.values()), default=0.0)
    sum_abs = sum(abs(d) for d in drift_per.values())

    clipped = sum(
        1 for b in idx if liquid_mask[b] and abs(float(execution_dollars[b])) <= _TINY_DOLLAR
    )

    return LiquidityOverlayDiagnostics(
        illiquid_buckets=illiquid_buckets,
        policy_weight_per_illiquid=pol_per,
        current_weight_per_illiquid=cur_per,
        drift_per_illiquid=drift_per,
        max_abs_illiquid_drift=max_abs,
        sum_abs_illiquid_drift=sum_abs,
        clipped_to_zero_liquid_count=clipped,
    )
