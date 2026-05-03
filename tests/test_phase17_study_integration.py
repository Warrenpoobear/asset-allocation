"""Phase 17 / L20 — StudyConfig integration tests.

9 tests. Synthetic fixtures only — no live workbook, no real positions,
no manager names. See MODEL_DOCUMENTATION.md §Phase 17 design.

Coverage (9 tests):
1.  PositionIngestionConfig: valid schema round-trip.
2.  PositionIngestionConfig: manifest_path required (not inline manifest).
3.  PositionIngestionConfig: manifest_version colon raises.
4.  StudyConfig: position_ingestion defaults to None (default-off).
5.  load_position_manifest: FileNotFoundError on missing path.
6.  load_position_manifest: loads a valid YAML manifest from tmp file.
7.  _run_liquidity_coverage: wires positions + manifest + obligations correctly.
8.  render_coverage_report_section: spending_base_mode label in output.
9.  render_coverage_report_section: None mode renders default label.
"""

from __future__ import annotations

import datetime
import textwrap
from pathlib import Path

import pytest
from aa_model.ingestion.schemas_position import (
    PositionIngestionDiagnostics,
    PositionIngestionResult,
    PositionManifestConfig,
    PositionRecord,
)
from aa_model.io.schemas import PositionIngestionConfig
from aa_model.liquidity.coverage import (
    LiquidityObligationConfig,
    compute_liquidity_coverage,
    render_coverage_report_section,
)

# ---- shared synthetic builder ----------------------------------------------


def _pos(
    bucket: str,
    nav: float,
    *,
    unfunded: float | None = None,
) -> PositionRecord:
    return PositionRecord(
        position_id=f"p_{bucket}_{int(nav)}",
        account_id="acct_synthetic",
        manager_id=None,
        market_value_usd=nav,
        unfunded_commitment_usd=unfunded,
        liquidity_bucket=bucket,
        valuation_date=datetime.date(2026, 3, 31),
        source_row=1,
    )


def _synthetic_ingestion_result(
    positions: list[PositionRecord],
) -> PositionIngestionResult:
    diag = PositionIngestionDiagnostics(
        workbook_hash="aabbccdd" * 8,
        workbook_version="1",
        manifest_version="1",
        positions_total=len(positions),
        stale_valuation_count=0,
        positions_missing_bucket=0,
    )
    return PositionIngestionResult(
        accounts=[],
        positions=positions,
        diagnostics=diag,
    )


def _minimal_manifest() -> PositionManifestConfig:
    return PositionManifestConfig(
        manifest_version="1",
        workbook_version="1",
        as_of_date=datetime.date(2026, 3, 31),
    )


# ---- 1. PositionIngestionConfig schema round-trip -------------------------


def test_position_ingestion_config_valid():
    """Phase 17 #1: valid PositionIngestionConfig round-trips correctly."""
    cfg = PositionIngestionConfig(
        workbook_path="/path/to/workbook.xlsx",
        manifest_path="/path/to/manifest.yaml",
        manifest_version="2",
    )
    assert cfg.workbook_path == "/path/to/workbook.xlsx"
    assert cfg.manifest_path == "/path/to/manifest.yaml"
    assert cfg.manifest_version == "2"


# ---- 2. manifest_path required (not inline manifest) ----------------------


def test_position_ingestion_config_manifest_path_required():
    """Phase 17 #2: manifest_path is a required path field, not an inline dict."""
    import pydantic

    with pytest.raises((pydantic.ValidationError, TypeError)):
        PositionIngestionConfig(workbook_path="/path/to/workbook.xlsx")


# ---- 3. manifest_version colon raises -------------------------------------


def test_position_ingestion_config_manifest_version_no_colons():
    """Phase 17 #3: manifest_version containing a colon raises ValidationError."""
    import pydantic

    with pytest.raises(pydantic.ValidationError, match="URL-safe"):
        PositionIngestionConfig(
            workbook_path="/path/to/workbook.xlsx",
            manifest_path="/path/to/manifest.yaml",
            manifest_version="1:0",
        )


# ---- 4. StudyConfig.position_ingestion defaults to None -------------------


