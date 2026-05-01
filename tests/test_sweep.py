"""End-to-end sweep tests + Phase 2 exit gate."""

from __future__ import annotations

import time

from aa_model.assumptions.scenario_builder import make_scenarios
from aa_model.integration.comparison_report import write_comparison_report
from aa_model.integration.sweep import run_scenario_sweep
from aa_model.io.loaders import load_study_config


def test_sweep_runs_five_scenarios_in_under_60s(base_config_path):
    """SPEC §6 Phase 2 exit gate: ≥5 scenarios complete in <60s on fixtures."""
    cfg = load_study_config(base_config_path)
    scenarios = make_scenarios(cfg.fixture_scenario, cfg.pe_pacing, cfg.spending)
    assert len(scenarios) >= 5

    t0 = time.perf_counter()
    sweep = run_scenario_sweep(base_config_path, scenarios, dry_run=False)
    elapsed = time.perf_counter() - t0

    assert elapsed < 60.0, f"sweep too slow: {elapsed:.2f}s"
    assert len(sweep.results) == len(scenarios)


def test_sweep_writes_comparison_html_and_md(base_config_path):
    cfg = load_study_config(base_config_path)
    scenarios = make_scenarios(cfg.fixture_scenario, cfg.pe_pacing, cfg.spending)
    sweep = run_scenario_sweep(base_config_path, scenarios, dry_run=False)
    html_path = write_comparison_report(sweep)
    md_path = sweep.output_dir / "comparison.md"

    assert html_path.is_file()
    assert html_path.name == "comparison.html"
    assert md_path.is_file()

    html = html_path.read_text(encoding="utf-8")
    assert "<table>" in html
    # Every scenario name appears in the HTML.
    for r in sweep.results:
        assert r.name in html


def test_sweep_runs_land_in_distinct_dirs(base_config_path):
    cfg = load_study_config(base_config_path)
    scenarios = make_scenarios(cfg.fixture_scenario, cfg.pe_pacing, cfg.spending)
    sweep = run_scenario_sweep(base_config_path, scenarios, dry_run=False)
    out_dirs = {r.run.output_dir for r in sweep.results}
    assert len(out_dirs) == len(sweep.results)


def test_per_scenario_metrics_are_finite(base_config_path):
    cfg = load_study_config(base_config_path)
    scenarios = make_scenarios(cfg.fixture_scenario, cfg.pe_pacing, cfg.spending)
    sweep = run_scenario_sweep(base_config_path, scenarios, dry_run=True)
    for r in sweep.results:
        m = r.metrics
        assert m.final_nav_usd > 0
        assert m.max_drawdown_pct <= 0
        assert 0.0 <= m.shortfall_frequency <= 1.0
        # Coverage must be finite on the fixtures (annual_spend > 0).
        assert m.min_coverage_months > 0


def test_drawdown_scenario_shows_actual_drawdown(base_config_path):
    """Among the five fixtures, only public_drawdown should have max_dd < 0."""
    cfg = load_study_config(base_config_path)
    scenarios = make_scenarios(cfg.fixture_scenario, cfg.pe_pacing, cfg.spending)
    sweep = run_scenario_sweep(base_config_path, scenarios, dry_run=True)
    by_name = {r.name: r for r in sweep.results}
    assert by_name["public_drawdown"].metrics.max_drawdown_pct < -1.0
    assert by_name["base"].metrics.max_drawdown_pct == 0.0
    assert by_name["public_drawdown"].metrics.drawdown_quarters >= 1
