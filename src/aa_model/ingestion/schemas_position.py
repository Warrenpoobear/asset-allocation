"""Phase 15 — Investment Summary / Account-Position ingestion schemas.

Pydantic v2 models for the normalized position universe output and the
manifest config that maps the Investment Summary workbook's layout to
the ingestor's parser. Follows the Phase 12-14 discipline: URL-safe ids,
finite non-negative amounts, explicit upstream classification.

Reviewer tightenings codified here:

* **T1 (flat/hierarchical unification)**: ``AccountRecord`` always present
  in normalized output; flat sheets synthesize
  ``account_id = "synthetic:<sheet_id>"``,  ``account_type = "direct"``.
* **T2 (valuation date required after fallback)**: ``PositionRecord.valuation_date``
  is required; ingestor resolves it via fallback chain. Stale-valuation
  diagnostics surface when ``valuation_date`` > 90 days before
  ``PositionManifestConfig.as_of_date``.
* **T3 (Phase15→Phase12 mapping)**: ``PositionManifestConfig.liquidity_tier_overrides``
  holds any non-default bucket→tier overrides; validated against both
  the Phase 15 bucket vocabulary and the Phase 12 tier vocabulary.
* **T4 (income_cash_flow_flag human-authored only)**: field exists on
  ``PositionRecord``; discovery layer may propose candidates in
  local_private mode but never sets the final value.
* **T5 (ManagerTermsRecord confidence/completeness)**: non-``"unknown"``
  confidence requires ``redemption_frequency``, at least one of
  ``fee_basis``/``management_fee_bps``, and at least one of
  ``source_document``/``source_reference``.
"""

from __future__ import annotations

import math
import re as _re
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_STRICT = ConfigDict(extra="forbid")

_FORMULA_CACHE_CAVEAT_TEXT: str = (
    "Investment Summary ingestion uses cached formula values "
    "(openpyxl data_only=True). If the workbook was edited but not "
    "recalculated and saved in Excel, ingested values may be stale. "
    "Open the workbook in Excel, allow it to recalculate, save, "
    "then re-run ingestion before relying on the output."
)

_POSITION_NAV_CAVEAT_TEXT: str = (
    "market_value_usd reflects manager-reported NAV as of each "
    "position's valuation_date. Legal liquidity may differ due to "
    "gates, side pockets, lockups, or notice periods not captured "
    "in ManagerTermsRecord. income_cash_flow_flag reflects "
    "human-authored classification; economic or legal income "
    "availability is not automatically inferred."
)

_LIQUIDITY_BUCKET_LITERAL = Literal[
    "cash_equivalent",  # money market, T-bills, bank sweep — T+0/T+1
    "daily_liquid",  # public equity/ETF/IG bond, T+2 settlement
    "semi_liquid",  # quarterly/annual redemption with notice; HFs
    "illiquid",  # PE/PC/RE funds, lockup > 1 yr, no redemption
    "locked_strategic",  # no near-term exit path; early-vintage PE
    "re_stabilized",  # income-producing RE, monetize ~12-24 mo
    "re_development",  # development stage, monetize 2-4 yr
    "re_land",  # raw land, monetize 3+ yr
    "opco_strategic",  # operating company strategic hold
]

_ASSET_CLASS_LITERAL = Literal[
    "public_equity",
    "fixed_income_public",
    "cash_equivalent",
    "hedge_fund",
    "private_equity",
    "private_credit",
    "real_estate_equity",
    "real_estate_debt",
    "infrastructure",
    "commodity",
    "direct_operating",
    "other",
]

_TERMS_CONFIDENCE_LITERAL = Literal[
    "actual",
    "contractual",
    "estimated",
    "unknown",
]

_ACCOUNT_TYPE_LITERAL = Literal[
    "taxable",
    "tax_deferred",
    "tax_exempt",
    "trust",
    "partnership",
    "direct",
]

_REDEMPTION_FREQUENCY_LITERAL = Literal[
    "daily",
    "monthly",
    "quarterly",
    "semi_annual",
    "annual",
    "none",
]

_DISTRIBUTION_POLICY_LITERAL = Literal[
    "discretionary",
    "mandatory",
    "reinvest",
    "unknown",
]

_FEE_BASIS_LITERAL = Literal[
    "committed",
    "invested",
    "nav",
    "unknown",
]

_LAYOUT_TYPE_POSITION_LITERAL = Literal[
    "flat_position",  # one row per position, no account grouping
    "account_position",  # explicit account header rows above positions
    "display_only",  # aggregate/summary sheet; not parsed for positions
]

_PHASE12_TIER_LITERAL = Literal[
    "liquid",
    "semi_liquid",
    "illiquid",
    "locked_strategic",
]

_VALID_PHASE15_BUCKETS: frozenset[str] = frozenset(
    {
        "cash_equivalent",
        "daily_liquid",
        "semi_liquid",
        "illiquid",
        "locked_strategic",
        "re_stabilized",
        "re_development",
        "re_land",
        "opco_strategic",
    }
)

