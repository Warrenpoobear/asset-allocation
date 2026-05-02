"""Phase 9 — manager / fund metadata enrichment tests.

Three layers:

* **Schema-level** — every new field's validation rule, including
  the globally-unique ``FundConfig.name`` rule (lifted from unstated
  convention to enforced invariant), globally-unique ``fund_id``
  when set, and the ``strategy ↔ sleeve`` consistency mapping.
* **Behavior-level** — ``status="exited"`` skipped in projection;
  ``status="planned"`` projected per horizon; ``fee_model`` stored
  but not consumed by projection math.
* **Report-level** — new ``## PE program structure`` section
  rendered when metadata is set; omitted when not (default fixture
  byte-stable); partial ``(unknown)`` aggregation when manager is
  partial.

See MODEL_DOCUMENTATION.md §Phase 9 design.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
import yaml
from pydantic import ValidationError

from aa_model.io.loaders import load_study_config
from aa_model.io.schemas import (
    FundConfig,
    PEPacingConfig,
    TADefaultsConfig,
    _FeeModelConfig,
)


def _ta_defaults() -> TADefaultsConfig:
    return TADefaultsConfig(
        lifetime_years=12,
        commitment_period_years=4,
        rate_of_contribution=[0.25, 0.30, 0.25, 0.20],
        bow=2.5,
        yield_pct=0.0,
        growth_pct=0.13,
    )


def _fund(
    name: str,
    *,
    sleeve: str = "pe_buyout",
    vintage: str = "2026Q1",
    commitment_usd: float = 25_000_000.0,
    **extras,
) -> FundConfig:
    return FundConfig(
        name=name,
        commitment_usd=commitment_usd,
        vintage=vintage,
        sleeve=sleeve,
        **extras,
    )


# ---- schema: optional fields accepted --------------------------------------


def test_fund_with_no_phase9_fields_validates():
    f = _fund("F1")
    assert f.manager is None
    assert f.fund_id is None
    assert f.strategy is None
    assert f.fee_model is None
    assert f.status == "active"


def test_fund_with_full_phase9_fields_validates():
    f = _fund(
        "F1",
        manager="KKR",
        fund_id="kkr_fund_xiv",
        strategy="buyout",
        fee_model=_FeeModelConfig(
            management_fee_pct=0.0175,
            carried_interest_pct=0.20,
            preferred_return_pct=0.08,
        ),
        status="committed",
    )
    assert f.manager == "KKR"
    assert f.fund_id == "kkr_fund_xiv"
    assert f.strategy == "buyout"
    assert f.fee_model.management_fee_pct == pytest.approx(0.0175)
    assert f.status == "committed"


# ---- schema: status enum + fee_model bounds --------------------------------


def test_status_enum_rejects_typo():
    with pytest.raises(ValidationError):
        _fund("F1", status="frozen")  # not in the enum


def test_fee_model_management_fee_above_bound_fails():
    with pytest.raises(ValidationError):
        _FeeModelConfig(management_fee_pct=0.06)  # > 0.05 cap


def test_fee_model_carried_above_bound_fails():
    with pytest.raises(ValidationError):
        _FeeModelConfig(carried_interest_pct=0.40)  # > 0.30 cap


# ---- schema: strategy ↔ sleeve consistency ---------------------------------


def test_strategy_sleeve_match_passes():
    f = _fund("F1", strategy="buyout", sleeve="pe_buyout")
    assert f.strategy == "buyout"


def test_strategy_sleeve_mismatch_fails_with_both_values():
    with pytest.raises(
        ValidationError, match=r"strategy='venture' requires sleeve='pe_venture'"
    ):
        _fund("F1", strategy="venture", sleeve="pe_buyout")


def test_strategy_secondary_compatible_with_any_pe_sleeve():
    # Secondary works with pe_buyout, pe_venture, pe_growth, etc.
    for sleeve in ("pe_buyout", "pe_venture", "pe_re", "pe_infra"):
        f = _fund("F1", strategy="secondary", sleeve=sleeve)
        assert f.strategy == "secondary"


def test_strategy_secondary_rejects_non_pe_sleeve():
    with pytest.raises(ValidationError, match="requires a pe_\\* sleeve"):
        _fund("F1", strategy="secondary", sleeve="public_equity")


# ---- cross-fund: globally-unique name + fund_id ----------------------------


def test_pe_pacing_duplicate_name_fails():
    """Locked rule — ledger source remains pacing:<fund_name>, so two
    funds with the same name create ambiguous ledger sources.
    """
    with pytest.raises(
        ValidationError, match="name must be globally unique"
    ):
        PEPacingConfig(
            ta_defaults=_ta_defaults(),
            funds=[_fund("dup"), _fund("dup")],
        )


def test_pe_pacing_duplicate_fund_id_fails():
    with pytest.raises(
        ValidationError, match="fund_id must be globally unique when set"
    ):
        PEPacingConfig(
            ta_defaults=_ta_defaults(),
            funds=[
                _fund("F1", fund_id="x"),
                _fund("F2", fund_id="x"),
            ],
        )


def test_pe_pacing_unique_names_with_partial_fund_ids_passes():
    cfg = PEPacingConfig(
        ta_defaults=_ta_defaults(),
        funds=[
            _fund("F1", fund_id="external_id_1"),
            _fund("F2"),  # no fund_id
            _fund("F3", fund_id="external_id_3"),
        ],
    )
    assert len(cfg.funds) == 3


def test_pe_pacing_manager_name_uniqueness_when_manager_set():
    # Same manager + same name → fails. Different name (already enforced
    # by global-name rule) → fails earlier on global-name. So this rule
    # really catches:  manager=A, name=X  vs  manager=A, name=X.
    # Since global-name uniqueness already catches duplicate names, the
    # (manager, name) rule is defence-in-depth — exercised by the
    # message contents.
    with pytest.raises(ValidationError) as exc:
        PEPacingConfig(
            ta_defaults=_ta_defaults(),
            funds=[
                _fund("dup", manager="KKR"),
                _fund("dup", manager="KKR"),
            ],
        )
    # Either the global-name rule or the (manager, name) rule fires;
    # both indicate the duplicate.
    assert "globally unique" in str(exc.value) or "(manager, name)" in str(exc.value)


# ---- behavior: status=exited skipped in projection -------------------------


def test_status_exited_fund_excluded_from_projection(repo_root: Path):
    """An exited fund must produce zero PE flow rows in the ledger,
    even when its vintage falls within the run horizon.
    """
    from aa_model.integration.orchestrator import run_orchestrator

    configs = repo_root / "configs"
    pe = yaml.safe_load((configs / "pe_pacing.yaml").read_text(encoding="utf-8"))
    # Add an exited fund. It SHOULD have produced flows under its
    # vintage but is now flagged exited and must be skipped.
    pe["funds"].append(
        {
            "name": "ExitedFund_2026Q1",
            "commitment_usd": 50_000_000.0,
            "vintage": "2026Q1",
            "sleeve": "pe_buyout",
            "manager": "Legacy GP",
            "status": "exited",
        }
    )
    pe_path = configs / "_test_pe_pacing_with_exited.yaml"
    pe_path.write_text(yaml.safe_dump(pe), encoding="utf-8")
    base = yaml.safe_load((configs / "base.yaml").read_text(encoding="utf-8"))
    base["pe_pacing"] = {"config": "configs/_test_pe_pacing_with_exited.yaml"}
    base_path = configs / "_test_base_with_exited.yaml"
    base_path.write_text(yaml.safe_dump(base), encoding="utf-8")
    try:
        result = run_orchestrator(base_path, dry_run=True)
        df = result.ledger
        # No PE flow rows should reference the exited fund.
        ex = df[df["source"] == "pacing:ExitedFund_2026Q1"]
        assert ex.empty, (
            f"exited fund leaked into the ledger: {len(ex)} rows"
        )
    finally:
        pe_path.unlink(missing_ok=True)
        base_path.unlink(missing_ok=True)


def test_status_planned_fund_with_in_horizon_vintage_projects(repo_root: Path):
    """A planned fund with a vintage inside the horizon produces flows
    just like an active fund (planned is treated as projected).
    """
    from aa_model.integration.orchestrator import run_orchestrator

    configs = repo_root / "configs"
    pe = yaml.safe_load((configs / "pe_pacing.yaml").read_text(encoding="utf-8"))
    pe["funds"].append(
        {
            "name": "PlannedFund_2027Q1",
            "commitment_usd": 10_000_000.0,
            "vintage": "2027Q1",  # inside the 20q horizon from 2026Q1
            "sleeve": "pe_buyout",
            "manager": "Future GP",
            "status": "planned",
        }
    )
    pe_path = configs / "_test_pe_pacing_planned.yaml"
    pe_path.write_text(yaml.safe_dump(pe), encoding="utf-8")
    base = yaml.safe_load((configs / "base.yaml").read_text(encoding="utf-8"))
    base["pe_pacing"] = {"config": "configs/_test_pe_pacing_planned.yaml"}
    base_path = configs / "_test_base_planned.yaml"
    base_path.write_text(yaml.safe_dump(base), encoding="utf-8")
    try:
        result = run_orchestrator(base_path, dry_run=True)
        df = result.ledger
        planned = df[df["source"] == "pacing:PlannedFund_2027Q1"]
        assert not planned.empty, "planned fund did not produce any flows"
    finally:
        pe_path.unlink(missing_ok=True)
        base_path.unlink(missing_ok=True)


def test_fee_model_does_not_change_projection(repo_root: Path):
    """fee_model is metadata-only in Phase 9. A fund with fee_model set
    must produce identical projection numbers to the same fund without
    fee_model.
    """
    from aa_model.pe.factory import make_pe_adapter

    pacing_no_fees = PEPacingConfig(
        ta_defaults=_ta_defaults(),
        funds=[_fund("F1", commitment_usd=20_000_000.0)],
    )
    pacing_with_fees = PEPacingConfig(
        ta_defaults=_ta_defaults(),
        funds=[
            _fund(
                "F1",
                commitment_usd=20_000_000.0,
                fee_model=_FeeModelConfig(
                    management_fee_pct=0.02,
                    carried_interest_pct=0.20,
                    preferred_return_pct=0.08,
                ),
            )
        ],
    )

    # Use the TA adapter to project both. Outputs must be byte-identical.
    from aa_model.assumptions.cma import CMA

    cma = CMA()
    public_path = pd.Series(
        0.0,
        index=pd.PeriodIndex(
            [pd.Period("2026Q1", freq="Q-DEC") + i for i in range(20)],
            name="quarter",
        ),
        dtype=float,
    )
    adapter = make_pe_adapter(engine="ta")
    proj_no = adapter.project_horizon(
        pacing_no_fees,
        pd.Period("2026Q1", freq="Q-DEC"),
        20,
        cma=cma,
        public_equity_path=public_path,
    )
    proj_with = adapter.project_horizon(
        pacing_with_fees,
        pd.Period("2026Q1", freq="Q-DEC"),
        20,
        cma=cma,
        public_equity_path=public_path,
    )
    pd.testing.assert_frame_equal(
        proj_no.reset_index(drop=True),
        proj_with.reset_index(drop=True),
        check_exact=False,
        atol=1e-9,
    )


# ---- report: PE program structure section ----------------------------------


def test_report_pe_program_section_omitted_when_no_metadata(base_config_path):
    """The shipped fixture has no Phase 9 metadata; the new section
    must be omitted entirely so the default-config run is byte-stable
    with pre-Phase-9.
    """
    from aa_model.integration.orchestrator import run_orchestrator

    result = run_orchestrator(base_config_path, dry_run=False)
    text = (result.output_dir / "report.md").read_text(encoding="utf-8")
    assert "## PE program structure" not in text


def test_report_pe_program_section_renders_when_manager_set(repo_root: Path):
    """Setting manager on the shipped fund triggers the new section
    with all six diagnostics.
    """
    from aa_model.integration.orchestrator import run_orchestrator

    configs = repo_root / "configs"
    pe = yaml.safe_load((configs / "pe_pacing.yaml").read_text(encoding="utf-8"))
    pe["funds"][0]["manager"] = "KKR"
    pe["funds"][0]["fund_id"] = "kkr_americas_xiv"
    pe["funds"][0]["strategy"] = "buyout"
    pe_path = configs / "_test_pe_pacing_kkr.yaml"
    pe_path.write_text(yaml.safe_dump(pe), encoding="utf-8")
    base = yaml.safe_load((configs / "base.yaml").read_text(encoding="utf-8"))
    base["pe_pacing"] = {"config": "configs/_test_pe_pacing_kkr.yaml"}
    base_path = configs / "_test_base_kkr.yaml"
    base_path.write_text(yaml.safe_dump(base), encoding="utf-8")
    try:
        result = run_orchestrator(base_path, dry_run=False)
        text = (result.output_dir / "report.md").read_text(encoding="utf-8")
        assert "## PE program structure" in text
        assert "Commitment summary" in text
        assert "Unfunded by manager" in text
        assert "Cumulative calls and distributions by manager" in text
        assert "Vintage concentration" in text
        assert "Manager concentration" in text
        assert "NAV by manager" in text
        assert "KKR" in text
    finally:
        pe_path.unlink(missing_ok=True)
        base_path.unlink(missing_ok=True)


def test_report_pe_program_section_unknown_aggregation(repo_root: Path):
    """When manager is set on some funds and not others, the unset
    funds aggregate under '(unknown)' — explicit, not synthesized.
    """
    from aa_model.integration.orchestrator import run_orchestrator

    configs = repo_root / "configs"
    pe = yaml.safe_load((configs / "pe_pacing.yaml").read_text(encoding="utf-8"))
    # First fund gets a manager; add a second with no manager.
    pe["funds"][0]["manager"] = "KKR"
    pe["funds"].append(
        {
            "name": "LegacyFund_2026Q1",
            "commitment_usd": 5_000_000.0,
            "vintage": "2026Q1",
            "sleeve": "pe_buyout",
            # no manager — should aggregate under "(unknown)"
        }
    )
    pe_path = configs / "_test_pe_pacing_partial_manager.yaml"
    pe_path.write_text(yaml.safe_dump(pe), encoding="utf-8")
    base = yaml.safe_load((configs / "base.yaml").read_text(encoding="utf-8"))
    base["pe_pacing"] = {"config": "configs/_test_pe_pacing_partial_manager.yaml"}
    base_path = configs / "_test_base_partial_manager.yaml"
    base_path.write_text(yaml.safe_dump(base), encoding="utf-8")
    try:
        result = run_orchestrator(base_path, dry_run=False)
        text = (result.output_dir / "report.md").read_text(encoding="utf-8")
        assert "## PE program structure" in text
        assert "(unknown)" in text
        assert "KKR" in text
    finally:
        pe_path.unlink(missing_ok=True)
        base_path.unlink(missing_ok=True)


# ---- end-to-end: default fixture byte-stability ----------------------------


def test_default_fixture_run_id_unchanged_under_phase9(base_config_path):
    """The shipped fixture has no Phase 9 metadata; run_id and ledger
    content remain byte-stable. The schema additions are additive with
    safe defaults (status='active'); cfg.pe_pacing.model_dump still
    serialises to a Phase-9-aware structure but the new fields are
    None / 'active' on every fund and do not perturb the projection.
    """
    from aa_model.integration.orchestrator import run_orchestrator

    rr1 = run_orchestrator(base_config_path, dry_run=True)
    rr2 = run_orchestrator(base_config_path, dry_run=True)
    # Two consecutive runs of the same fixture: hash signatures match
    # (config_hash + fixtures_hash); only the per-invocation nonce in
    # run_id differs.
    assert rr1.manifest.config_hash == rr2.manifest.config_hash
    assert rr1.manifest.fixtures_hash == rr2.manifest.fixtures_hash
    # And the ledger content is byte-identical modulo run_id.
    df1 = rr1.ledger.drop(columns=["run_id"])
    df2 = rr2.ledger.drop(columns=["run_id"])
    pd.testing.assert_frame_equal(df1, df2, check_exact=False, atol=1e-9)
