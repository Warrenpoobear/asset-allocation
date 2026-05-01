"""YAML config loaders + canonical hashing helpers.

The loader resolves the base config, then walks the sub-config + fixture refs
relative to the repo root so configs can be invoked from any cwd.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml

from aa_model.io.schemas import (
    BaseConfig,
    FixtureScenarioConfig,
    PEPacingConfig,
    PublicAllocationConfig,
    ScenariosConfig,
    SpendingConfig,
    StudyConfig,
)


def _read_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_base_config(path: Path) -> BaseConfig:
    return BaseConfig.model_validate(_read_yaml(path))


def load_public_allocation_config(path: Path) -> PublicAllocationConfig:
    return PublicAllocationConfig.model_validate(_read_yaml(path))


def load_spending_config(path: Path) -> SpendingConfig:
    return SpendingConfig.model_validate(_read_yaml(path))


def load_pe_pacing_config(path: Path) -> PEPacingConfig:
    return PEPacingConfig.model_validate(_read_yaml(path))


def load_scenarios_config(path: Path) -> ScenariosConfig:
    return ScenariosConfig.model_validate(_read_yaml(path))


def load_fixture_scenario(path: Path) -> FixtureScenarioConfig:
    return FixtureScenarioConfig.model_validate(_read_yaml(path))


def resolve_repo_root(start: Path) -> Path:
    p = start.resolve().parent if start.is_file() else start.resolve()
    while p != p.parent:
        if (p / "configs").is_dir() and (p / "src" / "aa_model").is_dir():
            return p
        p = p.parent
    raise FileNotFoundError(
        f"Could not find repo root (configs/ + src/aa_model/) above {start}"
    )


def load_study_config(base_path: Path) -> StudyConfig:
    base_path = Path(base_path).resolve()
    root = resolve_repo_root(base_path)

    base = load_base_config(base_path)
    allocation = load_public_allocation_config(root / base.allocation.config)
    spending = load_spending_config(root / base.spending.config)
    pe_pacing = load_pe_pacing_config(root / base.pe_pacing.config)
    scenarios = load_scenarios_config(root / base.scenarios.config)
    fixture = load_fixture_scenario(root / base.fixtures.scenario)

    return StudyConfig(
        base=base,
        allocation=allocation,
        spending=spending,
        pe_pacing=pe_pacing,
        scenarios=scenarios,
        fixture_scenario=fixture,
    )


def collect_config_paths(base_path: Path) -> list[Path]:
    """Every YAML the run depends on, for hashing."""
    base_path = Path(base_path).resolve()
    root = resolve_repo_root(base_path)
    base = load_base_config(base_path)
    return [
        base_path,
        root / base.allocation.config,
        root / base.spending.config,
        root / base.pe_pacing.config,
        root / base.scenarios.config,
        root / base.fixtures.scenario,
    ]


def canonicalize_yaml_for_hash(path: Path) -> bytes:
    """Canonical bytes for hashing: parse YAML, JSON-dump with sorted keys + 2-space indent."""
    data = _read_yaml(path)
    return json.dumps(data, sort_keys=True, indent=2, default=str).encode("utf-8")


def hash_files(paths: list[Path]) -> str:
    """SHA-256 over canonicalized concat of files in sorted-by-path order (SPEC §8)."""
    h = hashlib.sha256()
    for p in sorted(paths):
        h.update(canonicalize_yaml_for_hash(p))
    return f"sha256:{h.hexdigest()}"
