"""Phase 6 (L6) — correlation shock tests.

Two layers:

* **Schema-level** — discriminated-union routing + per-variant
  validation (positive magnitude on scale; per-cell + asymmetric-supply
  + diagonal on override).
* **Apply-time** — scale sign-preservation + clip count, override
  partial merge + auto-mirror, unknown-bucket failure, baseline
  immutability, PSD failure with eigenvalue surfaced.

End-to-end orchestrator coverage of the shipped ``crisis_correlation``
scenario lives in ``test_orchestrator.py`` and ``test_scenario_builder.py``.

See MODEL_DOCUMENTATION.md §Phase 6 design.
"""

from __future__ import annotations

import math

import pytest
from pydantic import TypeAdapter, ValidationError

from aa_model.assumptions.correlation_shock import apply_correlation_shock
from aa_model.io.schemas import (
    CMAConfig,
    CorrelationShock,
    _OverrideCorrelationShock,
    _ScaleCorrelationShock,
)


def _identity_cma() -> CMAConfig:
    """Three-bucket baseline with identity correlations — tightly controlled
    starting point for shock semantics tests.
    """
    return CMAConfig.model_validate(
        {
            "expected_returns_annual": {"a": 0.05, "b": 0.06, "c": 0.07},
            "vol_annual": {"a": 0.10, "b": 0.15, "c": 0.20},
            "correlations": {
                "a": {"a": 1.0, "b": 0.0, "c": 0.0},
                "b": {"a": 0.0, "b": 1.0, "c": 0.0},
                "c": {"a": 0.0, "b": 0.0, "c": 1.0},
            },
        }
    )


def _signed_cma() -> CMAConfig:
    """Baseline with a positive AND a negative off-diagonal so we can test
    sign-preserving multiplication.
    """
    return CMAConfig.model_validate(
        {
            "expected_returns_annual": {"a": 0.05, "b": 0.06, "c": 0.07},
            "vol_annual": {"a": 0.10, "b": 0.15, "c": 0.20},
            "correlations": {
                "a": {"a": 1.0, "b": 0.4, "c": -0.3},
                "b": {"a": 0.4, "b": 1.0, "c": 0.0},
                "c": {"a": -0.3, "b": 0.0, "c": 1.0},
            },
        }
    )


# ---- discriminated-union routing -------------------------------------------


def test_discriminator_routes_scale():
    adapter = TypeAdapter(CorrelationShock)
    s = adapter.validate_python({"type": "scale", "magnitude": 1.5})
    assert isinstance(s, _ScaleCorrelationShock)
    assert s.magnitude == 1.5


def test_discriminator_routes_override():
    adapter = TypeAdapter(CorrelationShock)
    s = adapter.validate_python(
        {"type": "override", "matrix": {"a": {"b": 0.5}}}
    )
    assert isinstance(s, _OverrideCorrelationShock)
    assert s.matrix == {"a": {"b": 0.5}}


def test_discriminator_unknown_type_fails():
    adapter = TypeAdapter(CorrelationShock)
    with pytest.raises(ValidationError):
        adapter.validate_python({"type": "shrink_toward_one", "alpha": 0.5})


# ---- scale: schema-level ---------------------------------------------------


def test_scale_rejects_non_positive_magnitude():
    with pytest.raises(ValidationError, match="must be > 0"):
        _ScaleCorrelationShock.model_validate({"type": "scale", "magnitude": 0.0})
    with pytest.raises(ValidationError, match="must be > 0"):
        _ScaleCorrelationShock.model_validate({"type": "scale", "magnitude": -1.0})


def test_scale_rejects_non_finite_magnitude():
    with pytest.raises(ValidationError, match="not finite"):
        _ScaleCorrelationShock.model_validate({"type": "scale", "magnitude": math.inf})


# ---- override: schema-level ------------------------------------------------


def test_override_rejects_out_of_range_value():
    with pytest.raises(ValidationError, match=r"out of \[-1, 1\]"):
        _OverrideCorrelationShock.model_validate(
            {"type": "override", "matrix": {"a": {"b": 1.5}}}
        )


def test_override_rejects_diagonal_not_one():
    with pytest.raises(ValidationError, match="diagonal must be 1.0"):
        _OverrideCorrelationShock.model_validate(
            {"type": "override", "matrix": {"a": {"a": 0.99}}}
        )


def test_override_rejects_asymmetric_supply():
    with pytest.raises(ValidationError, match="auto-mirrored"):
        _OverrideCorrelationShock.model_validate(
            {
                "type": "override",
                "matrix": {
                    "a": {"b": 0.5},
                    "b": {"a": 0.4},
                },
            }
        )


def test_override_accepts_symmetric_supply():
    """Specifying both directions with EQUAL values is allowed (the
    auto-mirror will be a no-op)."""
    s = _OverrideCorrelationShock.model_validate(
        {
            "type": "override",
            "matrix": {
                "a": {"b": 0.5},
                "b": {"a": 0.5},
            },
        }
    )
    assert s.matrix["a"]["b"] == 0.5
    assert s.matrix["b"]["a"] == 0.5


# ---- apply: scale semantics ------------------------------------------------


