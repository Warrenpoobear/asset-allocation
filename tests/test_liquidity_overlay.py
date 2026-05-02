"""Phase 8 (L8) — illiquidity overlay tests.

Three layers:

* **Unit (overlay function)** — closed-form hand-worked anchor,
  multi-sleeve illiquid case, edge-case failures (`liquid_nav < 0`
  and the conditional `liquid_nav == 0` rule).
* **Schema / cross-config** — overlay-on requires CMA liquidity
  coverage, ``pe_*`` tagged ``illiquid``, non-empty liquid set,
  positive aggregate liquid policy weight.
* **End-to-end orchestrator** — default-on shipped fixture has zero
  PE rebalance rows; internal opt-out reproduces pre-L8 PE-tradable
  ledger; the new ``## Illiquidity overlay`` report section renders.

See MODEL_DOCUMENTATION.md §Phase 8 design.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
import yaml

from aa_model.allocation.liquidity_overlay import (
    LiquidityOverlayDiagnostics,
    apply_liquidity_overlay,
)
from aa_model.io.loaders import load_study_config
from aa_model.io.validation import validate_study_config


def _series(d: dict, dtype=float) -> pd.Series:
    return pd.Series(d, dtype=dtype)


# ---- unit: hand-worked anchor ----------------------------------------------


def test_liquid_renormalisation_hand_worked():
    """Phase 8 design example: policy 5/20/50/25, current 3/18/44/35
    (PE 35% drifted up from 25%). Liquid NAV residual = 65%; liquid
    policy renormalises to 5:20:50 across cash/bond/equity. Execution
    weights:
      cash:   65% × (5/75)   = 4.33%
      bond:   65% × (20/75)  = 17.33%
      equity: 65% × (50/75)  = 43.33%
      pe:     35.00%
    """
    policy = _series({"cash": 0.05, "public_bond": 0.20, "public_equity": 0.50, "pe": 0.25})
    # Use $1 as V_total for arithmetic clarity.
    current = _series(
        {"cash": 0.03, "public_bond": 0.18, "public_equity": 0.44, "pe": 0.35}
    )
    liquidity = _series(
        {"cash": "liquid", "public_bond": "liquid", "public_equity": "liquid", "pe": "illiquid"},
        dtype=object,
    )
    exec_w, diag = apply_liquidity_overlay(
        policy_weights=policy,
        current_dollars=current,
        liquidity=liquidity,
    )

    expected = {
        "cash":          0.65 * (0.05 / 0.75),
        "public_bond":   0.65 * (0.20 / 0.75),
        "public_equity": 0.65 * (0.50 / 0.75),
        "pe":            0.35,
    }
    for b, e in expected.items():
        assert abs(float(exec_w[b]) - e) < 1e-12, f"{b}: got {float(exec_w[b])}, expected {e}"
    assert abs(float(exec_w.sum()) - 1.0) < 1e-12

    assert diag.illiquid_buckets == ("pe",)
    assert diag.policy_weight_per_illiquid["pe"] == pytest.approx(0.25)
    assert diag.current_weight_per_illiquid["pe"] == pytest.approx(0.35)
    assert diag.drift_per_illiquid["pe"] == pytest.approx(0.10)
    assert diag.max_abs_illiquid_drift == pytest.approx(0.10)


def test_overlay_pins_pe_at_current_when_below_target():
    """When PE is below target (e.g., immediately post-vintage with no
    calls), the overlay locks PE at its current ($0 in extreme case)
    rather than buying it up — that's the whole point of L8.
    """
    policy = _series({"cash": 0.05, "public_bond": 0.20, "public_equity": 0.50, "pe": 0.25})
    # PE is at $0 (just-issued fund vintage, no calls yet).
    current = _series(
        {"cash": 0.20, "public_bond": 0.30, "public_equity": 0.50, "pe": 0.0}
    )
    liquidity = _series(
        {"cash": "liquid", "public_bond": "liquid", "public_equity": "liquid", "pe": "illiquid"},
        dtype=object,
    )
    exec_w, diag = apply_liquidity_overlay(
        policy_weights=policy,
        current_dollars=current,
        liquidity=liquidity,
    )
    assert exec_w["pe"] == 0.0  # NOT 0.25 — PE is locked at current.
    assert diag.drift_per_illiquid["pe"] == pytest.approx(-0.25)


def test_overlay_sum_to_one_across_parameter_sweep():
    """Σ execution_weight == 1 within 1e-12 across multiple
    pre-rebalance dollar mixes."""
    policy = _series({"cash": 0.05, "public_bond": 0.20, "public_equity": 0.50, "pe": 0.25})
    liquidity = _series(
        {"cash": "liquid", "public_bond": "liquid", "public_equity": "liquid", "pe": "illiquid"},
        dtype=object,
    )
    for pe_cur in [0.05, 0.10, 0.20, 0.30, 0.40]:
        # Distribute the rest 0.10/0.30/(rest) across the liquid buckets.
        rest = 1.0 - pe_cur
        cash_share = 0.10
        bond_share = 0.30
        equity_share = rest - cash_share - bond_share
        current = _series(
            {
                "cash": cash_share,
                "public_bond": bond_share,
                "public_equity": equity_share,
                "pe": pe_cur,
            }
        )
        exec_w, _ = apply_liquidity_overlay(
            policy_weights=policy,
            current_dollars=current,
            liquidity=liquidity,
        )
        assert abs(float(exec_w.sum()) - 1.0) < 1e-12


# ---- unit: multi-sleeve illiquid -------------------------------------------


def test_multi_sleeve_illiquid_renormalisation():
    """Two illiquid PE sleeves (pe_buyout + pe_venture) both lock at
    current. Liquid sleeves renormalise across the combined liquid
    policy weight.
    """
    policy = _series(
        {
            "cash": 0.05,
            "public_bond": 0.15,
            "public_equity": 0.40,
            "pe_buyout": 0.25,
            "pe_venture": 0.15,
        }
    )
    current = _series(
        {
            "cash": 0.10,
            "public_bond": 0.20,
            "public_equity": 0.30,
            "pe_buyout": 0.30,
            "pe_venture": 0.10,
        }
    )
    liquidity = _series(
        {
            "cash": "liquid",
            "public_bond": "liquid",
            "public_equity": "liquid",
            "pe_buyout": "illiquid",
            "pe_venture": "illiquid",
        },
        dtype=object,
    )
    exec_w, diag = apply_liquidity_overlay(
        policy_weights=policy,
        current_dollars=current,
        liquidity=liquidity,
    )
    # Both PE sleeves locked at current.
    assert exec_w["pe_buyout"] == 0.30
    assert exec_w["pe_venture"] == 0.10
    # Liquid NAV residual = 1 - 0.40 = 0.60. Liquid policy weight sum =
    # 0.05 + 0.15 + 0.40 = 0.60. Each renormalised liquid weight =
    # original / 0.60. Execution dollars = 0.60 × (orig/0.60) = orig.
    # So exec weights for liquid sleeves = original policy weights.
    assert exec_w["cash"] == pytest.approx(0.05)
    assert exec_w["public_bond"] == pytest.approx(0.15)
    assert exec_w["public_equity"] == pytest.approx(0.40)
    assert abs(float(exec_w.sum()) - 1.0) < 1e-12

    assert sorted(diag.illiquid_buckets) == ["pe_buyout", "pe_venture"]
    assert diag.drift_per_illiquid["pe_buyout"] == pytest.approx(0.05)
    assert diag.drift_per_illiquid["pe_venture"] == pytest.approx(-0.05)


# ---- unit: edge cases ------------------------------------------------------


def test_liquid_nav_negative_fails_loudly():
    """Pathological leveraged-via-PE state: illiquid current dollars
    exceed total NAV. Must fail loudly with per-bucket breakdown.
    """
    policy = _series({"cash": 0.50, "pe": 0.50})
    # $50 of cash, $80 of PE — total $130 nominal but the model treats
    # this as a balance sheet where total NAV is $130 - external = $130.
    # liquid_nav = V - illiquid_current = 130 - 80 = 50, NOT negative.
    # To force liquid_nav < 0 we need illiquid_current > V_total. That
    # requires negative cash or negative bond:
    current = _series({"cash": -10.0, "pe": 80.0})  # V_total = 70, illiq = 80, liquid_nav = -10
    liquidity = _series({"cash": "liquid", "pe": "illiquid"}, dtype=object)
    with pytest.raises(ValueError, match="liquid_nav"):
        apply_liquidity_overlay(
            policy_weights=policy,
            current_dollars=current,
            liquidity=liquidity,
        )


def test_liquid_nav_zero_with_zero_liquid_current_is_no_op():
    """liquid_nav == 0 is allowed when every liquid bucket already
    has zero current dollars (genuine no-op, e.g., a freshly-funded
    PE-only program)."""
    policy = _series({"cash": 0.50, "pe": 0.50})
    current = _series({"cash": 0.0, "pe": 100.0})
    liquidity = _series({"cash": "liquid", "pe": "illiquid"}, dtype=object)
    exec_w, _ = apply_liquidity_overlay(
        policy_weights=policy,
        current_dollars=current,
        liquidity=liquidity,
    )
    assert exec_w["pe"] == 1.0
    assert exec_w["cash"] == 0.0


def test_liquid_nav_zero_with_nonzero_liquid_current_fails_loudly():
    """liquid_nav == 0 with nonzero liquid current dollars implies
    the overlay would have to sell liquid down to zero — which is
    almost certainly an upstream pacing error. Fail loudly.
    """
    policy = _series({"cash": 0.50, "pe": 0.50})
    # cash=10, pe=-10 → V_total=0, illiquid_current=-10, liquid_nav = 0-(-10)=10 — not zero.
    # Construct: V_total = 100, pe = 100, cash = 0 actually that's what
    # the prior test handled. To get liquid_nav=0 with nonzero liquid
    # current: V_total = X, pe (illiquid) = X, cash (liquid) = some positive
    # but offset by a negative liquid bucket. With only one liquid bucket
    # this is degenerate; use two liquid buckets.
    policy2 = _series({"cash": 0.40, "bond": 0.10, "pe": 0.50})
    current2 = _series({"cash": 50.0, "bond": -50.0, "pe": 100.0})
    # V_total = 50 - 50 + 100 = 100; illiq = 100; liquid_nav = 0;
    # but bond = -50 != 0. Should fail.
    liquidity2 = _series(
        {"cash": "liquid", "bond": "liquid", "pe": "illiquid"}, dtype=object
    )
    with pytest.raises(ValueError, match="liquid_nav ≈ 0"):
        apply_liquidity_overlay(
            policy_weights=policy2,
            current_dollars=current2,
            liquidity=liquidity2,
        )


# ---- cross-config validation -----------------------------------------------


def test_cross_config_missing_liquidity_fails(repo_root: Path):
    """Overlay-on requires cma.liquidity present."""
    configs = repo_root / "configs"
    cma = yaml.safe_load((configs / "cma.yaml").read_text(encoding="utf-8"))
    if "liquidity" in cma:
        del cma["liquidity"]
    cma_path = configs / "_test_cma_no_liquidity.yaml"
    cma_path.write_text(yaml.safe_dump(cma), encoding="utf-8")
    base = yaml.safe_load((configs / "base.yaml").read_text(encoding="utf-8"))
    base["cma"] = {"config": "configs/_test_cma_no_liquidity.yaml"}
    base_path = configs / "_test_base_no_liquidity.yaml"
    base_path.write_text(yaml.safe_dump(base), encoding="utf-8")
    try:
        cfg = load_study_config(base_path)
        with pytest.raises(ValueError, match="requires cma.liquidity"):
            validate_study_config(cfg)
    finally:
        cma_path.unlink(missing_ok=True)
        base_path.unlink(missing_ok=True)


def test_cross_config_pe_tagged_liquid_fails(repo_root: Path):
    """Every pe_* bucket must be tagged illiquid under the overlay."""
    configs = repo_root / "configs"
    cma = yaml.safe_load((configs / "cma.yaml").read_text(encoding="utf-8"))
    cma["liquidity"]["pe_buyout"] = "liquid"  # wrong
    cma_path = configs / "_test_cma_pe_liquid.yaml"
    cma_path.write_text(yaml.safe_dump(cma), encoding="utf-8")
    base = yaml.safe_load((configs / "base.yaml").read_text(encoding="utf-8"))
    base["cma"] = {"config": "configs/_test_cma_pe_liquid.yaml"}
    base_path = configs / "_test_base_pe_liquid.yaml"
    base_path.write_text(yaml.safe_dump(base), encoding="utf-8")
    try:
        cfg = load_study_config(base_path)
        with pytest.raises(ValueError, match="must be tagged 'illiquid'"):
            validate_study_config(cfg)
    finally:
        cma_path.unlink(missing_ok=True)
        base_path.unlink(missing_ok=True)


def test_cross_config_overlay_off_skips_liquidity_checks(repo_root: Path):
    """When the overlay is off, the L8 cross-config checks are bypassed
    — pe_* tagged liquid is OK in regression-anchor configs.
    """
    configs = repo_root / "configs"
    cma = yaml.safe_load((configs / "cma.yaml").read_text(encoding="utf-8"))
    cma["liquidity"]["pe_buyout"] = "liquid"
    cma_path = configs / "_test_cma_overlay_off.yaml"
    cma_path.write_text(yaml.safe_dump(cma), encoding="utf-8")
    base = yaml.safe_load((configs / "base.yaml").read_text(encoding="utf-8"))
    base["cma"] = {"config": "configs/_test_cma_overlay_off.yaml"}
    base.setdefault("rebalance", {})["illiquid_overlay"] = False
    base_path = configs / "_test_base_overlay_off.yaml"
    base_path.write_text(yaml.safe_dump(base), encoding="utf-8")
    try:
        cfg = load_study_config(base_path)
        validate_study_config(cfg)  # must not raise
    finally:
        cma_path.unlink(missing_ok=True)
        base_path.unlink(missing_ok=True)


# ---- end-to-end orchestrator -----------------------------------------------


def test_default_on_run_has_zero_pe_rebalance_rows(base_config_path):
    """The default shipped fixture must produce exactly zero rebalance
    rows on illiquid (pe_*) buckets under the default-on overlay.
    """
    from aa_model.integration.orchestrator import run_orchestrator

    result = run_orchestrator(base_config_path, dry_run=True)
    df = result.ledger
    pe_rb = df[
        (df["flow_type"] == "rebalance") & df["bucket"].str.startswith("pe_")
    ]
    assert len(pe_rb) == 0, (
        f"L8 violated: {len(pe_rb)} rebalance rows on pe_* buckets, "
        f"sum |amount| = {float(pe_rb['amount_usd'].abs().sum())}"
    )


def test_default_on_pe_call_distribution_pairing_intact(base_config_path):
    """L8 must not affect PE call/distribution pairing — the existing
    cash-offset invariant continues to hold under overlay-on.
    """
    from aa_model.integration.orchestrator import run_orchestrator

    result = run_orchestrator(base_config_path, dry_run=True)
    df = result.ledger
    for ftype in ("pe_call", "pe_distribution"):
        sub = df[df["flow_type"] == ftype]
        if sub.empty:
            continue
        for q, sub_q in sub.groupby("quarter"):
            net = float(sub_q["amount_usd"].sum())
            assert abs(net) < 1e-9, f"{ftype} not zero-sum at {q}: {net}"


def test_internal_opt_out_reproduces_pre_l8_behavior(repo_root: Path):
    """The internal-only ``rebalance.illiquid_overlay: false`` flag
    reproduces pre-L8 PE-tradable behavior — at least one pe_*
    rebalance row appears under the default fixture's drifted state.
    This is the regression-anchor for archaeology, not a recommended
    user-facing mode.
    """
    from aa_model.integration.orchestrator import run_orchestrator

    configs = repo_root / "configs"
    base = yaml.safe_load((configs / "base.yaml").read_text(encoding="utf-8"))
    base.setdefault("rebalance", {})["illiquid_overlay"] = False
    base_path = configs / "_test_base_overlay_off_e2e.yaml"
    base_path.write_text(yaml.safe_dump(base), encoding="utf-8")
    try:
        result = run_orchestrator(base_path, dry_run=True)
        df = result.ledger
        pe_rb = df[
            (df["flow_type"] == "rebalance")
            & df["bucket"].str.startswith("pe_")
        ]
        assert len(pe_rb) > 0, (
            "internal opt-out failed to reproduce pre-L8 PE rebalancing"
        )
    finally:
        base_path.unlink(missing_ok=True)


def test_overlay_section_renders_in_report(base_config_path):
    """report.md gains a '## Illiquidity overlay' section with the
    per-bucket worst-quarter drift table when the overlay is active.
    """
    from aa_model.integration.orchestrator import run_orchestrator

    result = run_orchestrator(base_config_path, dry_run=False)
    text = (result.output_dir / "report.md").read_text(encoding="utf-8")
    assert "## Illiquidity overlay" in text
    assert "illiquid buckets locked" in text
    assert "per-bucket worst-quarter drift" in text
    assert "max |drift| across all illiquid buckets" in text
    assert "rebalance trades on illiquid buckets are zero by construction" in text


def test_overlay_diagnostics_dataclass_shape():
    """LiquidityOverlayDiagnostics carries the documented fields."""
    policy = _series({"cash": 0.5, "pe": 0.5})
    current = _series({"cash": 30.0, "pe": 70.0})
    liquidity = _series({"cash": "liquid", "pe": "illiquid"}, dtype=object)
    _, diag = apply_liquidity_overlay(
        policy_weights=policy,
        current_dollars=current,
        liquidity=liquidity,
    )
    assert isinstance(diag, LiquidityOverlayDiagnostics)
    assert diag.illiquid_buckets == ("pe",)
    assert diag.policy_weight_per_illiquid["pe"] == 0.5
    assert diag.current_weight_per_illiquid["pe"] == pytest.approx(0.7)
    assert diag.drift_per_illiquid["pe"] == pytest.approx(0.2)
    assert diag.max_abs_illiquid_drift == pytest.approx(0.2)
    assert diag.sum_abs_illiquid_drift == pytest.approx(0.2)
    assert diag.clipped_to_zero_liquid_count == 0
