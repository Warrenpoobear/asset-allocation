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
    CMAConfig,
    FixtureScenarioConfig,
    PEPacingConfig,
    PositionIngestionConfig,
    PublicAllocationConfig,
    ScenariosConfig,
    SpendingConfig,
    StudyConfig,
    WorkbookIngestionConfig,
)


def _read_yaml(path: Path | str) -> dict:
    with Path(path).open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_base_config(path: Path) -> BaseConfig:
    return BaseConfig.model_validate(_read_yaml(path))


def load_public_allocation_config(path: Path) -> PublicAllocationConfig:
    return PublicAllocationConfig.model_validate(_read_yaml(path))


def load_cma_config(path: Path) -> CMAConfig:
    return CMAConfig.model_validate(_read_yaml(path))


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
    raise FileNotFoundError(f"Could not find repo root (configs/ + src/aa_model/) above {start}")


def load_study_config(base_path: Path) -> StudyConfig:
    base_path = Path(base_path).resolve()
    root = resolve_repo_root(base_path)

    base = load_base_config(base_path)
    allocation = load_public_allocation_config(root / base.allocation.config)
    cma = load_cma_config(root / base.cma.config)
    spending = load_spending_config(root / base.spending.config)
    pe_pacing = load_pe_pacing_config(root / base.pe_pacing.config)
    scenarios = load_scenarios_config(root / base.scenarios.config)
    fixture = load_fixture_scenario(root / base.fixtures.scenario)

    return StudyConfig(
        base=base,
        allocation=allocation,
        cma=cma,
        spending=spending,
        pe_pacing=pe_pacing,
        scenarios=scenarios,
        fixture_scenario=fixture,
    )


def load_local_study_config(local_path: Path) -> StudyConfig:
    """Load a StudyConfig from a local overlay YAML (e.g. configs/base_local.yaml).

    The overlay format supports ``extends_from``, ``workbook_ingestion``
    (with a ``manifest_path`` key resolved here), and ``position_ingestion``.
    All paths are resolved relative to the repo root discovered from ``local_path``.
    """
    local_path = Path(local_path).resolve()
    root = resolve_repo_root(local_path)
    overlay = _read_yaml(local_path)

    extends_from = overlay.get("extends_from")
    if not extends_from:
        raise ValueError(f"overlay {local_path} must contain 'extends_from' key")

    base_cfg = load_study_config(root / extends_from)
    overrides: dict = {}

    wi_raw = dict(overlay.get("workbook_ingestion") or {})
    if wi_raw:
        manifest_path_str = wi_raw.pop("manifest_path", None)
        if manifest_path_str:
            wi_raw["manifest"] = _read_yaml(root / manifest_path_str)
        overrides["workbook_ingestion"] = WorkbookIngestionConfig(**wi_raw)

    pi_raw = dict(overlay.get("position_ingestion") or {})
    if pi_raw:
        raw_mp = pi_raw.get("manifest_path")
        if raw_mp and not Path(raw_mp).is_absolute():
            pi_raw["manifest_path"] = str(root / raw_mp)
        overrides["position_ingestion"] = PositionIngestionConfig(**pi_raw)

    for key in ("liquidity_obligations", "liquidity_coverage_config", "reconciliation_gates"):
        if key in overlay:
            overrides[key] = overlay[key]

    return base_cfg.model_copy(update=overrides)


def _hash_objects_canonical(objects: list[dict]) -> str:
    """SHA-256 over JSON-canonicalized dicts (sorted keys, fixed indent).

    Object-based hashing makes the hash invariant to whether the inputs
    came from disk or were synthesized in memory — Phase 2 scenarios
    perturb configs in memory, so hashing files would miss the override.
    """
    h = hashlib.sha256()
    parts = sorted(json.dumps(d, sort_keys=True, indent=2, default=str) for d in objects)
    for p in parts:
        h.update(p.encode("utf-8"))
    return f"sha256:{h.hexdigest()}"


def hash_study_config(cfg: StudyConfig) -> tuple[str, str]:
    """Return ``(config_hash, fixtures_hash)`` from the resolved study config.

    ``config_hash`` covers base + sub-configs (allocation, spending, pe_pacing,
    scenarios). ``fixtures_hash`` covers the active fixture scenario only.
    Each is computed from pydantic ``model_dump(mode='json')`` so an
    in-memory override changes the hash exactly the same way an edited
    YAML file would.
    """
    cfg_objs = [
        cfg.base.model_dump(mode="json"),
        cfg.allocation.model_dump(mode="json"),
        cfg.cma.model_dump(mode="json"),
        cfg.spending.model_dump(mode="json"),
        cfg.pe_pacing.model_dump(mode="json"),
        cfg.scenarios.model_dump(mode="json"),
    ]
    fix_objs = [cfg.fixture_scenario.model_dump(mode="json")]
    return _hash_objects_canonical(cfg_objs), _hash_objects_canonical(fix_objs)
