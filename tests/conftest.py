"""Shared pytest fixtures.

Adds ``src/`` to sys.path so the package is importable without an editable
install. Also wires the standard repo paths every test reuses.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_SRC = _REPO / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return _REPO


@pytest.fixture(scope="session")
def configs_dir(repo_root: Path) -> Path:
    return repo_root / "configs"


@pytest.fixture(scope="session")
def base_config_path(configs_dir: Path) -> Path:
    return configs_dir / "base.yaml"


@pytest.fixture
def with_drawdown_config(repo_root):
    """Yield a base.yaml inside the repo configs/ that points at the drawdown fixture.

    The loader's repo-root resolver requires the config to live inside the
    repo, so a tmp_path-based file would not work.
    """
    configs = repo_root / "configs"
    src_text = (configs / "base.yaml").read_text(encoding="utf-8")
    swapped = src_text.replace(
        "data/fixtures/scenarios/base.yaml",
        "data/fixtures/scenarios/drawdown.yaml",
    )
    dst = configs / "_test_drawdown.yaml"
    dst.write_text(swapped, encoding="utf-8")
    try:
        yield dst
    finally:
        dst.unlink(missing_ok=True)


@pytest.fixture
def with_cvxportfolio_config(repo_root):
    """Yield a base.yaml inside repo configs/ with implementation.engine =
    cvxportfolio and bps_per_trade > 0. Same constraint as drawdown: the
    loader requires the config to live inside the repo.
    """
    import yaml

    configs = repo_root / "configs"
    cfg = yaml.safe_load((configs / "base.yaml").read_text(encoding="utf-8"))
    cfg["implementation"] = {"engine": "cvxportfolio", "bps_per_trade": 5.0}
    dst = configs / "_test_cvxportfolio.yaml"
    dst.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    try:
        yield dst
    finally:
        dst.unlink(missing_ok=True)
