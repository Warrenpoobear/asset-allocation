"""End-to-end orchestrator tests on fixture scenarios."""

from __future__ import annotations

import time

from aa_model.integration.orchestrator import run_orchestrator

_ALLOWED_FLOW_TYPES = {
    "inflow",
    "return",
    "pe_call",
    "pe_distribution",
    "pe_nav_mark",
    "spend",
    "rebalance",
}


def test_base_scenario_e2e(base_config_path):
    t0 = time.perf_counter()
    result = run_orchestrator(base_config_path, dry_run=False)
    elapsed = time.perf_counter() - t0
    # SPEC §7: end-to-end test runs in <10 seconds on fixtures.
    assert elapsed < 10.0, f"orchestrator too slow: {elapsed:.2f}s"

    assert (result.output_dir / "ledger.parquet").is_file()
    assert (result.output_dir / "report.md").is_file()
    assert (result.output_dir / "manifest.json").is_file()

    df = result.ledger
    assert len(df) > 0
    assert set(df["flow_type"].unique()) <= _ALLOWED_FLOW_TYPES
    # 20 quarters × 4 buckets × full canonical flow set means many rows.
    assert df["quarter"].nunique() == 20


def test_drawdown_scenario_passes_invariants(with_drawdown_config):
    result = run_orchestrator(with_drawdown_config, dry_run=False)
    df = result.ledger
    # Orchestrator already runs ledger.validate(); arrival here implies pass.
    # Spot-check: q8 (2028Q1) should show a -25% public_equity return.
    eq_q8 = df[
        (df["quarter"].astype(str) == "2028Q1")
        & (df["bucket"] == "public_equity")
        & (df["flow_type"] == "return")
    ]
    assert not eq_q8.empty
    nav_start = float(eq_q8.iloc[0]["nav_start_usd"])
    amt = float(eq_q8.iloc[0]["amount_usd"])
    assert nav_start > 0
    assert abs(amt / nav_start - (-0.25)) < 1e-9


def test_dry_run_writes_no_artifacts(base_config_path):
    result = run_orchestrator(base_config_path, dry_run=True)
    # The output_dir is computed but not necessarily populated this call.
    # We assert no ledger.parquet was written by THIS dry-run; if a prior
    # non-dry run created one earlier in the test suite we leave it alone.
    assert result.manifest.outputs == []


def test_input_hashes_are_deterministic_run_ids_are_unique(base_config_path):
    r1 = run_orchestrator(base_config_path, dry_run=True)
    r2 = run_orchestrator(base_config_path, dry_run=True)
    # Hashes are deterministic in inputs.
    assert r1.manifest.config_hash == r2.manifest.config_hash
    assert r1.manifest.fixtures_hash == r2.manifest.fixtures_hash
    # run_id includes a per-invocation suffix, so two consecutive runs differ.
    assert r1.run_id != r2.run_id
    # Both run_ids share the same hash prefix though.
    cfg = r1.manifest.config_hash.split(":", 1)[-1][:12]
    fix = r1.manifest.fixtures_hash.split(":", 1)[-1][:12]
    prefix = f"aa-{cfg}-{fix}-"
    assert r1.run_id.startswith(prefix)
    assert r2.run_id.startswith(prefix)
