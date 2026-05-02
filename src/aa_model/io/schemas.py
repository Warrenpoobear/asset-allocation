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
    liquidity: dict[str, Literal["liquid", "semi_liquid", "illiquid"]] | None = None

    @field_validator("expected_returns_annual")
    @classmethod
    def _er_per_cell(cls, v: dict[str, float]) -> dict[str, float]:
        if not v:
            raise ValueError("expected_returns_annual must be non-empty")
        for bucket, x in v.items():
            xf = float(x)
            if not math.isfinite(xf):
                raise ValueError(
                    f"expected_returns_annual[{bucket!r}] = {x!r} is not finite"
                )
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
    def _corr_per_cell(
        cls, v: dict[str, dict[str, float]]
    ) -> dict[str, dict[str, float]]:
        if not v:
            raise ValueError("correlations must be non-empty")
        outer_buckets = set(v.keys())
        for i, row in v.items():
            if set(row.keys()) != outer_buckets:
                missing = sorted(outer_buckets - set(row.keys()))
                extra = sorted(set(row.keys()) - outer_buckets)
                raise ValueError(
                    f"correlations[{i!r}] keys mismatch — "
                    f"missing: {missing}, extra: {extra}"
                )
            for j, x in row.items():
                xf = float(x)
                if not math.isfinite(xf):
                    raise ValueError(
                        f"correlations[{i!r}][{j!r}] = {x!r} is not finite"
                    )
                if abs(xf) > _CORR_BOUND + _NUMERIC_TOLERANCE:
                    raise ValueError(
                        f"correlations[{i!r}][{j!r}] = {xf} out of [-1, 1]"
                    )
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
            raise ValueError(
                f"liquidity bucket set mismatch — missing: {missing}, extra: {extra}"
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
    def _matrix_well_formed(
        cls, v: dict[str, dict[str, float]]
    ) -> dict[str, dict[str, float]]:
        if not v:
            raise ValueError("correlation_shock.override.matrix must be non-empty")
        for i, row in v.items():
            for j, x in row.items():
                xf = float(x)
                if not math.isfinite(xf):
                    raise ValueError(
                        f"correlation_shock.override.matrix[{i!r}][{j!r}] = "
                        f"{x!r} is not finite"
                    )
                if abs(xf) > _CORR_BOUND + _NUMERIC_TOLERANCE:
                    raise ValueError(
                        f"correlation_shock.override.matrix[{i!r}][{j!r}] = "
                        f"{xf} out of [-1, 1]"
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
    strategy: Literal[
        "buyout",
        "venture",
        "growth",
        "credit",
        "real_estate",
        "infra",
        "secondary",
    ] | None = None
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
                f"pe_pacing.funds: name must be globally unique; "
                f"duplicates: {dups}"
            )

        # Phase 9: globally-unique fund_id when set on any fund.
        ids = [f.fund_id for f in self.funds if f.fund_id is not None]
        if len(ids) != len(set(ids)):
            dups = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(
                f"pe_pacing.funds: fund_id must be globally unique when set; "
                f"duplicates: {dups}"
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
