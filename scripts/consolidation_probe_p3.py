"""Phase 3 consolidation probe — cross-product run report.

This is a research probe, not a feature. It iterates every allocation ×
implementation × spending-rule × scenario combination through the existing
orchestrator and produces a single comparison frame summarising:

* pass / fail (orchestrator returned a valid RunResult vs raised)
* final NAV
* min / mean coverage months
* max drawdown
* total transaction cost (when applicable)
* total spending

Usage::

    python scripts/consolidation_probe_p3.py [--out PATH]

Outputs the report to stdout (and optionally a Markdown file).
"""

from __future__ import annotations

import argparse
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path

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

ALLOC_ENGINES = ["stub", "riskfolio"]
IMPL_VARIANTS = [("stub", 0.0), ("cvxportfolio", 5.0)]
SPENDING_RULES = ["flat_real", "smoothing", "owl"]
SCENARIO_NAMES = [
    "base",
    "public_drawdown",
    "delayed_pe_distributions",
    "clustered_calls",
    "inflation_shock",
]


def _spending_yaml(rule: str) -> dict:
    base = {
        "rule": rule,
        "annual_spend_usd": 4_000_000.0,
        "inflation_pct": 0.025,
        "smoothing": {"window_quarters": 12, "weight": 0.5 if rule == "smoothing" else 0.0},
        "floor_usd": 0.0,
        "ceiling_usd": 1.0e12,
    }
    if rule == "owl":
        base["guardrail"] = {
            "upper_band_pct": 0.20,
            "lower_band_pct": 0.20,
            "raise_pct": 0.10,
            "cut_pct": 0.10,
            "forecast_quarterly_return_pct": 0.04,
        }
    return base


def _write_combo_configs(
    repo_root: Path, alloc: str, impl: str, bps: float, rule: str, tag: str
) -> Path:
    configs = repo_root / "configs"
    spending_path = configs / f"_probe_spending_{tag}.yaml"
    spending_path.write_text(yaml.safe_dump(_spending_yaml(rule)), encoding="utf-8")

    base_cfg = yaml.safe_load((configs / "base.yaml").read_text(encoding="utf-8"))
    base_cfg["allocation"]["engine"] = alloc
    base_cfg["implementation"] = {"engine": impl, "bps_per_trade": bps}
    base_cfg["spending"]["config"] = f"configs/_probe_spending_{tag}.yaml"
    base_path = configs / f"_probe_base_{tag}.yaml"
    base_path.write_text(yaml.safe_dump(base_cfg), encoding="utf-8")
    return base_path


def _cleanup(repo_root: Path, tag: str) -> None:
    configs = repo_root / "configs"
    (configs / f"_probe_base_{tag}.yaml").unlink(missing_ok=True)
    (configs / f"_probe_spending_{tag}.yaml").unlink(missing_ok=True)


@dataclass
class ComboResult:
    alloc: str
    impl: str
    bps: float
    rule: str
    scenario: str
    status: str
    final_nav: float | None = None
    cum_return_pct: float | None = None
    min_coverage_months: float | None = None
    max_drawdown_pct: float | None = None
    drawdown_quarters: int | None = None
    total_tx_cost: float | None = None
    total_spend: float | None = None
    error: str | None = None


def _run_one(
    repo_root: Path, alloc: str, impl: str, bps: float, rule: str, scenario_name: str
) -> ComboResult:
    tag = f"{alloc}_{impl}_{int(bps)}_{rule}_{scenario_name}"
    base_path = _write_combo_configs(repo_root, alloc, impl, bps, rule, tag)
    try:
        cfg = load_study_config(base_path)
        scenarios = make_scenarios(cfg.fixture_scenario, cfg.pe_pacing, cfg.spending)
        scenario = next(s for s in scenarios if s.name == scenario_name)
        result = run_orchestrator(base_path, scenario=scenario, dry_run=False)
        metrics = _metrics_for_run(result, floor_months=float(cfg.base.liquidity.floor_months))
        df = result.ledger
        tx = df[df["flow_type"] == "transaction_cost"]
        sp = df[df["flow_type"] == "spend"]
        return ComboResult(
            alloc=alloc,
            impl=impl,
            bps=bps,
            rule=rule,
            scenario=scenario_name,
            status="ok",
            final_nav=metrics.final_nav_usd,
            cum_return_pct=metrics.cumulative_return_pct,
            min_coverage_months=metrics.min_coverage_months,
            max_drawdown_pct=metrics.max_drawdown_pct,
            drawdown_quarters=metrics.drawdown_quarters,
            total_tx_cost=float(-tx["amount_usd"].sum()) if not tx.empty else 0.0,
            total_spend=float(-sp["amount_usd"].sum()) if not sp.empty else 0.0,
        )
    except Exception as exc:
        return ComboResult(
            alloc=alloc,
            impl=impl,
            bps=bps,
            rule=rule,
            scenario=scenario_name,
            status=type(exc).__name__,
            error=str(exc).splitlines()[0][:160],
        )
    finally:
        _cleanup(repo_root, tag)


