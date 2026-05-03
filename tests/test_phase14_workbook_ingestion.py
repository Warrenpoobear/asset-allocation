"""Phase 14 / L19 — workbook ingestion tests.

12 tests across schema, workbook reading, validation rules, bridge to
Phase 13, and end-to-end report rendering. See MODEL_DOCUMENTATION.md
§Phase 14 design.

DISCIPLINE — Phase 14 reviewer tightening 4
============================================

Tests use SYNTHETIC workbook fixtures only. Each test constructs a
minimal openpyxl workbook in tmp_path via the openpyxl write API and
exercises the ingestor against it. NO real workbook rows, live
values, sheet extracts, person names, or entity names committed.
The real Cashflow Modeling v7.xlsx is used for local validation only.

Schema (3):
1. EntityRecord rejects entity_id with colons; full entity_type Literal accepted.
2. CashFlowLineRecord sign convention + finite amount.
3. WorkbookManifestConfig: workbook_version URL-safe, entity_id /
   sheet uniqueness, distributable_candidate requires domain.

Workbook reading (3):
4. Missing workbook path → FileNotFoundError with resolved path.
5. Synthetic fixture parses deterministically; SHA256 hash stable
   across repeated reads.
6. Period header normalization: each supported format parses; bad
   formats land in unparseable_period_headers.

Validation rules (3):
7. Subtotal rows excluded by manifest patterns.
8. Restricted rows excluded from producer bridge; counted in
   excluded_restricted_count.
9. workbook_hash + manifest_version captured in diagnostics.

Bridge to Phase 13 (2):
10. Distributable rows become DistributionEntryConfig entries with
    deterministic producer_id (uses workbook_version, NOT hash).
11. End-to-end ingestion → producer → Owl distributable_income runs
    without zero-income guard firing.

End-to-end (1):
12. Report renders ## Workbook ingestion (advisory) with all
    sub-sections + standing CAVEAT line.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pandas as pd
import pytest
from aa_model.ingestion.schemas import (
    CashFlowLineRecord,
    EntityRecord,
    EntitySheetSpec,
    REPartnershipSheetSpec,
    RowClassificationRule,
    WorkbookManifestConfig,
)
from aa_model.ingestion.workbook import (
    _parse_period_header,
    ingest_workbook,
    workbook_lines_to_producer_config,
)
from pydantic import ValidationError

# ---- synthetic workbook builder --------------------------------------------


def _build_synthetic_workbook(
    path: Path,
    *,
    sheets: dict[str, list[list[object]]],
) -> None:
    """Construct a minimal openpyxl workbook at ``path`` with the
    given sheets. ``sheets`` maps sheet_name → list of rows; each row
    is a list of cell values. The first row is treated as the header
    row by the ingestor.

    Synthetic discipline: no real workbook data; row labels and
    amounts are arbitrary placeholders chosen to exercise specific
    parser branches. Person names and live entity identifiers are
    NEVER used.
    """
    from openpyxl import Workbook

    wb = Workbook()
    # Remove the default empty sheet.
    default = wb.active
    wb.remove(default)
    for sheet_name, rows in sheets.items():
        ws = wb.create_sheet(title=sheet_name)
        for row in rows:
            ws.append(row)
    wb.save(str(path))


def _minimal_manifest(*, workbook_filename: str = "synthetic_v1.xlsx") -> WorkbookManifestConfig:
    return WorkbookManifestConfig(
        workbook_version="v1",
        expected_workbook_filename=workbook_filename,
        family_aggregate_sheets=[],
        entity_sheets=[],
        re_partnership_sheets=[],
        board_snapshot_sheets=[],
    )


# ---- 1-3. Schema-level validation ------------------------------------------


def test_entity_record_validation():
    """Phase 14 #1: entity_id URL-safe; entity_type Literal."""
    rec = EntityRecord(
        entity_id="entity_a",
        display_name="Entity A",
        entity_type="operating_llc",
        cash_flow_role="operating",
        source_sheet="EntityA",
        source_workbook="synthetic_v1.xlsx",
    )
    assert rec.entity_id == "entity_a"

    # Reject colons in entity_id.
    with pytest.raises(ValidationError, match="entity_id may not contain colons"):
        EntityRecord(
            entity_id="bad:id",
            display_name="X",
            entity_type="operating_llc",
            cash_flow_role="operating",
            source_sheet="X",
            source_workbook="x.xlsx",
        )

    # All entity_type Literal values accepted.
    for et in (
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
    ):
        EntityRecord(
            entity_id=f"e_{et}",
            display_name=et,
            entity_type=et,
            cash_flow_role="operating",
            source_sheet="X",
            source_workbook="x.xlsx",
        )


