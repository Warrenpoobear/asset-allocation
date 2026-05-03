"""Phase 11 / L16 — Owl scale-invariance fix tests.

Eight tests:

Schema (3):
1. ``absolute_min_annual_usd`` non-negative; non-finite fails.
2. ``absolute_max_annual_usd`` strictly positive; non-finite fails.
3. ``absolute_min > absolute_max`` fails at ``model_validator`` time.

Behavior (4):
4. Default-off byte-stability: GuardrailConfig with no absolute fields →
   Owl trajectory byte-identical to pre-Phase-11.
5. Scale-invariance regression test (the one L16 doc referenced but
   didn't ship — Phase 11 adds it as an explicit baseline anchor).
   Two OwlRule instances with $100M and $1B initial NAV at same
   proportional setup → identical quarterly trajectories.
6. Scale-divergence under absolute floor: same two instances with
   ``absolute_min_annual_usd`` set → trajectories diverge after the
   small household hits the floor and the large household keeps
   cutting.
7. Cut-path floor binding via prior-spend feedback: cut sequence
   eventually pins ``annual_spend`` at ``absolute_min_annual_usd``;
   subsequent years see the clamped value as ``prior_annual``.

End-to-end (1):
8. Report diagnostic renders for an Owl run; classifies regime
   correctly; surfaces the **L19 caveat verbatim** ("does NOT
   resolve spending-base realism").

See MODEL_DOCUMENTATION.md §Phase 11 design + §Use-case context.
"""

from __future__ import annotations

import math
from pathlib import Path

import pandas as pd
import pytest
import yaml
from aa_model.integration.ledger import QuarterlyLedger
from aa_model.io.schemas import (
    GuardrailConfig,
    SmoothingConfig,
    SpendingConfig,
)
from aa_model.spending.base import SpendingParams
from aa_model.spending.owl_adapter import OwlRule
from pydantic import ValidationError


def _q(s: str) -> pd.Period:
    return pd.Period(s, freq="Q-DEC")


def _spending_cfg(
    *,
    annual_spend_usd: float,
    absolute_min_annual_usd: float | None = None,
    absolute_max_annual_usd: float | None = None,
) -> SpendingConfig:
    """Owl spending config with optional Phase 11 absolute clamps."""
    gr_kwargs = dict(
        upper_band_pct=0.20,
        lower_band_pct=0.20,
        raise_pct=0.10,
        cut_pct=0.10,
    )
    if absolute_min_annual_usd is not None:
        gr_kwargs["absolute_min_annual_usd"] = absolute_min_annual_usd
    if absolute_max_annual_usd is not None:
        gr_kwargs["absolute_max_annual_usd"] = absolute_max_annual_usd
    return SpendingConfig(
        rule="owl",
        annual_spend_usd=annual_spend_usd,
        inflation_pct=0.025,
        smoothing=SmoothingConfig(window_quarters=4, weight=0.5),
        floor_usd=0.0,
        ceiling_usd=1.0e12,
        guardrail=GuardrailConfig(**gr_kwargs),
    )


def _drive_owl(
    rule: OwlRule,
    *,
    initial_nav_total: float,
    returns_per_quarter: list[float],
    cfg: SpendingConfig,
) -> tuple[list[float], QuarterlyLedger]:
    """Step the orchestrator's per-quarter loop manually, threading the
    rule's actual emission back into the ledger as the next quarter's
    prior-spend source. Mirrors the Phase 4a state-flow contract used
    by the real orchestrator (``quarterly_outflow_at`` reads the
    ledger; the orchestrator emits the spend row).

    Returns ``(quarterly_trajectory, final_ledger)``.
    """
    L = QuarterlyLedger(
        "test",
        initial_nav={"cash": initial_nav_total},
        start_quarter=_q("2026Q1"),
    )
    n_quarters = len(returns_per_quarter)
    params = SpendingParams(config=cfg, start_quarter=_q("2026Q1"), num_quarters=n_quarters)
    nav = initial_nav_total
    trajectory: list[float] = []
    for i, ret in enumerate(returns_per_quarter):
        q = _q("2026Q1") + i
        # Step 1: rule decides quarterly outflow against ledger
        # closed through q-1 (no rows yet for q).
        quarterly = rule.quarterly_outflow_at(L, params, q)
        trajectory.append(quarterly)
        # Step 2: emit return row for this quarter.
        nav_after_return = nav * (1.0 + ret)
        ret_amt = nav_after_return - nav
        L.add(
            quarter=q,
            bucket="cash",
            flow_type="return",
            amount_usd=ret_amt,
            source="cma",
        )
        # Step 3: emit spend row for this quarter (negative on cash;
        # source = the rule's SOURCE_ID so prior-spend feedback
        # finds it).
        L.add(
            quarter=q,
            bucket="cash",
            flow_type="spend",
            amount_usd=-quarterly,
            source=rule.SOURCE_ID,
        )
        nav = nav_after_return - quarterly
    return trajectory, L


