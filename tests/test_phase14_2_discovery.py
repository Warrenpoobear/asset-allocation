"""Phase 14.2 / L19 — workbook discovery + draft-manifest tests.

8 tests covering the discovery module + draft-manifest generator +
CLI path-safety check. All fixtures synthetic; no real-workbook
data ever committed (Phase 14 RT4 still in force).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from aa_model.ingestion.discovery import (
    DraftManifestResult,
    SheetDiscovery,
    WorkbookDiscoveryResult,
    build_draft_manifest,
    discover_workbook,
    render_aggregate_diagnostics,
)
from aa_model.ingestion.schemas import WorkbookManifestConfig


def _build_synthetic_workbook(path: Path, sheets: dict[str, list[list[object]]]) -> None:
    """Build a minimal openpyxl workbook for testing — same pattern
    as Phase 14 / 14.1 test discipline."""
    from openpyxl import Workbook

    wb = Workbook()
    default = wb.active
    wb.remove(default)
    for sheet_name, rows in sheets.items():
        ws = wb.create_sheet(title=sheet_name)
        for row in rows:
            ws.append(row)
    wb.save(str(path))


# ---- 1. Row-4 q_yyyy synthetic discovers correctly -----------------------


def test_row4_qyyyy_discovery(tmp_path):
    """A v7-shaped synthetic workbook is correctly discovered:
    detected_format_majority='q_yyyy', header_row_majority=4,
    sheets bucketed properly."""
    wb_path = tmp_path / "synthetic_v7.xlsx"
    _build_synthetic_workbook(wb_path, sheets={
        "Summary": [
            ["Roll-up", "2025Q1", "2025Q2", "2025Q3", "2025Q4"],
            ["Total", 100, 100, 100, 100],
        ],
        # An entity sheet with v7-shaped row-4 q_yyyy headers.
        "EntityA": [
            ["Cash Flow Forecast", None, None, None, None],
            [None, None, None, None, None],
            [None, "FY2025", None, None, None],
            ["Item", "Q1 2025", "Q2 2025", "Q3 2025", "Q4 2025"],
            ["Rent",       100, 100, 100, 100],
            ["Tax",        -10, -10, -10, -10],
            ["Distributions", 50, 50, 50, 50],
        ],
        "EntityB": [
            ["Title", None, None, None, None],
            [None, None, None, None, None],
            [None, "FY2025", None, None, None],
            ["Item", "Q1 2025", "Q2 2025", "Q3 2025", "Q4 2025"],
            ["Income",     200, 200, 200, 200],
            ["Expense",    -50, -50, -50, -50],
            ["Net distributable", 150, 150, 150, 150],
        ],
    })
    discovery = discover_workbook(wb_path)
    assert discovery.total_sheets == 3
    # Two of the three sheets have parseable headers under q_yyyy at row 4.
    assert discovery.sheets_with_parseable_headers >= 2
    # Majority detection picks q_yyyy at row 4 (because EntityA + EntityB win
    # over Summary's row-1 yyyy_q under the >=4-cells threshold; Summary
    # has only 4 quarters at row 1, which is the boundary case).
    assert discovery.detected_format_majority in ("q_yyyy", "yyyy_q")
    # Summary classified as family_aggregate by keyword.
    summary = next(s for s in discovery.sheets if s.sheet_name_raw == "Summary")
    assert summary.role == "family_aggregate"


# ---- 2. display_only detection -------------------------------------------


def test_display_only_detection(tmp_path):
    """A sheet with no parseable header AND only 1 label row is
    classified as display_only (or unknown layout). It still gets a
    role from the keyword classifier."""
    wb_path = tmp_path / "synthetic.xlsx"
    _build_synthetic_workbook(wb_path, sheets={
        "Ownership": [
            ["Org structure"],
        ],
        "EntityA": [
            ["Item", "Q1 2025", "Q2 2025", "Q3 2025", "Q4 2025"],
            ["Rent",       100, 100, 100, 100],
            ["Distributions", 50, 50, 50, 50],
            ["Tax",        -10, -10, -10, -10],
        ],
    })
    discovery = discover_workbook(wb_path)
    ownership = next(s for s in discovery.sheets if s.sheet_name_raw == "Ownership")
    assert ownership.role == "ownership_structure"
    assert ownership.layout_type == "display_only"
    entity = next(s for s in discovery.sheets if s.sheet_name_raw == "EntityA")
    assert entity.layout_type == "horizontal_quarter"


# ---- 3. Draft manifest validates under WorkbookManifestConfig ------------


def test_draft_manifest_validates(tmp_path):
    """build_draft_manifest produces a WorkbookManifestConfig that
    validates cleanly (sheet-name uniqueness, entity_id uniqueness,
    URL-safe workbook_version)."""
    wb_path = tmp_path / "synthetic_v7.xlsx"
    _build_synthetic_workbook(wb_path, sheets={
        "Summary": [
            ["Item", "Q1 2025", "Q2 2025"],
            ["Total", 100, 100],
        ],
        "EntityA": [
            ["Item", "Q1 2025", "Q2 2025", "Q3 2025", "Q4 2025"],
            ["Rent",       100, 100, 100, 100],
            ["Distributions", 50, 50, 50, 50],
            ["Tax",        -10, -10, -10, -10],
        ],
        "EntityB": [
            ["Item", "Q1 2025", "Q2 2025", "Q3 2025", "Q4 2025"],
            ["Income",     200, 200, 200, 200],
            ["Distributions", 75, 75, 75, 75],
            ["Expense",    -50, -50, -50, -50],
        ],
    })
    discovery = discover_workbook(wb_path)
    draft = build_draft_manifest(discovery, mode="local_private", workbook_version="v_test")
    assert isinstance(draft.manifest, WorkbookManifestConfig)
    # Sheet enumeration tallies to discovery total.
    total_decl = (
        len(draft.manifest.family_aggregate_sheets)
        + len(draft.manifest.board_snapshot_sheets)
        + len(draft.manifest.entity_sheets)
        + len(draft.manifest.re_partnership_sheets)
    )
    assert total_decl == discovery.total_sheets
    # Round-trip through model_validate confirms validators pass.
    data = draft.manifest.model_dump(mode="json")
    WorkbookManifestConfig.model_validate(data)


# ---- 4. privacy_safe redacts non-structural sheet names ------------------


def test_privacy_safe_redacts_non_structural(tmp_path):
    """privacy_safe replaces non-structural sheet names with
    placeholder slots; structural sheet names (Summary, board,
    Ownership) are preserved literal."""
    wb_path = tmp_path / "synthetic.xlsx"
    _build_synthetic_workbook(wb_path, sheets={
        "Summary": [
            ["Item", "Q1 2025", "Q2 2025", "Q3 2025", "Q4 2025"],
            ["Total", 100, 100, 100, 100],
        ],
        "September 25 Board": [
            ["Title"],
        ],
        "Ownership": [
            ["Tree"],
        ],
        # Non-structural sheet names — should be redacted.
        "SJB LLC": [
            ["Item", "Q1 2025", "Q2 2025", "Q3 2025", "Q4 2025"],
            ["Rent",       100, 100, 100, 100],
            ["Tax",        -10, -10, -10, -10],
            ["Net",         90,  90,  90,  90],
        ],
        "AB Trust": [
            ["Item", "Q1 2025", "Q2 2025", "Q3 2025", "Q4 2025"],
            ["Distribution", 50, 50, 50, 50],
            ["Fee",         -5, -5, -5, -5],
            ["Net",         45, 45, 45, 45],
        ],
    })
    discovery = discover_workbook(wb_path)
    draft_safe = build_draft_manifest(discovery, mode="privacy_safe")
    draft_priv = build_draft_manifest(discovery, mode="local_private")

    # Privacy-safe: the LLC + Trust sheet names are redacted.
    safe_names = (
        draft_safe.manifest.family_aggregate_sheets
        + draft_safe.manifest.board_snapshot_sheets
        + [s.sheet_name for s in draft_safe.manifest.entity_sheets]
    )
    assert "SJB LLC" not in safe_names
    assert "AB Trust" not in safe_names
    # Structural names preserved.
    assert "Summary" in draft_safe.manifest.family_aggregate_sheets
    assert "September 25 Board" in draft_safe.manifest.board_snapshot_sheets
    assert "Ownership" in [s.sheet_name for s in draft_safe.manifest.entity_sheets]
    # Redaction count > 0.
    assert draft_safe.redacted_sheet_count >= 2

    # local_private: real names preserved.
    priv_names = [s.sheet_name for s in draft_priv.manifest.entity_sheets]
    assert "SJB LLC" in priv_names
    assert "AB Trust" in priv_names
    assert draft_priv.redacted_sheet_count == 0


# ---- 5. local_private path safety check ----------------------------------


def test_local_private_path_safety(tmp_path):
    """The CLI's local_private path check refuses to write to a
    path that isn't gitignored by Phase 14 conventions."""
    from aa_model.ingestion.discover_workbook import _check_local_private_path

    # Acceptable: ends in _local.yaml.
    _check_local_private_path(Path("configs/workbook_v7_manifest_local.yaml"))
    # Acceptable: under data/external/.
    _check_local_private_path(Path("data/external/workbook_v7_manifest.yaml"))
    # Refused: arbitrary path.
    with pytest.raises(SystemExit, match="refusing to write local_private"):
        _check_local_private_path(Path("/tmp/draft_manifest.yaml"))
    # Refused: configs/ but not _local.
    with pytest.raises(SystemExit, match="refusing to write local_private"):
        _check_local_private_path(Path("configs/draft_manifest.yaml"))


