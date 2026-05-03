"""Pydantic v2 models for every config the package consumes.

All inputs are validated through one of these models before any engine fires.
Validation failures are loud per SPEC §2.2. Unknown keys raise via
``extra='forbid'``.
"""

from __future__ import annotations

import math
import re
from typing import Annotated, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

QUARTER_RE = re.compile(r"^\d{4}Q[1-4]$")
_STRICT = ConfigDict(extra="forbid")


# ---- shared primitives -----------------------------------------------------


class TaxConfig(BaseModel):
    model_config = _STRICT
    jurisdiction: Literal["US"] = "US"


class GovernanceConfig(BaseModel):
    model_config = _STRICT
    size_usd: float = Field(gt=0)
    tax: TaxConfig
    license: str = "MIT"


class SolverConfig(BaseModel):
    model_config = _STRICT
    preferred: str
    fallback_chain: list[str]


class LiquidityConfig(BaseModel):
    model_config = _STRICT
    floor_months: int = Field(ge=0)


class PEConfig(BaseModel):
    model_config = _STRICT
    sleeve_target_pct: float = Field(ge=0.0, le=1.0)
    scope: list[Literal["buyout", "venture", "growth", "infra", "re", "pc"]]
    # Phase 7 / STAIRS: PE projection engine. Default "ta" (existing
    # Takahashi–Alexander model) keeps every shipped config bit-stable.
    # "stairs" opts into the CMA-coupled deterministic single-path
    # adapter; the cross-config validator then requires
    # pe_pacing.stairs_defaults to be present and aligned with
    # allocation.stub_weights pe_* sleeves.
    engine: Literal["ta", "stairs"] = "ta"


class RebalanceConfig(BaseModel):
    model_config = _STRICT
    frequency: Literal["quarterly"]
    # Phase 8 / L8: when true (default), the rebalancer cannot trade
    # illiquid buckets. PE exposure can only change via pe_call /
    # pe_distribution / pe_nav_mark; liquid sleeves absorb the
    # rebalancing burden over the residual liquid NAV. Setting to
    # false reproduces the pre-L8 PE-tradable behavior; reserved for
    # internal regression-anchor tests, NOT a recommended user-facing
    # mode. See MODEL_DOCUMENTATION.md §Phase 8 design.
    illiquid_overlay: bool = True


class HorizonConfig(BaseModel):
    model_config = _STRICT
    start_quarter: str
    num_quarters: int = Field(ge=1)

    @field_validator("start_quarter")
    @classmethod
    def _check_quarter(cls, v: str) -> str:
        if not QUARTER_RE.match(v):
            raise ValueError(f"start_quarter must match YYYYQN, got {v!r}")
        return v


# ---- base config -----------------------------------------------------------


class AllocationRefConfig(BaseModel):
    model_config = _STRICT
    # Phase 1 supports only the stub. Phase 3 widens this Literal.
    # Stub is the Phase 1 reference implementation; "riskfolio" was added in
    # Phase 3a behind an opt-in flag. "cvxportfolio" (Phase 4b) is the
    # cost-aware allocator engine — opt-in. New engines extend this Literal.
    engine: Literal["stub", "riskfolio", "cvxportfolio"]
    config: str


class ImplementationRefConfig(BaseModel):
    """Rebalancer engine + cost parameters. Phase 3b extension."""

    model_config = _STRICT
    # Stub is the zero-cost rebalancer (Phase 1 reference); cvxportfolio
    # (Phase 3b) applies a linear transaction cost via the existing
    # CostModel channel. New engines extend this Literal.
    engine: Literal["stub", "cvxportfolio"] = "stub"
    bps_per_trade: float = Field(ge=0.0, default=0.0)


class _SubConfigRef(BaseModel):
    model_config = _STRICT
    config: str


class FixturesConfig(BaseModel):
    model_config = _STRICT
    scenario: str


class OutputConfig(BaseModel):
    model_config = _STRICT
    base_dir: str


class BaseConfig(BaseModel):
    model_config = _STRICT
    version: str
    seed: int
    currency: Literal["USD"]
    governance: GovernanceConfig
    solver: SolverConfig
    liquidity: LiquidityConfig
    pe: PEConfig
    rebalance: RebalanceConfig
    allocation: AllocationRefConfig
    implementation: ImplementationRefConfig = ImplementationRefConfig()
    cma: _SubConfigRef
    spending: _SubConfigRef
    pe_pacing: _SubConfigRef
    scenarios: _SubConfigRef
    fixtures: FixturesConfig
    horizon: HorizonConfig
    output: OutputConfig


# ---- public allocation -----------------------------------------------------


class PublicAllocationConfig(BaseModel):
    model_config = _STRICT
    stub_weights: dict[str, float]
    # Phase 4b: cost-aware allocation policy-loss weight, **normalized**.
    # The cost-aware allocator computes the effective coefficient as
    # ``λ_eff = policy_loss_lambda_norm / V_total²`` per quarter, so the
    # user-facing value is stable across portfolio sizes (the V_total²
    # factor in the dollar-quadratic policy term cancels). Consumed by
    # the cvxportfolio allocator engine; ignored by stub / riskfolio.
    # See MODEL_DOCUMENTATION.md §Phase 4b — normalized λ.
    policy_loss_lambda_norm: float = Field(default=1.0, gt=0.0)

    @model_validator(mode="after")
    def _weights_well_formed(self) -> PublicAllocationConfig:
        if not self.stub_weights:
            raise ValueError("stub_weights must be non-empty")
        for bucket, w in self.stub_weights.items():
            if w < 0.0:
                raise ValueError(f"stub_weights[{bucket}] = {w} < 0")
        total = sum(self.stub_weights.values())
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"stub_weights must sum to 1.0 within 1e-9; got {total}")
        return self


