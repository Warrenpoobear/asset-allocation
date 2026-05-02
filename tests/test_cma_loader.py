"""Phase 5 — CMA schema, validation, loader, and cross-config tests.

Each per-cell rule has a paired pass/fail test (where applicable). The
PSD check has a constructed counter-example whose pairwise correlations
all sit in ``[-1, 1]`` but whose full covariance matrix has a negative
eigenvalue. The cross-config bucket-alignment check is exercised
through ``validate_study_config`` against an in-tree base config.

See MODEL_DOCUMENTATION.md §Phase 5 design.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
import yaml
from pydantic import ValidationError

from aa_model.assumptions.cma import CMA
from aa_model.io.loaders import load_cma_config, load_study_config
from aa_model.io.schemas import CMAConfig
from aa_model.io.validation import validate_study_config


def _valid_cma_dict() -> dict:
    return {
        "expected_returns_annual": {
            "cash": 0.04,
            "public_bond": 0.045,
            "public_equity": 0.075,
            "pe_buyout": 0.105,
        },
        "vol_annual": {
            "cash": 0.005,
            "public_bond": 0.04,
            "public_equity": 0.16,
            "pe_buyout": 0.20,
        },
        "correlations": {
            "cash": {
                "cash": 1.0,
                "public_bond": 0.10,
                "public_equity": -0.05,
                "pe_buyout": 0.0,
            },
            "public_bond": {
                "cash": 0.10,
                "public_bond": 1.0,
                "public_equity": 0.20,
                "pe_buyout": 0.30,
            },
            "public_equity": {
                "cash": -0.05,
                "public_bond": 0.20,
                "public_equity": 1.0,
                "pe_buyout": 0.65,
            },
            "pe_buyout": {
                "cash": 0.0,
                "public_bond": 0.30,
                "public_equity": 0.65,
                "pe_buyout": 1.0,
            },
        },
        "liquidity": {
            "cash": "liquid",
            "public_bond": "liquid",
            "public_equity": "liquid",
            "pe_buyout": "illiquid",
        },
    }


# ---- 1. Round-trip ----------------------------------------------------------


def test_valid_cma_round_trip_through_dataclass(tmp_path: Path):
    payload = _valid_cma_dict()
    p = tmp_path / "cma.yaml"
    p.write_text(yaml.safe_dump(payload), encoding="utf-8")

    cfg = load_cma_config(p)
    cma = CMA.from_config(cfg)

    expected_buckets = sorted(payload["expected_returns_annual"].keys())
    assert list(cma.expected_returns_annual.index) == expected_buckets
    assert list(cma.vol_annual.index) == expected_buckets
    assert list(cma.corr.index) == expected_buckets
    assert list(cma.corr.columns) == expected_buckets

    for b in expected_buckets:
        assert cma.expected_returns_annual[b] == payload["expected_returns_annual"][b]
        assert cma.vol_annual[b] == payload["vol_annual"][b]
        assert cma.liquidity[b] == payload["liquidity"][b]
    for i in expected_buckets:
        for j in expected_buckets:
            assert cma.corr.loc[i, j] == payload["correlations"][i][j]


# ---- 2. Negative vol --------------------------------------------------------


def test_negative_vol_fails():
    payload = _valid_cma_dict()
    payload["vol_annual"]["public_equity"] = -0.1
    with pytest.raises(ValidationError, match=r"vol_annual\['public_equity'\] = -0\.1 < 0"):
        CMAConfig.model_validate(payload)


# ---- 3. NaN expected return -------------------------------------------------


def test_nan_expected_return_fails():
    payload = _valid_cma_dict()
    payload["expected_returns_annual"]["cash"] = float("nan")
    with pytest.raises(ValidationError, match="expected_returns_annual.+is not finite"):
        CMAConfig.model_validate(payload)


# ---- 4. Out-of-bounds expected return (percent-vs-decimal guard) -----------


def test_expected_return_above_bound_fails():
    payload = _valid_cma_dict()
    payload["expected_returns_annual"]["public_equity"] = 5.0  # 500%, likely meant 0.05
    with pytest.raises(ValidationError, match="out of bounds"):
        CMAConfig.model_validate(payload)


def test_expected_return_negative_below_bound_fails():
    payload = _valid_cma_dict()
    payload["expected_returns_annual"]["public_equity"] = -2.0
    with pytest.raises(ValidationError, match="out of bounds"):
        CMAConfig.model_validate(payload)


# ---- 5. Out-of-range correlation -------------------------------------------


def test_correlation_above_one_fails():
    payload = _valid_cma_dict()
    payload["correlations"]["cash"]["public_bond"] = 1.05
    payload["correlations"]["public_bond"]["cash"] = 1.05  # symmetric, isolate the |.|>1 rule
    with pytest.raises(ValidationError, match=r"out of \[-1, 1\]"):
        CMAConfig.model_validate(payload)


# ---- 6. Asymmetric correlation ---------------------------------------------


def test_asymmetric_correlation_fails():
    payload = _valid_cma_dict()
    payload["correlations"]["cash"]["public_bond"] = 0.5
    payload["correlations"]["public_bond"]["cash"] = 0.4
    with pytest.raises(ValidationError, match="asymmetry"):
        CMAConfig.model_validate(payload)


# ---- 7. Diagonal != 1 -------------------------------------------------------


def test_correlation_diagonal_not_one_fails():
    payload = _valid_cma_dict()
    payload["correlations"]["cash"]["cash"] = 0.99
    with pytest.raises(ValidationError, match="diagonal must be 1.0"):
        CMAConfig.model_validate(payload)


# ---- 8. Bucket-set mismatch (cross-config) ---------------------------------


def test_cma_bucket_mismatch_fails_cross_config(tmp_path: Path, repo_root: Path):
    """Cross-config: CMA's bucket set must equal allocation.stub_weights."""
    # Build a temp base.yaml that points at a CMA missing pe_buyout and adding pe_growth.
    configs = repo_root / "configs"
    base = yaml.safe_load((configs / "base.yaml").read_text(encoding="utf-8"))
    cma_payload = _valid_cma_dict()
    # remove pe_buyout, add pe_growth
    for d in (cma_payload["expected_returns_annual"], cma_payload["vol_annual"]):
        del d["pe_buyout"]
        d["pe_growth"] = 0.04
    new_corr = {}
    for outer, row in cma_payload["correlations"].items():
        if outer == "pe_buyout":
            continue
        new_row = {k: v for k, v in row.items() if k != "pe_buyout"}
        new_row["pe_growth"] = 0.0
        new_corr[outer] = new_row
    new_corr["pe_growth"] = {
        "cash": 0.0,
        "public_bond": 0.0,
        "public_equity": 0.0,
        "pe_growth": 1.0,
    }
    cma_payload["correlations"] = new_corr
    if cma_payload["liquidity"] is not None:
        del cma_payload["liquidity"]["pe_buyout"]
        cma_payload["liquidity"]["pe_growth"] = "illiquid"

    bad_cma_path = configs / "_test_cma_bad_buckets.yaml"
    bad_cma_path.write_text(yaml.safe_dump(cma_payload), encoding="utf-8")
    base["cma"] = {"config": "configs/_test_cma_bad_buckets.yaml"}
    base_path = configs / "_test_base_bad_cma.yaml"
    base_path.write_text(yaml.safe_dump(base), encoding="utf-8")
    try:
        cfg = load_study_config(base_path)
        with pytest.raises(
            ValueError,
            match=r"CMA bucket set does not match allocation\.stub_weights",
        ):
            validate_study_config(cfg)
    finally:
        bad_cma_path.unlink(missing_ok=True)
        base_path.unlink(missing_ok=True)


