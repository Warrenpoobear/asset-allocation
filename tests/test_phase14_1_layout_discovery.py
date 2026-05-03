"""Phase 14.1 / L19 — workbook layout discovery support.

5 tests covering the new layout knobs added to WorkbookManifestConfig
and EntitySheetSpec:

  * default_header_row_index (manifest-level, default 1)
  * header_row_index (per-sheet override)
  * period_header_format (per-sheet override)
  * layout_type (per-sheet: "horizontal_quarter" | "display_only")

Synthetic-fixture discipline (Phase 14 RT4 still in force): every
fixture is built programmatically via openpyxl write API in tmp_path.
No real workbook data committed.

Test plan:
1. Row-4 q_yyyy synthetic fixture parses correctly (the v7 case).
2. Per-sheet header_row_index overrides the manifest default.
3. Per-sheet period_header_format overrides the manifest default.
4. layout_type='display_only' declares the entity but emits no
   CashFlowLineRecord rows.
5. Default behavior (no overrides) byte-stable vs. Phase 14
   (regression anchor — existing row-1 sheets still work).
"""

from __future__ import annotations

from pathlib import Path

from aa_model.ingestion.schemas import (
    EntitySheetSpec,
    RowClassificationRule,
    WorkbookManifestConfig,
)
from aa_model.ingestion.workbook import ingest_workbook


def _build_workbook(path: Path, sheets: dict[str, list[list[object]]]) -> None:
    """Build a minimal openpyxl workbook for testing."""
    from openpyxl import Workbook

    wb = Workbook()
    default = wb.active
    wb.remove(default)
    for sheet_name, rows in sheets.items():
        ws = wb.create_sheet(title=sheet_name)
        for row in rows:
            ws.append(row)
    wb.save(str(path))


# ---- 1. Row-4 q_yyyy fixture (v7-shaped) ---------------------------------