def test_cash_flow_line_sign_convention_and_finite():
    """Phase 14 #2: inflow/outflow sign mapping + finite amount."""
    # Valid inflow (positive).
    line = CashFlowLineRecord(
        source_workbook="x.xlsx",
        sheet_name="EntityA",
        row_label="Rent",
        entity_id="entity_a",
        quarter="2026Q1",
        amount_usd=100_000.0,
        category="rent",
        direction="inflow",
        certainty="contractual",
    )
    assert line.direction == "inflow"

    # Valid outflow (negative).
    line2 = CashFlowLineRecord(
        source_workbook="x.xlsx",
        sheet_name="EntityA",
        row_label="Tax",
        entity_id="entity_a",
        quarter="2026Q1",
        amount_usd=-50_000.0,
        category="tax",
        direction="outflow",
        certainty="forecast",
    )
    assert line2.direction == "outflow"

    # Inflow + negative amount fails.
    with pytest.raises(ValidationError, match="direction='inflow' requires"):
        CashFlowLineRecord(
            source_workbook="x.xlsx",
            sheet_name="X",
            row_label="X",
            entity_id="x",
            quarter="2026Q1",
            amount_usd=-1.0,
            category="x",
            direction="inflow",
            certainty="forecast",
        )
    # Outflow + positive amount fails.
    with pytest.raises(ValidationError, match="direction='outflow' requires"):
        CashFlowLineRecord(
            source_workbook="x.xlsx",
            sheet_name="X",
            row_label="X",
            entity_id="x",
            quarter="2026Q1",
            amount_usd=+1.0,
            category="x",
            direction="outflow",
            certainty="forecast",
        )

    # Non-finite amount fails.
    with pytest.raises(ValidationError, match="amount_usd must be finite"):
        CashFlowLineRecord(
            source_workbook="x.xlsx",
            sheet_name="X",
            row_label="X",
            entity_id="x",
            quarter="2026Q1",
            amount_usd=float("inf"),
            category="x",
            direction="inflow",
            certainty="forecast",
        )


def test_manifest_validators():
    """Phase 14 #3: workbook_version URL-safe; entity_id + sheet
    uniqueness; distributable_candidate requires domain."""
    # workbook_version with colon → fail.
    with pytest.raises(ValidationError, match="workbook_version must be URL-safe"):
        WorkbookManifestConfig(
            workbook_version="v:1",
            expected_workbook_filename="x.xlsx",
        )

    # Duplicate entity_id across entity_sheets + re_partnership_sheets → fail.
    with pytest.raises(ValidationError, match="entity_id must be globally unique"):
        WorkbookManifestConfig(
            workbook_version="v1",
            expected_workbook_filename="x.xlsx",
            entity_sheets=[
                EntitySheetSpec(
                    sheet_name="A",
                    entity_id="dup",
                    entity_type="operating_llc",
                    display_name="A",
                    cash_flow_role="operating",
                ),
            ],
            re_partnership_sheets=[
                REPartnershipSheetSpec(
                    sheet_name="B",
                    entity_id="dup",
                    entity_type="real_estate_partnership",
                    display_name="B",
                    cash_flow_role="operating",
                ),
            ],
        )

    # Duplicate sheet name across roles → fail.
    with pytest.raises(ValidationError, match="cross-role duplicates"):
        WorkbookManifestConfig(
            workbook_version="v1",
            expected_workbook_filename="x.xlsx",
            family_aggregate_sheets=["Summary"],
            entity_sheets=[
                EntitySheetSpec(
                    sheet_name="Summary",
                    entity_id="e1",
                    entity_type="operating_llc",
                    display_name="E1",
                    cash_flow_role="operating",
                ),
            ],
        )

    # distributable_candidate=True without domain → fail at the rule.
    with pytest.raises(ValidationError, match="distributable_candidate=True requires domain"):
        RowClassificationRule(
            row_label_pattern="rent",
            direction="inflow",
            category="rent",
            distributable_candidate=True,
        )


# ---- 4-6. Workbook reading -------------------------------------------------