# ---- 9. Non-PSD matrix ------------------------------------------------------


def test_non_psd_matrix_fails():
    """3-bucket counter-example: pairwise correlations all in [-1, 1] but
    the full matrix is not PSD. Constructed via the classic
    ``corr(a,b) = corr(b,c) = 0.99, corr(a,c) = -0.99`` pattern.
    """
    payload = {
        "expected_returns_annual": {"a": 0.05, "b": 0.06, "c": 0.07},
        "vol_annual": {"a": 0.10, "b": 0.10, "c": 0.10},
        "correlations": {
            "a": {"a": 1.0, "b": 0.99, "c": -0.99},
            "b": {"a": 0.99, "b": 1.0, "c": 0.99},
            "c": {"a": -0.99, "b": 0.99, "c": 1.0},
        },
    }
    with pytest.raises(ValidationError, match="not positive semi-definite"):
        CMAConfig.model_validate(payload)


# ---- 10. Liquidity optional + validated ------------------------------------


def test_liquidity_absent_is_valid():
    payload = _valid_cma_dict()
    del payload["liquidity"]
    cfg = CMAConfig.model_validate(payload)
    assert cfg.liquidity is None


def test_liquidity_invalid_tag_fails():
    payload = _valid_cma_dict()
    payload["liquidity"]["public_equity"] = "kinda_liquid"
    with pytest.raises(ValidationError):
        CMAConfig.model_validate(payload)


def test_liquidity_bucket_mismatch_fails():
    payload = _valid_cma_dict()
    del payload["liquidity"]["pe_buyout"]
    with pytest.raises(ValidationError, match="liquidity bucket set mismatch"):
        CMAConfig.model_validate(payload)


# ---- shipped configs/cma.yaml is well-formed -------------------------------


def test_shipped_cma_yaml_loads_and_aligns(repo_root: Path):
    """The repo-shipped configs/cma.yaml must round-trip cleanly and align
    with allocation.stub_weights — guards against drift if either file is
    edited in isolation.
    """
    cfg = load_cma_config(repo_root / "configs" / "cma.yaml")
    cma = CMA.from_config(cfg)
    public_alloc = yaml.safe_load(
        (repo_root / "configs" / "public_allocation.yaml").read_text(encoding="utf-8")
    )
    assert set(cma.expected_returns_annual.index) == set(public_alloc["stub_weights"].keys())
