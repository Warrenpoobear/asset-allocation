"""Phase 6 (L6) — apply a scenario-driven correlation shock to a CMA.

Strict perturbation layer: produces a new :class:`CMAConfig` with shocked
correlations and the baseline ``vol_annual`` / ``expected_returns_annual``
/ ``liquidity`` passed through unchanged. The baseline ``CMAConfig`` is
**never mutated**. The post-shock matrix is fed back through
``CMAConfig.model_validate`` so the existing per-cell + PSD checks
re-run on the shocked values — failure raises :class:`ValidationError`
with the smallest eigenvalue in the message. **No PSD repair, no
nearest-matrix projection, no blending.**

Operating on ``CMAConfig`` (dict form) rather than the ``CMA`` dataclass
keeps the shock visible to ``hash_study_config`` automatically:
``cfg.cma`` is included in ``config_hash``, so a scenario that shocks
the correlations gets a distinct ``run_id`` for free.

See MODEL_DOCUMENTATION.md §Phase 6 design.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from aa_model.io.schemas import (
    CMAConfig,
    _OverrideCorrelationShock,
    _ScaleCorrelationShock,
)

if TYPE_CHECKING:
    from aa_model.io.schemas import CorrelationShock


_NUMERIC_TOLERANCE: float = 1e-9


@dataclass(frozen=True)
class CorrelationShockDiagnostics:
    """Reporting diagnostics for an applied shock.

    All values describe the OFF-DIAGONAL portion of the correlation
    matrix (diagonal is preserved by both variants).

    * ``shock_type``: ``"scale"`` or ``"override"``.
    * ``magnitude``: the multiplier (``scale`` only); ``None`` for
      override.
    * ``override_pairs``: the count of unique unordered cell pairs
      replaced (override only); ``None`` for scale.
    * ``clipped_pairs``: count of unique unordered cell pairs whose
      pre-clip value left ``[-1, 1]`` (scale only); ``None`` for
      override.
    * ``max_abs_delta``: max ``|ρ_new - ρ_baseline|`` across all
      off-diagonal entries.
    """

    shock_type: str
    magnitude: float | None
    override_pairs: int | None
    clipped_pairs: int | None
    max_abs_delta: float


def apply_correlation_shock(
    baseline: CMAConfig,
    shock: CorrelationShock,
) -> tuple[CMAConfig, CorrelationShockDiagnostics]:
    """Return a new ``CMAConfig`` with shocked correlations + diagnostics.

    The ``baseline`` is read but not mutated. The shocked dict is fed
    through ``CMAConfig.model_validate`` so the full validation chain
    (per-cell + PSD) re-runs on the post-shock state — any violation
    raises before the new config can be used.
    """
    if not baseline.correlations:
        raise ValueError("apply_correlation_shock: baseline CMAConfig has no correlation matrix")

    buckets = sorted(baseline.correlations.keys())
    # Deep copy of the correlation dict so we never touch the baseline.
    new_corr: dict[str, dict[str, float]] = {
        i: {j: float(baseline.correlations[i][j]) for j in buckets} for i in buckets
    }

    if isinstance(shock, _ScaleCorrelationShock):
        shock_type = "scale"
        magnitude = float(shock.magnitude)
        override_pairs = None
        clipped_pairs = 0
        for ix_i, i in enumerate(buckets):
            for j in buckets[ix_i + 1 :]:
                pre = float(baseline.correlations[i][j]) * magnitude
                clipped = pre
                if clipped > 1.0:
                    clipped = 1.0
                    clipped_pairs += 1
                elif clipped < -1.0:
                    clipped = -1.0
                    clipped_pairs += 1
                new_corr[i][j] = clipped
                new_corr[j][i] = clipped
    elif isinstance(shock, _OverrideCorrelationShock):
        shock_type = "override"
        magnitude = None
        clipped_pairs = None
        bucket_set = set(buckets)
        # Bucket alignment.
        for i, row in shock.matrix.items():
            if i not in bucket_set:
                raise ValueError(
                    f"correlation_shock.override: bucket {i!r} not in CMA "
                    f"(known buckets: {buckets})"
                )
            for j in row:
                if j not in bucket_set:
                    raise ValueError(
                        f"correlation_shock.override: bucket {j!r} not in CMA "
                        f"(known buckets: {buckets})"
                    )
        # Apply, auto-mirroring; track unique unordered pairs replaced.
        applied_pairs: set[tuple[str, str]] = set()
        for i, row in shock.matrix.items():
            for j, x in row.items():
                if i == j:
                    # Diagonal == 1 was already validated at schema time;
                    # keep the baseline 1.0 untouched.
                    continue
                xf = float(x)
                new_corr[i][j] = xf
                new_corr[j][i] = xf
                applied_pairs.add(tuple(sorted((i, j))))
        override_pairs = len(applied_pairs)
    else:  # pragma: no cover — discriminator should prevent this
        raise TypeError(f"unsupported correlation_shock type: {type(shock)!r}")

    # Compute max |Δρ| across off-diagonals.
    max_abs_delta = 0.0
    for ix_i, i in enumerate(buckets):
        for j in buckets[ix_i + 1 :]:
            d = abs(new_corr[i][j] - float(baseline.correlations[i][j]))
            if d > max_abs_delta:
                max_abs_delta = d

    # Round-trip through CMAConfig.model_validate so the per-cell + PSD
    # checks re-run on the shocked state. Failure surfaces as a
    # ValidationError with the original CMA validator's eigenvalue
    # message.
    new_payload = {
        "expected_returns_annual": dict(baseline.expected_returns_annual),
        "vol_annual": dict(baseline.vol_annual),
        "correlations": new_corr,
    }
    if baseline.liquidity is not None:
        new_payload["liquidity"] = dict(baseline.liquidity)
    new_cma_cfg = CMAConfig.model_validate(new_payload)

    diag = CorrelationShockDiagnostics(
        shock_type=shock_type,
        magnitude=magnitude,
        override_pairs=override_pairs,
        clipped_pairs=clipped_pairs,
        max_abs_delta=max_abs_delta,
    )
    return new_cma_cfg, diag