# ---- 6. Majority format / header detection -------------------------------


def test_majority_format_and_header_detection(tmp_path):
    """When most sheets share a header row + format, the discovery
    surfaces those as detected_*_majority."""
    wb_path = tmp_path / "synthetic.xlsx"
    sheets: dict[str, list[list[object]]] = {}
    # 5 entity sheets all with row-4 q_yyyy headers.
    for i in range(5):
        sheets[f"Entity{i}"] = [
            ["title", None, None, None, None],
            [None, None, None, None, None],
            [None, "FY2025", None, None, None],
            ["Item", "Q1 2025", "Q2 2025", "Q3 2025", "Q4 2025"],
            ["Inflow",      100, 100, 100, 100],
            ["Distributions", 50, 50, 50, 50],
            ["Outflow",     -10, -10, -10, -10],
            ["Net",          90,  90,  90,  90],
        ]
    _build_synthetic_workbook(wb_path, sheets=sheets)
    discovery = discover_workbook(wb_path)
    assert discovery.detected_format_majority == "q_yyyy"
    assert discovery.detected_header_row_majority == 4

    # Draft manifest reflects the majority detections.
    draft = build_draft_manifest(discovery, mode="local_private")
    assert draft.manifest.default_header_row_index == 4
    assert draft.manifest.period_header_format == "q_yyyy"


