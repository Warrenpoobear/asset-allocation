"""Cost-aware allocation adapter (Phase 4b).

A cost-aware :class:`AllocationAdapter` that solves a single convex problem
per quarter to produce a target weight vector that may partially defer
rebalances when transaction cost exceeds the marginal benefit of trading
toward policy.

Optimization (single solver call per quarter)
=============================================

For policy weights ``w_policy``, pre-rebalance dollars ``current_dollars``,
total NAV ``V_total = current_dollars.sum()``, cost coefficient
``cost_per_dollar = bps_per_trade / 1e4``, and **normalized** policy-loss
weight ``λ_norm`` (config: ``allocation.policy_loss_lambda_norm``)::

    λ_eff         = λ_norm / V_total²
    trade_dollars = w · V_total - current_dollars

    minimize  λ_eff · ‖ w · V_total - w_policy · V_total ‖²
            + cost_per_dollar · ‖ trade_dollars ‖₁
    subject to
            Σ w = 1
            0 ≤ w ≤ 1
            min_w ≤ w ≤ max_w  (box constraints from fit())

The ``V_total²`` factor in the policy term cancels against the ``λ_eff``
divisor, so the user-facing ``λ_norm`` is stable across portfolio sizes
(at the same fractional deviation, the policy-loss intensity is
identical regardless of NAV). Both terms are in dollars; the
``trade_dollars`` framing makes per-quarter turnover explicit (cost is
proportional to trade size, not to position deviation from policy). See
MODEL_DOCUMENTATION.md §Phase 4b design.

Contract / discipline
=====================

* **Path-blindness.** ``target_at`` reads ONLY ``current_dollars``,
  ``self.weights()``, ``cost_model``, and the configured ``λ``. It does
  **not** read ``ledger``. Two runs that arrive at the same
  ``current_dollars`` from different histories produce the same target.
* **Single solver call per quarter.** No fixed-point, no inner loop, no
  multi-period optimization.
* **q0** returns ``self.weights()`` (no current-state context).
* **Determinism via canonicalization.** Solver outputs are clipped to
  ``≥ 0``, rounded to 12 decimals, and renormalized to ``sum == 1``
  exactly by correction on the largest-weight bucket. The ledger sees
  only the canonicalized values, regardless of solver bit-noise across
  versions or platforms.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from aa_model.allocation.base import AllocationAdapter, AllocationParams
from aa_model.allocation.constraints import Constraints
from aa_model.assumptions.cma import CMA
from aa_model.io.schemas import PublicAllocationConfig

if TYPE_CHECKING:
    from aa_model.implementation.base import CostModel
    from aa_model.integration.ledger import QuarterlyLedger


_ROUND_DECIMALS = 12


class CvxportfolioAllocator(AllocationAdapter):
    """Cost-aware allocator. ``weights()`` returns the cost-blind policy
    reference (config-verbatim, mirroring stub semantics);
    ``target_at(...)`` solves the cost-aware optimization.
    """

    def __init__(self, config: PublicAllocationConfig) -> None:
        # Lazy backend import: keeps cvxpy optional. Surface failures at
        # construction time, not mid-run.
        import cvxpy

        self._cvxpy_version = cvxpy.__version__
        self._policy_weights = pd.Series(config.stub_weights, dtype=float).sort_index()
        self._policy_loss_lambda_norm = float(config.policy_loss_lambda_norm)
        self._constraints: Constraints = Constraints()
        # Phase 4b post-calibration: per-quarter advisory record of the
        # configured ``λ_norm`` vs the rule-of-thumb suggested value
        # (``bps × V_total × 1e-3``). Diagnostic only — does NOT influence
        # the optimization. See MODEL_DOCUMENTATION.md §Phase 4b
        # calibration note.
        self._calibration_history: list[dict] = []
        self._diagnostics: dict = {
            "engine": "cvxportfolio",
            "cvxpy_version": self._cvxpy_version,
            "policy_loss_lambda_norm": self._policy_loss_lambda_norm,
            "suggested_lambda_norm_formula": "bps_per_trade * V_total * 1e-3",
            "solver": "CLARABEL",
            "round_decimals": _ROUND_DECIMALS,
        }

    def fit(self, returns: pd.DataFrame, cma: CMA, constraints: Constraints) -> None:
        # No fit-time optimization; policy is config-given. We do retain
        # constraints so target_at can apply box bounds.
        self._constraints = constraints if constraints is not None else Constraints()
        self._diagnostics["fit_inputs"] = {
            "returns_shape": tuple(returns.shape) if returns is not None else None,
            "n_constraints": (
                len(self._constraints.min_weights) + len(self._constraints.max_weights)
            ),
        }

    def weights(self) -> pd.Series:
        return self._policy_weights.copy()

    def diagnostics(self) -> dict:
        out = dict(self._diagnostics)
        out["calibration_history"] = list(self._calibration_history)
        out["calibration_summary"] = self._calibration_summary()
        return out

    def _calibration_summary(self) -> dict:
        """Summary statistics over the per-quarter calibration history.

        Empty / single-bucket NaN-safe. The summary is the diagnostic
        renderers consume by default; the full per-quarter history stays
        available for deeper inspection.
        """
        if not self._calibration_history:
            return {
                "n_quarters": 0,
                "v_total_usd_median": None,
                "suggested_policy_loss_lambda_norm_median": None,
                "policy_loss_lambda_norm_used": self._policy_loss_lambda_norm,
                "ratio_used_over_suggested_median": None,
            }
        v_arr = np.array(
            [r["v_total_usd"] for r in self._calibration_history], dtype=float
        )
        sug_arr = np.array(
            [r["suggested_policy_loss_lambda_norm"] for r in self._calibration_history],
            dtype=float,
        )
        ratio_vals = [
            r["ratio_used_over_suggested"]
            for r in self._calibration_history
            if r["ratio_used_over_suggested"] is not None
        ]
        return {
            "n_quarters": len(self._calibration_history),
            "v_total_usd_median": float(np.median(v_arr)),
            "suggested_policy_loss_lambda_norm_median": float(np.median(sug_arr)),
            "policy_loss_lambda_norm_used": self._policy_loss_lambda_norm,
            "ratio_used_over_suggested_median": (
                float(np.median(np.array(ratio_vals, dtype=float)))
                if ratio_vals
                else None
            ),
        }

    def target_at(
        self,
        ledger: QuarterlyLedger,
        params: AllocationParams,
        quarter: pd.Period,
        current_dollars: pd.Series,
        cost_model: CostModel,
    ) -> pd.Series:
        # q0: no current-state context — return policy unchanged.
        if quarter == params.start_quarter:
            return self._canonicalize(self._policy_weights, idx=self._policy_weights.index)

        # Align bucket axes (union of policy + current; missing entries → 0).
        idx = self._policy_weights.index.union(current_dollars.index).sort_values()
        w_policy = self._policy_weights.reindex(idx).fillna(0.0).astype(float).to_numpy()
        cur = current_dollars.reindex(idx).fillna(0.0).astype(float).to_numpy()
        V_total = float(cur.sum())

        # Degenerate: non-positive total NAV. Return policy; nothing
        # meaningful to optimize against.
        if V_total <= 0.0:
            return self._canonicalize(pd.Series(w_policy, index=idx), idx=idx)

        # Calibration record (advisory; see MODEL_DOCUMENTATION.md §Phase
        # 4b). The 2026-05-02 sweep showed that
        # ``λ_norm ≈ bps_per_trade × V_total × 1e-3`` is the threshold
        # for engaging interior partial-trade behavior. We store the
        # ratio of the configured value to that suggestion per quarter
        # so reports can flag corner-dominated regimes
        # (``ratio_used_over_suggested ≪ 1``) without altering the
        # optimization.
        bps_per_trade = float(cost_model.bps_per_trade)
        suggested_lambda_norm = bps_per_trade * V_total * 1e-3
        ratio: float | None
        if suggested_lambda_norm > 0.0:
            ratio = self._policy_loss_lambda_norm / suggested_lambda_norm
        else:
            ratio = None
        self._calibration_history.append(
            {
                "quarter": str(quarter),
                "v_total_usd": V_total,
                "bps_per_trade": bps_per_trade,
                "policy_loss_lambda_norm_used": self._policy_loss_lambda_norm,
                "suggested_policy_loss_lambda_norm": suggested_lambda_norm,
                "ratio_used_over_suggested": ratio,
            }
        )

        cost_per_dollar = bps_per_trade / 1e4

        # Zero-cost short-circuit. At ``cost_per_dollar == 0`` the L1
        # term vanishes and the strictly-convex policy quadratic has its
        # unique global minimum at ``w = w_policy`` (subject to
        # constraints). Skipping the solver here is mathematically
        # equivalent and avoids an ill-conditioning pitfall: at small
        # ``λ_norm`` against a large ``V_total`` (e.g. $100M+ portfolios
        # with ``λ_norm ≤ 0.1``), ``λ_eff = λ_norm / V_total²`` is small
        # enough that CLARABEL's default tolerance can stop short of
        # tight policy convergence — the calibration sweep on
        # 2026-05-02 surfaced 3–5pp policy deviation in this regime
        # despite cost being exactly zero. Short-circuit pins zero-cost
        # parity across every ``λ_norm > 0`` and every NAV scale.
        if cost_per_dollar == 0.0:
            return self._canonicalize(pd.Series(w_policy, index=idx), idx=idx)

        n = len(idx)

        import cvxpy as cp

        # Normalized λ → effective λ. The V_total² factor in the dollar-
        # quadratic policy term and the V_total² divisor here cancel
        # mathematically, so the user-facing λ_norm is scale-invariant in
        # the policy term. The cvxpy expression is written in the locked
        # Phase 4b form (V_total factors retained) so the objective text
        # in code matches MODEL_DOCUMENTATION.md verbatim.
        lambda_eff = self._policy_loss_lambda_norm / (V_total * V_total)

        w = cp.Variable(n, nonneg=True)
        trade_dollars = w * V_total - cur

        policy_loss = lambda_eff * cp.sum_squares((w - w_policy) * V_total)
        cost_term = cost_per_dollar * cp.norm1(trade_dollars)

        constraints: list = [cp.sum(w) == 1.0]
        # Box bounds from Constraints, aligned to idx. Missing entries
        # fall back to [0, 1].
        if self._constraints.min_weights or self._constraints.max_weights:
            min_arr = np.array(
                [float(self._constraints.min_weights.get(b, 0.0)) for b in idx],
                dtype=float,
            )
            max_arr = np.array(
                [float(self._constraints.max_weights.get(b, 1.0)) for b in idx],
                dtype=float,
            )
            constraints.append(w >= min_arr)
            constraints.append(w <= max_arr)
        else:
            constraints.append(w <= 1.0)  # nonneg comes from Variable(nonneg=True)

        prob = cp.Problem(cp.Minimize(policy_loss + cost_term), constraints)
        prob.solve(solver=cp.CLARABEL, verbose=False)

        if prob.status not in ("optimal", "optimal_inaccurate"):
            raise RuntimeError(
                f"cvxportfolio allocator solver returned status={prob.status!r} "
                f"at quarter={quarter}"
            )

        raw = np.asarray(w.value, dtype=float)
        return self._canonicalize(pd.Series(raw, index=idx), idx=idx)

    def _canonicalize(self, weights: pd.Series, *, idx: pd.Index) -> pd.Series:
        """Deterministic post-processing: clip negatives, round, fix sum-to-1
        exactly by correction on the largest-weight bucket. The ledger sees
        only the canonicalized values regardless of solver bit-noise.

        A tail assertion pins ``sum(w) ≈ 1.0`` within ``1e-12`` as a
        defense-in-depth guardrail — protects the downstream
        ``target_nav = w * V_total`` step (which depends on sum-to-one for
        total-NAV conservation) against silent drift if the
        canonicalization logic ever changes.
        """
        arr = weights.reindex(idx).fillna(0.0).astype(float).to_numpy()
        arr = np.clip(arr, 0.0, None)
        arr = np.round(arr, _ROUND_DECIMALS)
        s = float(arr.sum())
        if s > 0.0 and abs(s - 1.0) > 0.0:
            j = int(np.argmax(arr))
            arr[j] = arr[j] + (1.0 - s)
        final_sum = float(arr.sum())
        assert abs(final_sum - 1.0) < 1e-12, (
            f"cost-aware allocator canonicalization produced sum={final_sum!r}, "
            f"expected 1.0 within 1e-12"
        )
        return pd.Series(arr, index=idx, dtype=float, name="weight")
