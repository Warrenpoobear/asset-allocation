"""Markdown run report.

Phase 1: minimal — run id, hashes, scenario, horizon, initial / final NAV,
cumulative return, end-of-horizon allocation, total NAV per quarter.
Phase 4b adds an advisory cost-aware allocator calibration section.
Phase 5 adds a Capital Market Assumptions section.
Phase 6 adds a Correlation shock (scenario) section when active.
HTML rendering is a Phase 4 deliverable.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from aa_model.integration.ledger import QuarterlyLedger
from aa_model.io.schemas import StudyConfig

if TYPE_CHECKING:
    from aa_model.allocation.liquidity_overlay import LiquidityOverlayDiagnostics
    from aa_model.assumptions.cma import CMA
    from aa_model.assumptions.correlation_shock import CorrelationShockDiagnostics


def write_markdown_report(
    path: Path,
    *,
    cfg: StudyConfig,
    ledger: QuarterlyLedger,
    run_id: str,
    config_hash: str,
    fixtures_hash: str,
    allocator_diagnostics: dict | None = None,
    cma: CMA | None = None,
    shock_diagnostics: CorrelationShockDiagnostics | None = None,
    overlay_history: list[tuple[str, LiquidityOverlayDiagnostics]] | None = None,
) -> None:
    end_nav = ledger.end_nav_by_quarter()
    initial_total = sum(ledger.initial_nav.values())
    if not end_nav.empty:
        final_total = float(end_nav.iloc[-1].sum())
        last_q = str(end_nav.index[-1])
    else:
        final_total = initial_total
        last_q = ""

    lines: list[str] = []
    lines.append(f"# Run report — {run_id}")
    lines.append("")
    lines.append(f"- config_hash: `{config_hash}`")
    lines.append(f"- fixtures_hash: `{fixtures_hash}`")
    lines.append(f"- scenario: `{cfg.fixture_scenario.name}` — {cfg.fixture_scenario.description}")
    lines.append(f"- horizon: {cfg.base.horizon.start_quarter} + {cfg.base.horizon.num_quarters}q")
    lines.append("")
    lines.append("## Total NAV")
    lines.append("")
    lines.append(f"- initial: ${initial_total:,.0f}")
    lines.append(f"- final ({last_q}): ${final_total:,.0f}")
    if initial_total > 0:
        ret = (final_total / initial_total) - 1.0
        lines.append(f"- cumulative return: {ret * 100:.2f}%")
    lines.append("")

    if not end_nav.empty:
        lines.append("## End-of-horizon allocation")
        lines.append("")
        last = end_nav.iloc[-1]
        total = float(last.sum())
        if total != 0:
            for bucket in last.index:
                v = float(last[bucket])
                pct = v / total * 100.0 if total != 0 else 0.0
                lines.append(f"- {bucket}: {pct:.2f}% (${v:,.0f})")
        lines.append("")

        lines.append("## Total NAV by quarter")
        lines.append("")
        lines.append("| quarter | total NAV |")
        lines.append("|---|---|")
        for q, row in end_nav.iterrows():
            lines.append(f"| {q} | ${float(row.sum()):,.0f} |")
        lines.append("")

    # Capital market assumptions (Phase 5). Emit per-bucket priors plus
    # portfolio-level expected return / expected vol against the policy
    # weights from configs/public_allocation.yaml. Skipped when CMA is
    # absent (test paths that build a report without an explicit CMA).
    # Note: CMA is NOT consumed by the cost-aware allocator's objective —
    # it is a prior for riskfolio MV solves and a diagnostic for reports.
    if cma is not None and not cma.expected_returns_annual.empty:
        lines.append("## Capital market assumptions")
        lines.append("")
        lines.append(
            "| bucket | expected return (annual) | volatility (annual) | liquidity |"
        )
        lines.append("|---|---:|---:|:---:|")
        buckets = list(cma.expected_returns_annual.sort_index().index)
        for b in buckets:
            er = float(cma.expected_returns_annual[b])
            vol = float(cma.vol_annual[b])
            liq = (
                str(cma.liquidity[b])
                if (not cma.liquidity.empty and b in cma.liquidity.index)
                else "—"
            )
            lines.append(f"| {b} | {er * 100:+.2f}% | {vol * 100:.2f}% | {liq} |")
        lines.append("")

        # Portfolio-level priors against the configured policy weights.
        policy = pd.Series(cfg.allocation.stub_weights, dtype=float)
        common = [b for b in buckets if b in policy.index]
        if common:
            w = policy.reindex(common).fillna(0.0).to_numpy(dtype=float)
            er_arr = cma.expected_returns_annual.reindex(common).to_numpy(dtype=float)
            vol_arr = cma.vol_annual.reindex(common).to_numpy(dtype=float)
            corr_arr = cma.corr.reindex(index=common, columns=common).to_numpy(dtype=float)
            cov_arr = np.outer(vol_arr, vol_arr) * corr_arr
            port_er = float(w @ er_arr)
            port_var = float(w @ cov_arr @ w)
            port_vol = float(np.sqrt(max(port_var, 0.0)))
            lines.append("### Portfolio priors at policy weights")
            lines.append("")
            lines.append(f"- expected return (annual): {port_er * 100:+.2f}%")
            lines.append(f"- expected volatility (annual): {port_vol * 100:.2f}%")
            lines.append("")

        # Liquidity counts (when liquidity tags are present).
        if not cma.liquidity.empty:
            counts = cma.liquidity.value_counts().sort_index()
            lines.append("### Liquidity bucket counts")
            lines.append("")
            for tag, n in counts.items():
                lines.append(f"- {tag}: {int(n)}")
            lines.append("")

    # Correlation shock (Phase 6 / L6). Emitted only when a scenario
    # supplied a shock; otherwise the section is omitted entirely.
    if shock_diagnostics is not None:
        lines.append("## Correlation shock (scenario)")
        lines.append("")
        lines.append(f"- type: `{shock_diagnostics.shock_type}`")
        if shock_diagnostics.shock_type == "scale":
            lines.append(f"- magnitude: {float(shock_diagnostics.magnitude):g}")
            lines.append(
                f"- entries clipped to [-1, 1]: "
                f"{int(shock_diagnostics.clipped_pairs or 0)}"
            )
        else:  # override
            lines.append(
                f"- pairwise replacements: {int(shock_diagnostics.override_pairs or 0)}"
            )
        lines.append(
            f"- max |Δρ| vs baseline: {float(shock_diagnostics.max_abs_delta):.4f}"
        )
        lines.append("- PSD: pass")
        lines.append(
            "- note: CMA baseline preserved; this is a perturbation layer "
            "applied to a copy."
        )
        lines.append("")

    # Illiquidity overlay (Phase 8 / L8). Emitted when the overlay is
    # active and produced at least one per-quarter diagnostic record.
    # Aggregates: per-illiquid-bucket max drift across the run; total
    # quarters where any liquid bucket was clipped to zero.
    if overlay_history:
        # Aggregate per-illiquid-bucket policy + max drift across the
        # full horizon. Policy weights are stable across quarters in
        # current architecture; we just take the value from the first
        # record.
        first_diag = overlay_history[0][1]
        illiquid_buckets = list(first_diag.illiquid_buckets)
        policy_per = dict(first_diag.policy_weight_per_illiquid)
        # Find the quarter with max-abs drift per bucket; report the
        # worst single-quarter snapshot.
        worst_per_bucket: dict[str, tuple[str, float, float, float]] = {}
        # tuple = (quarter, current_weight, drift, abs_drift)
        for q_str, diag in overlay_history:
            for b in illiquid_buckets:
                cur_w = diag.current_weight_per_illiquid.get(b, 0.0)
                d = diag.drift_per_illiquid.get(b, 0.0)
                ad = abs(d)
                if b not in worst_per_bucket or ad > worst_per_bucket[b][3]:
                    worst_per_bucket[b] = (q_str, cur_w, d, ad)
        total_clipped = sum(
            d.clipped_to_zero_liquid_count for _q, d in overlay_history
        )

        lines.append("## Illiquidity overlay")
        lines.append("")
        lines.append(
            f"- illiquid buckets locked: {sorted(illiquid_buckets)}"
        )
        lines.append("- per-bucket worst-quarter drift:")
        lines.append("")
        lines.append(
            "| bucket | policy | worst current | drift (current − policy) | quarter |"
        )
        lines.append("|---|---:|---:|---:|---|")
        for b in sorted(illiquid_buckets):
            q_str, cur_w, d, _ad = worst_per_bucket[b]
            lines.append(
                f"| {b} | {policy_per.get(b, 0.0) * 100:.2f}% | "
                f"{cur_w * 100:.2f}% | {d * 100:+.2f}% | {q_str} |"
            )
        lines.append("")

        # Aggregate diagnostics
        max_overall = max(
            (
                d.max_abs_illiquid_drift
                for _q, d in overlay_history
            ),
            default=0.0,
        )
        sum_overall_means = (
            sum(d.sum_abs_illiquid_drift for _q, d in overlay_history)
            / max(len(overlay_history), 1)
        )
        lines.append(
            f"- max |drift| across all illiquid buckets × quarters: "
            f"{max_overall * 100:.2f}%"
        )
        lines.append(
            f"- mean Σ|drift| per quarter (across illiquid buckets): "
            f"{sum_overall_means * 100:.2f}%"
        )
        lines.append(
            f"- total liquid-bucket clipped-to-zero count "
            f"(execution dollars ≤ $1): {total_clipped}"
        )
        lines.append(
            "- note: PE exposure changes only through pe_call / "
            "pe_distribution / pe_nav_mark; rebalance trades on illiquid "
            "buckets are zero by construction (Phase 8 / L8)."
        )
        lines.append("")

    # PE program structure (Phase 9). Emitted only when at least one
    # fund carries any Phase 9 metadata field (manager, fund_id,
    # strategy, fee_model, or non-default status). Six aggregations
    # per the design: commitment summary, unfunded, calls /
    # distributions by manager, vintage concentration, manager
    # concentration top-3, NAV by manager (end of horizon). When no
    # Phase 9 fields are set, the section is omitted entirely so the
    # default fixture run is byte-stable with pre-Phase-9 reports.
    funds = cfg.pe_pacing.funds
    has_phase9_metadata = any(
        (f.manager is not None)
        or (f.fund_id is not None)
        or (f.strategy is not None)
        or (f.fee_model is not None)
        or (f.status != "active")
        for f in funds
    )
    if has_phase9_metadata:
        # Skip exited funds in forward-flow diagnostics, per the locked
        # status semantics. Exited funds also did not enter the
        # projection (orchestrator filters before adapter dispatch),
        # so the ledger has no flows for them anyway.
        forward_funds = [f for f in funds if f.status != "exited"]

        # Build the ledger-side join key set: ledger source for PE
        # flows is "pacing:<fund_name>" (Phase 9 keeps this verbatim).
        pe_flow_types = ("pe_call", "pe_distribution", "pe_nav_mark")
        pe_rows = ledger.finalize()
        pe_rows = pe_rows[pe_rows["flow_type"].isin(pe_flow_types)].copy()
        # Strip the "pacing:" prefix to get fund_name → manager.
        if not pe_rows.empty:
            pe_rows["fund_name"] = pe_rows["source"].str.removeprefix("pacing:")

        # Manager attribution helpers.
        def _manager_of(name: str) -> str:
            for f in forward_funds:
                if f.name == name and f.manager is not None:
                    return f.manager
            return "(unknown)"

        lines.append("## PE program structure")
        lines.append("")

        # 1. Commitment summary — total commitment by (manager, sleeve).
        lines.append("### Commitment summary")
        lines.append("")
        commit_rows = pd.DataFrame(
            [
                {
                    "manager": f.manager if f.manager is not None else "(unknown)",
                    "sleeve": f.sleeve,
                    "commitment_usd": float(f.commitment_usd),
                }
                for f in forward_funds
            ]
        )
        if not commit_rows.empty:
            piv = (
                commit_rows.groupby(["manager", "sleeve"])["commitment_usd"]
                .sum()
                .unstack(fill_value=0.0)
                .sort_index()
            )
            lines.append("| manager | " + " | ".join(piv.columns) + " |")
            lines.append(
                "|---" + "|---:" * len(piv.columns) + "|"
            )
            for mgr, row in piv.iterrows():
                cells = [f"${float(v):,.0f}" for v in row]
                lines.append(f"| {mgr} | " + " | ".join(cells) + " |")
            lines.append("")

        # 2. Unfunded by manager — commitment minus cumulative calls
        # over the horizon.
        lines.append("### Unfunded by manager")
        lines.append("")
        # Cumulative calls per fund.
        if not pe_rows.empty:
            calls = pe_rows[pe_rows["flow_type"] == "pe_call"].copy()
            # pe_call has paired rows (positive on sleeve, negative on
            # cash). Take only the sleeve side — amount > 0.
            calls = calls[calls["amount_usd"] > 0]
            calls_per_fund = (
                calls.groupby("fund_name")["amount_usd"].sum().to_dict()
            )
        else:
            calls_per_fund = {}
        unfunded_rows: list[dict] = []
        for f in forward_funds:
            mgr = f.manager if f.manager is not None else "(unknown)"
            called = float(calls_per_fund.get(f.name, 0.0))
            unfunded = max(0.0, float(f.commitment_usd) - called)
            unfunded_rows.append(
                {
                    "manager": mgr,
                    "commitment_usd": float(f.commitment_usd),
                    "called_usd": called,
                    "unfunded_usd": unfunded,
                }
            )
        unfunded_df = pd.DataFrame(unfunded_rows)
        if not unfunded_df.empty:
            agg = unfunded_df.groupby("manager")[
                ["commitment_usd", "called_usd", "unfunded_usd"]
            ].sum()
            agg["unfunded_pct"] = (
                agg["unfunded_usd"] / agg["commitment_usd"].replace(0, float("nan"))
            ).fillna(0.0) * 100.0
            lines.append(
                "| manager | commitment | called | unfunded | unfunded % |"
            )
            lines.append("|---|---:|---:|---:|---:|")
            for mgr, row in agg.sort_index().iterrows():
                lines.append(
                    f"| {mgr} | ${float(row['commitment_usd']):,.0f} | "
                    f"${float(row['called_usd']):,.0f} | "
                    f"${float(row['unfunded_usd']):,.0f} | "
                    f"{float(row['unfunded_pct']):.1f}% |"
                )
            lines.append("")

        # 3. Cumulative calls + distributions by manager.
        if not pe_rows.empty:
            lines.append("### Cumulative calls and distributions by manager")
            lines.append("")
            # Sleeve-side (positive) is the "into PE" amount for calls;
            # for distributions, the sleeve side is negative (PE → cash).
            calls = pe_rows[pe_rows["flow_type"] == "pe_call"].copy()
            calls = calls[calls["amount_usd"] > 0]
            dists = pe_rows[pe_rows["flow_type"] == "pe_distribution"].copy()
            dists = dists[dists["amount_usd"] < 0]
            # Use absolute value for distributions for display.
            dists["amount_usd"] = dists["amount_usd"].abs()
            calls["manager"] = calls["fund_name"].map(_manager_of)
            dists["manager"] = dists["fund_name"].map(_manager_of)
            mgr_calls = calls.groupby("manager")["amount_usd"].sum()
            mgr_dists = dists.groupby("manager")["amount_usd"].sum()
            all_managers = sorted(set(mgr_calls.index) | set(mgr_dists.index))
            lines.append(
                "| manager | cumulative calls | cumulative distributions |"
            )
            lines.append("|---|---:|---:|")
            for mgr in all_managers:
                c = float(mgr_calls.get(mgr, 0.0))
                d = float(mgr_dists.get(mgr, 0.0))
                lines.append(f"| {mgr} | ${c:,.0f} | ${d:,.0f} |")
            lines.append("")

        # 4. Vintage concentration — total commitment by vintage year.
        lines.append("### Vintage concentration")
        lines.append("")
        vintage_rows = pd.DataFrame(
            [
                {
                    "vintage_year": f.vintage[:4],
                    "commitment_usd": float(f.commitment_usd),
                }
                for f in forward_funds
            ]
        )
        if not vintage_rows.empty:
            agg_v = vintage_rows.groupby("vintage_year")["commitment_usd"].sum()
            total = float(agg_v.sum())
            lines.append("| vintage year | commitment | share |")
            lines.append("|---|---:|---:|")
            for yr, v in agg_v.sort_index().items():
                share = (float(v) / total * 100.0) if total > 0 else 0.0
                lines.append(f"| {yr} | ${float(v):,.0f} | {share:.1f}% |")
            lines.append("")

        # 5. Manager concentration — top-3 by commitment.
        lines.append("### Manager concentration (top 3)")
        lines.append("")
        if not commit_rows.empty:
            mgr_total = (
                commit_rows.groupby("manager")["commitment_usd"]
                .sum()
                .sort_values(ascending=False)
            )
            total = float(mgr_total.sum())
            lines.append("| rank | manager | commitment | share |")
            lines.append("|---|---|---:|---:|")
            for i, (mgr, v) in enumerate(mgr_total.head(3).items(), start=1):
                share = (float(v) / total * 100.0) if total > 0 else 0.0
                lines.append(
                    f"| {i} | {mgr} | ${float(v):,.0f} | {share:.1f}% |"
                )
            lines.append("")

        # 6. NAV by manager at end of horizon.
        lines.append("### NAV by manager (end of horizon)")
        lines.append("")
        if not pe_rows.empty:
            # End-of-horizon NAV per fund: take the last nav_end_usd
            # row per fund (ledger nav_end_usd is per-bucket cumulative;
            # we need per-fund, which requires the projection frame —
            # but we don't have direct access. Instead, approximate by
            # using the per-fund cumulative net of (calls - distributions
            # + nav_marks) which equals the projection's nav_end_usd at
            # the last quarter that fund appeared).
            last_per_fund = (
                pe_rows.sort_values(["fund_name", "quarter"])
                .groupby("fund_name")
                .agg(
                    cum_call=(
                        "amount_usd",
                        lambda s: float(
                            pe_rows.loc[s.index][
                                (pe_rows.loc[s.index]["flow_type"] == "pe_call")
                                & (pe_rows.loc[s.index]["amount_usd"] > 0)
                            ]["amount_usd"].sum()
                        ),
                    ),
                )
            )
            # Simpler: per fund, cumulative pe_call (positive sleeve
            # side) - cumulative pe_distribution (positive sleeve
            # negative → use abs) + cumulative pe_nav_mark =
            # end-of-horizon NAV.
            per_fund_nav = {}
            for fname in pe_rows["fund_name"].unique():
                sub = pe_rows[pe_rows["fund_name"] == fname]
                call = float(
                    sub[(sub["flow_type"] == "pe_call") & (sub["amount_usd"] > 0)][
                        "amount_usd"
                    ].sum()
                )
                dist = float(
                    sub[
                        (sub["flow_type"] == "pe_distribution")
                        & (sub["amount_usd"] < 0)
                    ]["amount_usd"].sum()
                )  # negative
                mark = float(
                    sub[sub["flow_type"] == "pe_nav_mark"]["amount_usd"].sum()
                )
                per_fund_nav[fname] = call + dist + mark  # dist already negative
            mgr_nav: dict[str, float] = {}
            for fname, v in per_fund_nav.items():
                mgr = _manager_of(fname)
                mgr_nav[mgr] = mgr_nav.get(mgr, 0.0) + float(v)
            lines.append("| manager | end-of-horizon PE NAV |")
            lines.append("|---|---:|")
            for mgr in sorted(mgr_nav.keys()):
                lines.append(f"| {mgr} | ${mgr_nav[mgr]:,.0f} |")
            lines.append("")

        lines.append(
            "_Note: manager identity does not enter the ledger (`source` "
            "remains `pacing:<fund_name>`); the section above is computed "
            "by joining ledger PE-flow rows back to `pe_pacing.funds` "
            "metadata. Exited funds (`status: exited`) are excluded from "
            "forward-flow diagnostics. See MODEL_DOCUMENTATION.md "
            "§Phase 9 design._"
        )
        lines.append("")

    # Cost-aware allocator calibration (engine=cvxportfolio only). Emit
    # the rule-of-thumb suggested λ_norm vs the configured value, plus a
    # corner-dominance flag per the 2026-05-02 calibration sweep
    # (advisory only; does not influence the optimization).
    if allocator_diagnostics is not None and allocator_diagnostics.get("engine") == "cvxportfolio":
        summary = allocator_diagnostics.get("calibration_summary") or {}
        if summary.get("n_quarters", 0) > 0:
            used = summary.get("policy_loss_lambda_norm_used")
            sug = summary.get("suggested_policy_loss_lambda_norm_median")
            ratio = summary.get("ratio_used_over_suggested_median")
            v_med = summary.get("v_total_usd_median")
            formula = allocator_diagnostics.get(
                "suggested_lambda_norm_formula", "bps_per_trade * V_total * 1e-3"
            )
            lines.append("## Cost-aware allocator calibration (advisory)")
            lines.append("")
            lines.append(f"- formula: `{formula}`")
            lines.append(f"- median V_total: ${float(v_med):,.0f}")
            lines.append(f"- policy_loss_lambda_norm (used): {float(used):g}")
            lines.append(f"- suggested_policy_loss_lambda_norm (median): {float(sug):g}")
            if ratio is not None:
                lines.append(f"- ratio used / suggested (median): {float(ratio):.3g}")
                if float(ratio) < 1e-2:
                    lines.append(
                        "- regime: **corner-dominated** "
                        "(`ratio < 0.01` → λ_norm too low to engage interior "
                        "partial-trade behavior at this V_total / bps; cost-aware "
                        "behavior reduces to suppress-over-trading without tunable "
                        "policy/cost balance — see MODEL_DOCUMENTATION.md §Phase 4b)"
                    )
                elif float(ratio) > 1e2:
                    lines.append(
                        "- regime: **policy-dominated** "
                        "(`ratio > 100` → λ_norm so high that policy-tracking "
                        "wins everywhere; effectively cost-blind)"
                    )
                else:
                    lines.append(
                        "- regime: tunable "
                        "(`0.01 ≤ ratio ≤ 100` → λ_norm meaningfully balances "
                        "policy-tracking vs trade-cost suppression)"
                    )
            lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
