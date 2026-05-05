"""Regression tests for the 2026-05-05 external code review.

Covers:
  #2  hash_study_config now distinguishes overlay configs that previously
      collided on the same config_hash.
  #6  load_local_study_config resolves workbook paths relative to repo
      root (previously only ``manifest_path`` was normalized).
  #7  ReconciliationGatesConfig rejects unknown keys; LiquidityCoverageConfig
      rejects ill-ordered breach/warning thresholds.
  #8  runway_horizon_quarters is wired into the warnings list.
"""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest
import yaml

from aa_model.io.loaders import (
    hash_study_config,
    load_local_study_config,
    load_study_config,
)
from aa_model.liquidity.coverage import (
    LiquidityCoverageConfig,
    LiquidityObligationConfig,
    compute_liquidity_coverage,
)
from aa_model.pe.reconciliation_gates import ReconciliationGatesConfig


# ---- #2 hash coverage -------------------------------------------------------


def _study(base_config_path: Path):
    return load_study_config(base_config_path)


def test_hash_changes_with_liquidity_obligations(base_config_path):
    cfg = _study(base_config_path)
    h_a, _ = hash_study_config(cfg)

    cfg_b = cfg.model_copy(update={"liquidity_obligations": {"annual_spend_usd": 100_000.0}})
    h_b, _ = hash_study_config(cfg_b)

    assert h_a != h_b


def test_hash_changes_with_liquidity_coverage_config(base_config_path):
    cfg = _study(base_config_path)
    h_a, _ = hash_study_config(cfg)

    cfg_b = cfg.model_copy(update={"liquidity_coverage_config": {"runway_horizon_quarters": 12}})
    h_b, _ = hash_study_config(cfg_b)

    assert h_a != h_b


def test_hash_changes_with_reconciliation_gates(base_config_path):
    cfg = _study(base_config_path)
    h_a, _ = hash_study_config(cfg)

    cfg_b = cfg.model_copy(update={"reconciliation_gates": {"warning_pct": 0.05}})
    h_b, _ = hash_study_config(cfg_b)

    assert h_a != h_b


def test_hash_unchanged_when_optional_fields_remain_none(base_config_path):
    """The new fields are additive: a study with all overlays absent
    must hash identically to its prior behavior. Two equivalent loads
    of the same base config produce the same hash."""
    h_a, f_a = hash_study_config(_study(base_config_path))
    h_b, f_b = hash_study_config(_study(base_config_path))
    assert h_a == h_b
    assert f_a == f_b


# ---- #6 overlay workbook path resolution -----------------------------------


def test_local_overlay_resolves_workbook_paths(repo_root, tmp_path):
    """workbook_ingestion.workbook_path and position_ingestion.workbook_path
    must both be normalized to absolute paths relative to the repo root,
    matching the pre-existing manifest_path behavior."""
    configs = repo_root / "configs"
    overlay_path = configs / "_test_overlay_paths.yaml"
    overlay = {
        "extends_from": "configs/base.yaml",
        "workbook_ingestion": {
            "workbook_path": "data/fake_workbook.xlsx",
            "manifest_version": "1",
            # No manifest_path — overlay loader treats manifest as default empty dict.
        },
        "position_ingestion": {
            "workbook_path": "data/fake_positions.xlsx",
            "manifest_path": "data/fake_position_manifest.yaml",
        },
    }
    # Position-ingestion manifest must exist on disk because the loader
    # would normally read it — but load_local_study_config only normalizes
    # the path; it does NOT open the manifest_path (that happens later at
    # orchestration time). Confirm by reading the loader.
    overlay_path.write_text(yaml.safe_dump(overlay), encoding="utf-8")
    try:
        cfg = load_local_study_config(overlay_path)
    finally:
        overlay_path.unlink(missing_ok=True)

    assert cfg.workbook_ingestion is not None
    assert Path(cfg.workbook_ingestion.workbook_path).is_absolute()
    assert cfg.workbook_ingestion.workbook_path.endswith("data/fake_workbook.xlsx") or \
        cfg.workbook_ingestion.workbook_path.endswith("data\\fake_workbook.xlsx")

    assert cfg.position_ingestion is not None
    assert Path(cfg.position_ingestion.workbook_path).is_absolute()
    assert Path(cfg.position_ingestion.manifest_path).is_absolute()


# ---- #7 config strictness --------------------------------------------------


def test_reconciliation_gates_rejects_unknown_keys():
    """Unknown keys must raise rather than be silently ignored — matches
    the rest of the io.schemas discipline (extra='forbid')."""
    with pytest.raises(ValueError):
        ReconciliationGatesConfig(warninng_pct=0.10)  # noqa — typo intentional


def test_liquidity_coverage_config_rejects_inverted_thresholds():
    """warning_threshold < breach_threshold is non-sensical (warning is a
    weaker signal than breach); fail loudly at config time."""
    with pytest.raises(ValueError):
        LiquidityCoverageConfig(
            liquid_coverage_breach_threshold=2.0,
            liquid_coverage_warning_threshold=1.0,
        )


def test_liquidity_coverage_config_rejects_zero_runway_horizon():
    with pytest.raises(ValueError):
        LiquidityCoverageConfig(runway_horizon_quarters=0)


# ---- #8 runway horizon warning ---------------------------------------------


def test_runway_horizon_quarters_emits_warning_when_below():
    """When liquidity runway falls below the configured horizon, a warning
    must be emitted. Default horizon is 8 quarters."""
    from aa_model.ingestion.schemas_position import PositionRecord

    pos = PositionRecord(
        position_id="p1",
        account_id="acct",
        manager_id=None,
        market_value_usd=200_000.0,
        unfunded_commitment_usd=None,
        liquidity_bucket="daily_liquid",
        valuation_date=datetime.date(2026, 3, 31),
        source_row=1,
    )
    # Liquid 200k, spend 100k → runway = 200k / 25k = 8 quarters → not below default 8.
    obs = LiquidityObligationConfig(annual_spend_usd=100_000.0)
    result = compute_liquidity_coverage([pos], obs)
    assert result.liquidity_runway_quarters == 8
    assert not any("liquidity_runway=" in w for w in result.diagnostics.warnings)

    # Tighten horizon to 12 → 8 < 12 → warning fires.
    cfg_strict = LiquidityCoverageConfig(runway_horizon_quarters=12)
    result_strict = compute_liquidity_coverage([pos], obs, config=cfg_strict)
    assert any("liquidity_runway=" in w for w in result_strict.diagnostics.warnings)


def test_runway_horizon_warning_silent_when_runway_unknown():
    """When annual_spend is None, runway is None — no warning should fire
    (silent rather than warning on a non-computable metric)."""
    from aa_model.ingestion.schemas_position import PositionRecord

    pos = PositionRecord(
        position_id="p1",
        account_id="acct",
        manager_id=None,
        market_value_usd=200_000.0,
        unfunded_commitment_usd=None,
        liquidity_bucket="daily_liquid",
        valuation_date=datetime.date(2026, 3, 31),
        source_row=1,
    )
    obs = LiquidityObligationConfig()  # annual_spend_usd=None
    result = compute_liquidity_coverage([pos], obs)
    assert result.liquidity_runway_quarters is None
    assert not any("liquidity_runway=" in w for w in result.diagnostics.warnings)