_VALID_PHASE12_TIERS: frozenset[str] = frozenset(
    {
        "liquid",
        "semi_liquid",
        "illiquid",
        "locked_strategic",
    }
)

_SEMI_ILLIQUID_BUCKETS: frozenset[str] = frozenset({"semi_liquid", "illiquid"})

_URL_SAFE_RE = _re.compile(r"^[A-Za-z0-9_\-\.]+$")
_SYNTHETIC_PREFIX = "synthetic:"


# ---- normalized output records ---------------------------------------------


class AccountRecord(BaseModel):
    """One normalized account row. Always present in output (T1).

    Flat sheets produce ``account_id = "synthetic:<sheet_id>"``
    and ``account_type = "direct"``.
    """

    model_config = _STRICT

    account_id: str
    entity_id: str
    custodian: str = ""
    account_type: _ACCOUNT_TYPE_LITERAL = "direct"
    valuation_date: date
    source_sheet: str = ""  # local-private only

    @field_validator("account_id")
    @classmethod
    def _account_id_valid(cls, v: str) -> str:
        if v.startswith(_SYNTHETIC_PREFIX):
            rest = v[len(_SYNTHETIC_PREFIX) :]
            if not rest:
                raise ValueError(
                    f"synthetic account_id must have a non-empty sheet_id "
                    f"after 'synthetic:'; got {v!r}"
                )
            return v
        if not _URL_SAFE_RE.match(v):
            raise ValueError(
                f"account_id must be URL-safe (alphanumeric, _, -, .) "
                f"or start with 'synthetic:'; got {v!r}"
            )
        return v

    @field_validator("entity_id")
    @classmethod
    def _entity_id_no_colons(cls, v: str) -> str:
        if ":" in v:
            raise ValueError(
                f"entity_id must not contain colons "
                f"(reserved for source convention separator); got {v!r}"
            )
        return v


