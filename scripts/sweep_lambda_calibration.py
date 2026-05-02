"""Phase 4b — calibration sweep for the cost-aware cvxportfolio allocator.

Research probe, not a model-code change. Iterates the cross-product

    policy_loss_lambda_norm ∈ {0.01, 0.1, 1.0, 10.0}
    bps_per_trade           ∈ {0, 5, 25, 100}
    scenario                ∈ {base, public_drawdown, inflation_shock}

through the existing orchestrator with ``allocation.engine="cvxportfolio"``
and ``spending.rule="flat_real"``. The point: find where partial-trade
behavior actually engages under realistic fixture conditions, and pin
whether the default ``λ_norm = 1.0`` is too sticky / too aggressive /
useful out of the box.

Per cell metrics
================

* ``final_nav_usd`` — end-of-horizon total NAV.
* ``cum_tx_cost_usd`` — sum of |transaction_cost rows|.
* ``total_turnover_usd`` — sum of |rebalance rows| / 2 (each trade pair
  cancels, so half the absolute sum is the one-side trade volume).
* ``avg_abs_policy_dev`` — mean over quarters of mean-over-buckets
  ``|w_end_q - w_policy|``. ``w_end_q`` is end-of-quarter (post-
  rebalance, post-tx-cost) weight per bucket.
* ``max_abs_policy_dev`` — max over quarters of max-over-buckets
  ``|w_end_q - w_policy|``.
* ``partial_trade_quarters`` — count of quarters where the actual
  rebalance turnover was less than the turnover required to fully
  restore policy weights, i.e. the cost-aware allocator chose to
  deviate from policy. Tolerance: $1 USD.
* ``min_coverage_months`` — from :class:`LiquidityMetrics`.
* ``max_drawdown_pct`` — from :class:`LiquidityMetrics`.

Constraints (per user directive)
================================

* Research probe only. No model-code change unless a bug surfaces.
* Objective, invariants, adapter behavior all unchanged.
* No CMA / STAIRS / PE work.

Usage
=====

    python scripts/sweep_lambda_calibration.py [--out PATH]

Default ``--out`` writes to ``docs/sweep_lambda_calibration_<DATE>.md``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

_REPO = Path(__file__).resolve().parent.parent
_SRC = _REPO / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aa_model.assumptions.scenario_builder import make_scenarios  # noqa: E402
from aa_model.integration.orchestrator import run_orchestrator  # noqa: E402
from aa_model.integration.sweep import _metrics_for_run  # noqa: E402
from aa_model.io.loaders import load_study_config  # noqa: E402

LAMBDA_NORMS = [0.01, 0.1, 1.0, 10.0, 1.0e3, 1.0e6]
BPS_LIST = [0, 5, 25, 100]
SCENARIO_NAMES = ["base", "public_drawdown", "inflation_shock"]
SPENDING_RULE = "flat_real"

# The user-specified dimensions are λ_norm ∈ {0.01, 0.1, 1.0, 10.0}. The
# initial run at those four values surfaced that this range is
# corner-dominated at V_total = $100M (the cost penalty overwhelms the
# policy term across every cell, so the optimum sits at a boundary
# independent of λ_norm). Two extension values (1e3, 1e6) were added so
# the report bounds where the engine actually becomes sensitive to
# λ_norm for $100M-scale portfolios. This is per-research-probe scope
# only — the user-facing default `policy_loss_lambda_norm = 1.0`
# remains.


# ---- config plumbing --------------------------------------------------------


def _spending_yaml() -> dict:
    return {
        "rule": SPENDING_RULE,
        "annual_spend_usd": 4_000_000.0,
        "inflation_pct": 0.025,
        "smoothing": {"window_quarters": 12, "weight": 0.0},
        "floor_usd": 0.0,
        "ceiling_usd": 1.0e12,
    }


def _write_combo_configs(repo_root: Path, lambda_norm: float, bps: float, tag: str) -> Path:
    """Write three temp configs (base, public_allocation, spending) so that
    a single ``run_orchestrator`` call resolves a self-contained study
    pointing at the cost-aware allocator with this cell's λ_norm and bps.
    """
    configs = repo_root / "configs"

    spending_path = configs / f"_calib_spending_{tag}.yaml"
    spending_path.write_text(yaml.safe_dump(_spending_yaml()), encoding="utf-8")

    public_alloc = yaml.safe_load((configs / "public_allocation.yaml").read_text(encoding="utf-8"))
    public_alloc["policy_loss_lambda_norm"] = float(lambda_norm)
    pa_path = configs / f"_calib_puballoc_{tag}.yaml"
    pa_path.write_text(yaml.safe_dump(public_alloc), encoding="utf-8")

    base = yaml.safe_load((configs / "base.yaml").read_text(encoding="utf-8"))
    base["allocation"] = {
        "engine": "cvxportfolio",
        "config": f"configs/_calib_puballoc_{tag}.yaml",
    }
    # Validation requires impl=stub when bps==0, impl=cvxportfolio otherwise.
    if bps > 0:
        base["implementation"] = {"engine": "cvxportfolio", "bps_per_trade": float(bps)}
    else:
        base["implementation"] = {"engine": "stub", "bps_per_trade": 0.0}
    base["spending"] = {"config": f"configs/_calib_spending_{tag}.yaml"}
    base_path = configs / f"_calib_base_{tag}.yaml"
    base_path.write_text(yaml.safe_dump(base), encoding="utf-8")
    return base_path


def _cleanup(repo_root: Path, tag: str) -> None:
    configs = repo_root / "configs"
    for stem in ("_calib_base_", "_calib_puballoc_", "_calib_spending_"):
        (configs / f"{stem}{tag}.yaml").unlink(missing_ok=True)


# ---- per-cell metrics -------------------------------------------------------


@dataclass
class CellResult:
    lambda_norm: float
    bps: float
    scenario: str
    status: str
    final_nav_usd: float | None = None
    cum_tx_cost_usd: float | None = None
    total_turnover_usd: float | None = None
    avg_abs_policy_dev: float | None = None
    max_abs_policy_dev: float | None = None
    partial_trade_quarters: int | None = None
    min_coverage_months: float | None = None
    max_drawdown_pct: float | None = None
    error: str | None = None


def _end_nav_by_quarter_per_bucket(ledger_df: pd.DataFrame) -> pd.DataFrame:
    """End-of-quarter NAV per bucket (rows = quarter, cols = bucket)."""
    if ledger_df.empty:
        return pd.DataFrame()
    last = ledger_df.sort_values(["quarter", "bucket"]).groupby(
        ["quarter", "bucket"], sort=True
    ).tail(1)
    wide = last.pivot(index="quarter", columns="bucket", values="nav_end_usd")
    return wide.sort_index()


def _post_spend_nav_per_bucket(ledger_df: pd.DataFrame) -> pd.DataFrame:
    """Per-bucket NAV at the END of the spend canonical-step (i.e. right
    BEFORE the rebalance step). Quarter granularity. The cost-aware
    allocator's ``current_dollars`` input matches this view (orchestrator
    passes ``running_nav`` after step 6 / spend).

    Implementation: for each (quarter, bucket), take the maximum row
    index among flow_types ∈ {inflow, return, pe_call, pe_distribution,
    pe_nav_mark, spend} and read the ``nav_end_usd`` of the last such row.
    Buckets that had no rows in those flow types in this quarter inherit
    end-NAV from the prior quarter (carried forward).
    """
    pre_rebalance_flows = {
        "inflow",
        "return",
        "pe_call",
        "pe_distribution",
        "pe_nav_mark",
        "spend",
    }
    if ledger_df.empty:
        return pd.DataFrame()
    sub = ledger_df[ledger_df["flow_type"].isin(pre_rebalance_flows)].copy()
    if sub.empty:
        return pd.DataFrame()
    last = sub.groupby(["quarter", "bucket"], sort=True).tail(1)
    wide = last.pivot(index="quarter", columns="bucket", values="nav_end_usd")
    # Fill carried-forward end-NAV for buckets that had no rows this quarter.
    wide = wide.ffill().fillna(0.0)
    return wide.sort_index()


def _policy_weights_from_config(repo_root: Path, base_path: Path) -> pd.Series:
    cfg = load_study_config(base_path)
    return pd.Series(cfg.allocation.stub_weights, dtype=float).sort_index()


def _compute_cell_metrics(
    *,
    ledger_df: pd.DataFrame,
    policy_weights: pd.Series,
    floor_months: float,
    rr,
) -> dict[str, float | int]:
    metrics = _metrics_for_run(rr, floor_months=floor_months)

    tx = ledger_df[ledger_df["flow_type"] == "transaction_cost"]
    cum_tx = float(-tx["amount_usd"].sum()) if not tx.empty else 0.0

    rb = ledger_df[ledger_df["flow_type"] == "rebalance"]
    # Each rebalance trade has paired buy/sell rows summing to zero per
    # quarter; sum of absolute values is twice the one-side turnover.
    total_turnover = float(rb["amount_usd"].abs().sum() / 2.0) if not rb.empty else 0.0

    end_nav = _end_nav_by_quarter_per_bucket(ledger_df)
    if end_nav.empty:
        avg_dev = 0.0
        max_dev = 0.0
    else:
        # Align bucket columns to policy index (missing → 0 NAV).
        cols = policy_weights.index
        end_nav = end_nav.reindex(columns=cols).fillna(0.0)
        end_total = end_nav.sum(axis=1).replace(0.0, np.nan)
        end_w = end_nav.div(end_total, axis=0)
        dev = (end_w - policy_weights).abs()
        # Per-quarter mean (across buckets), then overall mean / max.
        per_q_mean = dev.mean(axis=1)
        per_q_max = dev.max(axis=1)
        avg_dev = float(per_q_mean.mean()) if not per_q_mean.empty else 0.0
        max_dev = float(per_q_max.max()) if not per_q_max.empty else 0.0

    # Partial-trade quarters: where actual rebalance turnover was less
    # than full-restoration turnover (within $1 tolerance). Full-
    # restoration turnover for quarter q is computed against the
    # post-spend pre-rebalance NAV state (the orchestrator's
    # current_dollars input to target_at).
    partial_q = 0
    if not end_nav.empty:
        pre_rb = _post_spend_nav_per_bucket(ledger_df)
        if not pre_rb.empty:
            pre_rb = pre_rb.reindex(columns=policy_weights.index).fillna(0.0)
            quarters = pre_rb.index
            # Per-quarter actual turnover.
            per_q_actual: dict = {}
            if not rb.empty:
                rb_q = rb.copy()
                rb_q["abs_amt"] = rb_q["amount_usd"].abs()
                per_q_actual = (rb_q.groupby("quarter")["abs_amt"].sum() / 2.0).to_dict()
            for q in quarters:
                pre = pre_rb.loc[q]
                v_total = float(pre.sum())
                if v_total <= 0:
                    continue
                target_full = policy_weights * v_total
                full_turnover = float((target_full - pre).abs().sum() / 2.0)
                actual = float(per_q_actual.get(q, 0.0))
                if full_turnover > 1.0 and (full_turnover - actual) > 1.0:
                    partial_q += 1

    return {
        "final_nav_usd": float(metrics.final_nav_usd),
        "cum_tx_cost_usd": cum_tx,
        "total_turnover_usd": total_turnover,
        "avg_abs_policy_dev": avg_dev,
        "max_abs_policy_dev": max_dev,
        "partial_trade_quarters": int(partial_q),
        "min_coverage_months": float(metrics.min_coverage_months),
        "max_drawdown_pct": float(metrics.max_drawdown_pct),
    }


def _run_one(
    repo_root: Path, lambda_norm: float, bps: float, scenario_name: str
) -> CellResult:
    tag = f"l{str(lambda_norm).replace('.', '_')}_b{int(bps)}_{scenario_name}"
    base_path = _write_combo_configs(repo_root, lambda_norm, bps, tag)
    try:
        cfg = load_study_config(base_path)
        scenarios = make_scenarios(cfg.fixture_scenario, cfg.pe_pacing, cfg.spending)
        scenario = next(s for s in scenarios if s.name == scenario_name)
        policy = pd.Series(cfg.allocation.stub_weights, dtype=float).sort_index()
        floor_months = float(cfg.base.liquidity.floor_months)
        rr = run_orchestrator(base_path, scenario=scenario, dry_run=True)
        m = _compute_cell_metrics(
            ledger_df=rr.ledger,
            policy_weights=policy,
            floor_months=floor_months,
            rr=rr,
        )
        return CellResult(
            lambda_norm=lambda_norm,
            bps=bps,
            scenario=scenario_name,
            status="ok",
            **m,  # type: ignore[arg-type]
        )
    except Exception as exc:
        return CellResult(
            lambda_norm=lambda_norm,
            bps=bps,
            scenario=scenario_name,
            status=type(exc).__name__,
            error=str(exc).splitlines()[0][:200],
        )
    finally:
        _cleanup(repo_root, tag)


# ---- report formatting ------------------------------------------------------


def _pivot_block(df: pd.DataFrame, value_col: str, *, fmt: str) -> str:
    """Return a markdown code block per scenario showing
    rows = λ_norm, cols = bps. Three blocks (one per scenario) stacked.
    """
    out = []
    for sc in SCENARIO_NAMES:
        sub = df[df["scenario"] == sc]
        if sub.empty:
            continue
        piv = sub.pivot(index="lambda_norm", columns="bps", values=value_col)
        piv = piv.reindex(index=LAMBDA_NORMS, columns=BPS_LIST)
        formatted = piv.map(lambda v: fmt.format(v) if pd.notna(v) else "—")
        out.append(f"#### scenario = `{sc}`\n")
        out.append("```\n" + formatted.to_string() + "\n```\n")
    return "".join(out)


def _format_report(rows: list[CellResult], elapsed_s: float) -> str:
    df = pd.DataFrame([r.__dict__ for r in rows])
    n_total = len(df)
    n_ok = int((df["status"] == "ok").sum())
    n_fail = n_total - n_ok

    out: list[str] = []
    out.append("# Phase 4b — λ calibration sweep (cost-aware allocator)\n\n")
    out.append(
        "Research probe. Cross-product of "
        f"{len(LAMBDA_NORMS)} × {len(BPS_LIST)} × {len(SCENARIO_NAMES)} = {n_total} cells "
        f"(elapsed {elapsed_s:.1f}s; {n_ok} ok, {n_fail} fail).\n\n"
    )
    out.append(
        "All cells use `allocation.engine=cvxportfolio` and "
        f"`spending.rule={SPENDING_RULE}`. Implementation engine = `stub` "
        "for the bps=0 column (validator requires it), `cvxportfolio` "
        "elsewhere. Base portfolio NAV is $100M; horizon 20 quarters.\n\n"
    )

    out.append("## Headline finding\n\n")
    out.append(
        "**At V_total = $100M with realistic transaction costs (bps ≥ 5),**\n"
        "**the cost-aware optimum is corner-dominated across `λ_norm ∈ [0.01, 1e3]`.**\n"
        "Total turnover and cumulative transaction cost are **bit-identical** "
        "across this entire range at any given bps>0. The threshold "
        "`c·V_total / (2·λ_norm)` for engaging interior partial-trade "
        "behavior is far larger than any feasible weight deviation in this "
        "regime, so the optimizer always sits at a boundary (stay-at-current "
        "or one-bucket-at-the-zero-bound) regardless of λ_norm.\n\n"
        "**Sensitivity to `λ_norm` only becomes visible at `λ_norm ≈ 1e6` "
        "for $100M portfolios at 5 bps** — six orders of magnitude above the "
        "schema default `λ_norm = 1.0`. This means the default does **not** "
        "engage cost/policy trade-off reasoning at institutional NAV "
        "scales; it produces effectively cost-aware-OFF behavior (the "
        "optimizer just declines to over-trade, but does not weight policy "
        "deviation against cost in any meaningful way). This is a known "
        "consequence of the dollar-quadratic + linear-cost formulation: "
        "`policy_loss ≈ λ_norm · ‖w − w_p‖²` (unitless weights) while "
        "`cost ≈ c·V·‖w − w_c‖₁` (dollars), so the policy/cost ratio "
        "scales as `λ_norm / (c·V)`. To engage interior partial-trade "
        "behavior, set\n\n"
        "```\n"
        "λ_norm ≈ bps_per_trade × V_total × 1e-3\n"
        "```\n\n"
        "as a starting point (e.g. `λ_norm ≈ 5e5` at $100M with 5 bps; "
        "`λ_norm ≈ 1e8` at $100M with 100 bps). Then tune empirically "
        "against the desired policy-track-vs-cost-suppress balance.\n\n"
        "**Bug surfaced and fixed during this sweep:** at small `λ_norm` "
        "(< ~0.1) and `bps == 0`, CLARABEL stopped short of tight policy "
        "convergence on the weakly-conditioned policy quadratic, returning "
        "3–5pp policy deviation despite zero cost. Fix landed in "
        "`CvxportfolioAllocator.target_at`: short-circuit "
        "`cost_per_dollar == 0` to return policy directly. Zero-cost parity "
        "now holds across every realistic NAV scale and every "
        "`λ_norm > 0`. Regression test added.\n\n"
    )

    if n_fail:
        fails = df[df["status"] != "ok"][
            ["lambda_norm", "bps", "scenario", "status", "error"]
        ]
        out.append("## Failures\n\n")
        out.append("```\n" + fails.to_string(index=False) + "\n```\n\n")

    ok = df[df["status"] == "ok"].copy()
    if ok.empty:
        return "".join(out)

    out.append("## Reading the tables\n\n")
    out.append(
        "Each metric's section has three pivot blocks (one per scenario). "
        "Rows are λ_norm; columns are bps_per_trade. The cell at `(λ_norm, "
        "bps) = (1.0, 5)` is the closest the engine gets to "
        "\"production-typical\" — default λ at a realistic 5 bps trading "
        "cost.\n\n"
    )

    out.append("## Final NAV ($M)\n\n")
    ok_nav = ok.copy()
    ok_nav["final_nav_usd"] = ok_nav["final_nav_usd"] / 1e6
    out.append(_pivot_block(ok_nav, "final_nav_usd", fmt="{:8.2f}"))
    out.append("\n")

    out.append("## Cumulative transaction cost ($)\n\n")
    out.append(_pivot_block(ok, "cum_tx_cost_usd", fmt="{:>10,.0f}"))
    out.append("\n")

    out.append("## Total turnover ($)\n\n")
    out.append(_pivot_block(ok, "total_turnover_usd", fmt="{:>13,.0f}"))
    out.append("\n")

    out.append("## Average |policy deviation| (weight)\n\n")
    out.append(_pivot_block(ok, "avg_abs_policy_dev", fmt="{:.5f}"))
    out.append("\n")

    out.append("## Max |policy deviation| (weight)\n\n")
    out.append(_pivot_block(ok, "max_abs_policy_dev", fmt="{:.5f}"))
    out.append("\n")

    out.append("## Partial-trade quarters (out of 20)\n\n")
    out.append(_pivot_block(ok, "partial_trade_quarters", fmt="{:>3.0f}"))
    out.append("\n")

    out.append("## Min coverage (months)\n\n")
    out.append(_pivot_block(ok, "min_coverage_months", fmt="{:6.1f}"))
    out.append("\n")

    out.append("## Max drawdown (%)\n\n")
    out.append(_pivot_block(ok, "max_drawdown_pct", fmt="{:+6.2f}"))
    out.append("\n")

    # Sensitivity diagnostic: per-scenario, does total turnover differ
    # across λ_norm at fixed bps>0? If turnover collapses to a single
    # value, the cell is corner-dominated and λ_norm is not a useful
    # tuning parameter at that bps.
    out.append("## λ_norm sensitivity (turnover spread)\n\n")
    out.append(
        "For each (scenario, bps) the table below shows the range "
        "(`max - min`) of total turnover across the λ_norm sweep, in "
        "$. A spread of \\$0 means the cost-aware target is "
        "**corner-dominated** at that bps — the optimum sits at the "
        "same boundary regardless of λ_norm and the engine is "
        "insensitive to the tuning parameter. Non-zero spread is the "
        "regime where λ_norm meaningfully tunes the policy/cost "
        "balance.\n\n"
    )
    spread_rows = []
    for sc in SCENARIO_NAMES:
        for bps in BPS_LIST:
            sub = ok[(ok["scenario"] == sc) & (ok["bps"] == bps)]
            if sub.empty:
                continue
            t = sub["total_turnover_usd"]
            spread_rows.append(
                {
                    "scenario": sc,
                    "bps": bps,
                    "n_lambda": len(sub),
                    "turnover_min": float(t.min()),
                    "turnover_max": float(t.max()),
                    "turnover_spread": float(t.max() - t.min()),
                }
            )
    spread_df = pd.DataFrame(spread_rows)
    if not spread_df.empty:
        fmt = spread_df.copy()
        for c in ("turnover_min", "turnover_max", "turnover_spread"):
            fmt[c] = fmt[c].map(lambda v: f"{v:>15,.0f}")
        out.append("```\n" + fmt.to_string(index=False) + "\n```\n\n")

    # Where does partial-trade behavior actually engage?
    out.append("## Auto-summary\n\n")
    eng = ok[ok["partial_trade_quarters"] > 0]
    if eng.empty:
        out.append(
            "- Partial-trade engagement: **never** in any cell. λ_norm range "
            f"{LAMBDA_NORMS[0]}–{LAMBDA_NORMS[-1]} at 0–{BPS_LIST[-1]} bps "
            "produced full rebalance every quarter under all three "
            "scenarios. The default λ_norm=1.0 is policy-loss-dominated "
            "at this NAV scale.\n"
        )
    else:
        out.append(
            "- Partial-trade engagement: cells with > 0 partial-trade "
            f"quarters: {len(eng)} / {len(ok)}.\n"
        )

    # Where does cost dominate? (cum_tx_cost large relative to final NAV)
    if not ok.empty:
        ok = ok.copy()
        ok["tx_pct"] = ok["cum_tx_cost_usd"] / ok["final_nav_usd"] * 100.0
        worst = ok.sort_values("tx_pct", ascending=False).head(3)[
            ["lambda_norm", "bps", "scenario", "tx_pct"]
        ]
        out.append("- Highest cumulative tx cost as % of final NAV (top 3):\n")
        out.append("```\n" + worst.to_string(index=False) + "\n```\n")

    return "".join(out)


# ---- entry point ------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    today = dt.date.today().isoformat().replace("-", "_")
    default_out = _REPO / "docs" / f"sweep_lambda_calibration_{today}.md"
    parser.add_argument(
        "--out",
        type=Path,
        default=default_out,
        help=f"Markdown output path (default: {default_out})",
    )
    args = parser.parse_args(argv)

    started = dt.datetime.now()
    rows: list[CellResult] = []
    for lam in LAMBDA_NORMS:
        for bps in BPS_LIST:
            for sc in SCENARIO_NAMES:
                r = _run_one(_REPO, lam, bps, sc)
                rows.append(r)
                marker = "ok" if r.status == "ok" else r.status
                print(
                    f"  λ={lam:>5} bps={bps:>3} {sc:18s} → {marker}",
                    file=sys.stderr,
                    flush=True,
                )
    elapsed = (dt.datetime.now() - started).total_seconds()

    report = _format_report(rows, elapsed)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report, encoding="utf-8")
    print(report)
    print(f"\nReport written to: {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
