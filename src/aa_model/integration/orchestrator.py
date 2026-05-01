"""End-to-end Phase 1 run pipeline.

Loads + validates the study config, builds a quarterly ledger by emitting
flows in canonical SPEC §5.1 order, validates every invariant, and writes
``ledger.parquet`` / ``report.md`` / ``manifest.json`` into a per-invocation
run directory under ``data/processed/runs/<run_id>/``.

``run_id`` = ``aa-<cfg_hash[:12]>-<fix_hash[:12]>-<UTC_ts>-<nonce>``. The
hash segments are deterministic in the inputs; the timestamp + nonce make
each invocation unique so reruns never overwrite a prior dir (SPEC §8).
Determinism applies to ledger *content*: two runs of the same config produce
parquets that are byte-identical once the ``run_id`` column is dropped.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from aa_model.allocation.constraints import Constraints
from aa_model.allocation.stub import StubAllocator
from aa_model.assumptions.cma import CMA
from aa_model.implementation.base import CostModel
from aa_model.implementation.stub import StubImplementation
from aa_model.integration.ledger import QuarterlyLedger
from aa_model.integration.manifest import Manifest, make_run_id, utcnow_iso
from aa_model.integration.report import write_markdown_report
from aa_model.io.loaders import (
    collect_config_paths,
    collect_fixture_paths,
    hash_files,
    load_study_config,
    resolve_repo_root,
)
from aa_model.io.schemas import StudyConfig
from aa_model.io.validation import validate_study_config
from aa_model.pe.pacing import project_horizon
from aa_model.spending.base import SpendingParams
from aa_model.spending.rules import make_rule


@dataclass(frozen=True)
class RunResult:
    run_id: str
    output_dir: Path
    ledger: pd.DataFrame
    manifest: Manifest


def run_orchestrator(
    base_config_path: Path,
    *,
    dry_run: bool = False,
    invocation_id: str | None = None,
) -> RunResult:
    base_config_path = Path(base_config_path).resolve()
    repo_root = resolve_repo_root(base_config_path)
    cfg = load_study_config(base_config_path)
    validate_study_config(cfg)

    config_hash = hash_files(collect_config_paths(base_config_path))
    fixtures_hash = hash_files(collect_fixture_paths(base_config_path))
    run_id = make_run_id(config_hash, fixtures_hash, invocation_id=invocation_id)

    started_at = utcnow_iso()
    ledger, expected_externals = _build_ledger(cfg, run_id)
    ledger.validate(expected_externals_by_quarter=expected_externals)

    out_dir = repo_root / cfg.base.output.base_dir / run_id
    outputs: list[str] = []

    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
        ledger_path = out_dir / "ledger.parquet"
        _write_ledger_parquet(ledger.finalize(), ledger_path)
        outputs.append("ledger.parquet")

        write_markdown_report(
            out_dir / "report.md",
            cfg=cfg,
            ledger=ledger,
            run_id=run_id,
            config_hash=config_hash,
            fixtures_hash=fixtures_hash,
        )
        outputs.append("report.md")
        outputs.append("manifest.json")

    finished_at = utcnow_iso()
    manifest = Manifest.build(
        run_id=run_id,
        config_hash=config_hash,
        fixtures_hash=fixtures_hash,
        seed=cfg.base.seed,
        started_at=started_at,
        finished_at=finished_at,
        outputs=outputs,
    )
    if not dry_run:
        manifest.write(out_dir / "manifest.json")

    return RunResult(
        run_id=run_id,
        output_dir=out_dir,
        ledger=ledger.finalize(),
        manifest=manifest,
    )


def _build_ledger(cfg: StudyConfig, run_id: str) -> tuple[QuarterlyLedger, dict[pd.Period, float]]:
    start_q = pd.Period(cfg.base.horizon.start_quarter, freq="Q-DEC")
    n_q = cfg.base.horizon.num_quarters
    initial = {b: float(v) for b, v in cfg.fixture_scenario.nav_initial.items()}

    ledger = QuarterlyLedger(run_id, initial_nav=initial, start_quarter=start_q)
    running_nav: dict[str, float] = dict(initial)

    rule = make_rule(cfg.spending.rule)
    spending = rule.quarterly_outflows(
        ledger,
        SpendingParams(config=cfg.spending, start_quarter=start_q, num_quarters=n_q),
    )

    alloc = StubAllocator(cfg.allocation)
    alloc.fit(returns=pd.DataFrame(), cma=CMA(), constraints=Constraints())
    target_weights = alloc.weights()
    impl = StubImplementation()
    cost_model = CostModel(bps_per_trade=0.0)

    rate_table: dict[str, list[float]] = {}
    for bucket, path in cfg.fixture_scenario.returns.items():
        rates = [path.quarterly] * n_q
        for ov in path.overrides:
            if ov.quarter_index < n_q:
                rates[ov.quarter_index] = ov.value
        rate_table[bucket] = rates

    pe_proj = project_horizon(cfg.pe_pacing, start_q, n_q)
    pe_by_q: dict[str, pd.DataFrame] = (
        {str(q): pe_proj[pe_proj["quarter"] == str(q)] for q in (start_q + i for i in range(n_q))}
        if not pe_proj.empty
        else {}
    )

    expected_externals: dict[pd.Period, float] = {}
    ext_inflow_amt = float(cfg.fixture_scenario.external_inflows.default_quarterly_usd)

    for i in range(n_q):
        q = start_q + i

        # 1. inflow
        if ext_inflow_amt != 0.0:
            ledger.add(
                quarter=q,
                bucket="cash",
                flow_type="inflow",
                amount_usd=ext_inflow_amt,
                source="fixture",
            )
            running_nav["cash"] = running_nav.get("cash", 0.0) + ext_inflow_amt

        # 2. returns on liquid (non-PE) buckets, marked on running nav
        for bucket, rates in rate_table.items():
            rate = rates[i]
            nav_start = running_nav.get(bucket, 0.0)
            return_amt = rate * nav_start
            ledger.add(
                quarter=q,
                bucket=bucket,
                flow_type="return",
                amount_usd=return_amt,
                source="cma",
            )
            running_nav[bucket] = nav_start + return_amt

        # 3-5. PE flows for this quarter
        sub = pe_by_q.get(str(q)) if pe_by_q else None
        if sub is not None and not sub.empty:
            for _, r in sub.iterrows():
                sleeve = r["sleeve"]
                src = f"pacing:{r['fund_name']}"
                call = float(r["call_usd"])
                dist = float(r["distribution_usd"])
                mark = float(r["nav_mark_usd"])
                if call != 0.0:
                    ledger.add(
                        quarter=q, bucket=sleeve, flow_type="pe_call", amount_usd=+call, source=src
                    )
                    ledger.add(
                        quarter=q, bucket="cash", flow_type="pe_call", amount_usd=-call, source=src
                    )
                    running_nav[sleeve] = running_nav.get(sleeve, 0.0) + call
                    running_nav["cash"] = running_nav.get("cash", 0.0) - call
                if dist != 0.0:
                    ledger.add(
                        quarter=q,
                        bucket=sleeve,
                        flow_type="pe_distribution",
                        amount_usd=-dist,
                        source=src,
                    )
                    ledger.add(
                        quarter=q,
                        bucket="cash",
                        flow_type="pe_distribution",
                        amount_usd=+dist,
                        source=src,
                    )
                    running_nav[sleeve] = running_nav.get(sleeve, 0.0) - dist
                    running_nav["cash"] = running_nav.get("cash", 0.0) + dist
                if mark != 0.0:
                    ledger.add(
                        quarter=q,
                        bucket=sleeve,
                        flow_type="pe_nav_mark",
                        amount_usd=+mark,
                        source=src,
                    )
                    running_nav[sleeve] = running_nav.get(sleeve, 0.0) + mark

        # 6. spend
        spend_amt = float(spending.iloc[i])
        if spend_amt != 0.0:
            ledger.add(
                quarter=q,
                bucket="cash",
                flow_type="spend",
                amount_usd=-spend_amt,
                source="spending",
            )
            running_nav["cash"] = running_nav.get("cash", 0.0) - spend_amt

        expected_externals[q] = ext_inflow_amt - spend_amt

        # 7. rebalance to target weights
        total_nav = sum(running_nav.values())
        target_nav = (target_weights * total_nav).reindex(running_nav.keys()).fillna(0.0)
        current_nav = pd.Series(running_nav, dtype=float)
        result = impl.rebalance(current_nav, target_nav, cost_model)
        for bucket, trade in result.trades.items():
            t = float(trade)
            if t != 0.0:
                ledger.add(
                    quarter=q,
                    bucket=bucket,
                    flow_type="rebalance",
                    amount_usd=t,
                    source="rebalance",
                )
                running_nav[bucket] = running_nav.get(bucket, 0.0) + t

    return ledger, expected_externals


def _write_ledger_parquet(df: pd.DataFrame, path: Path) -> None:
    """Write the finalized ledger to parquet with deterministic on-disk bytes.

    Period is converted to string; buckets/flow_types/sources stay as object
    columns (string in parquet). pyarrow + uncompressed pages give stable
    bytes across runs in the same environment.
    """
    out = df.copy()
    out["quarter"] = out["quarter"].astype(str)
    out.to_parquet(
        path,
        engine="pyarrow",
        index=False,
        compression=None,
    )
