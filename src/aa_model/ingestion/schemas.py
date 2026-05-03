"""Phase 14 / L19 — workbook ingestion schemas.

Pydantic v2 models for the normalized ingestion output and the
manifest config that maps the workbook's structural layout to the
ingestor's parser. All schemas follow the Phase 12-12.5-13 discipline:
URL-safe ids, finite amounts, no colons in identifiers (reserved for
the Phase 13 source-convention separator), explicit upstream
classification.

Phase 14 reviewer tightenings codified here:

* **RT1 (stale formula cache)**: ``IngestionDiagnostics.formula_cache_caveat``
  carries a standing advisory string surfaced into the report.
* **RT2 (workbook_version required + URL-safe; producer_id uses
  version, not hash)**: ``WorkbookManifestConfig.workbook_version``
  is required + URL-safe-validated; the bridge function in
  ``workbook.py`` uses it as the producer_id prefix. Hash is
  captured separately for provenance only.
* **RT3 (reconciliation advisory only)**: deltas surface as
  diagnostic entries, never raise.
* **RT4 (synthetic-fixture-only tests)**: discipline is enforced by
  the test layer; this module has no test-specific code.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_STRICT = ConfigDict(extra="forbid")


_FORMULA_CACHE_CAVEAT_TEXT: str = (
    "Workbook ingestion uses cached formula values "
    "(openpyxl data_only=True). If the workbook was edited but not "
    "recalculated and saved in Excel, ingested values may be stale. "
    "Open the workbook in Excel, allow it to recalculate, save, "
    "then re-run ingestion before relying on the output."
)


_ENTITY_TYPE_LITERAL = Literal[
    "operating_llc",
    "holding_llc",
    "trust_crut",
    "trust_family",
    "trust_gift",
    "trust_gst",
    "individual_account",
    "real_estate_partnership",
    "opco",
    "family_aggregate",
]


_DOMAIN_LITERAL = Literal[
    "real_estate",
    "opco",
    "land",
    "development",
    "portfolio",
    "entity",
]


_RECURRENCE_LITERAL = Literal["recurring", "one_time", "unknown"]
_CERTAINTY_LITERAL = Literal["actual", "contractual", "forecast", "scenario"]
_DIRECTION_LITERAL = Literal["inflow", "outflow"]
_CASH_FLOW_ROLE_LITERAL = Literal["operating", "investment", "distribution_only"]
_PERIOD_HEADER_LITERAL = Literal["yyyy_q", "q_yy", "q_yyyy", "calendar_qe"]


# ---- normalized output schemas ---------------------------------------------


class EntityRecord(BaseModel):
    """One normalized entity row produced by the ingestor.

    Carries STRUCTURAL metadata only (type, parent, cash-flow role,
    provenance). Distributability is per-line, not per-entity — see
    :class:`CashFlowLineRecord.distributable_candidate`.
    """

    model_config = _STRICT
    entity_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    entity_type: _ENTITY_TYPE_LITERAL
    parent_entity_id: str | None = None
    cash_flow_role: _CASH_FLOW_ROLE_LITERAL
    source_sheet: str = Field(min_length=1)
    source_workbook: str = Field(min_length=1)

    @field_validator("entity_id", "parent_entity_id")
    @classmethod
    def _no_colons(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if ":" in v:
            raise ValueError(
                f"entity_id may not contain colons; reserved for the Phase 13 "
                f"source-convention separator. got {v!r}"
            )
        return v


class CashFlowLineRecord(BaseModel):
    """One normalized cash-flow line — a single quarter's amount on a
    single row label of a single entity sheet.

    Sign convention: ``direction == "inflow"`` requires
    ``amount_usd >= 0``; ``direction == "outflow"`` requires
    ``amount_usd <= 0``. Violations fail at construction.
    """

    model_config = _STRICT
    source_workbook: str = Field(min_length=1)
    sheet_name: str = Field(min_length=1)
    row_label: str = Field(min_length=1)
    entity_id: str = Field(min_length=1)
    quarter: str = Field(pattern=r"^\d{4}Q[1-4]$")
    amount_usd: float
    category: str = Field(min_length=1)
    direction: _DIRECTION_LITERAL
    certainty: _CERTAINTY_LITERAL
    recurrence_type: _RECURRENCE_LITERAL = "unknown"
    distributable_candidate: bool = False
    restricted: bool = False
    source_reference: str | None = None

    @field_validator("amount_usd")
    @classmethod
    def _amount_finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError(f"amount_usd must be finite; got {v!r}")
        return v

    @model_validator(mode="after")
    def _direction_sign_consistent(self) -> CashFlowLineRecord:
        # Sign convention enforced at construction. A workbook line
        # with mismatched sign + direction is a classification error
        # the ingestor surfaces immediately.
        if self.direction == "inflow" and self.amount_usd < 0:
            raise ValueError(
                f"direction='inflow' requires amount_usd >= 0; got {self.amount_usd}"
            )
        if self.direction == "outflow" and self.amount_usd > 0:
            raise ValueError(
                f"direction='outflow' requires amount_usd <= 0; got {self.amount_usd}"
            )
        return self


# ---- manifest schema -------------------------------------------------------


class RowClassificationRule(BaseModel):
    """Manifest-side classification rule for a workbook row label.

    The workbook has free-text row labels (e.g., "Rent Collected",
    "Distribution to FO", "Tax Payment"). The manifest maps those
    labels to the strict :class:`CashFlowLineRecord` classification
    fields. The ingestor matches each row's label against the rules
    in order; the first matching rule supplies the classification.
    Rows that match no rule are still emitted as
    :class:`CashFlowLineRecord` (with default classification:
    direction inferred from sign, category="unknown",
    distributable_candidate=False) but never become producer
    candidates.
    """

    model_config = _STRICT
    row_label_pattern: str = Field(min_length=1)
    direction: _DIRECTION_LITERAL
    category: str = Field(min_length=1)
    domain: _DOMAIN_LITERAL | None = None
    distributable_candidate: bool = False
    restricted: bool = False
    recurrence_type: _RECURRENCE_LITERAL = "unknown"
    certainty: _CERTAINTY_LITERAL = "forecast"

    @model_validator(mode="after")
    def _distributable_requires_domain(self) -> RowClassificationRule:
        # A rule that marks rows as distributable_candidate=True must
        # also supply a domain — otherwise the producer bridge has no
        # way to construct a source string that satisfies the Phase
        # 12.5 source convention (distribution:<domain>:<id>).
        if self.distributable_candidate and self.domain is None:
            raise ValueError(
                "distributable_candidate=True requires domain to be set "
                "(needed for the Phase 12.5 source convention)"
            )
        return self


class EntitySheetSpec(BaseModel):
    """Manifest spec for a single entity-level workbook sheet."""

    model_config = _STRICT
    sheet_name: str = Field(min_length=1)
    entity_id: str = Field(min_length=1)
    entity_type: _ENTITY_TYPE_LITERAL
    display_name: str = Field(min_length=1)
    parent_entity_id: str | None = None
    cash_flow_role: _CASH_FLOW_ROLE_LITERAL
    row_classification_rules: list[RowClassificationRule] = Field(default_factory=list)

    @field_validator("entity_id", "parent_entity_id")
    @classmethod
    def _no_colons(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if ":" in v:
            raise ValueError(
                f"entity_id may not contain colons; reserved for the Phase 13 "
                f"source-convention separator. got {v!r}"
            )
        return v


class REPartnershipSheetSpec(EntitySheetSpec):
    """Real-estate partnership sheet spec.

    Adds per-row asset_id mapping so the producer bridge can use a
    finer identifier than entity_id for stabilized-RE distributions.
    """

    asset_id_by_row_label: dict[str, str] = Field(default_factory=dict)

    @field_validator("asset_id_by_row_label")
    @classmethod
    def _asset_ids_url_safe(cls, v: dict[str, str]) -> dict[str, str]:
        for label, asset_id in v.items():
            if ":" in asset_id:
                raise ValueError(
                    f"asset_id may not contain colons (reserved for source "
                    f"convention separator); got {asset_id!r} for row label "
                    f"{label!r}"
                )
        return v


class WorkbookManifestConfig(BaseModel):
    """Maps the workbook's structural layout to the ingestor's parser.

    The manifest is committed; the workbook is not (PROJECT_SCOPE.md
    §5.3). Phase 14 reviewer tightening 2: ``workbook_version`` is
    REQUIRED and URL-safe; it anchors deterministic producer_ids.
    """

    model_config = _STRICT
    workbook_version: str = Field(min_length=1)
    expected_workbook_filename: str = Field(min_length=1)

    family_aggregate_sheets: list[str] = Field(default_factory=list)
    entity_sheets: list[EntitySheetSpec] = Field(default_factory=list)
    re_partnership_sheets: list[REPartnershipSheetSpec] = Field(default_factory=list)
    board_snapshot_sheets: list[str] = Field(default_factory=list)

    period_header_format: _PERIOD_HEADER_LITERAL = "yyyy_q"
    subtotal_label_patterns: list[str] = Field(
        default_factory=lambda: [
            "total",
            "subtotal",
            "sum",
            "grand total",
            "net cash",
        ]
    )

    @field_validator("workbook_version")
    @classmethod
    def _version_url_safe(cls, v: str) -> str:
        # Phase 14 reviewer tightening 2: workbook_version anchors
        # deterministic producer_ids of the form
        # f"{workbook_version}__{sheet}__{row_label}__{quarter}".
        # Colons are reserved for the Phase 12.5 source-convention
        # separator (distribution:<domain>:<id>). Reject them here so
        # producer_ids never silently corrupt downstream rollups.
        if ":" in v:
            raise ValueError(
                f"workbook_version must be URL-safe (no colons); got {v!r}"
            )
        return v

    @model_validator(mode="after")
    def _entity_ids_globally_unique(self) -> WorkbookManifestConfig:
        ids: list[str] = [s.entity_id for s in self.entity_sheets]
        ids += [s.entity_id for s in self.re_partnership_sheets]
        if len(ids) != len(set(ids)):
            dups = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(
                f"WorkbookManifestConfig: entity_id must be globally unique "
                f"across entity_sheets + re_partnership_sheets; "
                f"duplicates: {dups}"
            )
        return self

    @model_validator(mode="after")
    def _sheet_names_globally_unique(self) -> WorkbookManifestConfig:
        names: list[str] = list(self.family_aggregate_sheets)
        names += [s.sheet_name for s in self.entity_sheets]
        names += [s.sheet_name for s in self.re_partnership_sheets]
        names += list(self.board_snapshot_sheets)
        if len(names) != len(set(names)):
            dups = sorted({n for n in names if names.count(n) > 1})
            raise ValueError(
                f"WorkbookManifestConfig: sheet names must be globally unique "
                f"across all role buckets; duplicates: {dups}"
            )
        return self


# ---- diagnostics + result --------------------------------------------------


@dataclass
class IngestionDiagnostics:
    """Run-level ingestion diagnostics. Populated by the ingestor;
    surfaced via the report's ## Workbook ingestion (advisory) section.
    """

    workbook_hash: str = ""
    workbook_filename: str = ""
    workbook_version: str = ""
    manifest_version: str = ""
    sheets_ingested: list[str] = field(default_factory=list)
    unmapped_sheets: list[str] = field(default_factory=list)
    missing_optional_sheets: list[str] = field(default_factory=list)
    blank_rows_skipped: int = 0
    excluded_subtotal_rows: int = 0
    unparseable_period_headers: list[str] = field(default_factory=list)
    stale_formula_warnings: list[str] = field(default_factory=list)

    # Reconciliation against family-aggregate / board-snapshot tabs.
    # Phase 14 reviewer tightening 3: ADVISORY ONLY. Each entry:
    # (snapshot_label, snapshot_total_usd, ingestor_total_usd,
    # abs_delta_usd, abs_delta_pct).
    board_snapshot_reconciliations: list[tuple[str, float, float, float, float]] = field(
        default_factory=list
    )

    # Per-entity totals over the run horizon.
    total_inflows_usd_by_entity: dict[str, float] = field(default_factory=dict)
    total_outflows_usd_by_entity: dict[str, float] = field(default_factory=dict)

    # Distribution-candidate breakdown.
    distribution_candidates_by_domain_usd: dict[str, float] = field(default_factory=dict)
    distribution_candidates_count: int = 0
    excluded_restricted_count: int = 0
    excluded_restricted_usd: float = 0.0

    # Lines that didn't match any classification rule.
    unmatched_lines_count: int = 0
    unmatched_lines_sample: list[str] = field(default_factory=list)

    # Phase 14 reviewer tightening 1: standing CAVEAT for cached-
    # formula stale-state risk. Always populated on any ingestion run.
    formula_cache_caveat: str = _FORMULA_CACHE_CAVEAT_TEXT


# ---- ingestion result ------------------------------------------------------


# IngestionResult is intentionally NOT frozen — its lists / records
# are immutable Pydantic models / frozen dataclasses, but the result
# itself is constructed mutably by the ingestor as it processes
# sheets and finalized at end of ingestion.
@dataclass
class IngestionResult:
    entities: list[EntityRecord] = field(default_factory=list)
    cash_flow_lines: list[CashFlowLineRecord] = field(default_factory=list)
    diagnostics: IngestionDiagnostics = field(default_factory=IngestionDiagnostics)
