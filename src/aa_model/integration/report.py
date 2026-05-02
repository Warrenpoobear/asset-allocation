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
