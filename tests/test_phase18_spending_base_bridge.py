"""Phase 18 / L20 — SpendingBaseBreakdown bridge tests.

8 tests. Synthetic fixtures only — no live workbook, no real positions,
no OwlRule instance. See MODEL_DOCUMENTATION.md §Phase 18 design.

Coverage (8 tests):
1.  _normalize_bool_keyed_dict: Python bool keys pass through unchanged.
2.  _normalize_bool_keyed_dict: string keys ("true"/"false") normalize correctly.
3.  _extract_spending_base_for_coverage: None diagnostics → (None, []).
4.  _extract_spending_base_for_coverage: non-Owl engine → (None, []).
5.  _extract_spending_base_for_coverage: NAV-side, run too short → (None, advisory).
6.  _extract_spending_base_for_coverage: NAV-side, year-boundary reached → breakdown.
7.  _extract_spending_base_for_coverage: distributable_income, bootstrap → advisory.
8.  _extract_spending_base_for_coverage: distributable_income, run too short → (None, advisory).
"""

from __future__ import annotations

import pytest

from aa_model.integration.orchestrator import (
    _extract_spending_base_for_coverage,
    _normalize_bool_keyed_dict,
)


# ---- shared synthetic helpers -----------------------------------------------


def _nav_diagnostics(
    *,
    spending_base_mode: str | None = None,
    spending_base_run_end_usd: float = 1_000_000.0,
    excluded_nav_by_tier_usd: dict | None = None,
    excluded_nav_by_income_flag_usd: dict | None = None,
) -> dict:
    return {
        "engine": "OwlRule",
        "spending_base_mode": spending_base_mode,
        "spending_base_run_end_usd": spending_base_run_end_usd,
        "excluded_nav_by_tier_usd": excluded_nav_by_tier_usd or {},
        "excluded_nav_by_income_flag_usd": excluded_nav_by_income_flag_usd or {},
        "trailing_distributable_income_usd": 0.0,
        "distributable_income_by_source_usd": {},
        "used_bootstrap_at_run_end": False,
    }


def _income_diagnostics(
    *,
    trailing_usd: float = 80_000.0,
    is_bootstrap: bool = False,
    by_source: dict | None = None,
) -> dict:
    return {
        "engine": "OwlRule",
        "spending_base_mode": "distributable_income",
        "spending_base_run_end_usd": 0.0,
        "excluded_nav_by_tier_usd": {},
        "excluded_nav_by_income_flag_usd": {},
        "trailing_distributable_income_usd": trailing_usd,
        "distributable_income_by_source_usd": by_source or {},
        "used_bootstrap_at_run_end": is_bootstrap,
    }


def _stub_cfg():
    """Minimal StudyConfig stub — _extract_spending_base_for_coverage only reads
    cfg.spending.rule and cfg.spending.guardrail; we don't exercise that path
    in these unit tests (it's covered in orchestrator integration tests).
    We pass a plain object with the minimum attribute surface needed.
    """
    from pathlib import Path
    from aa_model.io.loaders import load_study_config

    config_path = Path(__file__).parents[1] / "configs" / "base.yaml"
    if not config_path.exists():
        pytest.skip("base.yaml fixture not available in this environment")
    return load_study_config(config_path)


# ---- 1. _normalize_bool_keyed_dict: Python bool keys -----------------------


def test_normalize_bool_keyed_dict_python_bool():
    """Phase 18 #1: Python bool keys pass through as-is."""
    d = {True: 400_000.0, False: 200_000.0}
    result = _normalize_bool_keyed_dict(d)
    assert result[True] == pytest.approx(400_000.0)
    assert result[False] == pytest.approx(200_000.0)


# ---- 2. _normalize_bool_keyed_dict: string keys normalize ------------------


def test_normalize_bool_keyed_dict_string_keys():
    """Phase 18 #2: string 'true'/'false' and 'True'/'False' normalize to bool."""
    d = {"true": 300_000.0, "False": 100_000.0}
    result = _normalize_bool_keyed_dict(d)
    assert result[True] == pytest.approx(300_000.0)
    assert result[False] == pytest.approx(100_000.0)