def test_missing_workbook_raises_with_resolved_path(tmp_path):
    """Phase 14 #4: missing path → FileNotFoundError + resolved path."""
    bogus = tmp_path / "nope.xlsx"
    with pytest.raises(FileNotFoundError, match="workbook not found at resolved path"):
        ingest_workbook(bogus, _minimal_manifest())


def test_synthetic_fixture_parses_deterministically(tmp_path):
    """Phase 14 #5: same workbook bytes → same hash, same row counts."""
    wb_path = tmp_path / "synthetic_v1.xlsx"
    _build_synthetic_workbook(
        wb_path,
        sheets={
            "EntityA": [
                ["Item", "2026Q1", "2026Q2", "2026Q3", "2026Q4"],
                ["Rent collected", 100_000, 100_000, 100_000, 100_000],
                ["Tax payment", -10_000, -10_000, -10_000, -10_000],
            ],
        },
    )
    manifest = WorkbookManifestConfig(
        workbook_version="v1",
        expected_workbook_filename="synthetic_v1.xlsx",
        entity_sheets=[
            EntitySheetSpec(
                sheet_name="EntityA",
                entity_id="entity_a",
                entity_type="operating_llc",
                display_name="Entity A",
                cash_flow_role="operating",
                row_classification_rules=[
                    RowClassificationRule(
                        row_label_pattern="rent collected",
                        direction="inflow",
                        category="rent",
                        domain="real_estate",
                        distributable_candidate=True,
                        recurrence_type="recurring",
                        certainty="contractual",
                    ),
                    RowClassificationRule(
                        row_label_pattern="tax payment",
                        direction="outflow",
                        category="tax",
                        recurrence_type="recurring",
                        certainty="contractual",
                    ),
                ],
            ),
        ],
    )
    a = ingest_workbook(wb_path, manifest)
    b = ingest_workbook(wb_path, manifest)
    assert a.diagnostics.workbook_hash == b.diagnostics.workbook_hash
    assert len(a.cash_flow_lines) == len(b.cash_flow_lines) == 8
    # Hash matches a manual recompute over the bytes on disk.
    h = hashlib.sha256()
    h.update(wb_path.read_bytes())
    assert a.diagnostics.workbook_hash == h.hexdigest()


def test_period_header_normalization():
    """Phase 14 #6: each supported format parses; bad formats fail
    cleanly via the parser primitive."""
    # yyyy_q
    assert _parse_period_header("2026Q1", "yyyy_q") == "2026Q1"
    assert _parse_period_header(" 2026 Q1 ", "yyyy_q") == "2026Q1"
    # q_yy (with curly apostrophe variant)
    assert _parse_period_header("Q1'26", "q_yy") == "2026Q1"
    assert _parse_period_header("Q1’26", "q_yy") == "2026Q1"
    # q_yyyy
    assert _parse_period_header("Q1 2026", "q_yyyy") == "2026Q1"
    assert _parse_period_header("Q1-2026", "q_yyyy") == "2026Q1"
    # calendar_qe
    assert _parse_period_header(pd.Timestamp("2026-03-31"), "calendar_qe") == "2026Q1"
    assert _parse_period_header(pd.Timestamp("2026-12-31"), "calendar_qe") == "2026Q4"
    # Unparseable input returns None.
    assert _parse_period_header("not-a-quarter", "yyyy_q") is None
    assert _parse_period_header(None, "yyyy_q") is None
    assert _parse_period_header("", "yyyy_q") is None


# ---- 7-9. Validation rules -------------------------------------------------


def test_subtotal_rows_excluded(tmp_path):
    """Phase 14 #7: rows whose label hits subtotal_label_patterns are
    excluded from cash_flow_lines and counted in diagnostics."""
    wb_path = tmp_path / "synthetic_v1.xlsx"
    _build_synthetic_workbook(
        wb_path,
        sheets={
            "EntityA": [
                ["Item", "2026Q1"],
                ["Rent collected", 100_000],
                ["Total operating cash", 100_000],  # subtotal — excluded
                ["Subtotal: Q1", 50_000],  # subtotal — excluded
                ["Tax payment", -10_000],
            ],
        },
    )
    manifest = WorkbookManifestConfig(
        workbook_version="v1",
        expected_workbook_filename="synthetic_v1.xlsx",
        entity_sheets=[
            EntitySheetSpec(
                sheet_name="EntityA",
                entity_id="entity_a",
                entity_type="operating_llc",
                display_name="Entity A",
                cash_flow_role="operating",
                row_classification_rules=[
                    RowClassificationRule(
                        row_label_pattern="rent collected",
                        direction="inflow",
                        category="rent",
                        domain="real_estate",
                        distributable_candidate=True,
                        recurrence_type="recurring",
                        certainty="contractual",
                    ),
                    RowClassificationRule(
                        row_label_pattern="tax payment",
                        direction="outflow",
                        category="tax",
                        recurrence_type="recurring",
                        certainty="contractual",
                    ),
                ],
            ),
        ],
    )
    result = ingest_workbook(wb_path, manifest)
    # Two data rows kept (rent + tax); two subtotal rows excluded.
    assert len(result.cash_flow_lines) == 2
    assert result.diagnostics.excluded_subtotal_rows == 2