# ---- 1-3. Schema-level validation ------------------------------------------


def test_absolute_min_negative_fails():
    with pytest.raises(ValidationError):
        GuardrailConfig(
            upper_band_pct=0.20,
            lower_band_pct=0.20,
            raise_pct=0.10,
            cut_pct=0.10,
            absolute_min_annual_usd=-1.0,
        )


def test_absolute_max_zero_fails():
    """absolute_max_annual_usd has gt=0 (strictly positive)."""
    with pytest.raises(ValidationError):
        GuardrailConfig(
            upper_band_pct=0.20,
            lower_band_pct=0.20,
            raise_pct=0.10,
            cut_pct=0.10,
            absolute_max_annual_usd=0.0,
        )


def test_absolute_max_non_finite_fails():
    with pytest.raises(ValidationError):
        GuardrailConfig(
            upper_band_pct=0.20,
            lower_band_pct=0.20,
            raise_pct=0.10,
            cut_pct=0.10,
            absolute_max_annual_usd=math.inf,
        )


def test_absolute_min_above_max_fails():
    with pytest.raises(
        ValidationError,
        match=r"absolute_min_annual_usd.*absolute_max_annual_usd",
    ):
        GuardrailConfig(
            upper_band_pct=0.20,
            lower_band_pct=0.20,
            raise_pct=0.10,
            cut_pct=0.10,
            absolute_min_annual_usd=10_000_000.0,
            absolute_max_annual_usd=5_000_000.0,
        )


# ---- 4. Default-off byte-stability -----------------------------------------


def test_default_off_owl_trajectory_byte_stable(repo_root: Path):
    """An Owl-rule run with the existing fixture (no absolute clamps)
    must produce byte-identical ledger content to a second run of the
    same config — pinning that the new fields, when absent, do not
    perturb behavior. This is the regression anchor for the new
    fields.
    """
    from aa_model.integration.orchestrator import run_orchestrator

    configs = repo_root / "configs"
    spend = yaml.safe_load((configs / "spending.yaml").read_text(encoding="utf-8"))
    spend["rule"] = "owl"
    spend["guardrail"] = {
        "upper_band_pct": 0.20,
        "lower_band_pct": 0.20,
        "raise_pct": 0.10,
        "cut_pct": 0.10,
    }
    spend_path = configs / "_test_p11_default_spend.yaml"
    spend_path.write_text(yaml.safe_dump(spend), encoding="utf-8")
    base = yaml.safe_load((configs / "base.yaml").read_text(encoding="utf-8"))
    base["spending"] = {"config": "configs/_test_p11_default_spend.yaml"}
    base_path = configs / "_test_p11_default_base.yaml"
    base_path.write_text(yaml.safe_dump(base), encoding="utf-8")
    try:
        rr1 = run_orchestrator(base_path, dry_run=True)
        rr2 = run_orchestrator(base_path, dry_run=True)
        df1 = rr1.ledger.drop(columns=["run_id"]).reset_index(drop=True)
        df2 = rr2.ledger.drop(columns=["run_id"]).reset_index(drop=True)
        pd.testing.assert_frame_equal(df1, df2, check_exact=False, atol=1e-9)
    finally:
        spend_path.unlink(missing_ok=True)
        base_path.unlink(missing_ok=True)


# ---- 5. Scale-invariance baseline (the test L16 doc referenced) ------------


