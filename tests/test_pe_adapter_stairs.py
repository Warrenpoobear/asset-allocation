"""Phase 7 — STAIRS PE adapter tests.

Two layers:

* **Schema-level** — STAIRS-specific fields (per-sleeve drift bound,
  finite beta) + cross-config validation (engine=stairs requires
  stairs_defaults; per_sleeve keys must equal pe_* subset of
  allocation.stub_weights).
* **Adapter-level** — parity at zero coupling, beta amplification,
  idiosyncratic-only path, public-equity decoupling, linear
  commitment, growth-clip activation.

End-to-end orchestrator coverage (parity bit-stable byte-for-byte at
zero coupling) lives in this file too.

See MODEL_DOCUMENTATION.md §Phase 7 design.
"""

from __future__ import annotations

import math
from pathlib import Path

import pandas as pd
import pytest
import yaml
from aa_model.assumptions.cma import CMA
from aa_model.io.loaders import load_study_config
from aa_model.io.schemas import (
    PEPacingConfig,
    StairsDefaultsConfig,
    _StairsSleeveParams,
)
from aa_model.io.validation import validate_study_config
from aa_model.pe.factory import make_pe_adapter
from aa_model.pe.stairs_adapter import STAIRSAdapter
from aa_model.pe.ta_adapter import TAAdapter
from pydantic import ValidationError


def _q(s: str) -> pd.Period:
    return pd.Period(s, freq="Q-DEC")


def _zero_path(start_q: pd.Period, n: int) -> pd.Series:
    idx = pd.PeriodIndex([start_q + i for i in range(n)], name="quarter")
    return pd.Series(0.0, index=idx, dtype=float, name="public_equity")


def _flat_path(start_q: pd.Period, n: int, rate: float) -> pd.Series:
    idx = pd.PeriodIndex([start_q + i for i in range(n)], name="quarter")
    return pd.Series(rate, index=idx, dtype=float, name="public_equity")


def _cma_with_pu_expected(annual_pu: float) -> CMA:
    er = pd.Series(
        {"cash": 0.0, "public_bond": 0.0, "public_equity": annual_pu, "pe_buyout": 0.0},
        dtype=float,
    ).sort_index()
    vol = pd.Series(
        {"cash": 0.005, "public_bond": 0.04, "public_equity": 0.16, "pe_buyout": 0.20},
        dtype=float,
    ).sort_index()
    idx = list(er.index)
    corr = pd.DataFrame(
        [[1.0 if i == j else 0.0 for j in idx] for i in idx],
        index=idx,
        columns=idx,
    )
    return CMA(expected_returns_annual=er, vol_annual=vol, corr=corr)


def _shipped_pacing(repo_root: Path) -> PEPacingConfig:
    """The repo-shipped pe_pacing.yaml as a parsed PEPacingConfig
    (without stairs_defaults). Useful as a baseline for parity tests.
    """
    payload = yaml.safe_load((repo_root / "configs" / "pe_pacing.yaml").read_text(encoding="utf-8"))
    return PEPacingConfig.model_validate(payload)


def _shipped_pacing_with_stairs(
    repo_root: Path,
    *,
    drift: float,
    beta: float,
) -> PEPacingConfig:
    """Shipped pacing extended with stairs_defaults for the lone pe_buyout
    sleeve. Used to construct parity / non-parity STAIRS configs.
    """
    base = _shipped_pacing(repo_root)
    return base.model_copy(
        update={
            "stairs_defaults": StairsDefaultsConfig(
                per_sleeve={
                    "pe_buyout": _StairsSleeveParams(
                        idiosyncratic_drift_pct=drift,
                        beta_to_public_equity=beta,
                    )
                }
            )
        }
    )


# ---- schema -----------------------------------------------------------------


def test_stairs_sleeve_params_drift_out_of_bounds_fails():
    with pytest.raises(ValidationError, match="out of bounds"):
        _StairsSleeveParams.model_validate(
            {"idiosyncratic_drift_pct": 5.0, "beta_to_public_equity": 1.0}
        )


def test_stairs_sleeve_params_drift_non_finite_fails():
    with pytest.raises(ValidationError, match="not finite"):
        _StairsSleeveParams.model_validate(
            {"idiosyncratic_drift_pct": math.inf, "beta_to_public_equity": 1.0}
        )


def test_stairs_sleeve_params_beta_non_finite_fails():
    with pytest.raises(ValidationError, match="not finite"):
        _StairsSleeveParams.model_validate(
            {"idiosyncratic_drift_pct": 0.05, "beta_to_public_equity": float("nan")}
        )


