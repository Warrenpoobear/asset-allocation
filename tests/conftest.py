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


def _build_owl_spending_config_under(repo_root, *, fixture_path: str) -> tuple[object, object]:
    """Internal helper used by Phase 4a exit-gate fixtures: write a
    base.yaml that points at ``fixture_path`` (a fixture YAML the loader
    can resolve relative to repo root) and uses the Owl spending rule.
    Returns (base_path, spending_path) so the caller can clean up.
    """
    import yaml

    configs = repo_root / "configs"
    spending_path = configs / "_test_spending_owl.yaml"
    spending_path.write_text(
        yaml.safe_dump(
            {
                "rule": "owl",
                "annual_spend_usd": 4_000_000.0,
                "inflation_pct": 0.025,
                "smoothing": {"window_quarters": 12, "weight": 0.0},
                "floor_usd": 0.0,
                "ceiling_usd": 1.0e12,
                "guardrail": {
                    "upper_band_pct": 0.20,
                    "lower_band_pct": 0.20,
                    "raise_pct": 0.10,
                    "cut_pct": 0.10,
                },
            }
        ),
        encoding="utf-8",
    )

    base_cfg = yaml.safe_load((configs / "base.yaml").read_text(encoding="utf-8"))
    base_cfg["spending"]["config"] = "configs/_test_spending_owl.yaml"
    base_cfg["fixtures"]["scenario"] = fixture_path
    base_path = configs / "_test_owl.yaml"
    base_path.write_text(yaml.safe_dump(base_cfg), encoding="utf-8")
    return base_path, spending_path


@pytest.fixture
def with_owl_spending_config(repo_root):
    """Yield a base.yaml inside repo configs/ + a sibling spending YAML
    using rule=owl with a guardrail block. Used to exercise the orchestrator
    end-to-end with the Owl spending rule.
    """
    import yaml

    configs = repo_root / "configs"

    spending_path = configs / "_test_spending_owl.yaml"
    spending_path.write_text(
        yaml.safe_dump(
            {
                "rule": "owl",
                "annual_spend_usd": 4_000_000.0,
                "inflation_pct": 0.025,
                "smoothing": {"window_quarters": 12, "weight": 0.0},
                "floor_usd": 0.0,
                "ceiling_usd": 1.0e12,
                "guardrail": {
                    "upper_band_pct": 0.20,
                    "lower_band_pct": 0.20,
                    "raise_pct": 0.10,
                    "cut_pct": 0.10,
                },
            }
        ),
        encoding="utf-8",
    )

    base_cfg = yaml.safe_load((configs / "base.yaml").read_text(encoding="utf-8"))
    base_cfg["spending"]["config"] = "configs/_test_spending_owl.yaml"
    base_path = configs / "_test_owl.yaml"
    base_path.write_text(yaml.safe_dump(base_cfg), encoding="utf-8")
    try:
        yield base_path
    finally:
        base_path.unlink(missing_ok=True)
        spending_path.unlink(missing_ok=True)


@pytest.fixture
def with_owl_on_drawdown_config(repo_root):
    """Phase 4a exit gate: Owl spending rule + drawdown fixture. Used to
    verify Owl reads realized post-shock NAV and cuts spending instead of
    raising.
    """
    base_path, spending_path = _build_owl_spending_config_under(
        repo_root, fixture_path="data/fixtures/scenarios/drawdown.yaml"
    )
    try:
        yield base_path
    finally:
        base_path.unlink(missing_ok=True)
        spending_path.unlink(missing_ok=True)


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


@pytest.fixture
def with_cvxportfolio_allocation_config(repo_root):
    """Phase 4b: cost-aware allocation engine end-to-end. Sets
    allocation.engine = cvxportfolio and a small policy_loss_lambda so
    target_at exercises the partial-trade branch under non-zero bps.
    """
    import yaml

    configs = repo_root / "configs"
    base_cfg = yaml.safe_load((configs / "base.yaml").read_text(encoding="utf-8"))
    base_cfg["allocation"] = {
        "engine": "cvxportfolio",
        "config": "configs/_test_cvx_alloc_public.yaml",
    }
    base_cfg["implementation"] = {"engine": "cvxportfolio", "bps_per_trade": 5.0}
    base_dst = configs / "_test_cvxportfolio_allocation.yaml"
    base_dst.write_text(yaml.safe_dump(base_cfg), encoding="utf-8")

    public_alloc = yaml.safe_load((configs / "public_allocation.yaml").read_text(encoding="utf-8"))
    # Base config governance.size_usd is $100M; setting λ_norm = 1e9 gives
    # λ_eff = 1e-7 (= 1e9 / (1e8)²) so the integration test reproduces the
    # cost-aware behavior used pre-normalization. See MODEL_DOCUMENTATION
    # §Phase 4b — normalized λ for migration formula.
    public_alloc["policy_loss_lambda_norm"] = 1e9
    public_dst = configs / "_test_cvx_alloc_public.yaml"
    public_dst.write_text(yaml.safe_dump(public_alloc), encoding="utf-8")
    try:
        yield base_dst
    finally:
        base_dst.unlink(missing_ok=True)
        public_dst.unlink(missing_ok=True)
