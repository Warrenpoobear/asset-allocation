"""Pydantic v2 models for every config the package consumes.

All inputs are validated through one of these models before any engine fires.
Validation failures are loud per SPEC §2.2. Unknown keys raise via
``extra='forbid'``.
"""

from __future__ import annotations

import re
from typing import Literal

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


class RebalanceConfig(BaseModel):
    model_config = _STRICT
    frequency: Literal["quarterly"]


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
    # Phase 3a behind an opt-in flag. New engines extend this Literal.
    engine: Literal["stub", "riskfolio"]
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


class FundConfig(BaseModel):
    model_config = _STRICT
    name: str
    commitment_usd: float = Field(gt=0.0)
    vintage: str
    sleeve: str

    @field_validator("vintage")
    @classmethod
    def _check_vintage(cls, v: str) -> str:
        if not QUARTER_RE.match(v):
            raise ValueError(f"vintage must match YYYYQN, got {v!r}")
        return v


class PEPacingConfig(BaseModel):
    model_config = _STRICT
    ta_defaults: TADefaultsConfig
    funds: list[FundConfig]


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
    spending: SpendingConfig
    pe_pacing: PEPacingConfig
    scenarios: ScenariosConfig
    fixture_scenario: FixtureScenarioConfig