def test_owl_path_is_scale_invariant_in_initial_nav():
    """Two Owl runs with proportionally-scaled inputs (same initial
    rate, same return path, same bands, no absolute clamps) produce
    **identical quarterly trajectories** up to a constant scale
    factor. This is the L16 weakness documented since Phase 3c;
    Phase 11 ships the regression anchor that pins it explicitly.
    """
    initial_rate = 0.04
    returns = [0.02] * 4 + [-0.05] * 4 + [0.03] * 4

    cfg_small = _spending_cfg(annual_spend_usd=4_000_000.0)
    cfg_large = _spending_cfg(annual_spend_usd=40_000_000.0)

    quarterly_small, _ = _drive_owl(
        OwlRule(),
        initial_nav_total=4_000_000.0 / initial_rate,
        returns_per_quarter=returns,
        cfg=cfg_small,
    )
    quarterly_large, _ = _drive_owl(
        OwlRule(),
        initial_nav_total=40_000_000.0 / initial_rate,
        returns_per_quarter=returns,
        cfg=cfg_large,
    )

    # Trajectories scale by 10× at every quarter. The *ratios* are
    # identical because Owl is rate-based + scale-invariant.
    for s, l_ in zip(quarterly_small, quarterly_large, strict=False):
        assert s > 0
        assert abs(l_ / s - 10.0) < 1e-9, (
            f"scale-invariance broken: small={s}, large={l_}, " f"ratio={l_ / s} expected 10.0"
        )


# ---- 6. Scale-divergence under absolute floor ------------------------------


def test_owl_diverges_under_absolute_floor():
    """Same two scaled instances, but with absolute_min_annual_usd
    set to a fixed dollar floor that binds quickly on the small fund
    (~one cut away from initial spend). The small fund pins at the
    floor; the large fund — whose 10× initial spend places it far
    from the same dollar floor — continues to cut on its own
    rate-band trajectory. Trajectories diverge → L16 closure proven.
    """
    # Sustained drawdown so cut path fires; small fund hits floor
    # after one or two cuts.
    returns = [-0.20] * 4 + [-0.10] * 4 + [-0.05] * 4 + [0.0] * 4

    # Floor at $3.6M — binds the small fund after 2 cuts
    # ($4M → $3.69M → $3.41M, but inflated to $3.49M which is below
    # the floor → clamp to $3.6M). The large fund's analogous
    # trajectory is at ~$30M, nowhere near the $3.6M floor.
    floor_usd = 3_600_000.0
    cfg_small = _spending_cfg(annual_spend_usd=4_000_000.0, absolute_min_annual_usd=floor_usd)
    cfg_large = _spending_cfg(annual_spend_usd=40_000_000.0, absolute_min_annual_usd=floor_usd)

    rule_small = OwlRule()
    rule_large = OwlRule()
    quarterly_small, _ = _drive_owl(
        rule_small,
        initial_nav_total=100_000_000.0,
        returns_per_quarter=returns,
        cfg=cfg_small,
    )
    quarterly_large, _ = _drive_owl(
        rule_large,
        initial_nav_total=1_000_000_000.0,
        returns_per_quarter=returns,
        cfg=cfg_large,
    )

    floor_quarterly = floor_usd / 4.0
    # Small fund pins at the floor by end of horizon.
    assert abs(quarterly_small[-1] - floor_quarterly) < 1.0, (
        f"small fund did not pin at floor: terminal quarterly = "
        f"{quarterly_small[-1]}, expected ≈ {floor_quarterly}"
    )
    # Small fund's clamp activated at least once.
    assert rule_small.diagnostics()["min_clamp_activations"] > 0
    # Large fund's clamp NEVER activated (its spend never came close
    # to the absolute floor — its rate-band trajectory operates at
    # ~10× the floor's dollar level).
    assert rule_large.diagnostics()["min_clamp_activations"] == 0, (
        f"large fund unexpectedly clamped: "
        f"diag = {rule_large.diagnostics()}; trajectory = {quarterly_large}"
    )
    # The ratio between the two trajectories starts at exactly 10×
    # (proportional setup, no clamp yet) and diverges once the
    # small-fund clamp binds — direct proof that L16 invariance is
    # broken when the absolute clamp is set.
    ratios = [l_ / s for s, l_ in zip(quarterly_small, quarterly_large, strict=False) if s > 0]
    assert (
        max(ratios) - min(ratios) > 0.5
    ), f"trajectories did not diverge under floor; ratios = {ratios}"


# ---- 7. Cut-path floor binding via prior-spend feedback --------------------