def test_scale_preserves_sign_amplifies_magnitude():
    cma = _signed_cma()
    shock = _ScaleCorrelationShock(type="scale", magnitude=1.5)
    new_cma, diag = apply_correlation_shock(cma, shock)

    # +0.4 * 1.5 = +0.6  (more positive)
    assert new_cma.correlations["a"]["b"] == pytest.approx(0.6)
    assert new_cma.correlations["b"]["a"] == pytest.approx(0.6)
    # -0.3 * 1.5 = -0.45 (more negative — sign-preserving multiplication)
    assert new_cma.correlations["a"]["c"] == pytest.approx(-0.45)
    assert new_cma.correlations["c"]["a"] == pytest.approx(-0.45)
    # zero stays zero
    assert new_cma.correlations["b"]["c"] == 0.0
    # diagonal preserved
    assert new_cma.correlations["a"]["a"] == 1.0

    assert diag.shock_type == "scale"
    assert diag.magnitude == 1.5
    assert diag.clipped_pairs == 0
    assert diag.override_pairs is None
    assert diag.max_abs_delta == pytest.approx(0.2)  # +0.4 → +0.6


def test_scale_clips_to_unit_interval_and_counts_pairs():
    """Magnitude that pushes |ρ| past 1 must clip and report the count.
    Uses a 2-bucket fixture so the clipped matrix stays PSD (singular,
    within tolerance) — a 3-bucket clip-everything matrix would fail
    PSD on its own (caught by a separate test).
    """
    two_bucket = CMAConfig.model_validate(
        {
            "expected_returns_annual": {"a": 0.05, "b": 0.06},
            "vol_annual": {"a": 0.10, "b": 0.15},
            "correlations": {
                "a": {"a": 1.0, "b": 0.4},
                "b": {"a": 0.4, "b": 1.0},
            },
        }
    )
    shock = _ScaleCorrelationShock(type="scale", magnitude=4.0)
    new_cma, diag = apply_correlation_shock(two_bucket, shock)

    # 0.4 * 4 = 1.6 → clipped to 1.0
    assert new_cma.correlations["a"]["b"] == pytest.approx(1.0)
    assert new_cma.correlations["b"]["a"] == pytest.approx(1.0)
    assert diag.clipped_pairs == 1
    assert diag.max_abs_delta == pytest.approx(0.6)


def test_scale_clip_breaking_psd_fails_loudly():
    """A multiplicative shock that pushes the matrix outside PSD must
    fail at apply time with the eigenvalue surfaced. Constructed setup:
    3-bucket signed CMA × magnitude 4 → ρ = ±1 corner, not PSD.
    """
    cma = _signed_cma()
    shock = _ScaleCorrelationShock(type="scale", magnitude=4.0)
    with pytest.raises(ValidationError, match="not positive semi-definite"):
        apply_correlation_shock(cma, shock)


# ---- apply: override semantics ---------------------------------------------


def test_override_partial_merge_with_auto_mirror():
    cma = _identity_cma()
    shock = _OverrideCorrelationShock(
        type="override",
        matrix={"a": {"b": 0.85}},  # only one direction supplied
    )
    new_cma, diag = apply_correlation_shock(cma, shock)

    assert new_cma.correlations["a"]["b"] == pytest.approx(0.85)
    assert new_cma.correlations["b"]["a"] == pytest.approx(0.85)  # auto-mirror
    # Untouched off-diagonals stay at baseline (zero):
    assert new_cma.correlations["a"]["c"] == 0.0
    assert new_cma.correlations["b"]["c"] == 0.0
    # Diagonal preserved:
    assert new_cma.correlations["a"]["a"] == 1.0

    assert diag.shock_type == "override"
    assert diag.override_pairs == 1
    assert diag.clipped_pairs is None
    assert diag.max_abs_delta == pytest.approx(0.85)


def test_override_unknown_bucket_fails_with_bucket_name():
    cma = _identity_cma()
    shock = _OverrideCorrelationShock(
        type="override", matrix={"a": {"unknown_bucket": 0.5}}
    )
    with pytest.raises(ValueError, match="'unknown_bucket' not in CMA"):
        apply_correlation_shock(cma, shock)


# ---- apply: baseline immutability ------------------------------------------


def test_apply_does_not_mutate_baseline():
    cma = _identity_cma()
    baseline_snapshot = {i: dict(row) for i, row in cma.correlations.items()}
    shock = _OverrideCorrelationShock(
        type="override", matrix={"a": {"b": 0.95}}
    )
    apply_correlation_shock(cma, shock)
    # Baseline must be byte-identical after apply.
    assert cma.correlations == baseline_snapshot


# ---- apply: PSD failure ----------------------------------------------------


def test_override_that_breaks_psd_fails_loudly():
    """Force a non-PSD post-shock matrix and assert the eigenvalue is in
    the error message. Three-bucket counter-example: corr(a,b)=0.99,
    corr(b,c)=0.99, corr(a,c)=-0.99 → not PSD.
    """
    cma = _identity_cma()
    shock = _OverrideCorrelationShock(
        type="override",
        matrix={
            "a": {"b": 0.99, "c": -0.99},
            "b": {"c": 0.99},
        },
    )
    with pytest.raises(ValidationError, match="not positive semi-definite"):
        apply_correlation_shock(cma, shock)