# ---- 3. None diagnostics → (None, []) --------------------------------------


def test_extract_none_diagnostics_returns_none():
    """Phase 18 #3: None spending_diagnostics returns (None, []) — non-Owl rule."""
    cfg = _stub_cfg()
    breakdown, advisories = _extract_spending_base_for_coverage(None, cfg)
    assert breakdown is None
    assert advisories == []


# ---- 4. non-Owl engine → (None, []) ----------------------------------------


def test_extract_non_owl_engine_returns_none():
    """Phase 18 #4: non-Owl engine field returns (None, [])."""
    cfg = _stub_cfg()
    diag = {"engine": "flat_real", "spending_base_mode": None}
    breakdown, advisories = _extract_spending_base_for_coverage(diag, cfg)
    assert breakdown is None
    assert advisories == []


# ---- 5. NAV-side, run too short → (None, advisory) -------------------------


def test_extract_nav_side_run_too_short():
    """Phase 18 #5: NAV-side spending_base_run_end_usd=0 → None + run-too-short advisory."""
    cfg = _stub_cfg()
    diag = _nav_diagnostics(spending_base_run_end_usd=0.0)
    breakdown, advisories = _extract_spending_base_for_coverage(diag, cfg)
    assert breakdown is None
    assert len(advisories) == 1
    assert "too short" in advisories[0]
    assert "liquid_to_spending_base" in advisories[0]


# ---- 6. NAV-side, year-boundary reached → breakdown ------------------------


def test_extract_nav_side_year_boundary_reached():
    """Phase 18 #6: NAV-side with base_usd > 0 returns correct SpendingBaseBreakdown."""
    from aa_model.spending.spending_base import SpendingBaseBreakdown

    cfg = _stub_cfg()
    diag = _nav_diagnostics(
        spending_base_mode="liquid_nav",
        spending_base_run_end_usd=500_000.0,
        excluded_nav_by_tier_usd={"illiquid": 300_000.0},
        excluded_nav_by_income_flag_usd={False: 150_000.0},
    )
    breakdown, advisories = _extract_spending_base_for_coverage(diag, cfg)

    assert isinstance(breakdown, SpendingBaseBreakdown)
    assert breakdown.base_usd == pytest.approx(500_000.0)
    assert breakdown.excluded_by_tier_usd == {"illiquid": pytest.approx(300_000.0)}
    assert breakdown.excluded_by_income_flag_usd == {False: pytest.approx(150_000.0)}
    assert advisories == []


# ---- 7. distributable_income, bootstrap → advisory -------------------------


def test_extract_income_mode_bootstrap_emits_advisory():
    """Phase 18 #7: distributable_income bootstrap=True emits advisory but returns breakdown."""
    from aa_model.spending.spending_base import SpendingBaseBreakdown

    cfg = _stub_cfg()
    diag = _income_diagnostics(
        trailing_usd=60_000.0,
        is_bootstrap=True,
        by_source={"fixed_income": 60_000.0},
    )
    breakdown, advisories = _extract_spending_base_for_coverage(diag, cfg)

    assert isinstance(breakdown, SpendingBaseBreakdown)
    assert breakdown.base_usd == pytest.approx(60_000.0)
    assert breakdown.is_bootstrap is True
    assert breakdown.distributable_income_by_source_usd == {
        "fixed_income": pytest.approx(60_000.0)
    }
    assert len(advisories) == 1
    assert "bootstrap" in advisories[0]


# ---- 8. distributable_income, run too short → (None, advisory) -------------


def test_extract_income_mode_run_too_short():
    """Phase 18 #8: distributable_income trailing_usd=0 → None + run-too-short advisory."""
    cfg = _stub_cfg()
    diag = _income_diagnostics(trailing_usd=0.0)
    breakdown, advisories = _extract_spending_base_for_coverage(diag, cfg)
    assert breakdown is None
    assert len(advisories) == 1
    assert "too short" in advisories[0]
    assert "liquid_nav_to_annual_income_estimate" in advisories[0]