def test_stairs_defaults_per_sleeve_non_empty():
    with pytest.raises(ValidationError, match="must be non-empty"):
        StairsDefaultsConfig.model_validate({"per_sleeve": {}})


# ---- cross-config (engine=stairs alignment) --------------------------------


def test_stairs_engine_without_stairs_defaults_fails(repo_root: Path):
    configs = repo_root / "configs"
    base = yaml.safe_load((configs / "base.yaml").read_text(encoding="utf-8"))
    base.setdefault("pe", {})["engine"] = "stairs"
    base_path = configs / "_test_base_stairs_no_defaults.yaml"
    base_path.write_text(yaml.safe_dump(base), encoding="utf-8")
    try:
        cfg = load_study_config(base_path)
        with pytest.raises(ValueError, match="requires pe_pacing.stairs_defaults"):
            validate_study_config(cfg)
    finally:
        base_path.unlink(missing_ok=True)


def test_stairs_engine_with_sleeve_mismatch_fails(repo_root: Path):
    configs = repo_root / "configs"
    base = yaml.safe_load((configs / "base.yaml").read_text(encoding="utf-8"))
    base.setdefault("pe", {})["engine"] = "stairs"
    base["pe_pacing"] = {"config": "configs/_test_pe_pacing_stairs_bad.yaml"}
    base_path = configs / "_test_base_stairs_bad_sleeve.yaml"
    base_path.write_text(yaml.safe_dump(base), encoding="utf-8")

    pe = yaml.safe_load((configs / "pe_pacing.yaml").read_text(encoding="utf-8"))
    # CMA bucket pe_buyout is in stub_weights; stairs_defaults uses
    # pe_growth (not in stub_weights). Cross-config validator should
    # fail with both missing + extra.
    pe["stairs_defaults"] = {
        "per_sleeve": {
            "pe_growth": {
                "idiosyncratic_drift_pct": 0.05,
                "beta_to_public_equity": 1.5,
            }
        }
    }
    pe_path = configs / "_test_pe_pacing_stairs_bad.yaml"
    pe_path.write_text(yaml.safe_dump(pe), encoding="utf-8")

    try:
        cfg = load_study_config(base_path)
        with pytest.raises(ValueError, match=r"missing: \['pe_buyout'\].*extra: \['pe_growth'\]"):
            validate_study_config(cfg)
    finally:
        base_path.unlink(missing_ok=True)
        pe_path.unlink(missing_ok=True)


# ---- adapter parity at zero coupling ---------------------------------------


def test_stairs_at_zero_beta_matches_ta_per_fund(repo_root: Path):
    """Phase 7 structural anchor: at beta=0 and idiosyncratic_drift_pct =
    ta_defaults.growth_pct, STAIRS must produce byte-equivalent output
    to TA. Same pattern as riskfolio's binding-equality contract.
    """
    pacing_ta = _shipped_pacing(repo_root)
    pacing_stairs = _shipped_pacing_with_stairs(
        repo_root,
        drift=pacing_ta.ta_defaults.growth_pct,  # match TA's growth_pct
        beta=0.0,
    )
    cma = _cma_with_pu_expected(annual_pu=0.075)
    start_q = _q("2026Q1")
    n = 20
    # Path values are irrelevant at beta=0 — test fixture chooses non-zero
    # to prove decoupling.
    path = _flat_path(start_q, n, 0.05)

    ta = TAAdapter().project_horizon(pacing_ta, start_q, n, cma=cma, public_equity_path=path)
    stairs = STAIRSAdapter().project_horizon(
        pacing_stairs, start_q, n, cma=cma, public_equity_path=path
    )
    # Sort by (fund_name, quarter_index) to make comparison robust.
    sort_keys = ["fund_name", "quarter_index"]
    ta = ta.sort_values(sort_keys).reset_index(drop=True)
    stairs = stairs.sort_values(sort_keys).reset_index(drop=True)

    pd.testing.assert_frame_equal(
        ta[list(ta.columns)],
        stairs[list(ta.columns)],
        check_exact=False,
        atol=1e-9,
    )