def test_study_config_position_ingestion_default_none(tmp_path):
    """Phase 17 #4: StudyConfig.position_ingestion defaults to None (default-off)."""
    from aa_model.io.loaders import load_study_config

    # Use an existing config fixture — any of the default ones work.
    config_path = Path(__file__).parents[1] / "configs" / "base.yaml"
    if not config_path.exists():
        pytest.skip("base.yaml fixture not available in this environment")

    cfg = load_study_config(config_path)
    assert cfg.position_ingestion is None
    assert cfg.liquidity_obligations is None
    assert cfg.liquidity_coverage_config is None


# ---- 5. load_position_manifest: FileNotFoundError -------------------------


def test_load_position_manifest_file_not_found(tmp_path):
    """Phase 17 #5: load_position_manifest raises FileNotFoundError for missing file."""
    from aa_model.ingestion.investment_summary import load_position_manifest

    missing = tmp_path / "nonexistent_manifest.yaml"
    with pytest.raises(FileNotFoundError, match="Position manifest not found"):
        load_position_manifest(missing)


# ---- 6. load_position_manifest: valid YAML --------------------------------


def test_load_position_manifest_valid_yaml(tmp_path):
    """Phase 17 #6: load_position_manifest loads and validates a valid YAML manifest."""
    from aa_model.ingestion.investment_summary import load_position_manifest

    manifest_yaml = textwrap.dedent("""\
        manifest_version: "1"
        workbook_version: "7"
        as_of_date: "2026-03-31"
        accounts: []
        manager_terms: []
    """)
    p = tmp_path / "manifest.yaml"
    p.write_text(manifest_yaml, encoding="utf-8")

    result = load_position_manifest(p)
    assert isinstance(result, PositionManifestConfig)
    assert result.manifest_version == "1"
    assert result.workbook_version == "7"
    assert result.as_of_date == datetime.date(2026, 3, 31)


# ---- 7. _run_liquidity_coverage wiring ------------------------------------


def test_run_liquidity_coverage_wiring():
    """Phase 17 #7: _run_liquidity_coverage threads positions + manifest + cfg correctly."""
    from aa_model.integration.orchestrator import _run_liquidity_coverage
    from aa_model.io.loaders import load_study_config

    config_path = Path(__file__).parents[1] / "configs" / "base.yaml"
    if not config_path.exists():
        pytest.skip("base.yaml fixture not available in this environment")

    cfg = load_study_config(config_path)
    # Inject liquidity_obligations via model_copy
    cfg = cfg.model_copy(
        update={
            "liquidity_obligations": {"annual_spend_usd": 200_000.0},
            "liquidity_coverage_config": {},
        }
    )

    positions = [
        _pos("daily_liquid", 500_000),
        _pos("illiquid", 1_000_000, unfunded=100_000),
    ]
    ingestion_result = _synthetic_ingestion_result(positions)
    manifest = _minimal_manifest()

    result = _run_liquidity_coverage(ingestion_result, manifest, cfg)

    assert result.liquid_nav == pytest.approx(500_000)
    assert result.illiquid_nav == pytest.approx(1_000_000)
    assert result.total_unfunded_commitments_usd == pytest.approx(100_000)
    assert result.liquid_to_annual_spend == pytest.approx(2.5)


# ---- 8. render_coverage_report_section: spending_base_mode label ----------


def test_render_coverage_spending_base_mode_label():
    """Phase 17 #8: spending_base_mode appears in render output."""
    positions = [_pos("daily_liquid", 300_000)]
    obs = LiquidityObligationConfig(annual_spend_usd=100_000)
    result = compute_liquidity_coverage(positions, obs)

    output = render_coverage_report_section(result, spending_base_mode="liquid_nav")
    assert "liquid_nav" in output


# ---- 9. render_coverage_report_section: None mode renders default label ---


def test_render_coverage_none_mode_default_label():
    """Phase 17 #9: None spending_base_mode renders generic 'spending_base' label."""
    positions = [_pos("daily_liquid", 300_000)]
    obs = LiquidityObligationConfig(annual_spend_usd=100_000)
    result = compute_liquidity_coverage(positions, obs)

    output = render_coverage_report_section(result, spending_base_mode=None)
    assert "spending_base" in output
    # None mode: no parenthetical mode label
    assert "liquid / spending_base (" not in output