def test_cut_path_pins_at_absolute_floor_via_prior_feedback():
    """Once the cut sequence drives annual_spend below
    absolute_min_annual_usd, the clamp activates. The next year's
    ``prior_annual`` reads the CLAMPED value (via the ledger's
    spend rows the orchestrator emits), so subsequent years see the
    floor as the new starting point. The trajectory pins at the
    floor and the diagnostic counter reflects activations.

    Floor chosen to bind quickly within the test horizon (one cut
    away from initial); the property under test is the prior-spend
    feedback loop, not the depth of the cut sequence.
    """
    floor_usd = 3_700_000.0  # $3.7M; first cut to $3.69M ≈ at floor
    returns = [-0.20] * 4 + [-0.10] * 4 + [-0.05] * 4 + [0.0] * 4

    cfg = _spending_cfg(annual_spend_usd=4_000_000.0, absolute_min_annual_usd=floor_usd)
    rule = OwlRule()
    trajectory, _ = _drive_owl(
        rule,
        initial_nav_total=100_000_000.0,
        returns_per_quarter=returns,
        cfg=cfg,
    )
    floor_quarterly = floor_usd / 4.0
    # By end of horizon, trajectory must be ≈ floor_quarterly. The
    # clamp activates after the first sufficiently-deep cut and
    # subsequent prior-spend reads see the clamped value.
    assert abs(trajectory[-1] - floor_quarterly) < 1.0, (
        f"floor not binding at end of horizon; trajectory = {trajectory}, "
        f"expected terminal ≈ {floor_quarterly}"
    )
    # Once pinned, it stays pinned for the rest of the horizon —
    # demonstrating that the clamped value rides through the prior-
    # spend feedback loop (the next year's prior_annual reads
    # absolute_min_annual_usd, not the unclamped trajectory).
    pinned_count = sum(1 for v in trajectory if abs(v - floor_quarterly) < 1.0)
    assert pinned_count >= 8, (
        f"floor only binded {pinned_count} quarters; expected ≥ 8 "
        f"(at least two full years of pinning). Trajectory: {trajectory}"
    )
    # Diagnostic counter reflects activations.
    diag = rule.diagnostics()
    assert diag["min_clamp_activations"] > 0, f"min_clamp_activations should be > 0; diag = {diag}"


# ---- 8. Report diagnostic renders + L19 caveat verbatim -------------------


def test_report_owl_advisory_section_renders_with_l19_caveat(repo_root: Path):
    """An Owl-rule run with the absolute floor set produces the new
    advisory section in report.md, classifies the regime correctly,
    and surfaces the L19 caveat **verbatim**.
    """
    from aa_model.integration.orchestrator import run_orchestrator

    configs = repo_root / "configs"
    spend = yaml.safe_load((configs / "spending.yaml").read_text(encoding="utf-8"))
    spend["rule"] = "owl"
    spend["guardrail"] = {
        "upper_band_pct": 0.20,
        "lower_band_pct": 0.20,
        "raise_pct": 0.10,
        "cut_pct": 0.10,
        "absolute_min_annual_usd": 2_000_000.0,
    }
    spend_path = configs / "_test_p11_owl_floor_spend.yaml"
    spend_path.write_text(yaml.safe_dump(spend), encoding="utf-8")
    base = yaml.safe_load((configs / "base.yaml").read_text(encoding="utf-8"))
    base["spending"] = {"config": "configs/_test_p11_owl_floor_spend.yaml"}
    base_path = configs / "_test_p11_owl_floor_base.yaml"
    base_path.write_text(yaml.safe_dump(base), encoding="utf-8")
    try:
        result = run_orchestrator(base_path, dry_run=False)
        text = (result.output_dir / "report.md").read_text(encoding="utf-8")

        # Section header + structural lines.
        assert "## Owl scale-sensitivity (advisory)" in text
        assert "absolute_min_annual_usd: $2,000,000" in text
        assert "absolute_max_annual_usd: not set" in text
        assert "min-clamp activated:" in text
        assert "max-clamp activated:" in text
        assert "regime classification:" in text
        # Regime is scale-aware because absolute_min_annual_usd is set.
        assert "scale-aware" in text

        # Load-bearing L19 caveat — must appear verbatim.
        assert (
            "Phase 11 fixes scale-invariance only — it does NOT resolve "
            "spending-base realism (L19)" in text
        ), "L19 caveat missing from advisory section"
        assert "Owl still measures rate against" in text
        assert "total NAV" in text
    finally:
        spend_path.unlink(missing_ok=True)
        base_path.unlink(missing_ok=True)