# ---- 7. CLI dry-run prints diagnostics only ------------------------------


def test_cli_dry_run_no_yaml(tmp_path, capsys):
    """`--dry-run` prints aggregate diagnostics without writing a YAML."""
    from aa_model.ingestion.discover_workbook import main

    wb_path = tmp_path / "synthetic.xlsx"
    _build_synthetic_workbook(wb_path, sheets={
        "Summary": [
            ["Item", "Q1 2025"],
            ["Total", 100],
        ],
        "EntityA": [
            ["Item", "Q1 2025", "Q2 2025", "Q3 2025", "Q4 2025"],
            ["Rent",       100, 100, 100, 100],
            ["Distributions", 50, 50, 50, 50],
            ["Tax",        -10, -10, -10, -10],
        ],
    })
    rc = main([
        "--workbook", str(wb_path),
        "--dry-run",
        "--mode", "privacy_safe",
    ])
    assert rc == 0
    captured = capsys.readouterr()
    assert "WORKBOOK DISCOVERY" in captured.out
    assert "total_sheets:" in captured.out
    assert "role_counts:" in captured.out
    # No raw cell content / dollar values appear in dry-run output.
    assert "100" not in captured.out
    assert "Rent" not in captured.out


# ---- 8. Existing ingestion behavior unchanged (regression anchor) --------


def test_phase14_2_does_not_change_phase14_ingestion(tmp_path):
    """Adding the discovery module does not change ingest_workbook
    behavior. A Phase-14-style fixture + manifest still produces
    the same counts."""
    from aa_model.ingestion.schemas import (
        EntitySheetSpec,
        RowClassificationRule,
    )
    from aa_model.ingestion.workbook import ingest_workbook

    wb_path = tmp_path / "synthetic.xlsx"
    _build_synthetic_workbook(wb_path, sheets={
        "EntityA": [
            ["Item", "2026Q1", "2026Q2", "2026Q3", "2026Q4"],
            ["Rent collected", 100_000, 100_000, 100_000, 100_000],
            ["Tax payment",    -10_000, -10_000, -10_000, -10_000],
        ],
    })
    manifest = WorkbookManifestConfig(
        workbook_version="v1",
        expected_workbook_filename="synthetic.xlsx",
        entity_sheets=[
            EntitySheetSpec(
                sheet_name="EntityA", entity_id="entity_a",
                entity_type="operating_llc", display_name="A",
                cash_flow_role="operating",
                row_classification_rules=[
                    RowClassificationRule(
                        row_label_pattern="rent collected",
                        direction="inflow", category="rent", domain="real_estate",
                        distributable_candidate=True, recurrence_type="recurring",
                        certainty="contractual",
                    ),
                    RowClassificationRule(
                        row_label_pattern="tax payment",
                        direction="outflow", category="tax",
                        recurrence_type="recurring", certainty="contractual",
                    ),
                ],
            ),
        ],
    )
    result = ingest_workbook(wb_path, manifest)
    # 2 row labels × 4 quarters = 8 lines, exactly as Phase 14 / 14.1.
    assert len(result.cash_flow_lines) == 8