def test_restricted_rows_excluded_from_producer_bridge(tmp_path):
    """Phase 14 #8: a restricted row marked distributable_candidate=True
    in the rule does NOT become a DistributionEntryConfig entry."""
    wb_path = tmp_path / "synthetic_v1.xlsx"
    _build_synthetic_workbook(
        wb_path,
        sheets={
            "EntityA": [
                ["Item", "2026Q1"],
                ["Rent collected (open)", 100_000],  # ungated
                ["Rent collected (restricted)", 80_000],  # restricted=True via rule
            ],
        },
    )
    manifest = WorkbookManifestConfig(
        workbook_version="v1",
        expected_workbook_filename="synthetic_v1.xlsx",
        entity_sheets=[
            EntitySheetSpec(
                sheet_name="EntityA",
                entity_id="entity_a",
                entity_type="operating_llc",
                display_name="Entity A",
                cash_flow_role="operating",
                row_classification_rules=[
                    RowClassificationRule(
                        row_label_pattern="restricted",
                        direction="inflow",
                        category="rent",
                        domain="real_estate",
                        distributable_candidate=True,
                        restricted=True,
                        recurrence_type="recurring",
                        certainty="contractual",
                    ),
                    RowClassificationRule(
                        row_label_pattern="rent collected",
                        direction="inflow",
                        category="rent",
                        domain="real_estate",
                        distributable_candidate=True,
                        restricted=False,
                        recurrence_type="recurring",
                        certainty="contractual",
                    ),
                ],
            ),
        ],
    )
    result = ingest_workbook(wb_path, manifest)
    bridged = workbook_lines_to_producer_config(result, manifest)
    # Both lines are emitted as cash-flow records; only the open one
    # bridges to the producer.
    assert len(result.cash_flow_lines) == 2
    assert len(bridged.entries) == 1
    assert "open" in result.cash_flow_lines[0].row_label.lower()
    # Restricted exclusion captured in diagnostics.
    assert result.diagnostics.excluded_restricted_count == 1
    assert result.diagnostics.excluded_restricted_usd == pytest.approx(80_000.0)


def test_workbook_hash_and_manifest_version_captured(tmp_path):
    """Phase 14 #9: provenance fields populate on every run."""
    wb_path = tmp_path / "synthetic_v1.xlsx"
    _build_synthetic_workbook(
        wb_path,
        sheets={
            "EntityA": [
                ["Item", "2026Q1"],
                ["Rent collected", 100_000],
            ],
        },
    )
    manifest = WorkbookManifestConfig(
        workbook_version="v1",
        expected_workbook_filename="synthetic_v1.xlsx",
        entity_sheets=[
            EntitySheetSpec(
                sheet_name="EntityA",
                entity_id="entity_a",
                entity_type="operating_llc",
                display_name="E",
                cash_flow_role="operating",
            ),
        ],
    )
    result = ingest_workbook(wb_path, manifest, manifest_version="v14_1")
    diag = result.diagnostics
    assert len(diag.workbook_hash) == 64  # SHA256 hex
    assert diag.workbook_filename == "synthetic_v1.xlsx"
    assert diag.workbook_version == "v1"
    assert diag.manifest_version == "v14_1"
    # Standing CAVEAT always populated.
    assert diag.formula_cache_caveat
    assert "data_only=True" in diag.formula_cache_caveat
    assert "stale" in diag.formula_cache_caveat.lower()


# ---- 10-11. Bridge to Phase 13 ---------------------------------------------