def test_stairs_engine_at_parity_yields_byte_stable_orchestrator_run(
    repo_root: Path,
):
    """End-to-end parity: pe.engine=stairs with parity settings produces
    byte-identical ledger rows to the default pe.engine=ta run.
    """
    from aa_model.integration.orchestrator import run_orchestrator

    configs = repo_root / "configs"
    # Build a stairs-engine base + pe_pacing with parity settings.
    pe = yaml.safe_load((configs / "pe_pacing.yaml").read_text(encoding="utf-8"))
    pe["stairs_defaults"] = {
        "per_sleeve": {
            "pe_buyout": {
                "idiosyncratic_drift_pct": pe["ta_defaults"]["growth_pct"],
                "beta_to_public_equity": 0.0,
            }
        }
    }
    pe_path = configs / "_test_pe_pacing_stairs_parity.yaml"
    pe_path.write_text(yaml.safe_dump(pe), encoding="utf-8")

    base = yaml.safe_load((configs / "base.yaml").read_text(encoding="utf-8"))
    base.setdefault("pe", {})["engine"] = "stairs"
    base["pe_pacing"] = {"config": "configs/_test_pe_pacing_stairs_parity.yaml"}
    base_path = configs / "_test_base_stairs_parity.yaml"
    base_path.write_text(yaml.safe_dump(base), encoding="utf-8")

    try:
        rr_ta = run_orchestrator(configs / "base.yaml", dry_run=True)
        rr_stairs = run_orchestrator(base_path, dry_run=True)

        ta_pe = rr_ta.ledger[
            rr_ta.ledger["flow_type"].isin(["pe_call", "pe_distribution", "pe_nav_mark"])
        ].drop(columns=["run_id"])
        stairs_pe = rr_stairs.ledger[
            rr_stairs.ledger["flow_type"].isin(["pe_call", "pe_distribution", "pe_nav_mark"])
        ].drop(columns=["run_id"])
        ta_pe = ta_pe.sort_values(["quarter", "bucket", "flow_type", "source"]).reset_index(
            drop=True
        )
        stairs_pe = stairs_pe.sort_values(["quarter", "bucket", "flow_type", "source"]).reset_index(
            drop=True
        )
        pd.testing.assert_frame_equal(ta_pe, stairs_pe, check_exact=False, atol=1e-9)
    finally:
        base_path.unlink(missing_ok=True)
        pe_path.unlink(missing_ok=True)


# ---- adapter behavior beyond parity ----------------------------------------


def test_beta_amplification_under_drawdown(repo_root: Path):
    """Under a public_equity drawdown path, STAIRS at beta=1.5 produces
    strictly lower terminal PE NAV than at beta=0 (idiosyncratic drift
    held equal).
    """
    drift = 0.05
    cma = _cma_with_pu_expected(annual_pu=0.075)
    start_q = _q("2026Q1")
    n = 20
    # Crisis-style path: -10% per quarter for 4 quarters at offset 4.
    rates = [0.0] * n
    for i in range(4, 8):
        rates[i] = -0.10
    path = pd.Series(
        rates,
        index=pd.PeriodIndex([start_q + i for i in range(n)], name="quarter"),
        dtype=float,
    )

    pacing_zero = _shipped_pacing_with_stairs(repo_root, drift=drift, beta=0.0)
    pacing_high = _shipped_pacing_with_stairs(repo_root, drift=drift, beta=1.5)
    proj_zero = STAIRSAdapter().project_horizon(
        pacing_zero, start_q, n, cma=cma, public_equity_path=path
    )
    proj_high = STAIRSAdapter().project_horizon(
        pacing_high, start_q, n, cma=cma, public_equity_path=path
    )
    # Terminal NAV: last row per fund.
    term_zero = float(proj_zero.groupby("fund_name").tail(1)["nav_end_usd"].sum())
    term_high = float(proj_high.groupby("fund_name").tail(1)["nav_end_usd"].sum())
    assert (
        term_high < term_zero - 1.0
    ), f"beta-amplification did not bite: zero={term_zero}, high={term_high}"


def test_idiosyncratic_drift_monotonic_at_zero_beta(repo_root: Path):
    """At beta=0, terminal PE NAV is monotonically increasing in
    idiosyncratic_drift_pct — proves the drift term is wired."""
    cma = _cma_with_pu_expected(annual_pu=0.075)
    start_q = _q("2026Q1")
    n = 20
    path = _flat_path(start_q, n, 0.0)

    terminal: list[float] = []
    for drift in [0.05, 0.08, 0.10, 0.13]:
        pacing = _shipped_pacing_with_stairs(repo_root, drift=drift, beta=0.0)
        proj = STAIRSAdapter().project_horizon(pacing, start_q, n, cma=cma, public_equity_path=path)
        terminal.append(float(proj.groupby("fund_name").tail(1)["nav_end_usd"].sum()))
    # Strictly increasing.
    for prev, nxt in zip(terminal, terminal[1:], strict=False):
        assert nxt > prev + 1e-6, f"non-monotonic in drift: {terminal}"


