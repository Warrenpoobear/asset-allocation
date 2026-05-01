"""Phase 2 scenario builder tests."""

from __future__ import annotations

import pandas as pd
import pytest
from aa_model.assumptions.scenario_builder import Scenario, make_scenarios
from aa_model.integration.orchestrator import run_orchestrator
from aa_model.io.loaders import load_study_config


@pytest.fixture(scope="module")
def base_cfg(base_config_path):
    return load_study_config(base_config_path)


@pytest.fixture(scope="module")
def scenarios(base_cfg):
    return make_scenarios(base_cfg.fixture_scenario, base_cfg.pe_pacing, base_cfg.spending)


def test_five_canonical_scenarios(scenarios):
    names = [s.name for s in scenarios]
    assert names == [
        "base",
        "public_drawdown",
        "delayed_pe_distributions",
        "clustered_calls",
        "inflation_shock",
    ]


def test_each_scenario_carries_expected_override(scenarios):
    by_name = {s.name: s for s in scenarios}

    assert by_name["base"].fixture_scenario is None
    assert by_name["base"].pe_pacing is None
    assert by_name["base"].spending is None

    assert by_name["public_drawdown"].fixture_scenario is not None
    assert by_name["public_drawdown"].pe_pacing is None
    assert by_name["public_drawdown"].spending is None
    eq_overrides = by_name["public_drawdown"].fixture_scenario.returns["public_equity"].overrides
    assert any(o.quarter_index == 8 and o.value == -0.25 for o in eq_overrides)

    assert by_name["delayed_pe_distributions"].pe_pacing is not None
    assert by_name["delayed_pe_distributions"].pe_pacing.ta_defaults.bow == 4.0

    assert by_name["clustered_calls"].pe_pacing is not None
    assert by_name["clustered_calls"].pe_pacing.ta_defaults.rate_of_contribution == [
        0.50,
        0.30,
        0.15,
        0.05,
    ]

    assert by_name["inflation_shock"].spending is not None
    assert by_name["inflation_shock"].spending.inflation_pct == 0.06


def test_each_scenario_validates_and_runs(base_config_path, scenarios):
    for sc in scenarios:
        rr = run_orchestrator(base_config_path, scenario=sc, dry_run=True)
        assert rr.run_id  # validation passed; manifest built
        assert len(rr.ledger) > 0


def test_scenarios_produce_distinct_hash_signatures(base_config_path, scenarios):
    sigs = set()
    for sc in scenarios:
        rr = run_orchestrator(base_config_path, scenario=sc, dry_run=True)
        sigs.add((rr.manifest.config_hash, rr.manifest.fixtures_hash))
    # Every scenario should differ from base in at least one of the two hashes.
    assert len(sigs) == len(scenarios)


def test_scenario_reproducibility(base_config_path, scenarios):
    """Same scenario, two consecutive runs → byte-identical ledger content
    (modulo per-invocation run_id column).
    """
    for sc in scenarios:
        r1 = run_orchestrator(base_config_path, scenario=sc, dry_run=False)
        df1 = pd.read_parquet(r1.output_dir / "ledger.parquet").drop(columns="run_id")
        r2 = run_orchestrator(base_config_path, scenario=sc, dry_run=False)
        df2 = pd.read_parquet(r2.output_dir / "ledger.parquet").drop(columns="run_id")
        assert r1.manifest.config_hash == r2.manifest.config_hash
        assert r1.manifest.fixtures_hash == r2.manifest.fixtures_hash
        pd.testing.assert_frame_equal(df1, df2)


def test_scenario_dataclass_is_frozen():
    from dataclasses import FrozenInstanceError

    sc = Scenario(name="x", description="y")
    with pytest.raises(FrozenInstanceError):
        sc.name = "z"  # type: ignore[misc]