def test_distributable_rows_become_producer_entries(tmp_path):
    """Phase 14 #10: workbook_version drives deterministic producer_id;
    workbook_hash is captured separately and NOT in producer_id."""
    wb_path = tmp_path / "synthetic_v1.xlsx"
    _build_synthetic_workbook(
        wb_path,
        sheets={
            "EntityA": [
                ["Item", "2026Q1", "2026Q2"],
                ["Rent collected", 200_000, 200_000],
            ],
        },
    )
    manifest = WorkbookManifestConfig(
        workbook_version="v7_2026Q2_forecast",
        expected_workbook_filename="synthetic_v1.xlsx",
        entity_sheets=[
            EntitySheetSpec(
                sheet_name="EntityA",
                entity_id="bldg_a",
                entity_type="real_estate_partnership",
                display_name="Bldg A",
                cash_flow_role="operating",
                row_classification_rules=[
                    RowClassificationRule(
                        row_label_pattern="rent collected",
                        direction="inflow",
                        category="rent",
                        domain="real_estate",
                        distributable_candidate=True,
                        recurrence_type="recurring",
                        certainty="contractual",
                    ),
                ],
            ),
        ],
    )
    result = ingest_workbook(wb_path, manifest)
    bridged = workbook_lines_to_producer_config(result, manifest)

    # Two entries (one per quarter).
    assert len(bridged.entries) == 2
    ids = sorted(e.producer_id for e in bridged.entries)
    # Phase 14 reviewer tightening 2: workbook_version is the prefix,
    # NOT the workbook_hash. The hash never appears in producer_id.
    for pid in ids:
        assert pid.startswith("v7_2026Q2_forecast__")
        assert result.diagnostics.workbook_hash not in pid
    # Producer_ids are unique across entries.
    assert len(set(ids)) == 2

    # Source convention: distribution:<domain>:<entity_id>.
    sources = sorted(e.source_reference for e in bridged.entries)
    assert all(s.startswith("workbook=") for s in sources)
    # Distribution domain comes from the rule.
    domains = {e.domain for e in bridged.entries}
    assert domains == {"real_estate"}


def test_owl_distributable_income_runs_with_workbook_producer(tmp_path):
    """Phase 14 #11: ingestion → bridge → Owl distributable_income
    runs through 8 quarters without firing the zero-income guard."""
    from aa_model.integration.ledger import QuarterlyLedger
    from aa_model.io.schemas import GuardrailConfig, SmoothingConfig, SpendingConfig
    from aa_model.producers.distribution import (
        DistributionProducerDiagnostics,
        make_distribution_producer,
    )
    from aa_model.spending.base import SpendingParams
    from aa_model.spending.owl_adapter import OwlRule

    # Synthesize 8 quarters of $1M/qtr distributions.
    wb_path = tmp_path / "synthetic_v1.xlsx"
    quarters = [f"2026Q{q}" for q in (1, 2, 3, 4)] + [f"2027Q{q}" for q in (1, 2, 3, 4)]
    _build_synthetic_workbook(
        wb_path,
        sheets={
            "EntityA": [
                ["Item", *quarters],
                ["Rent collected", *([1_000_000] * 8)],
            ],
        },
    )
    manifest = WorkbookManifestConfig(
        workbook_version="v1",
        expected_workbook_filename="synthetic_v1.xlsx",
        entity_sheets=[
            EntitySheetSpec(
                sheet_name="EntityA",
                entity_id="bldg_a",
                entity_type="real_estate_partnership",
                display_name="Bldg A",
                cash_flow_role="operating",
                row_classification_rules=[
                    RowClassificationRule(
                        row_label_pattern="rent collected",
                        direction="inflow",
                        category="rent",
                        domain="real_estate",
                        distributable_candidate=True,
                        recurrence_type="recurring",
                        certainty="contractual",
                    ),
                ],
            ),
        ],
    )
    result = ingest_workbook(wb_path, manifest)
    bridged = workbook_lines_to_producer_config(result, manifest)
    producer = make_distribution_producer(bridged, engine="workbook")
    diag_acc = DistributionProducerDiagnostics()

    spend_cfg = SpendingConfig(
        rule="owl",
        annual_spend_usd=4_000_000.0,
        inflation_pct=0.025,
        smoothing=SmoothingConfig(window_quarters=4, weight=0.5),
        floor_usd=0.0,
        ceiling_usd=1.0e12,
        guardrail=GuardrailConfig(
            upper_band_pct=0.20,
            lower_band_pct=0.20,
            raise_pct=0.10,
            cut_pct=0.10,
            spending_base="distributable_income",
            distribution_window_quarters=4,
            bootstrap_distributable_income_usd=4_000_000.0,
        ),
    )
    rule = OwlRule()
    start_q = pd.Period("2026Q1", freq="Q-DEC")
    L = QuarterlyLedger("t", initial_nav={"cash": 100_000_000.0}, start_quarter=start_q)
    params = SpendingParams(
        config=spend_cfg,
        start_quarter=start_q,
        num_quarters=8,
    )
    for i in range(8):
        q = start_q + i
        spend_amt = rule.quarterly_outflow_at(L, params, q)
        emissions, delta = producer.emit_for_quarter(q)
        for em in emissions:
            L.add(
                quarter=q,
                bucket="cash",
                flow_type="distribution_inflow",
                amount_usd=em.amount_usd,
                source=em.source,
            )
        diag_acc.merge(delta)
        L.add(
            quarter=q,
            bucket="cash",
            flow_type="spend",
            amount_usd=-spend_amt,
            source=rule.SOURCE_ID,
        )

    # Realized window at q4 = $4M (4 × $1M); zero-income guard does
    # NOT fire.
    diags = rule.diagnostics()
    assert diags["used_bootstrap_at_run_end"] is False
    assert diags["trailing_distributable_income_usd"] == pytest.approx(4_000_000.0)
    # Producer accumulator captured 8 × $1M.
    assert diag_acc.total_emitted_usd == pytest.approx(8_000_000.0)