def test_beta_zero_decouples_from_public_equity(repo_root: Path):
    """At beta=0, two different public_equity paths must produce identical
    PE projections — proves no leakage."""
    drift = 0.10
    cma = _cma_with_pu_expected(annual_pu=0.075)
    start_q = _q("2026Q1")
    n = 20
    path_a = _flat_path(start_q, n, 0.0)
    path_b = _flat_path(start_q, n, 0.20)

    pacing = _shipped_pacing_with_stairs(repo_root, drift=drift, beta=0.0)
    proj_a = STAIRSAdapter().project_horizon(pacing, start_q, n, cma=cma, public_equity_path=path_a)
    proj_b = STAIRSAdapter().project_horizon(pacing, start_q, n, cma=cma, public_equity_path=path_b)
    pd.testing.assert_frame_equal(
        proj_a.sort_values(["fund_name", "quarter_index"]).reset_index(drop=True),
        proj_b.sort_values(["fund_name", "quarter_index"]).reset_index(drop=True),
        check_exact=False,
        atol=1e-9,
    )


def test_linear_commitment_property(repo_root: Path):
    """Two funds in the same sleeve with split commitment ($X + $Y vs
    single $X+Y) produce summed-equal pe_* flows under STAIRS — pins
    linearity in commitment size, a TA invariant that must survive.
    """
    from aa_model.io.schemas import FundConfig

    cma = _cma_with_pu_expected(annual_pu=0.075)
    start_q = _q("2026Q1")
    n = 20
    path = _flat_path(start_q, n, 0.05)
    drift = 0.10
    beta = 1.2

    base_pacing = _shipped_pacing_with_stairs(repo_root, drift=drift, beta=beta)

    # Single fund: $25M (matches the shipped fixture).
    pacing_single = base_pacing
    # Two funds: $10M + $15M, same sleeve and vintage.
    pacing_split = base_pacing.model_copy(
        update={
            "funds": [
                FundConfig(
                    name="Split_A_2026Q1",
                    commitment_usd=10_000_000.0,
                    vintage="2026Q1",
                    sleeve="pe_buyout",
                ),
                FundConfig(
                    name="Split_B_2026Q1",
                    commitment_usd=15_000_000.0,
                    vintage="2026Q1",
                    sleeve="pe_buyout",
                ),
            ]
        }
    )

    proj_single = STAIRSAdapter().project_horizon(
        pacing_single, start_q, n, cma=cma, public_equity_path=path
    )
    proj_split = STAIRSAdapter().project_horizon(
        pacing_split, start_q, n, cma=cma, public_equity_path=path
    )
    cols = ["call_usd", "distribution_usd", "nav_mark_usd", "nav_end_usd"]
    agg_single = proj_single.groupby("quarter")[cols].sum()
    agg_split = proj_split.groupby("quarter")[cols].sum()
    pd.testing.assert_frame_equal(agg_single, agg_split, check_exact=False, atol=1e-3)


def test_growth_clip_activates_under_extreme_drawdown(repo_root: Path):
    """A configuration that drives growth_pct_q below -1.0 must trigger
    the clip: the NAV chain stays non-negative and the diagnostic
    clipped_quarters count is > 0.
    """
    drift = 0.0
    cma = _cma_with_pu_expected(annual_pu=0.075)
    start_q = _q("2026Q1")
    n = 8
    # public_equity quarterly = -50% for one quarter; expected = 0.075/4 ≈ 0.01875.
    # excess ≈ -0.51875. beta=2.5 → growth_pct_q ≈ -1.30 → clipped to -0.99.
    rates = [0.0] * n
    rates[3] = -0.50
    path = pd.Series(
        rates,
        index=pd.PeriodIndex([start_q + i for i in range(n)], name="quarter"),
        dtype=float,
    )
    pacing = _shipped_pacing_with_stairs(repo_root, drift=drift, beta=2.5)

    adapter = STAIRSAdapter()
    proj = adapter.project_horizon(pacing, start_q, n, cma=cma, public_equity_path=path)

    # NAV must stay non-negative across the projection.
    assert (proj["nav_end_usd"] >= -1e-6).all(), (
        f"NAV went negative despite clip; min = " f"{float(proj['nav_end_usd'].min())}"
    )
    # Clip activated at least once.
    diag = adapter.diagnostics()
    assert diag["engine"] == "STAIRSAdapter"
    assert diag["growth_floor"] == -0.99
    assert diag["clipped_quarters"] > 0


# ---- factory + ABC ---------------------------------------------------------


def test_factory_returns_ta_adapter():
    a = make_pe_adapter(engine="ta")
    assert isinstance(a, TAAdapter)


def test_factory_returns_stairs_adapter():
    a = make_pe_adapter(engine="stairs")
    assert isinstance(a, STAIRSAdapter)


def test_factory_unknown_engine_fails():
    with pytest.raises(ValueError, match="unknown pe.engine"):
        make_pe_adapter(engine="not_a_real_engine")