# ---- capital market assumptions (CMA) --------------------------------------


_LIQUIDITY_VALUES: tuple[str, ...] = ("liquid", "semi_liquid", "illiquid")
_PSD_TOLERANCE: float = 1e-9
_EXPECTED_RETURN_BOUND: float = 1.0  # |ER| < 1.0 catches percent-vs-decimal mistakes
_CORR_BOUND: float = 1.0
_NUMERIC_TOLERANCE: float = 1e-9


class CMAConfig(BaseModel):
    """Capital market assumptions (Phase 5).

    Static priors over the allocation bucket universe. Consumed by the
    riskfolio MinRisk solve and by report diagnostics; **not** consumed
    by the Phase 4b cost-aware allocator (see MODEL_DOCUMENTATION.md
    §Phase 5 design / decision C).

    All values are annualized.
    """

    model_config = _STRICT
    expected_returns_annual: dict[str, float]
    vol_annual: dict[str, float]
    correlations: dict[str, dict[str, float]]
    # Phase 12 / L19: ``"locked_strategic"`` added as a fourth tier
    # (additive — old 3-tier configs load unchanged). Tag OpCo
    # equity, development real estate, development land, and any
    # other bucket whose value never enters spending decisions
    # short of an explicit liquidity event.
    liquidity: (
        dict[str, Literal["liquid", "semi_liquid", "illiquid", "locked_strategic"]] | None
    ) = None
    # Phase 12 / L19: optional per-bucket flag. Required by
    # StudyConfig cross-validator only when
    # spending.guardrail.spending_base == "liquid_plus_income_producing_nav".
    # NOTE: this is a bucket-level static CMA tag, not asset-,
    # entity-, or property-level cash-flow classification — a
    # bridge until Phase 12.5's distribution_inflow ledger flow
    # type lands. A True flag means "this bucket contains assets
    # that on average produce some distributable yield"; it does
    # NOT mean "this bucket's dollars are spendable income."
    income_producing: dict[str, bool] | None = None

    @field_validator("expected_returns_annual")
    @classmethod
    def _er_per_cell(cls, v: dict[str, float]) -> dict[str, float]:
        if not v:
            raise ValueError("expected_returns_annual must be non-empty")
        for bucket, x in v.items():
            xf = float(x)
            if not math.isfinite(xf):
                raise ValueError(f"expected_returns_annual[{bucket!r}] = {x!r} is not finite")
            if abs(xf) >= _EXPECTED_RETURN_BOUND:
                raise ValueError(
                    f"expected_returns_annual[{bucket!r}] = {xf} is out of bounds; "
                    f"expected |x| < {_EXPECTED_RETURN_BOUND} (decimal, not percent — "
                    "did you write 5 instead of 0.05?)"
                )
        return v

    @field_validator("vol_annual")
    @classmethod
    def _vol_per_cell(cls, v: dict[str, float]) -> dict[str, float]:
        if not v:
            raise ValueError("vol_annual must be non-empty")
        for bucket, x in v.items():
            xf = float(x)
            if not math.isfinite(xf):
                raise ValueError(f"vol_annual[{bucket!r}] = {x!r} is not finite")
            if xf < 0.0:
                raise ValueError(f"vol_annual[{bucket!r}] = {xf} < 0")
        return v

    @field_validator("correlations")
    @classmethod
    def _corr_per_cell(cls, v: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
        if not v:
            raise ValueError("correlations must be non-empty")
        outer_buckets = set(v.keys())
        for i, row in v.items():
            if set(row.keys()) != outer_buckets:
                missing = sorted(outer_buckets - set(row.keys()))
                extra = sorted(set(row.keys()) - outer_buckets)
                raise ValueError(
                    f"correlations[{i!r}] keys mismatch — " f"missing: {missing}, extra: {extra}"
                )
            for j, x in row.items():
                xf = float(x)
                if not math.isfinite(xf):
                    raise ValueError(f"correlations[{i!r}][{j!r}] = {x!r} is not finite")
                if abs(xf) > _CORR_BOUND + _NUMERIC_TOLERANCE:
                    raise ValueError(f"correlations[{i!r}][{j!r}] = {xf} out of [-1, 1]")
                if i == j and abs(xf - 1.0) > _NUMERIC_TOLERANCE:
                    raise ValueError(
                        f"correlations[{i!r}][{i!r}] = {xf}; diagonal must be 1.0 "
                        f"within {_NUMERIC_TOLERANCE}"
                    )
        # Symmetry within tolerance.
        keys = sorted(outer_buckets)
        for i, ki in enumerate(keys):
            for kj in keys[i + 1 :]:
                a = float(v[ki][kj])
                b = float(v[kj][ki])
                if abs(a - b) > _NUMERIC_TOLERANCE:
                    raise ValueError(
                        f"correlations[{ki!r}][{kj!r}] = {a} != "
                        f"correlations[{kj!r}][{ki!r}] = {b} (asymmetry)"
                    )
        return v

    @model_validator(mode="after")
    def _bucket_alignment_and_psd(self) -> CMAConfig:
        er_buckets = set(self.expected_returns_annual.keys())
        vol_buckets = set(self.vol_annual.keys())
        corr_buckets = set(self.correlations.keys())
        if not (er_buckets == vol_buckets == corr_buckets):
            raise ValueError(
                "CMA bucket sets disagree across fields — "
                f"expected_returns={sorted(er_buckets)}, "
                f"vol={sorted(vol_buckets)}, "
                f"correlations={sorted(corr_buckets)}"
            )
        if self.liquidity is not None and set(self.liquidity.keys()) != er_buckets:
            missing = sorted(er_buckets - set(self.liquidity.keys()))
            extra = sorted(set(self.liquidity.keys()) - er_buckets)
            raise ValueError(f"liquidity bucket set mismatch — missing: {missing}, extra: {extra}")

        # Phase 12 / L19: income_producing must cover every bucket
        # when present (no silent default-False — reviewer
        # tightening 2 / scope discipline).
        if self.income_producing is not None and set(self.income_producing.keys()) != er_buckets:
            missing = sorted(er_buckets - set(self.income_producing.keys()))
            extra = sorted(set(self.income_producing.keys()) - er_buckets)
            raise ValueError(
                f"income_producing bucket set mismatch — " f"missing: {missing}, extra: {extra}"
            )

        # PSD check on the assembled covariance matrix Σ = diag(vol)·corr·diag(vol).
        # User-supplied correlations can be pairwise valid yet structurally
        # non-PSD; this surfaces it loudly.
        buckets = sorted(er_buckets)
        vol = np.array([float(self.vol_annual[b]) for b in buckets], dtype=float)
        corr = np.array(
            [[float(self.correlations[i][j]) for j in buckets] for i in buckets],
            dtype=float,
        )
        cov = np.outer(vol, vol) * corr
        # Eigenvalues of a symmetric PSD matrix are real and ≥ 0; we use eigh
        # which assumes symmetry. If symmetry passed above, this is safe.
        eigs = np.linalg.eigvalsh(cov)
        min_eig = float(eigs.min())
        if min_eig < -_PSD_TOLERANCE:
            raise ValueError(
                f"CMA covariance matrix is not positive semi-definite; "
                f"smallest eigenvalue = {min_eig:.3e} (tolerance "
                f"{-_PSD_TOLERANCE:.0e})"
            )
        return self


# ---- correlation shock (Phase 6 / L6) --------------------------------------


class _ScaleCorrelationShock(BaseModel):
    """Sign-preserving multiplicative shock to every off-diagonal entry of
    the CMA correlation matrix. See MODEL_DOCUMENTATION.md §Phase 6 design.

    Diagonal entries are preserved. Results are clipped to ``[-1, 1]``;
    the clip count is surfaced in the report so saturation is visible.
    """

    model_config = _STRICT
    type: Literal["scale"]
    magnitude: float

    @field_validator("magnitude")
    @classmethod
    def _magnitude_positive_finite(cls, v: float) -> float:
        x = float(v)
        if not math.isfinite(x):
            raise ValueError(f"correlation_shock.scale.magnitude = {v!r} is not finite")
        if x <= 0.0:
            raise ValueError(
                f"correlation_shock.scale.magnitude = {x} must be > 0; "
                "negative magnitudes flip every off-diagonal sign and are almost "
                "certainly a user error"
            )
        return x


class _OverrideCorrelationShock(BaseModel):
    """Explicit pairwise replacement of correlation entries.

    Partial: unspecified entries pass through from the baseline CMA.
    Auto-mirrored: specifying ``matrix["a"]["b"] = 0.95`` also sets
    ``matrix["b"]["a"]``. If both directions are supplied and **disagree**,
    apply-time validation fails loudly. See MODEL_DOCUMENTATION.md §Phase 6.

    Bucket-set alignment with the CMA is checked at apply time (the
    schema does not have a CMA reference).
    """

    model_config = _STRICT
    type: Literal["override"]
    matrix: dict[str, dict[str, float]]

    @field_validator("matrix")
    @classmethod
    def _matrix_well_formed(cls, v: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
        if not v:
            raise ValueError("correlation_shock.override.matrix must be non-empty")
        for i, row in v.items():
            for j, x in row.items():
                xf = float(x)
                if not math.isfinite(xf):
                    raise ValueError(
                        f"correlation_shock.override.matrix[{i!r}][{j!r}] = " f"{x!r} is not finite"
                    )
                if abs(xf) > _CORR_BOUND + _NUMERIC_TOLERANCE:
                    raise ValueError(
                        f"correlation_shock.override.matrix[{i!r}][{j!r}] = " f"{xf} out of [-1, 1]"
                    )
                if i == j and abs(xf - 1.0) > _NUMERIC_TOLERANCE:
                    raise ValueError(
                        f"correlation_shock.override.matrix[{i!r}][{i!r}] = "
                        f"{xf}; diagonal must be 1.0 within {_NUMERIC_TOLERANCE} "
                        "if specified"
                    )
        # Asymmetric supply: if both [i][j] and [j][i] are given, they must agree.
        keys = sorted(v.keys())
        for i, ki in enumerate(keys):
            row_i = v[ki]
            for kj in keys[i + 1 :]:
                if kj not in row_i:
                    continue
                if kj not in v or ki not in v[kj]:
                    continue
                a = float(row_i[kj])
                b = float(v[kj][ki])
                if abs(a - b) > _NUMERIC_TOLERANCE:
                    raise ValueError(
                        f"correlation_shock.override.matrix[{ki!r}][{kj!r}] = {a} "
                        f"!= matrix[{kj!r}][{ki!r}] = {b} — supply only one "
                        "direction or two equal values; values are auto-mirrored"
                    )
        return v


CorrelationShock = Annotated[
    _ScaleCorrelationShock | _OverrideCorrelationShock,
    Field(discriminator="type"),
]


# ---- spending --------------------------------------------------------------


class SmoothingConfig(BaseModel):
    model_config = _STRICT
    window_quarters: int = Field(ge=1)
    weight: float = Field(ge=0.0, le=1.0)


class GuardrailConfig(BaseModel):
    """Owl (Guyton-Klinger) guardrail config.

    Bands are expressed as fractional deviations from the *initial*
    withdrawal rate (``annual_spend_usd / initial_nav_total`` at run start).
    The guardrail check fires only at year boundaries, after applying
    inflation:

    * if rate < initial_rate · (1 - lower_band_pct) → raise spending by raise_pct
    * if rate > initial_rate · (1 + upper_band_pct) → cut spending by cut_pct
    * otherwise spending stays at the inflation-adjusted prior level

    The NAV used in the rate check is **realized** end-of-prior-quarter
    NAV read from the ledger via ``ledger.end_nav_through(quarter - 1)``
    (Phase 4a; before Phase 4a, Owl used a deterministic forward forecast,
    which produced directionally wrong responses to inflation and return
    shocks — see L15 / L18 [resolved 2026-05-01]).
    """

    model_config = _STRICT
    upper_band_pct: float = Field(gt=0.0)  # cut trigger
    lower_band_pct: float = Field(gt=0.0)  # raise trigger
    raise_pct: float = Field(gt=0.0)
    cut_pct: float = Field(gt=0.0, lt=1.0)  # cut < 100% (cannot zero out spending)
    # Phase 11 / L16: optional absolute-dollar guardrail clamps.
    # Default None preserves the existing rate-band-only behavior
    # (which is scale-invariant under proportional setup, per L16).
    # When set, break scale-invariance by clamping the trigger output
    # to a dollar floor / ceiling that does NOT scale with initial NAV.
    # Static — not inflation-adjusted; users wanting inflation-indexed
    # bands set them externally as a policy choice. Owl-only.
    #
    # IMPORTANT: Phase 11 fixes scale-invariance only. It does NOT
    # resolve spending-base realism (L19). Owl still measures rate
    # against total NAV — see MODEL_DOCUMENTATION.md §Use-case context
    # + §Phase 11 design.
    absolute_min_annual_usd: float | None = Field(default=None, ge=0.0)
    absolute_max_annual_usd: float | None = Field(default=None, gt=0.0)

    # Phase 12 / L19: optional spending-base selector. Default
    # None ≡ "total_nav" — Owl measures rate against
    # ledger.end_nav_through(prior_q).sum() on both rate sides,
    # byte-identical to Phase 11. When set to a non-default
    # value, both initial_rate and current_rate denominators are
    # replaced by compute_spending_base(...) on the same NAV view.
    # Owl-only — flat_real / smoothing have no rate concept.
    #
    # ``"distributable_income"`` is parked in the Literal but
    # raises NotImplementedError at runtime; Phase 12.5 lands the
    # new ``distribution_inflow`` ledger flow type.
    #
    # ``"liquid_plus_income_producing_nav"`` includes the **NAV**
    # of buckets tagged ``income_producing``; it does NOT measure
    # actual distributable income (reviewer tightening 1).
    spending_base: (
        Literal[
            "total_nav",
            "liquid_nav",
            "liquid_plus_income_producing_nav",
            "custom_policy",
            "distributable_income",
        ]
        | None
    ) = Field(default=None)

    # Phase 12 / L19: only meaningful when spending_base ==
    # "custom_policy". Bucket-keyed (NOT tier-keyed) — gives the
    # SFO user per-bucket inclusion control. Validation lives on
    # StudyConfig (needs CMA bucket universe to check keys):
    # every key must be a valid CMA bucket; values finite, ≥0;
    # ≥1 positive; unspecified buckets default to 0; runtime
    # guard in OwlRule ensures resulting base > 0 when used as
    # the rate denominator (reviewer tightening 3).
    spending_base_weights: dict[str, float] | None = Field(default=None)

    # Phase 12.5 / L19 flow-side: trailing window for the
    # distributable_income base. Default 4 quarters (TTM). Required
    # when spending_base == "distributable_income". Smaller windows
    # are noisier; larger windows lag regime shifts.
    distribution_window_quarters: int | None = Field(default=None, ge=1, le=20)

    # Phase 12.5 / L19 flow-side: bootstrap distributable-income value
    # used for (a) the initial-rate denominator at run start (no
    # closed quarters yet) and (b) any year-boundary call where the
    # closed-prior-quarter window is incomplete. Required when
    # spending_base == "distributable_income". Strictly positive when
    # set; non-finite rejected explicitly.
    bootstrap_distributable_income_usd: float | None = Field(default=None, gt=0.0)

    @field_validator("absolute_min_annual_usd", "absolute_max_annual_usd")
    @classmethod
    def _absolute_clamp_finite(cls, v: float | None) -> float | None:
        # pydantic's ``ge`` / ``gt`` admit ``inf``; reject explicitly so a
        # user mistake (e.g., ``float("inf")``) fails loudly rather than
        # disabling the clamp by trivial bound.
        if v is None:
            return v
        if not math.isfinite(v):
            raise ValueError(f"absolute clamp value must be finite; got {v!r}")
        return v

    @field_validator("bootstrap_distributable_income_usd")
    @classmethod
    def _bootstrap_finite(cls, v: float | None) -> float | None:
        # Phase 12.5 / L19: pydantic's gt=0.0 admits inf; reject
        # non-finite explicitly so a user mistake fails loudly.
        if v is None:
            return v
        if not math.isfinite(v):
            raise ValueError(f"bootstrap_distributable_income_usd must be finite; got {v!r}")
        return v

    @field_validator("spending_base_weights")
    @classmethod
    def _weights_finite_nonneg_and_positive(
        cls, v: dict[str, float] | None
    ) -> dict[str, float] | None:
        # Phase 12 / L19 reviewer tightening 3: per-weight checks.
        # Bucket-key validity vs CMA universe is enforced at the
        # StudyConfig level (needs cross-config visibility).
        if v is None:
            return v
        if not v:
            raise ValueError(
                "spending_base_weights must be non-empty when set; "
                "use spending_base='total_nav' to include every bucket"
            )
        any_positive = False
        for bucket, w in v.items():
            wf = float(w)
            if not math.isfinite(wf):
                raise ValueError(f"spending_base_weights[{bucket!r}] = {w!r} is not finite")
            if wf < 0.0:
                raise ValueError(f"spending_base_weights[{bucket!r}] = {wf} < 0")
            if wf > 0.0:
                any_positive = True
        if not any_positive:
            raise ValueError(
                "spending_base_weights must have at least one strictly "
                "positive weight; all-zero blends produce a zero base"
            )
        return v

    @model_validator(mode="after")
    def _absolute_band_bounds_well_formed(self) -> GuardrailConfig:
        if (
            self.absolute_min_annual_usd is not None
            and self.absolute_max_annual_usd is not None
            and self.absolute_min_annual_usd > self.absolute_max_annual_usd
        ):
            raise ValueError(
                f"absolute_min_annual_usd ({self.absolute_min_annual_usd}) > "
                f"absolute_max_annual_usd ({self.absolute_max_annual_usd})"
            )
        return self

    @model_validator(mode="after")
    def _spending_base_weights_only_with_custom_policy(self) -> GuardrailConfig:
        # Phase 12 / L19: weights are meaningful only for
        # custom_policy. A weights dict with any other base is a
        # config mistake — fail loud rather than silently ignore.
        if self.spending_base_weights is not None and self.spending_base != "custom_policy":
            raise ValueError(
                "spending_base_weights is only meaningful when "
                f"spending_base='custom_policy'; got {self.spending_base!r}"
            )
        if self.spending_base == "custom_policy" and self.spending_base_weights is None:
            raise ValueError("spending_base='custom_policy' requires spending_base_weights")
        return self

    @model_validator(mode="after")
    def _distribution_fields_only_with_distributable_income(
        self,
    ) -> GuardrailConfig:
        # Phase 12.5 / L19 flow-side: distribution_window_quarters
        # and bootstrap_distributable_income_usd are meaningful only
        # for spending_base='distributable_income'. Setting them with
        # any other base is a config mistake — fail loud (matches the
        # weights-only-with-custom_policy discipline above).
        is_distributable_income = self.spending_base == "distributable_income"
        if self.distribution_window_quarters is not None and not is_distributable_income:
            raise ValueError(
                "distribution_window_quarters is only meaningful when "
                f"spending_base='distributable_income'; got "
                f"{self.spending_base!r}"
            )
        if self.bootstrap_distributable_income_usd is not None and not is_distributable_income:
            raise ValueError(
                "bootstrap_distributable_income_usd is only meaningful when "
                f"spending_base='distributable_income'; got "
                f"{self.spending_base!r}"
            )
        if is_distributable_income:
            missing = []
            if self.distribution_window_quarters is None:
                missing.append("distribution_window_quarters")
            if self.bootstrap_distributable_income_usd is None:
                missing.append("bootstrap_distributable_income_usd")
            if missing:
                raise ValueError(
                    "spending_base='distributable_income' requires: " + ", ".join(missing)
                )
        return self


class SpendingConfig(BaseModel):
    model_config = _STRICT
    rule: Literal["flat_real", "smoothing", "owl"]
    annual_spend_usd: float = Field(ge=0.0)
    inflation_pct: float
    smoothing: SmoothingConfig
    floor_usd: float = Field(ge=0.0)
    ceiling_usd: float = Field(ge=0.0)
    guardrail: GuardrailConfig | None = None

    @model_validator(mode="after")
    def _checks(self) -> SpendingConfig:
        if self.floor_usd > self.ceiling_usd:
            raise ValueError(f"floor_usd ({self.floor_usd}) > ceiling_usd ({self.ceiling_usd})")
        if self.rule == "owl" and self.guardrail is None:
            raise ValueError("rule='owl' requires spending.guardrail config")
        return self


# ---- pe pacing -------------------------------------------------------------


class TADefaultsConfig(BaseModel):
    model_config = _STRICT
    lifetime_years: int = Field(ge=1)
    commitment_period_years: int = Field(ge=1)
    rate_of_contribution: list[float]
    bow: float = Field(gt=0.0)
    yield_pct: float = Field(ge=0.0)
    growth_pct: float

    @model_validator(mode="after")
    def _checks(self) -> TADefaultsConfig:
        if len(self.rate_of_contribution) != self.commitment_period_years:
            raise ValueError(
                f"rate_of_contribution length ({len(self.rate_of_contribution)}) "
                f"!= commitment_period_years ({self.commitment_period_years})"
            )
        for r in self.rate_of_contribution:
            if r < 0.0:
                raise ValueError(f"rate_of_contribution element {r} < 0")
        s = sum(self.rate_of_contribution)
        if abs(s - 1.0) > 1e-9:
            raise ValueError(f"rate_of_contribution must sum to 1.0 within 1e-9; got {s}")
        if self.commitment_period_years > self.lifetime_years:
            raise ValueError("commitment_period_years cannot exceed lifetime_years")
        return self


_STRATEGY_TO_SLEEVE: dict[str, str] = {
    "buyout": "pe_buyout",
    "venture": "pe_venture",
    "growth": "pe_growth",
    "credit": "pe_credit",
    "real_estate": "pe_re",
    "infra": "pe_infra",
    # "secondary" is intentionally absent — secondaries are bought as
    # units of the underlying strategy, so any pe_* sleeve is valid.
}


class _FeeModelConfig(BaseModel):
    """Phase 9 metadata: fund-level fee economics carried for diagnostic
    and reporting purposes only. **Not consumed** by the projection math
    in Phase 9; charging management fees on unfunded commitment and
    reducing distributions for carried interest are Phase 10+ scope.
    Schema may evolve when fee economics actually land (loud-failure-
    friendly breaking change at that point).
    """

    model_config = _STRICT
    management_fee_pct: float = Field(default=0.0, ge=0.0, le=0.05)
    carried_interest_pct: float = Field(default=0.0, ge=0.0, le=0.30)
    preferred_return_pct: float = Field(default=0.0, ge=0.0, le=0.20)


class FundConfig(BaseModel):
    model_config = _STRICT
    name: str
    commitment_usd: float = Field(gt=0.0)
    vintage: str
    sleeve: str
    # ---- Phase 9 additions, all optional except status ----
    manager: str | None = None
    fund_id: str | None = None
    strategy: (
        Literal[
            "buyout",
            "venture",
            "growth",
            "credit",
            "real_estate",
            "infra",
            "secondary",
        ]
        | None
    ) = None
    fee_model: _FeeModelConfig | None = None
    status: Literal["active", "committed", "exited", "planned"] = "active"

    @field_validator("vintage")
    @classmethod
    def _check_vintage(cls, v: str) -> str:
        if not QUARTER_RE.match(v):
            raise ValueError(f"vintage must match YYYYQN, got {v!r}")
        return v

    @model_validator(mode="after")
    def _strategy_sleeve_consistent(self) -> FundConfig:
        # When ``strategy`` is set, it must agree with ``sleeve`` per
        # the documented mapping. ``secondary`` is the one flexible
        # case (compatible with any pe_* sleeve).
        if self.strategy is None:
            return self
        if self.strategy == "secondary":
            if not self.sleeve.startswith("pe_"):
                raise ValueError(
                    f"fund {self.name!r}: strategy='secondary' requires a "
                    f"pe_* sleeve, got sleeve={self.sleeve!r}"
                )
            return self
        expected_sleeve = _STRATEGY_TO_SLEEVE[self.strategy]
        if self.sleeve != expected_sleeve:
            raise ValueError(
                f"fund {self.name!r}: strategy={self.strategy!r} requires "
                f"sleeve={expected_sleeve!r}, got sleeve={self.sleeve!r}"
            )
        return self


class _StairsSleeveParams(BaseModel):
    """STAIRS per-sleeve parameters (Phase 7 / L1).

    ``idiosyncratic_drift_pct`` is the annual deterministic NAV-growth
    component (replaces TA's ``growth_pct``). ``beta_to_public_equity``
    is the coupling coefficient on the realized-vs-expected
    public_equity excess. Both finite.
    """

    model_config = _STRICT
    idiosyncratic_drift_pct: float
    beta_to_public_equity: float

    @field_validator("idiosyncratic_drift_pct")
    @classmethod
    def _drift_in_bounds(cls, v: float) -> float:
        x = float(v)
        if not math.isfinite(x):
            raise ValueError(f"idiosyncratic_drift_pct = {v!r} is not finite")
        if abs(x) >= _EXPECTED_RETURN_BOUND:
            raise ValueError(
                f"idiosyncratic_drift_pct = {x} is out of bounds; "
                f"expected |x| < {_EXPECTED_RETURN_BOUND} (decimal, not percent — "
                "did you write 5 instead of 0.05?)"
            )
        return x

    @field_validator("beta_to_public_equity")
    @classmethod
    def _beta_finite(cls, v: float) -> float:
        x = float(v)
        if not math.isfinite(x):
            raise ValueError(f"beta_to_public_equity = {v!r} is not finite")
        return x


class StairsDefaultsConfig(BaseModel):
    """Per-sleeve STAIRS parameters (Phase 7 / L1).

    Required when ``base.pe.engine == "stairs"`` (enforced at
    cross-config validation time). The ``per_sleeve`` keys must equal
    the ``pe_*`` subset of ``allocation.stub_weights``.
    """

    model_config = _STRICT
    per_sleeve: dict[str, _StairsSleeveParams]

    @field_validator("per_sleeve")
    @classmethod
    def _per_sleeve_non_empty(
        cls, v: dict[str, _StairsSleeveParams]
    ) -> dict[str, _StairsSleeveParams]:
        if not v:
            raise ValueError("stairs_defaults.per_sleeve must be non-empty")
        return v


class PEPacingConfig(BaseModel):
    model_config = _STRICT
    ta_defaults: TADefaultsConfig
    funds: list[FundConfig]
    # Phase 7 / STAIRS. Optional at the schema level; required at
    # cross-config validation when base.pe.engine == "stairs".
    stairs_defaults: StairsDefaultsConfig | None = None

    @model_validator(mode="after")
    def _funds_well_formed(self) -> PEPacingConfig:
        # Phase 9: globally-unique fund name (load-bearing rule lifted
        # from unstated convention — the ledger source uses
        # pacing:<fund_name>, so duplicate names create ambiguous
        # ledger sources and ambiguous metadata joins).
        names = [f.name for f in self.funds]
        if len(names) != len(set(names)):
            dups = sorted({n for n in names if names.count(n) > 1})
            raise ValueError(
                f"pe_pacing.funds: name must be globally unique; " f"duplicates: {dups}"
            )

        # Phase 9: globally-unique fund_id when set on any fund.
        ids = [f.fund_id for f in self.funds if f.fund_id is not None]
        if len(ids) != len(set(ids)):
            dups = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(
                f"pe_pacing.funds: fund_id must be globally unique when set; " f"duplicates: {dups}"
            )

        # Phase 9: (manager, name) uniqueness when manager is set —
        # redundant with the global name uniqueness rule above (the
        # tuple is unique whenever name is) but kept as defence-in-
        # depth; surfaces a clearer error message in the manager-
        # specific case.
        mn_pairs = [(f.manager, f.name) for f in self.funds if f.manager is not None]
        if len(mn_pairs) != len(set(mn_pairs)):
            dups = sorted({p for p in mn_pairs if mn_pairs.count(p) > 1})
            raise ValueError(
                f"pe_pacing.funds: (manager, name) must be unique when "
                f"manager is set; duplicates: {dups}"
            )
        return self


# ---- scenarios (Phase 2 placeholder) ---------------------------------------


class ScenariosConfig(BaseModel):
    model_config = _STRICT
    scenarios: list[str] = Field(default_factory=list)


# ---- fixture scenarios -----------------------------------------------------


class ReturnOverride(BaseModel):
    model_config = _STRICT
    quarter_index: int = Field(ge=0)
    value: float


class ReturnPath(BaseModel):
    model_config = _STRICT
    quarterly: float
    overrides: list[ReturnOverride] = Field(default_factory=list)


class ExternalInflows(BaseModel):
    model_config = _STRICT
    default_quarterly_usd: float = 0.0


class FixtureScenarioConfig(BaseModel):
    model_config = _STRICT
    name: str
    description: str
    horizon: HorizonConfig
    returns: dict[str, ReturnPath]
    nav_initial: dict[str, float]
    external_inflows: ExternalInflows


# ---- distribution producer (Phase 13 / L19 producer-side) ------------------


class DistributionEntryConfig(BaseModel):
    """One classified distribution event consumed by Phase 13's
    ConfigDrivenProducer.

    Each entry represents a SINGLE declared distribution event for a
    SINGLE quarter — already classified upstream as
    family-office-distributable. The producer trusts the upstream
    classification (Phase 12.5 reviewer tightening 1; Phase 13
    reviewer tightening 1): legal / tax / entity-governance
    distributability AND inter-entity cash-movement mechanics sit
    upstream, not in this schema.
    """

    model_config = _STRICT
    producer_id: str = Field(min_length=1)
    domain: Literal[
        "real_estate",
        "opco",
        "land",
        "development",
        "portfolio",
        "entity",
    ]
    entity_id: str = Field(min_length=1)
    asset_id: str | None = None
    quarter: str = Field(pattern=r"^\d{4}Q[1-4]$")
    amount_usd: float = Field(gt=0.0)
    recurrence_type: Literal["recurring", "one_time"]
    confidence: Literal["contractual", "forecast", "scenario"]
    restricted: bool = False
    source_reference: str | None = None

    @field_validator("amount_usd")
    @classmethod
    def _amount_finite(cls, v: float) -> float:
        # Phase 13: pydantic gt=0 admits inf; reject explicitly.
        if not math.isfinite(v):
            raise ValueError(f"amount_usd must be finite; got {v!r}")
        return v

    @field_validator("producer_id", "entity_id")
    @classmethod
    def _no_colons(cls, v: str) -> str:
        # Colons are reserved for the source-convention separator
        # (distribution:<domain>:<id>). A producer_id or entity_id
        # containing a colon would silently corrupt the parseable
        # source string.
        if ":" in v:
            raise ValueError(
                f"colons are reserved for the source convention separator; " f"got {v!r}"
            )
        return v

    @field_validator("asset_id")
    @classmethod
    def _no_colons_asset(cls, v: str | None) -> str | None:
        if v is not None and ":" in v:
            raise ValueError(
                f"asset_id may not contain colons (reserved for source "
                f"convention separator); got {v!r}"
            )
        return v

    @model_validator(mode="after")
    def _domain_recurrence_sanity(self) -> DistributionEntryConfig:
        # Phase 13 hard sanity rule: development and land have no
        # recurring yield by definition. After stabilization, an asset
        # graduates from "development" to "real_estate" in the spec.
        # Recurring agricultural / extraction land leases are a Phase
        # 13.x concern — not in scope for this initial schema.
        if self.domain in ("development", "land") and self.recurrence_type == "recurring":
            raise ValueError(
                f"domain={self.domain!r} cannot have "
                f"recurrence_type='recurring'; only one_time monetization "
                f"events qualify (sale, refi, capital event)"
            )
        return self


class DistributionProducerConfig(BaseModel):
    """Phase 13 producer-side spec. Consumed by ConfigDrivenProducer."""

    model_config = _STRICT
    entries: list[DistributionEntryConfig]

    @model_validator(mode="after")
    def _producer_id_globally_unique(self) -> DistributionProducerConfig:
        # Phase 13 reviewer tightening 2: uniqueness is on producer_id
        # ONLY; multiple entries may share (domain, entity_id,
        # asset_id, quarter) and emit the same source string in the
        # same quarter (e.g., recurring rent + one-time refi proceeds
        # from the same building). producer_id is the row-level audit
        # key on the producer-diagnostics side.
        ids = [e.producer_id for e in self.entries]
        if len(ids) != len(set(ids)):
            dups = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(
                f"DistributionProducerConfig: producer_id must be globally "
                f"unique; duplicates: {dups}"
            )
        return self


# ---- workbook ingestion (Phase 14 / L19 workbook-side) ---------------------


class WorkbookIngestionConfig(BaseModel):
    """Phase 14 / L19 workbook ingestion config.

    Bundles a workbook path with a Phase 14 ``WorkbookManifestConfig``
    (imported lazily so the io.schemas module has no openpyxl /
    ingestion dependency at top level). When this config is set on
    StudyConfig, the orchestrator runs ingestion before the per-quarter
    loop, derives a DistributionProducerConfig via the bridge
    function, and constructs a WorkbookDrivenProducer (engine="workbook").

    Default-off byte-stable: cfg.workbook_ingestion = None ⇒ no
    ingestion ⇒ Phase 13 trajectories byte-identical.
    """

    model_config = _STRICT
    workbook_path: str = Field(min_length=1)  # absolute or
    # repo-relative path
    manifest_version: str = Field(default="1", min_length=1)
    # The full WorkbookManifestConfig — typed as Any here because
    # importing the ingestion-side schema would create an inversion
    # (io.schemas → ingestion.schemas would tangle the import graph).
    # Validated structurally by the orchestrator at ingestion time.
    manifest: dict = Field(default_factory=dict)

    @field_validator("manifest_version")
    @classmethod
    def _manifest_version_url_safe(cls, v: str) -> str:
        if ":" in v:
            raise ValueError(f"manifest_version must be URL-safe (no colons); got {v!r}")
        return v


# ---- position ingestion (Phase 17 / L20 study integration) ------------------


class PositionIngestionConfig(BaseModel):
    """Phase 17 — Investment Summary position ingestion wired into StudyConfig.

    ``manifest_path`` points to a YAML file containing a
    ``PositionManifestConfig``; loaded by ``load_position_manifest()`` at
    orchestration time (reviewer tightening 2 — path not inline).
    ``workbook_path`` is the Investment Summary workbook. Both paths are
    validated at orchestration time; ``FileNotFoundError`` is raised for
    missing files (reviewer tightening 3 — fail fast).

    Default-off byte-stable: ``cfg.position_ingestion = None`` ⇒ no
    position ingestion; Phases 13–16 trajectories byte-identical.
    """

    model_config = _STRICT
    workbook_path: str = Field(min_length=1)
    manifest_path: str = Field(min_length=1)
    manifest_version: str = Field(default="1", min_length=1)

    @field_validator("manifest_version")
    @classmethod
    def _manifest_version_url_safe(cls, v: str) -> str:
        if ":" in v:
            raise ValueError(f"manifest_version must be URL-safe (no colons); got {v!r}")
        return v


# ---- top-level resolved view ------------------------------------------------


class StudyConfig(BaseModel):
    """Resolved view: base config + every sub-config + fixture scenario, all loaded."""

    model_config = _STRICT
    base: BaseConfig
    allocation: PublicAllocationConfig
    cma: CMAConfig
    spending: SpendingConfig
    pe_pacing: PEPacingConfig
    scenarios: ScenariosConfig
    fixture_scenario: FixtureScenarioConfig
    # Phase 13 / L19 producer-side: optional. Default None means
    # "no producer wired" — orchestrator emits zero distribution_inflow
    # rows; Phase 12.5 trajectories byte-identical.
    distribution_producer: DistributionProducerConfig | None = None
    # Phase 14 / L19 workbook-side: optional. Default None means
    # "no workbook ingestion" — orchestrator skips ingestion entirely;
    # Phase 13 trajectories byte-identical.
    workbook_ingestion: WorkbookIngestionConfig | None = None
    # Phase 17 / L20: position ingestion config. Default None means no
    # position ingestion; liquidity coverage diagnostics not computed.
    position_ingestion: PositionIngestionConfig | None = None
    # Phase 17 / L20: near-term obligation inputs for liquidity coverage.
    # Stored as raw dict; validated as LiquidityObligationConfig by the
    # orchestrator at run time. Consumed only when position_ingestion is set.
    liquidity_obligations: dict | None = None
    # Phase 17 / L20: policy thresholds for coverage breach / warning.
    # Stored as raw dict; validated as LiquidityCoverageConfig by the
    # orchestrator at run time. Consumed only when position_ingestion is set.
    liquidity_coverage_config: dict | None = None

    @model_validator(mode="after")
    def _phase12_spending_base_cross_config(self) -> StudyConfig:
        """Phase 12 / L19 cross-config validation for the spending base.

        These checks need both ``cma`` and ``spending.guardrail`` in
        scope, so they live here rather than on either sub-config.
        """
        gr = self.spending.guardrail
        if gr is None:
            return self
        base = gr.spending_base
        if base is None or base == "total_nav":
            return self  # default behavior — no cross-config requirements

        # Phase 12.5 / L19 flow-side: distributable_income reads ledger
        # `distribution_inflow` rows, not CMA tags. The window +
        # bootstrap fields are validated on GuardrailConfig itself; no
        # cross-CMA requirement here. Return early so the cma.liquidity
        # check below does not fire on this mode.
        if base == "distributable_income":
            return self

        cma_buckets = set(self.cma.expected_returns_annual.keys())

        # Any non-total_nav, non-distributable_income base requires CMA
        # liquidity tags covering every bucket. CMAConfig already
        # validates bucket coverage when liquidity is present; here we
        # enforce its presence.
        if self.cma.liquidity is None:
            raise ValueError(
                f"spending.guardrail.spending_base={base!r} requires "
                f"cma.liquidity to be set (covering every bucket)"
            )

        if base == "liquid_plus_income_producing_nav":
            if self.cma.income_producing is None:
                raise ValueError(
                    "spending.guardrail.spending_base="
                    "'liquid_plus_income_producing_nav' requires "
                    "cma.income_producing to be set (covering every bucket)"
                )
            # CMAConfig already enforced bucket-set equality when
            # income_producing is present; no further check needed here.

        if base == "custom_policy":
            weights = gr.spending_base_weights
            assert weights is not None  # GuardrailConfig validator caught this
            unknown = sorted(set(weights.keys()) - cma_buckets)
            if unknown:
                raise ValueError(
                    f"spending_base_weights references unknown CMA bucket(s): "
                    f"{unknown}. Valid buckets: {sorted(cma_buckets)}"
                )

        return self