# ---- 12. End-to-end report rendering ---------------------------------------


def test_report_renders_workbook_ingestion_advisory(tmp_path, repo_root):
    """Phase 14 #12: ## Workbook ingestion (advisory) section renders
    with all sub-sections + standing CAVEAT line."""
    from aa_model.integration.ledger import QuarterlyLedger
    from aa_model.integration.report import write_markdown_report
    from aa_model.io.loaders import load_study_config

    wb_path = tmp_path / "synthetic_v1.xlsx"
    _build_synthetic_workbook(
        wb_path,
        sheets={
            "EntityA": [
                ["Item", "2026Q1", "2026Q2"],
                ["Rent collected", 200_000, 200_000],
                ["Total cash", 200_000, 200_000],  # subtotal — excluded
            ],
        },
    )
    manifest = WorkbookManifestConfig(
        workbook_version="v1",
        expected_workbook_filename="synthetic_v1.xlsx",
        entity_sheets=[
            EntitySheetSpec(
                sheet_name="EntityA",
                entity_id="entity_a",
                entity_type="operating_llc",
                display_name="Entity A",
                cash_flow_role="operating",
                row_classification_rules=[
                    RowClassificationRule(
                        row_label_pattern="rent collected",
                        direction="inflow",
                        category="rent",
                        domain="real_estate",
                        distributable_candidate=True,
                        recurrence_type="recurring",
                        certainty="contractual",
                    ),
                ],
            ),
        ],
    )
    result = ingest_workbook(wb_path, manifest, manifest_version="v14_1")
    workbook_lines_to_producer_config(result, manifest)  # populates by-domain

    cfg = load_study_config(repo_root / "configs" / "base.yaml")
    L = QuarterlyLedger(
        "test_p14",
        initial_nav={
            b: 25_000_000.0 for b in ("cash", "public_bond", "public_equity", "pe_buyout")
        },
        start_quarter=pd.Period("2026Q1", freq="Q-DEC"),
    )
    L.finalize()
    out = tmp_path / "report.md"
    write_markdown_report(
        out,
        cfg=cfg,
        ledger=L,
        run_id="test_phase14",
        config_hash="0" * 12,
        fixtures_hash="0" * 12,
        workbook_ingestion_result=result,
    )
    text = out.read_text(encoding="utf-8")
    # Header + provenance fields.
    assert "## Workbook ingestion (advisory)" in text
    assert "filename: synthetic_v1.xlsx" in text
    assert "workbook_version (manifest): v1" in text
    assert "manifest_version: v14_1" in text
    # Sheet + row counts.
    assert "ingested: 1" in text
    assert "subtotal excluded: 1" in text
    # Per-entity inflow totals.
    assert "entity_a:" in text
    # Distribution candidates by domain.
    assert "real_estate:" in text
    # Phase 14 RT1 standing CAVEAT.
    assert "CAVEAT" in text
    assert "data_only=True" in text
    assert "stale" in text.lower()
    # Closing paragraph asserts read-only + advisory-reconciliation
    # framing.
    assert "read-only integration target" in text
    assert "never mutated" in text
    assert "Board-snapshot reconciliation deltas are advisory only" in text