class PositionRecord(BaseModel):
    """One normalized position row.

    ``market_value_usd >= 0`` always — positions are stocks of value,
    not flows (sign convention differs from ``CashFlowLineRecord``).
    ``valuation_date`` is required; the ingestor resolves it via the
    T2 fallback chain before construction.
    """

    model_config = _STRICT

    position_id: str
    account_id: str
    manager_id: str | None = None
    asset_class: _ASSET_CLASS_LITERAL = "other"
    strategy: str | None = None
    market_value_usd: float
    cost_basis_usd: float | None = None
    unfunded_commitment_usd: float | None = None
    income_cash_flow_flag: bool = False  # T4: human-authored only
    liquidity_bucket: _LIQUIDITY_BUCKET_LITERAL = "illiquid"
    time_horizon_quarters: int | None = None
    valuation_date: date
    source_row: int  # 1-indexed; no label content

    @field_validator("market_value_usd")
    @classmethod
    def _nav_non_negative(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError(f"market_value_usd must be finite; got {v}")
        if v < 0:
            raise ValueError(
                f"market_value_usd must be >= 0 (positions are stocks of "
                f"value, not flows); got {v}"
            )
        return v

    @field_validator("unfunded_commitment_usd")
    @classmethod
    def _commitment_non_negative(cls, v: float | None) -> float | None:
        if v is not None and v < 0:
            raise ValueError(f"unfunded_commitment_usd must be >= 0; got {v}")
        return v

    @field_validator("time_horizon_quarters")
    @classmethod
    def _horizon_non_negative(cls, v: int | None) -> int | None:
        if v is not None and v < 0:
            raise ValueError(f"time_horizon_quarters must be >= 0; got {v}")
        return v


class ManagerTermsRecord(BaseModel):
    """Fund / manager contractual terms. Human-authored in manifest.

    T5: non-``"unknown"`` confidence requires at minimum
    ``redemption_frequency``, one of ``fee_basis``/``management_fee_bps``,
    and one of ``source_document``/``source_reference``.
    Use ``confidence="unknown"`` for placeholder records with no
    documented terms.
    """

    model_config = _STRICT

    manager_id: str
    redemption_frequency: _REDEMPTION_FREQUENCY_LITERAL | None = None
    notice_days: int | None = None
    gate_pct: float | None = None
    side_pocket: bool = False
    lockup_end_date: date | None = None
    capital_call_notice_days: int | None = None
    distribution_policy: _DISTRIBUTION_POLICY_LITERAL = "unknown"
    management_fee_bps: int | None = None
    carry_pct: float | None = None
    hurdle_rate: float | None = None
    fee_basis: _FEE_BASIS_LITERAL = "unknown"
    source_document: str | None = None
    source_reference: str | None = None
    confidence: _TERMS_CONFIDENCE_LITERAL = "unknown"

    @field_validator("gate_pct", "carry_pct", "hurdle_rate")
    @classmethod
    def _rate_range(cls, v: float | None) -> float | None:
        if v is not None and not (0.0 <= v <= 1.0):
            raise ValueError(f"Rate value must be in [0.0, 1.0]; got {v}")
        return v

    @model_validator(mode="after")
    def _confidence_completeness(self) -> ManagerTermsRecord:
        if self.confidence == "unknown":
            return self
        missing: list[str] = []
        if self.redemption_frequency is None:
            missing.append("redemption_frequency")
        if self.fee_basis == "unknown" and self.management_fee_bps is None:
            missing.append("fee_basis or management_fee_bps")
        if self.source_document is None and self.source_reference is None:
            missing.append("source_document or source_reference")
        if missing:
            raise ValueError(
                f"ManagerTermsRecord confidence={self.confidence!r} requires "
                f"these fields to be populated: {missing}. "
                f"Use confidence='unknown' for placeholder records."
            )
        return self


# ---- manifest config -------------------------------------------------------


class AccountSheetSpec(BaseModel):
    """Manifest declaration for one sheet in the Investment Summary."""

    model_config = _STRICT

    account_id: str
    entity_id: str
    sheet_name: str  # local-private only; placeholder in committed scaffold
    layout_type: _LAYOUT_TYPE_POSITION_LITERAL = "flat_position"
    header_row_index: int | None = None  # 0-indexed; None → auto-detect
    value_column_index: int = 1  # 0-indexed column for market_value_usd
    name_column_index: int = 0  # 0-indexed column A default
    position_column_mappings: dict[str, int] = Field(default_factory=dict)
    valuation_date: date | None = None  # T2 fallback before manifest as_of_date

    @field_validator("account_id")
    @classmethod
    def _account_id_valid(cls, v: str) -> str:
        if v.startswith(_SYNTHETIC_PREFIX):
            return v
        if not _URL_SAFE_RE.match(v):
            raise ValueError(
                f"account_id must be URL-safe or start with 'synthetic:'; " f"got {v!r}"
            )
        return v


class PositionManifestConfig(BaseModel):
    """Top-level manifest for the Investment Summary workbook."""

    model_config = _STRICT

    manifest_version: str
    workbook_version: str
    expected_filename: str = ""
    as_of_date: date
    accounts: list[AccountSheetSpec] = Field(default_factory=list)
    manager_terms: list[ManagerTermsRecord] = Field(default_factory=list)
    liquidity_tier_overrides: dict[str, str] | None = None  # T3

    @field_validator("manifest_version", "workbook_version")
    @classmethod
    def _url_safe(cls, v: str) -> str:
        if not _URL_SAFE_RE.match(v):
            raise ValueError(f"Version must be URL-safe (alphanumeric, _, -, .); got {v!r}")
        return v

    @model_validator(mode="after")
    def _unique_account_ids(self) -> PositionManifestConfig:
        seen: set[str] = set()
        for spec in self.accounts:
            if spec.account_id in seen:
                raise ValueError(f"Duplicate account_id in manifest: {spec.account_id!r}")
            seen.add(spec.account_id)
        return self

    @model_validator(mode="after")
    def _unique_manager_ids(self) -> PositionManifestConfig:
        seen: set[str] = set()
        for terms in self.manager_terms:
            if terms.manager_id in seen:
                raise ValueError(f"Duplicate manager_id in manifest: {terms.manager_id!r}")
            seen.add(terms.manager_id)
        return self

    @model_validator(mode="after")
    def _valid_liquidity_overrides(self) -> PositionManifestConfig:
        if self.liquidity_tier_overrides is None:
            return self
        for bucket, tier in self.liquidity_tier_overrides.items():
            if bucket not in _VALID_PHASE15_BUCKETS:
                raise ValueError(
                    f"liquidity_tier_overrides key {bucket!r} is not a "
                    f"valid Phase 15 liquidity_bucket. "
                    f"Valid: {sorted(_VALID_PHASE15_BUCKETS)}"
                )
            if tier not in _VALID_PHASE12_TIERS:
                raise ValueError(
                    f"liquidity_tier_overrides value {tier!r} is not a "
                    f"valid Phase 12 liquidity tier. "
                    f"Valid: {sorted(_VALID_PHASE12_TIERS)}"
                )
        return self


# ---- diagnostics -----------------------------------------------------------


@dataclass
class PositionIngestionDiagnostics:
    workbook_hash: str
    workbook_version: str
    manifest_version: str
    formula_cache_caveat: str = field(default=_FORMULA_CACHE_CAVEAT_TEXT)
    position_nav_caveat: str = field(default=_POSITION_NAV_CAVEAT_TEXT)
    positions_total: int = 0
    positions_by_bucket: dict[str, int] = field(default_factory=dict)
    positions_by_asset_class: dict[str, int] = field(default_factory=dict)
    positions_missing_bucket: int = 0
    positions_missing_manager: int = 0
    unfunded_total_usd: float = 0.0
    manager_terms_coverage: dict[str, str] = field(default_factory=dict)
    positions_with_incomplete_terms: list[str] = field(default_factory=list)
    position_terms_status: dict[str, int] = field(default_factory=dict)
    positions_with_fallback_valuation_date: int = 0
    stale_valuation_count: int = 0
    max_valuation_age_days: int = 0
    unmatched_rows: list[int] = field(default_factory=list)


@dataclass
class PositionIngestionResult:
    accounts: list[AccountRecord]
    positions: list[PositionRecord]
    diagnostics: PositionIngestionDiagnostics