def _format_report(rows: list[ComboResult]) -> str:
    df = pd.DataFrame([r.__dict__ for r in rows])
    n_total = len(df)
    n_ok = int((df["status"] == "ok").sum())
    n_fail = n_total - n_ok

    out: list[str] = []
    out.append("# Phase 3 consolidation probe\n")
    out.append("## Combinations tested\n")
    out.append(
        f"- allocation engines: {ALLOC_ENGINES}\n"
        f"- implementation variants: {[(e, b) for e, b in IMPL_VARIANTS]}\n"
        f"- spending rules: {SPENDING_RULES}\n"
        f"- scenarios: {SCENARIO_NAMES}\n"
        f"- total combinations: {n_total} ({n_ok} ok, {n_fail} fail)\n"
    )

    if n_fail:
        out.append("## Failures\n")
        fails = df[df["status"] != "ok"][
            ["alloc", "impl", "bps", "rule", "scenario", "status", "error"]
        ]
        out.append("```\n" + fails.to_string(index=False) + "\n```\n")

    ok = df[df["status"] == "ok"].copy()
    if not ok.empty:
        ok["combo"] = (
            ok["alloc"]
            + "/"
            + ok["impl"]
            + ("@" + ok["bps"].astype(int).astype(str))
            + "/"
            + ok["rule"]
        )

        # Per-combination final NAV pivot (rows = combo, cols = scenario).
        out.append("## Final NAV ($M) by combination × scenario\n")
        nav_pivot = ok.pivot(index="combo", columns="scenario", values="final_nav") / 1e6
        nav_pivot = nav_pivot.reindex(columns=SCENARIO_NAMES)
        out.append("```\n" + nav_pivot.round(2).to_string() + "\n```\n")

        out.append("## Min coverage (months) by combination × scenario\n")
        cov_pivot = ok.pivot(index="combo", columns="scenario", values="min_coverage_months")
        cov_pivot = cov_pivot.reindex(columns=SCENARIO_NAMES)
        out.append("```\n" + cov_pivot.round(1).to_string() + "\n```\n")

        out.append("## Max drawdown (%) by combination × scenario\n")
        dd_pivot = ok.pivot(index="combo", columns="scenario", values="max_drawdown_pct")
        dd_pivot = dd_pivot.reindex(columns=SCENARIO_NAMES)
        out.append("```\n" + dd_pivot.round(2).to_string() + "\n```\n")

        out.append("## Total transaction cost ($) by combination × scenario\n")
        tx_pivot = ok.pivot(index="combo", columns="scenario", values="total_tx_cost")
        tx_pivot = tx_pivot.reindex(columns=SCENARIO_NAMES)
        out.append("```\n" + tx_pivot.round(0).astype("int64").to_string() + "\n```\n")

        out.append("## Total spending ($M) by combination × scenario\n")
        sp_pivot = ok.pivot(index="combo", columns="scenario", values="total_spend") / 1e6
        sp_pivot = sp_pivot.reindex(columns=SCENARIO_NAMES)
        out.append("```\n" + sp_pivot.round(2).to_string() + "\n```\n")

        # Cross-rule aggregate summaries
        out.append(
            "## Spending totals by rule (averaged across all engine combos × scenarios) ($M)\n"
        )
        rule_sp = ok.groupby("rule")["total_spend"].mean() / 1e6
        out.append("```\n" + rule_sp.round(2).to_string() + "\n```\n")

        out.append(
            "## Final NAV by allocation engine (averaged across impl × rule × scenario) ($M)\n"
        )
        alloc_nav = ok.groupby("alloc")["final_nav"].mean() / 1e6
        out.append("```\n" + alloc_nav.round(2).to_string() + "\n```\n")

        out.append("## Total tx cost by implementation engine (averaged) ($)\n")
        impl_tx = ok.groupby(["impl", "bps"])["total_tx_cost"].mean()
        out.append("```\n" + impl_tx.round(0).astype("int64").to_string() + "\n```\n")

    return "".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=None, help="optional Markdown output path")
    args = parser.parse_args(argv)

    rows: list[ComboResult] = []
    for alloc in ALLOC_ENGINES:
        for impl, bps in IMPL_VARIANTS:
            for rule in SPENDING_RULES:
                for scenario in SCENARIO_NAMES:
                    try:
                        r = _run_one(_REPO, alloc, impl, bps, rule, scenario)
                    except Exception as exc:
                        r = ComboResult(
                            alloc=alloc,
                            impl=impl,
                            bps=bps,
                            rule=rule,
                            scenario=scenario,
                            status=type(exc).__name__,
                            error=traceback.format_exc().splitlines()[-1][:160],
                        )
                    rows.append(r)
                    print(
                        f"  {r.alloc:9s} / {r.impl:12s} / {int(r.bps):>2d}bp / {r.rule:9s} / "
                        f"{r.scenario:25s} → {r.status}",
                        file=sys.stderr,
                        flush=True,
                    )

    report = _format_report(rows)
    print(report)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
