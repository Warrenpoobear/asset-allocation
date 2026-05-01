"""End-to-end run pipeline (Phases 1 + 2).

Loads + validates the study config, optionally applies a Phase 2 ``Scenario``
override, builds a quarterly ledger by emitting flows in canonical SPEC §5.1
order, validates every invariant, and writes ``ledger.parquet`` /
``report.md`` / ``manifest.json`` into a per-invocation run directory under
``data/processed/runs/<run_id>/``.

``run_id`` = ``aa-<cfg_hash[:12]>-<fix_hash[:12]>-<UTC_ts>-<nonce>``. The
hash segments are deterministic in the resolved configs (object-based hash,
robust to in-memory scenario overrides); the timestamp + nonce make each
invocation unique so reruns never overwrite a prior dir (SPEC §8).

Scenarios are inputs, not branches — the orchestrator never inspects
``scenario.name``. It just applies overrides via ``cfg.model_copy(update=...)``
and runs the same code path.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

from aa_model.allocation.constraints import Constraints
from aa_model.allocation.factory import make_allocator
from aa_model.assumptions.cma import CMA
from aa_model.implementation.base import CostModel
from aa_model.implementation.factory import make_implementation
from aa_model.integration.ledger import QuarterlyLedger
from aa_model.integration.manifest import Manifest, make_run_id, utcnow_iso
from aa_model.integration.report import write_markdown_report
from aa_model.io.loaders import (
    hash_study_config,
    load_study_config,
    resolve_repo_root,
)
from aa_model.io.schemas import StudyConfig
from aa_model.io.validation import validate_study_config
from aa_model.pe.pacing import project_horizon
from aa_model.spending.base import SpendingParams
from aa_model.spending.rules import make_rule

if TYPE_CHECKING:
    from aa_model.assumptions.scenario_builder import Scenario


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
    scenario: Scenario | None = None,
) -> RunResult:
    base_config_path = Path(base_config_path).resolve()
    repo_root = resolve_repo_root(base_config_path)
    cfg = load_study_config(base_config_path)
    if scenario is not None:
        cfg = _apply_scenario(cfg, scenario)
    validate_study_config(cfg)

    config_hash, fixtures_hash = hash_study_config(cfg)
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
    spend_params = SpendingParams(config=cfg.spending, start_quarter=start_q, num_quarters=n_q)

    alloc = make_allocator(cfg.allocation, engine=cfg.base.allocation.engine)
    alloc.fit(returns=pd.DataFrame(), cma=CMA(), constraints=Constraints())
    target_weights = alloc.weights()
    impl = make_implementation(engine=cfg.base.implementation.engine)
    cost_model = CostModel(bps_per_trade=cfg.base.implementation.bps_per_trade)

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

        # 0. Phase 4a: per-quarter spending decision against the closed
        # ledger view through q-1. Computed before any q rows are emitted
        # so the rule cannot accidentally observe partial current-quarter
        # state. The dollar amount is buffered and emitted in canonical
        # order at step 6 (spend) below.
        spend_amt = float(rule.quarterly_outflow_at(ledger, spend_params, q))

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

        # 6. spend (using the value computed at step 0 from the closed
        # prior-quarter view; emitted with the rule's own source id so
        # path-dependent rules can recover their own history per the
        # source-filter contract).
        if spend_amt != 0.0:
            ledger.add(
                quarter=q,
                bucket="cash",
                flow_type="spend",
                amount_usd=-spend_amt,
                source=rule.SOURCE_ID,
            )
            running_nav["cash"] = running_nav.get("cash", 0.0) - spend_amt

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

        # 8. transaction_cost (Phase 3b). Cost is always emitted on cash;
        # invariants treat it as an external outflow, so the orchestrator's
        # `expected_externals` for this quarter must include it.
        cost_usd = float(result.cost_usd)
        if cost_usd > 0.0:
            ledger.add(
                quarter=q,
                bucket="cash",
                flow_type="transaction_cost",
                amount_usd=-cost_usd,
                source=f"impl:{cfg.base.implementation.engine}",
            )
            running_nav["cash"] = running_nav.get("cash", 0.0) - cost_usd

        expected_externals[q] = ext_inflow_amt - spend_amt - cost_usd

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


def _apply_scenario(cfg: StudyConfig, scenario: Scenario) -> StudyConfig:
    """Apply scenario overrides to a resolved StudyConfig.

    Pure data substitution — no orchestrator branching on scenario identity.
    Any field on ``scenario`` set to None is left at the base value.
    """
    updates: dict = {}
    if scenario.fixture_scenario is not None:
        updates["fixture_scenario"] = scenario.fixture_scenario
    if scenario.pe_pacing is not None:
        updates["pe_pacing"] = scenario.pe_pacing
    if scenario.spending is not None:
        updates["spending"] = scenario.spending
    return cfg.model_copy(update=updates) if updates else cfg