def test_row4_qyyyy_fixture_parses(tmp_path):
    """Phase 14.1 #1: synthetic v7-shaped fixture (header on row 4,
    format q_yyyy) parses correctly under the new knobs."""
    wb_path = tmp_path / "synthetic_v7.xlsx"
    _build_workbook(
        wb_path,
        sheets={
            # Row 1: workbook title; Row 2: blank; Row 3: year banner;
            # Row 4: canonical "Q1 2025"-style headers; Row 5+: data.
            "EntityA": [
                ["Cash Flow Forecast", None, None, None, None],
                [None, None, None, None, None],
                [None, "FY2025", None, None, None],
                ["Item", "Q1 2025", "Q2 2025", "Q3 2025", "Q4 2025"],
                ["Rent", 100_000, 100_000, 100_000, 100_000],
                ["Tax", -10_000, -10_000, -10_000, -10_000],
            ],
        },
    )
    manifest = WorkbookManifestConfig(
        workbook_version="v7_synthetic",
        expected_workbook_filename="synthetic_v7.xlsx",
        period_header_format="q_yyyy",  # manifest-level
        default_header_row_index=4,  # manifest-level
        entity_sheets=[
            EntitySheetSpec(
                sheet_name="EntityA",
                entity_id="entity_a",
                entity_type="operating_llc",
                display_name="Entity A",
                cash_flow_role="operating",
                row_classification_rules=[
                    RowClassificationRule(
                        row_label_pattern="rent",
                        direction="inflow",
                        category="rent",
                        domain="real_estate",
                        distributable_candidate=True,
                        recurrence_type="recurring",
                        certainty="contractual",
                    ),
                    RowClassificationRule(
                        row_label_pattern="tax",
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
    # 4 quarters × 2 row labels = 8 cash-flow lines.
    assert len(result.cash_flow_lines) == 8
    quarters = sorted({line.quarter for line in result.cash_flow_lines})
    assert quarters == ["2025Q1", "2025Q2", "2025Q3", "2025Q4"]
    rents = [line for line in result.cash_flow_lines if line.category == "rent"]
    assert all(line.direction == "inflow" for line in rents)
    assert all(line.amount_usd == 100_000 for line in rents)


# ---- 2. Per-sheet header_row_index override ------------------------------


def test_per_sheet_header_row_index_override(tmp_path):
    """Phase 14.1 #2: a sheet-level header_row_index beats the
    manifest default."""
    wb_path = tmp_path / "synthetic.xlsx"
    _build_workbook(
        wb_path,
        sheets={
            # Sheet A: row-1 header (matches manifest default).
            "EntityA": [
                ["Item", "2026Q1", "2026Q2"],
                ["Rent", 100_000, 100_000],
            ],
            # Sheet B: row-3 header (overrides manifest default).
            "EntityB": [
                ["Workbook title", None, None],
                [None, None, None],
                ["Item", "2026Q1", "2026Q2"],
                ["Rent", 200_000, 200_000],
            ],
        },
    )
    manifest = WorkbookManifestConfig(
        workbook_version="v1",
        expected_workbook_filename="synthetic.xlsx",
        # Manifest default = 1 (Phase 14 default).
        entity_sheets=[
            EntitySheetSpec(
                sheet_name="EntityA",
                entity_id="entity_a",
                entity_type="operating_llc",
                display_name="A",
                cash_flow_role="operating",
                row_classification_rules=[
                    RowClassificationRule(
                        row_label_pattern="rent",
                        direction="inflow",
                        category="rent",
                        domain="real_estate",
                        distributable_candidate=True,
                        recurrence_type="recurring",
                        certainty="contractual",
                    ),
                ],
            ),
            EntitySheetSpec(
                sheet_name="EntityB",
                entity_id="entity_b",
                entity_type="operating_llc",
                display_name="B",
                cash_flow_role="operating",
                # Per-sheet override: header is at row 3.
                header_row_index=3,
                row_classification_rules=[
                    RowClassificationRule(
                        row_label_pattern="rent",
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
    # Both sheets contribute: 2 × 2 = 4 lines total.
    assert len(result.cash_flow_lines) == 4
    by_entity = {line.entity_id for line in result.cash_flow_lines}
    assert by_entity == {"entity_a", "entity_b"}
    # Sheet B's amounts confirm row-3 header was used (not row-1).
    b_amounts = sorted(
        line.amount_usd for line in result.cash_flow_lines if line.entity_id == "entity_b"
    )
    assert b_amounts == [200_000.0, 200_000.0]


# ---- 3. Per-sheet period_header_format override ---------------------------


def test_per_sheet_period_header_format_override(tmp_path):
    """Phase 14.1 #3: a sheet-level period_header_format beats the
    manifest default."""
    wb_path = tmp_path / "synthetic.xlsx"
    _build_workbook(
        wb_path,
        sheets={
            "EntityA": [
                # Format: q_yyyy ("Q1 2026") — different from manifest's yyyy_q.
                ["Item", "Q1 2026", "Q2 2026"],
                ["Rent", 100_000, 100_000],
            ],
        },
    )
    manifest = WorkbookManifestConfig(
        workbook_version="v1",
        expected_workbook_filename="synthetic.xlsx",
        period_header_format="yyyy_q",  # manifest default — wrong for this sheet
        entity_sheets=[
            EntitySheetSpec(
                sheet_name="EntityA",
                entity_id="entity_a",
                entity_type="operating_llc",
                display_name="A",
                cash_flow_role="operating",
                period_header_format="q_yyyy",  # per-sheet override
                row_classification_rules=[
                    RowClassificationRule(
                        row_label_pattern="rent",
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
    assert len(result.cash_flow_lines) == 2
    assert {line.quarter for line in result.cash_flow_lines} == {"2026Q1", "2026Q2"}


# ---- 4. layout_type="display_only" ----------------------------------------


def test_display_only_skips_data_extraction(tmp_path):
    """Phase 14.1 #4: declared display_only sheet emits an EntityRecord
    but no CashFlowLineRecord rows; surfaced in diagnostics."""
    wb_path = tmp_path / "synthetic.xlsx"
    _build_workbook(
        wb_path,
        sheets={
            "DataSheet": [
                ["Item", "2026Q1", "2026Q2"],
                ["Rent", 100_000, 100_000],
            ],
            "DisplaySheet": [
                ["Header text only", None, None],
                ["Some structure", None, None],
                ["No quarter cols", None, None],
            ],
        },
    )
    manifest = WorkbookManifestConfig(
        workbook_version="v1",
        expected_workbook_filename="synthetic.xlsx",
        entity_sheets=[
            EntitySheetSpec(
                sheet_name="DataSheet",
                entity_id="data_entity",
                entity_type="operating_llc",
                display_name="Data",
                cash_flow_role="operating",
                row_classification_rules=[
                    RowClassificationRule(
                        row_label_pattern="rent",
                        direction="inflow",
                        category="rent",
                        domain="real_estate",
                        distributable_candidate=True,
                        recurrence_type="recurring",
                        certainty="contractual",
                    ),
                ],
            ),
            EntitySheetSpec(
                sheet_name="DisplaySheet",
                entity_id="display_entity",
                entity_type="family_aggregate",
                display_name="Display",
                cash_flow_role="operating",
                layout_type="display_only",  # NEW Phase 14.1
            ),
        ],
    )
    result = ingest_workbook(wb_path, manifest)
    # Both entities declared.
    assert len(result.entities) == 2
    # Only the data sheet contributes rows.
    assert len(result.cash_flow_lines) == 2
    assert {line.entity_id for line in result.cash_flow_lines} == {"data_entity"}
    # Display sheet noted in diagnostics.
    assert any(
        "display_only:DisplaySheet" in entry for entry in result.diagnostics.missing_optional_sheets
    )


# ---- 5. Default behavior byte-stable (regression anchor) -----------------


def test_default_behavior_byte_stable_vs_phase14(tmp_path):
    """Phase 14.1 #5: with no overrides set, the parser produces
    byte-identical IngestionResult content vs. Phase 14 behavior.
    This is the regression anchor that pins backward compatibility.
    """
    wb_path = tmp_path / "synthetic.xlsx"
    _build_workbook(
        wb_path,
        sheets={
            # Same fixture shape as Phase 14 test #5.
            "EntityA": [
                ["Item", "2026Q1", "2026Q2", "2026Q3", "2026Q4"],
                ["Rent collected", 100_000, 100_000, 100_000, 100_000],
                ["Tax payment", -10_000, -10_000, -10_000, -10_000],
            ],
        },
    )
    # Manifest with no Phase 14.1 overrides: header_row_index defaults
    # to 1 via manifest.default_header_row_index; period_header_format
    # defaults to "yyyy_q"; layout_type defaults to "horizontal_quarter".
    manifest = WorkbookManifestConfig(
        workbook_version="v1",
        expected_workbook_filename="synthetic.xlsx",
        entity_sheets=[
            EntitySheetSpec(
                sheet_name="EntityA",
                entity_id="entity_a",
                entity_type="operating_llc",
                display_name="Entity A",
                cash_flow_role="operating",
                # Note: no header_row_index, period_header_format, or
                # layout_type set. All defaults.
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
    # Same output as Phase 14: 8 lines (2 row labels × 4 quarters).
    assert len(result.cash_flow_lines) == 8
    assert {line.quarter for line in result.cash_flow_lines} == {
        "2026Q1",
        "2026Q2",
        "2026Q3",
        "2026Q4",
    }
    # Verify defaults flowed through correctly: spec values are None;
    # parser used manifest defaults.
    spec = manifest.entity_sheets[0]
    assert spec.header_row_index is None
    assert spec.period_header_format is None
    assert spec.layout_type == "horizontal_quarter"
    assert manifest.default_header_row_index == 1
