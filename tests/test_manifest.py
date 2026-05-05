"""Reproducibility tests (SPEC §7): two consecutive runs on the same config
produce ledger content that is byte-identical once the per-invocation
``run_id`` metadata column is dropped, while landing in distinct run dirs
(SPEC §8 "never overwritten").
"""

from __future__ import annotations

import json

import pandas as pd
from aa_model.integration.manifest import make_run_id
from aa_model.integration.orchestrator import run_orchestrator


def _ledger_content(run_dir) -> pd.DataFrame:
    return pd.read_parquet(run_dir / "ledger.parquet").drop(columns="run_id")


def test_two_runs_have_distinct_dirs_but_byte_identical_content(base_config_path):
    r1 = run_orchestrator(base_config_path, dry_run=False)
    df1 = _ledger_content(r1.output_dir)

    r2 = run_orchestrator(base_config_path, dry_run=False)
    df2 = _ledger_content(r2.output_dir)

    # Distinct dirs / run_ids per invocation.
    assert r1.run_id != r2.run_id
    assert r1.output_dir != r2.output_dir
    assert r1.output_dir.is_dir() and r2.output_dir.is_dir()

    # Hashes deterministic in inputs.
    assert r1.manifest.config_hash == r2.manifest.config_hash
    assert r1.manifest.fixtures_hash == r2.manifest.fixtures_hash

    # Ledger content (excluding run_id column) is byte-identical.
    pd.testing.assert_frame_equal(df1, df2)


def test_manifest_json_is_valid_and_pinned_keys(base_config_path):
    result = run_orchestrator(base_config_path, dry_run=False)
    text = (result.output_dir / "manifest.json").read_text(encoding="utf-8")
    data = json.loads(text)
    expected_keys = {
        "run_id",
        "config_hash",
        "fixtures_hash",
        "library_versions",
        "seed",
        "started_at",
        "finished_at",
        "outputs",
    }
    assert expected_keys <= set(data.keys())
    assert data["seed"] == 42
    assert data["config_hash"].startswith("sha256:")
    assert data["fixtures_hash"].startswith("sha256:")


def test_run_id_combines_hashes_and_invocation_suffix():
    rid = make_run_id(
        "sha256:abcdef0123456789aabb",
        "sha256:zzzzyyyy00112233xxxx",
        invocation_id="20260501T120000Z-a3f9",
    )
    assert rid == "aa-abcdef012345-zzzzyyyy0011-20260501T120000Z-a3f9"


def test_explicit_invocation_id_reproduces_run_dir(base_config_path):
    r1 = run_orchestrator(base_config_path, dry_run=True, invocation_id="20260501T999999Z-test")
    r2 = run_orchestrator(base_config_path, dry_run=True, invocation_id="20260501T999999Z-test")
    assert r1.run_id == r2.run_id
    assert r1.output_dir == r2.output_dir


def test_invocation_id_rejects_path_traversal():
    import pytest

    cfg = "sha256:abcdef0123456789aabb"
    fix = "sha256:zzzzyyyy00112233xxxx"
    # Each of these would let the run_id escape its parent directory if
    # interpolated unchecked into ``base_dir / run_id``.
    for bad in ("../escape", "..", "a/b", "a\\b", "", "a" * 81, "spaces here", "a b"):
        with pytest.raises(ValueError, match="invocation_id"):
            make_run_id(cfg, fix, invocation_id=bad)


def test_invocation_id_accepts_safe_values():
    cfg = "sha256:abcdef0123456789aabb"
    fix = "sha256:zzzzyyyy00112233xxxx"
    # Plain timestamp+nonce, hyphen-suffixed scenarios, and underscores all OK.
    make_run_id(cfg, fix, invocation_id="20260501T120000Z-a3f9")
    make_run_id(cfg, fix, invocation_id="20260501T120000Z-a3f9-inflation_shock")
    make_run_id(cfg, fix, invocation_id="some_label-42")
