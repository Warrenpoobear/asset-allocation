"""Markdown run report.

Phase 1: minimal — run id, hashes, scenario, horizon, initial / final NAV,
cumulative return, end-of-horizon allocation, total NAV per quarter.
HTML rendering is a Phase 4 deliverable.
"""

from __future__ import annotations

from pathlib import Path

from aa_model.integration.ledger import QuarterlyLedger
from aa_model.io.schemas import StudyConfig


def write_markdown_report(
    path: Path,
    *,
    cfg: StudyConfig,
    ledger: QuarterlyLedger,
    run_id: str,
    config_hash: str,
    fixtures_hash: str,
    allocator_diagnostics: dict | None = None,
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
