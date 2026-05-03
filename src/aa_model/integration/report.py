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
    from aa_model.ingestion.schemas import IngestionResult
    from aa_model.ingestion.schemas_position import PositionIngestionResult
    from aa_model.liquidity.coverage import LiquidityCoverageResult
    from aa_model.pe.call_reconciliation import WorkbookCallReconciliationDiagnostics
    from aa_model.producers.distribution import DistributionProducerDiagnostics


# Phase 10 / L14 advisory thresholds. **Diagnostic heuristics, not
# validation gates.** Crossing them does not invalidate the run; it
# flags interpretation risk. The values are documented round-number
# heuristics, not derived from any specific market study. See
# MODEL_DOCUMENTATION.md §Phase 10 design.
_TX_COST_HEURISTIC_PCT_OF_INITIAL_NAV: float = 0.01  # 1% cumulative
_TX_QUARTERLY_LIQUID_TURNOVER_HEURISTIC_PCT: float = 0.25  # 25% per quarter


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
    spending_diagnostics: dict | None = None,
    distribution_producer_diagnostics: DistributionProducerDiagnostics | None = None,
    workbook_ingestion_result: IngestionResult | None = None,
    position_ingestion_result: PositionIngestionResult | None = None,
    liquidity_coverage_result: LiquidityCoverageResult | None = None,
    call_recon_diag: WorkbookCallReconciliationDiagnostics | None = None,
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
        lines.append("| bucket | expected return (annual) | volatility (annual) | liquidity |")
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
                f"- entries clipped to [-1, 1]: " f"{int(shock_diagnostics.clipped_pairs or 0)}"
            )
        else:  # override
            lines.append(f"- pairwise replacements: {int(shock_diagnostics.override_pairs or 0)}")
        lines.append(f"- max |Δρ| vs baseline: {float(shock_diagnostics.max_abs_delta):.4f}")
        lines.append("- PSD: pass")
        lines.append(
            "- note: CMA baseline preserved; this is a perturbation layer " "applied to a copy."
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
        total_clipped = sum(d.clipped_to_zero_liquid_count for _q, d in overlay_history)

        lines.append("## Illiquidity overlay")
        lines.append("")
        lines.append(f"- illiquid buckets locked: {sorted(illiquid_buckets)}")
        lines.append("- per-bucket worst-quarter drift:")
        lines.append("")
        lines.append("| bucket | policy | worst current | drift (current − policy) | quarter |")
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
            (d.max_abs_illiquid_drift for _q, d in overlay_history),
            default=0.0,
        )
        sum_overall_means = sum(d.sum_abs_illiquid_drift for _q, d in overlay_history) / max(
            len(overlay_history), 1
        )
        lines.append(
            f"- max |drift| across all illiquid buckets × quarters: " f"{max_overall * 100:.2f}%"
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
            lines.append("|---" + "|---:" * len(piv.columns) + "|")
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
            calls_per_fund = calls.groupby("fund_name")["amount_usd"].sum().to_dict()
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
            lines.append("| manager | commitment | called | unfunded | unfunded % |")
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
            lines.append("| manager | cumulative calls | cumulative distributions |")
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
                commit_rows.groupby("manager")["commitment_usd"].sum().sort_values(ascending=False)
            )
            total = float(mgr_total.sum())
            lines.append("| rank | manager | commitment | share |")
            lines.append("|---|---|---:|---:|")
            for i, (mgr, v) in enumerate(mgr_total.head(3).items(), start=1):
                share = (float(v) / total * 100.0) if total > 0 else 0.0
                lines.append(f"| {i} | {mgr} | ${float(v):,.0f} | {share:.1f}% |")
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
            # Per fund: cumulative pe_call (positive sleeve side)
            # - cumulative pe_distribution (negative on sleeve) +
            # cumulative pe_nav_mark = end-of-horizon NAV.
            per_fund_nav = {}
            for fname in pe_rows["fund_name"].unique():
                sub = pe_rows[pe_rows["fund_name"] == fname]
                call = float(
                    sub[(sub["flow_type"] == "pe_call") & (sub["amount_usd"] > 0)][
                        "amount_usd"
                    ].sum()
                )
                dist = float(
                    sub[(sub["flow_type"] == "pe_distribution") & (sub["amount_usd"] < 0)][
                        "amount_usd"
                    ].sum()
                )  # negative
                mark = float(sub[sub["flow_type"] == "pe_nav_mark"]["amount_usd"].sum())
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

    # Transaction cost summary (Phase 10 / L14). Renders only when
    # transaction_cost rows exist (non-stub implementation engine with
    # bps_per_trade > 0). Surfaces four metrics + a 3-message priority
    # advisory line + the load-bearing "diagnostic heuristics, not
    # validation failures" note. No math change; no schema change; no
    # config knob.
    full_df = ledger.finalize()
    tx_rows = full_df[full_df["flow_type"] == "transaction_cost"]
    if not tx_rows.empty:
        cum_tx = float(-tx_rows["amount_usd"].sum())  # tx amounts are negative

        # Liquid bucket set from cma.liquidity. Phase 8 cross-config
        # validator guarantees this is well-formed when the L8 overlay
        # is on; under overlay-off (regression mode), some liquidity
        # tags may be missing — fall back to "all non-pe_*" buckets in
        # that case so the diagnostic still surfaces.
        if cma is not None and not cma.liquidity.empty:
            liquid_buckets = {
                str(b) for b, tag in cma.liquidity.items() if str(tag) in ("liquid", "semi_liquid")
            }
        else:
            liquid_buckets = {b for b in full_df["bucket"].unique() if not str(b).startswith("pe_")}

        rb = full_df[full_df["flow_type"] == "rebalance"].copy()
        if not rb.empty:
            rb_liquid = rb[rb["bucket"].isin(liquid_buckets)].copy()
        else:
            rb_liquid = rb

        # Liquid turnover totals: paired buy / sell rows sum to zero per
        # quarter, so the one-side trade volume is sum(|trade|) / 2.
        if not rb_liquid.empty:
            total_liquid_turnover = float(rb_liquid["amount_usd"].abs().sum() / 2.0)
            rb_liquid_per_q = rb_liquid.groupby("quarter")["amount_usd"].apply(
                lambda s: float(s.abs().sum() / 2.0)
            )
        else:
            total_liquid_turnover = 0.0
            rb_liquid_per_q = pd.Series([], dtype=float)

        n_quarters = int(full_df["quarter"].nunique()) if not full_df.empty else 0
        mean_quarterly_liquid_turnover = (
            total_liquid_turnover / n_quarters if n_quarters > 0 else 0.0
        )

        initial_total = sum(ledger.initial_nav.values()) if ledger.initial_nav else 0.0
        cum_tx_pct = cum_tx / initial_total * 100.0 if initial_total > 0 else 0.0
        if not rb_liquid_per_q.empty and initial_total > 0:
            max_q_turnover_pct = float(rb_liquid_per_q.max() / initial_total * 100.0)
        else:
            max_q_turnover_pct = 0.0

        # Engine name + bps from the implementation config.
        impl_engine = cfg.base.implementation.engine
        impl_bps = float(cfg.base.implementation.bps_per_trade)

        lines.append("## Transaction cost summary")
        lines.append("")
        lines.append(f"- engine: `{impl_engine}` @ {impl_bps:g} bps")
        lines.append(f"- cumulative transaction_cost: ${cum_tx:,.0f}")
        lines.append(f"- as % of initial NAV: {cum_tx_pct:.2f}%")
        lines.append(
            f"- liquid rebalance turnover (sum |trade|, liquid buckets): "
            f"${total_liquid_turnover / 1e6:,.2f}M total, "
            f"${mean_quarterly_liquid_turnover / 1e3:,.0f}K / quarter mean"
        )
        lines.append(
            f"- max single-quarter liquid turnover as % of NAV: " f"{max_q_turnover_pct:.2f}%"
        )

        # Three-message advisory, priority order:
        #   1. max quarterly turnover > 25% wins
        #   2. cumulative cost > 1% of initial NAV
        #   3. otherwise, all-clear
        if max_q_turnover_pct > _TX_QUARTERLY_LIQUID_TURNOVER_HEURISTIC_PCT * 100.0:
            lines.append(
                f"- advisory: ⚠️ max quarterly liquid turnover > "
                f"{_TX_QUARTERLY_LIQUID_TURNOVER_HEURISTIC_PCT * 100:.0f}% of NAV "
                "— linear-bps approximation may underprice market impact at "
                "this trade size."
            )
        elif cum_tx_pct > _TX_COST_HEURISTIC_PCT_OF_INITIAL_NAV * 100.0:
            lines.append(
                f"- advisory: ⚠️ cumulative cost > "
                f"{_TX_COST_HEURISTIC_PCT_OF_INITIAL_NAV * 100:.0f}% of initial NAV "
                "— cost is material; consider per-bucket bps or a richer cost "
                "model for stress runs."
            )
        else:
            lines.append(
                "- advisory: linear-bps approximation covers this regime "
                "(turnover and cost both within typical scale)."
            )
        lines.append("")
        lines.append(
            "_These thresholds are diagnostic heuristics, not validation "
            "failures. Crossing them does not invalidate the run; it flags "
            "interpretation risk. PE-secondary / asymmetric / "
            "quadratic-impact / fee-economics costs are out of scope for "
            "the linear bps model. See MODEL_DOCUMENTATION.md §Phase 10 "
            "/ L14._"
        )
        lines.append("")

    # Owl scale-sensitivity (Phase 11 / L16). Gated on rule == "owl"
    # AND the rule actually fired (which it always does at q0 + each
    # year boundary, so the second condition is implicit when Owl is
    # selected and the run has at least one quarter). Surfaces the
    # absolute-clamp configuration, activation counts, and a regime
    # classification — plus the **L19 caveat verbatim** so future
    # readers don't mistake L16 closure for "Owl is family-office
    # realistic".
    if (
        cfg.spending.rule == "owl"
        and spending_diagnostics is not None
        and spending_diagnostics.get("engine") == "OwlRule"
    ):
        gr = cfg.spending.guardrail
        min_set = gr is not None and gr.absolute_min_annual_usd is not None
        max_set = gr is not None and gr.absolute_max_annual_usd is not None
        min_value = gr.absolute_min_annual_usd if min_set else None
        max_value = gr.absolute_max_annual_usd if max_set else None
        min_clamps = int(spending_diagnostics.get("min_clamp_activations", 0))
        max_clamps = int(spending_diagnostics.get("max_clamp_activations", 0))

        lines.append("## Owl scale-sensitivity (advisory)")
        lines.append("")
        lines.append("- absolute guardrail clamps:")
        if min_set:
            lines.append(f"  - absolute_min_annual_usd: ${float(min_value):,.0f}")
        else:
            lines.append("  - absolute_min_annual_usd: not set")
        if max_set:
            lines.append(f"  - absolute_max_annual_usd: ${float(max_value):,.0f}")
        else:
            lines.append("  - absolute_max_annual_usd: not set")
        lines.append("- clamp activations during run:")
        lines.append(f"  - min-clamp activated: {min_clamps} year-boundary quarters")
        lines.append(f"  - max-clamp activated: {max_clamps} year-boundary quarters")
        lines.append("- regime classification:")
        if min_set or max_set:
            lines.append(
                "  - **scale-aware (clamps configured)** — the absolute "
                "guardrail breaks the rate-based scale-invariance; spending "
                "trajectories diverge between same-rate-different-NAV "
                "households at the clamp boundary."
            )
        else:
            lines.append(
                "  - **scale-invariant (no absolute clamps configured)** — "
                "Owl's rate-band trigger is scale-invariant under proportional "
                "setup; a $100M and $1B household with the same initial spend "
                "rate get identical Owl trajectories. Set "
                "``absolute_min_annual_usd`` / ``absolute_max_annual_usd`` to "
                "break the invariance."
            )
        lines.append("")
        lines.append(
            "_**Phase 11 fixes scale-invariance only — it does NOT resolve "
            "spending-base realism (L19).** Owl still measures rate against "
            "**total NAV**, including illiquid private real estate, opco "
            "equity, development assets, and land. For a Gen3-Gen5 SFO this "
            "may overstate spending capacity. See MODEL_DOCUMENTATION.md "
            "§Use-case context + §Phase 11 design + L19._"
        )
        lines.append("")

    # Owl spending base (Phase 12 / L19). Two render modes:
    #   (a) non-default base — full diagnostic with both exclusion
    #       breakdowns + dual withdrawal rates + warning bands.
    #   (b) default base (total_nav) but ≥30% of NAV is illiquid /
    #       locked_strategic — short warning pointing the reader at
    #       the non-default modes (catches the "default-on but the
    #       SFO needs a non-default base" failure mode).
    # Gated on rule == "owl" and the rule fired.
    if (
        cfg.spending.rule == "owl"
        and spending_diagnostics is not None
        and spending_diagnostics.get("engine") == "OwlRule"
    ):
        mode_raw = spending_diagnostics.get("spending_base_mode")
        mode_label = mode_raw if mode_raw is not None else "total_nav (default)"
        is_default = mode_raw is None or mode_raw == "total_nav"
        total_nav = float(spending_diagnostics.get("total_nav_run_end_usd", 0.0))
        base_usd = float(spending_diagnostics.get("spending_base_run_end_usd", 0.0))
        excl_by_tier = spending_diagnostics.get("excluded_nav_by_tier_usd", {})
        excl_by_inc = spending_diagnostics.get("excluded_nav_by_income_flag_usd", {})
        rate_total = float(spending_diagnostics.get("withdrawal_rate_vs_total_nav", 0.0))
        rate_base = float(spending_diagnostics.get("withdrawal_rate_vs_spending_base", 0.0))
        material_share = float(spending_diagnostics.get("material_illiquid_share", 0.0))

        # Mode (c): Phase 12.5 / L19 flow-side render — distributable_income.
        # Dispatched before the NAV-side modes so the Phase 12 (a) block
        # below does NOT fire for this mode (its tier/income-flag exclusion
        # rollups are empty here, and the dollar-base interpretation is
        # different — trailing realized income vs. NAV slice).
        if mode_raw == "distributable_income":
            trailing_income = float(
                spending_diagnostics.get("trailing_distributable_income_usd", 0.0)
            )
            by_source = spending_diagnostics.get("distributable_income_by_source_usd", {})
            used_bootstrap = bool(spending_diagnostics.get("used_bootstrap_at_run_end", False))
            gr = cfg.spending.guardrail
            window_q = gr.distribution_window_quarters if gr is not None else None
            bootstrap_usd = gr.bootstrap_distributable_income_usd if gr is not None else None
            # Render only when Owl entered the year-boundary path —
            # diagnostics populated.
            if base_usd > 0.0 and total_nav > 0.0:
                lines.append("## Owl spending base (advisory)")
                lines.append("")
                lines.append("- selected base: distributable_income")
                lines.append("- run-end totals:")
                lines.append(f"  - total NAV:                          ${total_nav:,.0f}")
                window_label = f"{window_q}q" if window_q is not None else "—"
                lines.append(
                    f"  - trailing distributable income ({window_label}):  "
                    f"${trailing_income:,.0f}"
                )
                if bootstrap_usd is not None:
                    lines.append(
                        f"  - bootstrap distributable income:     " f"${float(bootstrap_usd):,.0f}"
                    )
                lines.append(
                    "  - source of base this year:           "
                    + ("bootstrap" if used_bootstrap else "realized")
                )
                if by_source:
                    lines.append("- distributable income by source (run end):")
                    for src in sorted(by_source.keys()):
                        lines.append(f"  - {src}: ${float(by_source[src]):,.0f}")
                lines.append("- withdrawal-rate comparison (run end):")
                lines.append(f"  - rate vs total NAV:                  {rate_total * 100:.2f}%")
                lines.append(
                    f"  - rate vs distributable-income base:  "
                    f"{rate_base * 100:.2f}%   "
                    "← rate the household actually faces"
                )
                lines.append("- regime:")
                lines.append("  - flow-side aware (selected base = trailing realized " "income)")
                if rate_base >= 1.00:
                    lines.append(
                        "  - **STRONG WARNING — rate vs distributable-income "
                        f"base ≥ 100% ({rate_base * 100:.0f}%); household is "
                        "spending more than it earns; trajectory will erode "
                        "capital.**"
                    )
                elif rate_base >= 0.80:
                    lines.append(
                        f"  - **WARNING — rate vs distributable-income base "
                        f"is {rate_base * 100:.0f}%; within 20% of income "
                        "ceiling.**"
                    )
                if used_bootstrap:
                    lines.append(
                        "  - INFO: run end used bootstrap (insufficient "
                        "closed window); realized window not yet complete."
                    )
                if len(by_source) == 1 and trailing_income > 0.0 and not used_bootstrap:
                    lines.append(
                        "  - INFO: single-source income concentration — "
                        "trailing window dominated by one originator."
                    )
                # Phase 12.5 reviewer tightening 3 — recurring-vs-one-time
                # caveat surfaces as a permanent CAVEAT line so a high
                # base is not silently mistaken for stable recurring yield.
                lines.append(
                    "  - **CAVEAT: Phase 12.5 treats every "
                    "``distribution_inflow`` row equally. Recurring vs. "
                    "one-time classification is deferred to the producer "
                    "layer (Phase 13/14). A high distributable-income base "
                    "may be overstated if the trailing window is dominated "
                    "by asset sales, refinancings, special dividends, or "
                    "one-time entity transfers. Review the by-source "
                    "breakdown above before relying on the headline rate.**"
                )
                lines.append("")
                lines.append(
                    "_Phase 12.5 / L19 lands the **infrastructure** for "
                    "flow-side spending-base realism — the ledger flow type, "
                    "base computation, and rate-band integration. "
                    "**Production-grade distributable-income realism remains "
                    "dependent on Phase 13 (RE+OpCo pipeline) and Phase 14 "
                    "(cash-flow / entity ingestion) producers.** Phase 12.5 "
                    "does not determine legal / tax / entity-governance "
                    "distributability; it consumes rows already classified "
                    "upstream as family-office-distributable._"
                )
                lines.append("")

        # Mode (a): non-default base full diagnostic. Render only when
        # the rule actually entered a year-boundary path (snapshots
        # populated) — at <4 quarters Owl never fires the year-boundary
        # logic and the base diagnostics stay zero. Phase 12.5 mode is
        # handled above and short-circuits this block.
        if (
            not is_default
            and mode_raw != "distributable_income"
            and base_usd > 0.0
            and total_nav > 0.0
        ):
            ratio = base_usd / total_nav
            lines.append("## Owl spending base (advisory)")
            lines.append("")
            lines.append(f"- selected base: {mode_label}")
            lines.append("- run-end totals:")
            lines.append(f"  - total NAV:     ${total_nav:,.0f}")
            lines.append(
                f"  - spending base: ${base_usd:,.0f}   " f"({ratio * 100:.0f}% of total NAV)"
            )
            if excl_by_tier:
                lines.append("- excluded NAV by liquidity tier:")
                for tier in ("liquid", "semi_liquid", "illiquid", "locked_strategic"):
                    if tier in excl_by_tier:
                        lines.append(f"  - {tier}: ${float(excl_by_tier[tier]):,.0f}")
            if excl_by_inc:
                lines.append("- excluded NAV by income_producing flag:")
                for flag in (False, True):
                    if flag in excl_by_inc:
                        lines.append(
                            f"  - income_producing={flag}: " f"${float(excl_by_inc[flag]):,.0f}"
                        )
            lines.append("- withdrawal-rate comparison (run end):")
            lines.append(f"  - rate vs total NAV:     {rate_total * 100:.2f}%")
            lines.append(
                f"  - rate vs spending base: {rate_base * 100:.2f}%   "
                "← rate the household actually faces"
            )
            lines.append("- regime:")
            if ratio < 0.4:
                lines.append(
                    "  - **STRONG WARNING — spending base is materially below "
                    f"total NAV ({ratio * 100:.0f}%). Confirm CMA tagging "
                    "policy reflects the actual balance sheet.**"
                )
            elif ratio < 0.7:
                lines.append(
                    f"  - **WARNING — spending base is {ratio * 100:.0f}% of "
                    "total NAV. Owl trajectory reflects spending-capacity "
                    "rate, not paper-NAV rate.**"
                )
            else:
                lines.append(
                    "  - spending-base aware (selected base near total NAV; "
                    "rate-band geometry preserved)."
                )
            lines.append("")
            lines.append(
                "_Phase 12 / L19 closes the base-side of spending realism. "
                "The flow-side (realized distributions) is Phase 12.5. The "
                "``liquid_plus_income_producing_nav`` mode includes the NAV "
                "of buckets tagged ``income_producing``; it does not measure "
                "actual distributable income. Stabilized real estate tagged "
                "``income_producing=true`` contributes its appraised NAV to "
                "this base — which still overstates spending capacity vs. "
                "true distributable yield. For a tight distributable-income "
                "figure see Phase 12.5 (`distributable_income` mode + new "
                "`distribution_inflow` ledger flow type)._"
            )
            lines.append("")

        # Mode (b): default base + material non-spendable NAV warning.
        # Triggers when default base is in use AND ≥30% of total NAV is
        # illiquid / locked_strategic. The diagnostic depends on the
        # OwlRule having seen a year-boundary path (so the snapshot is
        # populated); short runs (<4 quarters) skip this section.
        if is_default and total_nav > 0.0 and excl_by_tier and material_share >= 0.30:
            illiquid_usd = float(excl_by_tier.get("illiquid", 0.0))
            locked_usd = float(excl_by_tier.get("locked_strategic", 0.0))
            lines.append("## Owl spending base (advisory)")
            lines.append("")
            lines.append("- selected base: total_nav (default)")
            lines.append("- run-end totals:")
            lines.append(f"  - total NAV:        ${total_nav:,.0f}")
            if illiquid_usd > 0.0:
                lines.append(
                    f"  - illiquid NAV:     ${illiquid_usd:,.0f}   "
                    f"({illiquid_usd / total_nav * 100:.0f}% of total NAV)"
                )
            if locked_usd > 0.0:
                lines.append(
                    f"  - locked_strategic: ${locked_usd:,.0f}   "
                    f"({locked_usd / total_nav * 100:.0f}% of total NAV)"
                )
            lines.append(
                f"- **WARNING — spending_base = total_nav, but "
                f"{material_share * 100:.0f}% of total NAV is illiquid or "
                "locked_strategic. Owl is measuring withdrawal rate against "
                "paper NAV that is not spendable. Consider setting "
                "``spending.guardrail.spending_base`` to one of:**"
            )
            lines.append("  - ``liquid_nav`` (strictest)")
            lines.append(
                "  - ``liquid_plus_income_producing_nav`` (recommended SFO "
                "default; note this includes NAV of income-producing "
                "buckets, not distributable income)"
            )
            lines.append("  - ``custom_policy`` (per-bucket inclusion weights)")
            lines.append("")

    # Distribution producer (Phase 13 / L19 producer-side). Composes
    # with the Phase 12.5 ## Owl spending base section above — readers
    # get both consumer-side (Owl base view) and producer-side
    # (emissions, restricted, concentration) snapshots. Gated on the
    # producer being configured AND emitting at least one row.
    if (
        distribution_producer_diagnostics is not None
        and distribution_producer_diagnostics.total_emitted_usd > 0.0
    ):
        d = distribution_producer_diagnostics
        total = d.total_emitted_usd
        lines.append("## Distribution producer (advisory)")
        lines.append("")
        lines.append("- emissions by domain (run total):")
        for dom in sorted(d.emitted_by_domain_usd.keys()):
            usd = float(d.emitted_by_domain_usd[dom])
            lines.append(f"  - {dom}: ${usd:,.0f}")
        if d.emitted_by_recurrence_usd:
            lines.append("- emissions by recurrence type:")
            for rt in ("recurring", "one_time"):
                if rt in d.emitted_by_recurrence_usd:
                    usd = float(d.emitted_by_recurrence_usd[rt])
                    pct = (usd / total * 100.0) if total > 0 else 0.0
                    lines.append(f"  - {rt}: ${usd:,.0f}   ({pct:.0f}%)")
        if d.emitted_by_confidence_usd:
            lines.append("- emissions by confidence:")
            for conf in ("contractual", "forecast", "scenario"):
                if conf in d.emitted_by_confidence_usd:
                    usd = float(d.emitted_by_confidence_usd[conf])
                    pct = (usd / total * 100.0) if total > 0 else 0.0
                    lines.append(f"  - {conf}: ${usd:,.0f}   ({pct:.0f}%)")
        top3 = d.top_n_sources(3)
        if top3:
            lines.append("- top-3 sources (by USD, run total):")
            for src, usd in top3:
                lines.append(f"  - {src}: ${usd:,.0f}")
        if d.excluded_restricted_count > 0:
            lines.append("- excluded (restricted=True):")
            lines.append(f"  - count: {d.excluded_restricted_count} entries")
            lines.append(f"  - dollars: ${d.excluded_restricted_usd:,.0f}")
        # Regime + warning bands.
        lines.append("- regime:")
        lines.append("  - producer-feed active (config-driven)")
        if d.one_time_share_pct >= 0.30:
            lines.append(
                f"  - **WARNING — one-time share is "
                f"{d.one_time_share_pct * 100:.0f}% of trailing emissions; "
                "rate-band reads against a base materially dependent on "
                "non-recurring flows.**"
            )
        if d.top_3_source_concentration_pct >= 0.80:
            lines.append(
                f"  - **WARNING — top-3 sources account for "
                f"{d.top_3_source_concentration_pct * 100:.0f}% of "
                "emissions; concentration risk in the trailing-income "
                "base.**"
            )
        if d.forecast_scenario_share_pct >= 0.20:
            lines.append(
                f"  - **WARNING — "
                f"{d.forecast_scenario_share_pct * 100:.0f}% of emissions "
                "are forecast or scenario confidence; review producer "
                "config before relying on the trailing-income rate.**"
            )
        if d.excluded_restricted_count > 0:
            lines.append(
                f"  - INFO: {d.excluded_restricted_count} restricted "
                "entries surfaced for transparency (excluded from ledger)."
            )
        lines.append("")
        lines.append(
            "_Phase 13 implements the config-driven producer for "
            "``distribution_inflow`` rows. Workbook-driven ingestion of "
            "real SFO income data lands in Phase 14. The producer trusts "
            "upstream classification (legal / tax / entity-governance "
            "distributability, recurring vs one-time, restricted flag) "
            "per Phase 12.5 reviewer tightening 1; it does not determine "
            "distributability of its own. Phase 13 also does NOT model "
            "inter-entity cash-movement mechanics — configured entries "
            "are treated as already approved, distributable, and payable "
            "to the modeled liquidity pool (Phase 13 reviewer tightening "
            "1)._"
        )
        lines.append("")

    # Workbook ingestion (Phase 14 / L19 workbook-side). Composes
    # with Phase 12.5 + Phase 13 advisory sections — readers see the
    # full provenance chain: workbook → producer → spending base →
    # trajectory. Gated on an ingestion having run.
    if workbook_ingestion_result is not None:
        wir_diag = workbook_ingestion_result.diagnostics
        lines.append("## Workbook ingestion (advisory)")
        lines.append("")
        lines.append("- workbook:")
        lines.append(f"  - filename: {wir_diag.workbook_filename}")
        lines.append(f"  - hash: {wir_diag.workbook_hash[:16]}…")
        lines.append(f"  - workbook_version (manifest): {wir_diag.workbook_version}")
        lines.append(f"  - manifest_version: {wir_diag.manifest_version}")
        lines.append("- sheets:")
        lines.append(f"  - ingested: {len(wir_diag.sheets_ingested)}")
        if wir_diag.unmapped_sheets:
            lines.append(
                f"  - unmapped: {len(wir_diag.unmapped_sheets)} "
                f"({', '.join(repr(s) for s in wir_diag.unmapped_sheets[:3])}"
                + (
                    f", … +{len(wir_diag.unmapped_sheets) - 3} more"
                    if len(wir_diag.unmapped_sheets) > 3
                    else ""
                )
                + ")"
            )
        if wir_diag.missing_optional_sheets:
            lines.append(f"  - missing optional: {len(wir_diag.missing_optional_sheets)}")
        lines.append("- rows:")
        lines.append(f"  - parsed (data lines): {len(workbook_ingestion_result.cash_flow_lines)}")
        lines.append(f"  - blank skipped: {wir_diag.blank_rows_skipped}")
        lines.append(f"  - subtotal excluded: {wir_diag.excluded_subtotal_rows}")
        if wir_diag.unparseable_period_headers:
            lines.append(
                f"  - unparseable period headers: {len(wir_diag.unparseable_period_headers)}"
            )
        if wir_diag.total_inflows_usd_by_entity:
            lines.append("- per-entity inflow totals (run horizon):")
            for entity_id in sorted(wir_diag.total_inflows_usd_by_entity.keys()):
                amt = float(wir_diag.total_inflows_usd_by_entity[entity_id])
                lines.append(f"  - {entity_id}: ${amt:,.0f}")
        if wir_diag.board_snapshot_reconciliations:
            lines.append("- board-snapshot reconciliation (advisory):")
            for label, snap, det, _abs_d, abs_pct in wir_diag.board_snapshot_reconciliations:
                tag = " — within tolerance" if abs_pct <= 0.5 else " — **WARNING (>0.5%)**"
                lines.append(
                    f"  - {label!r}: snapshot=${snap:,.0f}  detail=${det:,.0f}  "
                    f"Δ={abs_pct:.2f}%{tag}"
                )
        if wir_diag.distribution_candidates_count > 0:
            lines.append("- distribution candidates (bridge to Phase 13 producer):")
            lines.append(f"  - count: {wir_diag.distribution_candidates_count} entries")
            if wir_diag.distribution_candidates_by_domain_usd:
                lines.append("  - by domain:")
                for dom in sorted(wir_diag.distribution_candidates_by_domain_usd.keys()):
                    amt = float(wir_diag.distribution_candidates_by_domain_usd[dom])
                    lines.append(f"    - {dom}: ${amt:,.0f}")
            if wir_diag.excluded_restricted_count > 0:
                lines.append(
                    f"  - excluded (restricted=True): "
                    f"{wir_diag.excluded_restricted_count} entries "
                    f"(${wir_diag.excluded_restricted_usd:,.0f})"
                )
        if wir_diag.unmatched_lines_count > 0:
            lines.append(
                f"- unmatched lines: {wir_diag.unmatched_lines_count} "
                "— **WARNING; see ingestion log for triage**"
            )
            if wir_diag.unmatched_lines_sample:
                lines.append("  - sample:")
                for s in wir_diag.unmatched_lines_sample[:5]:
                    lines.append(f"    - {s}")
        # Phase 14 reviewer tightening 1: standing CAVEAT for cached-
        # formula stale-state risk. Always rendered.
        lines.append("")
        lines.append(f"_**CAVEAT:** {wir_diag.formula_cache_caveat}_")
        lines.append("")
        lines.append(
            "_Phase 14 ingests ``Cashflow Modeling v7.xlsx`` as a "
            "read-only integration target. The workbook itself is "
            "never mutated. Workbook classifications (distributable, "
            "restricted, recurring vs one-time, certainty) flow "
            "through the manifest's row-classification rules into "
            "the Phase 13 producer unchanged. Phase 14 does NOT "
            "determine legal / tax / entity-governance "
            "distributability; it transcribes the manifest-mapped "
            "human classifications. Board-snapshot reconciliation "
            "deltas are advisory only (Phase 14 reviewer tightening "
            "3); they never block a run._"
        )
        lines.append("")

    # Position universe (Phase 15 / L20 investment summary ingestion).
    # Rendered when a position ingestion result was produced. Surfaces
    # provenance (workbook hash, manifest version), bucket/asset-class
    # counts, terms coverage, and stale-valuation advisory.
    if position_ingestion_result is not None:
        from aa_model.ingestion.investment_summary import render_position_report_section

        lines.append(render_position_report_section(position_ingestion_result))
        lines.append("")

    # Liquidity coverage (Phase 16 / L20). Rendered when a coverage
    # result was produced alongside a position ingestion result.
    # spending_base_mode is derived from the study spending config so
    # the label on liquid_to_spending_base reflects the active mode
    # (reviewer tightening 5 — explicit parameter, not inferred).
    if liquidity_coverage_result is not None:
        from aa_model.liquidity.coverage import render_coverage_report_section

        spending_base_mode: str | None = None
        gr = cfg.spending.guardrail
        if cfg.spending.rule == "owl" and gr is not None and gr.spending_base is not None:
            spending_base_mode = gr.spending_base

        lines.append(
            render_coverage_report_section(
                liquidity_coverage_result,
                spending_base_mode=spending_base_mode,
            )
        )
        lines.append("")

    # PE call-obligation reconciliation (Phase 20 / L20). Rendered when
    # position ingestion is configured. Source precedence:
    # explicit_config > cashflow_workbook > pe_pacing_model > unavailable.
    # Delta table rendered when both workbook and PE pacing are available.
    if call_recon_diag is not None:
        d20 = call_recon_diag
        calls_fmt = (
            f"${d20.next_12m_capital_calls_usd:,.0f}"
            if d20.next_12m_capital_calls_usd is not None
            else "n/a"
        )
        lines.append("## PE call-obligation reconciliation (Phase 20, advisory)")
        lines.append("")
        lines.append(f"  source_used:               {d20.source_used}")
        lines.append(f"  coverage_quarter:          {d20.coverage_quarter}")
        lines.append(f"  next_12m_capital_calls:    {calls_fmt}")
        lines.append(f"  window:                    {', '.join(d20.quarters_in_window)}")
        if d20.explicit_usd is not None:
            lines.append(f"  explicit_config_usd:       ${d20.explicit_usd:,.0f}")
        if d20.workbook_total_usd is not None:
            lines.append(f"  workbook_total_usd:        ${d20.workbook_total_usd:,.0f}")
            if d20.workbook_calls_by_quarter:
                lines.append("  workbook_calls_by_quarter:")
                for q_str in sorted(d20.workbook_calls_by_quarter.keys()):
                    lines.append(f"    {q_str}: ${d20.workbook_calls_by_quarter[q_str]:,.0f}")
        pe = d20.pe_bridge
        pe_total_fmt = (
            f"${pe.next_12m_capital_calls_usd:,.0f}"
            if pe.next_12m_capital_calls_usd is not None
            else "n/a"
        )
        lines.append(f"  pe_pacing_total_usd:       {pe_total_fmt}")
        if pe.calls_by_quarter:
            lines.append("  pe_calls_by_quarter:")
            for q_str in sorted(pe.calls_by_quarter.keys()):
                lines.append(f"    {q_str}: ${pe.calls_by_quarter[q_str]:,.0f}")
        if pe.top_contributors:
            lines.append("  pe_top_contributors (fund, next-12m call):")
            for fund_name, amt in pe.top_contributors:
                lines.append(f"    {fund_name}: ${amt:,.0f}")
        if d20.total_delta_usd is not None:
            lines.append(
                f"  reconciliation_delta:      ${d20.total_delta_usd:+,.0f} "
                f"({d20.total_delta_pct:.1f}% of max) — {d20.delta_classification}"
            )
            if d20.delta_by_quarter:
                lines.append("  delta_by_quarter (workbook − pe_pacing):")
                for q_str in sorted(d20.delta_by_quarter.keys()):
                    lines.append(f"    {q_str}: ${d20.delta_by_quarter[q_str]:+,.0f}")
        all_advisories = list(pe.advisories) + d20.advisories
        if all_advisories:
            lines.append(f"  ADVISORIES ({len(all_advisories)}): " + "; ".join(all_advisories))
        lines.append("")
        lines.append(
            "_Capital calls are derived from the cash-flow worksheet (primary) "
            "and deterministic PE pacing projections (cross-check). "
            "The worksheet is the operating forecast spine. "
            "T4: calls are never inferred from unfunded commitments × a percentage._"
        )
        lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
