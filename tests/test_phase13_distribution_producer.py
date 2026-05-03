"""Phase 13 / L19 — RE / OpCo distribution_inflow producer tests.

14 tests across schema, producer behavior (incl. duplicate-source
allowed per reviewer tightening 2), orchestrator integration, and
end-to-end report rendering. See MODEL_DOCUMENTATION.md
§Phase 13 design.

Schema (4):
1. Valid full-fields entry; rejects amount<=0 / non-finite / unparseable quarter.
2. URL-safety on producer_id / entity_id / asset_id (no colons).
3. producer_id globally unique across entries.
4. Domain × recurrence sanity (development/recurring + land/recurring → fail).

Producer behavior (5):
5. emit_for_quarter returns matching entries with canonical source format.
6. restricted=True filters at emit; surfaced in diagnostics.
7. asset_id-vs-entity_id precedence in source string.
8. Per-quarter purity (idempotent across calls; no module state).
9. Duplicate (source, quarter) allowed when producer_id is unique
   (reviewer tightening 2).

Orchestrator integration (3):
10. make_distribution_producer factory; rejects unknown engines.
11. End-to-end Owl + distributable_income with producer feed; runtime
    zero-income guard does NOT fire.
12. Default-off byte-stability — cfg.distribution_producer = None
    leaves Phase 12.5 trajectories byte-identical.

End-to-end (2):
13. Producer-diagnostic report renders with all sub-sections + warning
    bands.
14. Composes with Phase 12.5 advisory — both sections render together.
"""

from __future__ import annotations

import pandas as pd
import pytest
from aa_model.integration.ledger import QuarterlyLedger
from aa_model.io.schemas import (
    DistributionEntryConfig,
    DistributionProducerConfig,
    GuardrailConfig,
    SmoothingConfig,
    SpendingConfig,
)
from aa_model.producers.distribution import (
    ConfigDrivenProducer,
    DistributionProducerDiagnostics,
    make_distribution_producer,
)
from pydantic import ValidationError


def _q(s: str) -> pd.Period:
    return pd.Period(s, freq="Q-DEC")


def _entry(
    *,
    producer_id: str,
    domain: str = "real_estate",
    entity_id: str = "bldg_a",
    asset_id: str | None = None,
    quarter: str = "2026Q1",
    amount_usd: float = 100_000.0,
    recurrence_type: str = "recurring",
    confidence: str = "contractual",
    restricted: bool = False,
) -> DistributionEntryConfig:
    return DistributionEntryConfig(
        producer_id=producer_id,
        domain=domain,
        entity_id=entity_id,
        asset_id=asset_id,
        quarter=quarter,
        amount_usd=amount_usd,
        recurrence_type=recurrence_type,
        confidence=confidence,
        restricted=restricted,
    )


# ---- 1-4. Schema-level validation ------------------------------------------


def test_entry_field_validation():
    """Phase 13 #1: amount, finite-ness, quarter parse."""
    # Valid entry roundtrips.
    entry = _entry(producer_id="p1")
    assert entry.amount_usd == 100_000.0

    # amount_usd <= 0 fails.
    with pytest.raises(ValidationError):
        _entry(producer_id="p2", amount_usd=0.0)
    with pytest.raises(ValidationError):
        _entry(producer_id="p3", amount_usd=-1.0)

    # Non-finite amount fails.
    with pytest.raises(ValidationError):
        _entry(producer_id="p4", amount_usd=float("inf"))

    # Quarter must match YYYY[Q1-4] pattern.
    with pytest.raises(ValidationError):
        _entry(producer_id="p5", quarter="2026-Q1")
    with pytest.raises(ValidationError):
        _entry(producer_id="p6", quarter="not-a-quarter")


def test_id_url_safety_no_colons():
    """Phase 13 #2: producer_id, entity_id, asset_id reject colons."""
    with pytest.raises(ValidationError, match="colons are reserved"):
        _entry(producer_id="bad:id")
    with pytest.raises(ValidationError, match="colons are reserved"):
        _entry(producer_id="p1", entity_id="bad:entity")
    with pytest.raises(ValidationError, match="colons"):
        _entry(producer_id="p1", asset_id="bad:asset")


def test_producer_id_globally_unique():
    """Phase 13 #3: duplicate producer_id across entries → fail."""
    with pytest.raises(ValidationError, match="producer_id must be globally unique"):
        DistributionProducerConfig(
            entries=[
                _entry(producer_id="dup"),
                _entry(producer_id="dup", quarter="2026Q2"),
            ]
        )

    # Distinct producer_ids validate even with same source.
    cfg = DistributionProducerConfig(
        entries=[
            _entry(producer_id="p1"),
            _entry(producer_id="p2"),
        ]
    )
    assert len(cfg.entries) == 2


def test_domain_recurrence_sanity():
    """Phase 13 #4: development/recurring + land/recurring → fail."""
    with pytest.raises(ValidationError, match="cannot have recurrence_type='recurring'"):
        _entry(producer_id="p1", domain="development", recurrence_type="recurring")
    with pytest.raises(ValidationError, match="cannot have recurrence_type='recurring'"):
        _entry(producer_id="p2", domain="land", recurrence_type="recurring")

    # All other domain × recurrence combinations validate.
    for domain in ("real_estate", "opco", "portfolio", "entity"):
        for rt in ("recurring", "one_time"):
            entry = _entry(producer_id=f"p_{domain}_{rt}", domain=domain, recurrence_type=rt)
            assert entry.domain == domain
            assert entry.recurrence_type == rt
    # development / land + one_time validate.
    for domain in ("development", "land"):
        entry = _entry(
            producer_id=f"p_{domain}_one_time",
            domain=domain,
            recurrence_type="one_time",
        )
        assert entry.domain == domain


# ---- 5-9. Producer behavior -------------------------------------------------


def test_emit_for_quarter_canonical_source_format():
    """Phase 13 #5: source format is exactly distribution:<domain>:<id>."""
    cfg = DistributionProducerConfig(
        entries=[
            _entry(producer_id="p1", domain="real_estate", entity_id="bldg_a", quarter="2026Q1"),
            _entry(producer_id="p2", domain="opco", entity_id="liv_holding", quarter="2026Q1"),
            _entry(producer_id="p3", domain="portfolio", entity_id="dividends", quarter="2026Q2"),
        ]
    )
    p = ConfigDrivenProducer(cfg)
    emissions, _ = p.emit_for_quarter(_q("2026Q1"))
    sources = sorted(e.source for e in emissions)
    assert sources == [
        "distribution:opco:liv_holding",
        "distribution:real_estate:bldg_a",
    ]


def test_restricted_filters_at_emit():
    """Phase 13 #6: restricted=True excluded; counted in diagnostics."""
    cfg = DistributionProducerConfig(
        entries=[
            _entry(producer_id="p1", amount_usd=100_000.0),
            _entry(producer_id="p2", amount_usd=200_000.0, restricted=True),
            _entry(producer_id="p3", amount_usd=50_000.0),
        ]
    )
    p = ConfigDrivenProducer(cfg)
    emissions, delta = p.emit_for_quarter(_q("2026Q1"))
    assert len(emissions) == 2
    emitted_ids = {e.producer_id for e in emissions}
    assert "p2" not in emitted_ids
    assert delta.excluded_restricted_count == 1
    assert delta.excluded_restricted_usd == 200_000.0


def test_asset_id_vs_entity_id_precedence_in_source():
    """Phase 13 #7: asset_id wins when set; falls back to entity_id."""
    cfg = DistributionProducerConfig(
        entries=[
            _entry(producer_id="p1", entity_id="bldg_complex", asset_id="bldg_a"),
            _entry(producer_id="p2", entity_id="bldg_complex", asset_id=None),
        ]
    )
    p = ConfigDrivenProducer(cfg)
    emissions, _ = p.emit_for_quarter(_q("2026Q1"))
    sources = sorted(e.source for e in emissions)
    assert sources == [
        "distribution:real_estate:bldg_a",  # uses asset_id
        "distribution:real_estate:bldg_complex",  # falls back to entity_id
    ]


def test_emit_for_quarter_purity():
    """Phase 13 #8: emit_for_quarter is idempotent; no module state."""
    cfg = DistributionProducerConfig(
        entries=[
            _entry(producer_id="p1"),
            _entry(producer_id="p2", restricted=True),
        ]
    )
    p = ConfigDrivenProducer(cfg)
    a, da = p.emit_for_quarter(_q("2026Q1"))
    b, db = p.emit_for_quarter(_q("2026Q1"))
    assert [e.producer_id for e in a] == [e.producer_id for e in b]
    assert da.excluded_restricted_count == db.excluded_restricted_count
    assert da.emitted_by_source_usd == db.emitted_by_source_usd


def test_duplicate_source_quarter_allowed_when_producer_id_unique():
    """Phase 13 #9 (reviewer tightening 2): two entries with the same
    (domain, entity_id, asset_id, quarter) but distinct producer_id
    both emit. Sums add into the by-source rollup."""
    cfg = DistributionProducerConfig(
        entries=[
            # Same building, same quarter, same domain — recurring rent
            # PLUS one-time refi proceeds. Different producer_ids.
            _entry(
                producer_id="bldg_a_recurring_2026Q1",
                domain="real_estate",
                entity_id="bldg_complex",
                asset_id="bldg_a",
                amount_usd=200_000.0,
                recurrence_type="recurring",
            ),
            _entry(
                producer_id="bldg_a_refi_2026Q1",
                domain="real_estate",
                entity_id="bldg_complex",
                asset_id="bldg_a",
                amount_usd=1_500_000.0,
                recurrence_type="one_time",
            ),
        ]
    )
    p = ConfigDrivenProducer(cfg)
    emissions, delta = p.emit_for_quarter(_q("2026Q1"))
    # Both emit.
    assert len(emissions) == 2
    # Same source string on both.
    assert all(e.source == "distribution:real_estate:bldg_a" for e in emissions)
    # producer_id distinguishes the audit trail.
    assert {e.producer_id for e in emissions} == {
        "bldg_a_recurring_2026Q1",
        "bldg_a_refi_2026Q1",
    }
    # By-source rollup sums them.
    assert delta.emitted_by_source_usd["distribution:real_estate:bldg_a"] == pytest.approx(
        1_700_000.0
    )
    # By-recurrence breakdown captures both.
    assert delta.emitted_by_recurrence_usd["recurring"] == pytest.approx(200_000.0)
    assert delta.emitted_by_recurrence_usd["one_time"] == pytest.approx(1_500_000.0)


# ---- 10-12. Orchestrator integration ---------------------------------------


def test_factory_engine_dispatch():
    """Phase 13 #10: factory returns ConfigDrivenProducer for engine='config'.
    Phase 14 added engine='workbook' (returns WorkbookDrivenProducer).
    Unknown engines still fail loud."""
    cfg = DistributionProducerConfig(entries=[_entry(producer_id="p1")])
    p = make_distribution_producer(cfg, engine="config")
    assert isinstance(p, ConfigDrivenProducer)

    # Unknown engine fails loud.
    with pytest.raises(ValueError, match="unknown distribution producer engine"):
        make_distribution_producer(cfg, engine="not_a_real_engine")


def test_owl_distributable_income_runs_with_producer_feed():
    """Phase 13 #11: Owl + distributable_income consumes producer
    emissions; runtime zero-income guard does NOT fire after the
    bootstrap window elapses."""
    from aa_model.spending.base import SpendingParams
    from aa_model.spending.owl_adapter import OwlRule

    start_q = _q("2026Q1")
    spend_cfg = SpendingConfig(
        rule="owl",
        annual_spend_usd=4_000_000.0,
        inflation_pct=0.025,
        smoothing=SmoothingConfig(window_quarters=4, weight=0.5),
        floor_usd=0.0,
        ceiling_usd=1.0e12,
        guardrail=GuardrailConfig(
            upper_band_pct=0.20,
            lower_band_pct=0.20,
            raise_pct=0.10,
            cut_pct=0.10,
            spending_base="distributable_income",
            distribution_window_quarters=4,
            bootstrap_distributable_income_usd=4_000_000.0,
        ),
    )

    # Producer config with $1M / quarter on real_estate for 8 quarters.
    entries = []
    for i in range(8):
        q_str = str(start_q + i)
        entries.append(
            _entry(
                producer_id=f"bldg_a_{q_str}",
                domain="real_estate",
                entity_id="bldg_a",
                quarter=q_str,
                amount_usd=1_000_000.0,
            )
        )
    prod_cfg = DistributionProducerConfig(entries=entries)
    producer = ConfigDrivenProducer(prod_cfg)
    diag_acc = DistributionProducerDiagnostics()

    rule = OwlRule()
    L = QuarterlyLedger("t", initial_nav={"cash": 100_000_000.0}, start_quarter=start_q)
    params = SpendingParams(
        config=spend_cfg,
        start_quarter=start_q,
        num_quarters=8,
    )
    # Simulate the orchestrator's per-quarter loop.
    for i in range(8):
        q = start_q + i
        # Spending decision against closed_through(q-1) — Phase 4a contract.
        spend_amt = rule.quarterly_outflow_at(L, params, q)
        # Producer emits q rows.
        emissions, delta = producer.emit_for_quarter(q)
        for em in emissions:
            L.add(
                quarter=q,
                bucket="cash",
                flow_type="distribution_inflow",
                amount_usd=em.amount_usd,
                source=em.source,
            )
        diag_acc.merge(delta)
        # Spend row.
        L.add(
            quarter=q,
            bucket="cash",
            flow_type="spend",
            amount_usd=-spend_amt,
            source=rule.SOURCE_ID,
        )

    # Realized window at q4 = q0..q3 sum = 4 * $1M = $4M. Bootstrap was
    # also $4M, so transition is smooth. No zero-income guard fires.
    diags = rule.diagnostics()
    assert diags["spending_base_mode"] == "distributable_income"
    assert diags["used_bootstrap_at_run_end"] is False
    assert diags["trailing_distributable_income_usd"] == pytest.approx(4_000_000.0)
    # Diagnostics accumulator captured all 8 quarters' emissions.
    assert diag_acc.total_emitted_usd == pytest.approx(8_000_000.0)
    assert "real_estate" in diag_acc.emitted_by_domain_usd


def test_default_off_byte_stability_with_producer_field(repo_root):
    """Phase 13 #12: cfg.distribution_producer = None ⇒ no producer
    rows ⇒ Phase 12.5 trajectories byte-identical."""
    from aa_model.io.loaders import load_study_config

    cfg = load_study_config(repo_root / "configs" / "base.yaml")
    # The base study loads with distribution_producer = None by default
    # (the field is optional on StudyConfig). Validate.
    assert cfg.distribution_producer is None


# ---- 13-14. End-to-end report rendering ------------------------------------


def test_report_renders_distribution_producer_advisory(tmp_path, repo_root):
    """Phase 13 #13: producer-diagnostic section renders with all
    sub-sections + warning bands fire correctly for high
    concentration + forecast-heavy + restricted entries."""
    from aa_model.integration.ledger import QuarterlyLedger
    from aa_model.integration.report import write_markdown_report
    from aa_model.io.loaders import load_study_config

    cfg = load_study_config(repo_root / "configs" / "base.yaml")
    # Build a producer-diagnostics dataclass that triggers every band:
    # one-time share >= 30%, top-3 concentration >= 80%, forecast +
    # scenario >= 20%, restricted entries present.
    diag = DistributionProducerDiagnostics(
        emitted_by_domain_usd={
            "real_estate": 4_000_000.0,
            "opco": 1_000_000.0,
        },
        emitted_by_source_usd={
            "distribution:real_estate:bldg_a": 3_500_000.0,
            "distribution:real_estate:bldg_b": 500_000.0,
            "distribution:opco:liv_holding": 1_000_000.0,
        },
        emitted_by_recurrence_usd={
            "recurring": 3_000_000.0,
            "one_time": 2_000_000.0,  # 40% — warning band
        },
        emitted_by_confidence_usd={
            "contractual": 3_500_000.0,
            "forecast": 1_000_000.0,  # 20%
            "scenario": 500_000.0,  # 10%  → forecast+scenario = 30%
        },
        excluded_restricted_count=2,
        excluded_restricted_usd=350_000.0,
    )
    L = QuarterlyLedger(
        "test_p13",
        initial_nav={
            b: 25_000_000.0 for b in ("cash", "public_bond", "public_equity", "pe_buyout")
        },
        start_quarter=_q("2026Q1"),
    )
    L.finalize()
    out = tmp_path / "report.md"
    write_markdown_report(
        out,
        cfg=cfg,
        ledger=L,
        run_id="test_phase13",
        config_hash="0" * 12,
        fixtures_hash="0" * 12,
        distribution_producer_diagnostics=diag,
    )
    text = out.read_text(encoding="utf-8")
    assert "## Distribution producer (advisory)" in text
    assert "emissions by domain" in text
    assert "real_estate: $4,000,000" in text
    assert "opco: $1,000,000" in text
    assert "emissions by recurrence type" in text
    assert "recurring:" in text
    assert "one_time:" in text
    assert "emissions by confidence" in text
    assert "contractual:" in text
    assert "forecast:" in text
    assert "scenario:" in text
    assert "top-3 sources" in text
    assert "distribution:real_estate:bldg_a" in text
    assert "excluded (restricted=True)" in text
    assert "count: 2 entries" in text
    # Warning bands fire.
    assert "WARNING" in text
    assert "one-time share" in text  # one_time = 40% >= 30%
    assert "top-3 sources account for" in text  # top-3 concentration >= 80%
    assert "forecast or scenario" in text  # forecast+scenario = 30% >= 20%
    # Closing paragraph carries Phase 13 reviewer-tightening framing.
    assert "Workbook-driven ingestion" in text
    assert "Phase 14" in text
    assert "inter-entity cash-movement" in text


def test_report_composes_phase13_with_phase125_advisory(tmp_path, repo_root):
    """Phase 13 #14: when Owl is on distributable_income AND a producer
    is configured, the report renders BOTH the Phase 12.5 spending-base
    advisory AND the Phase 13 producer advisory."""
    from aa_model.integration.report import write_markdown_report
    from aa_model.io.loaders import load_study_config

    cfg = load_study_config(repo_root / "configs" / "base.yaml")
    new_spending = SpendingConfig(
        rule="owl",
        annual_spend_usd=4_000_000.0,
        inflation_pct=0.025,
        smoothing=SmoothingConfig(window_quarters=4, weight=0.5),
        floor_usd=0.0,
        ceiling_usd=1.0e12,
        guardrail=GuardrailConfig(
            upper_band_pct=0.20,
            lower_band_pct=0.20,
            raise_pct=0.10,
            cut_pct=0.10,
            spending_base="distributable_income",
            distribution_window_quarters=4,
            bootstrap_distributable_income_usd=4_000_000.0,
        ),
    )
    cfg = cfg.model_copy(update={"spending": new_spending})

    spending_diag = {
        "engine": "OwlRule",
        "min_clamp_activations": 0,
        "max_clamp_activations": 0,
        "spending_base_mode": "distributable_income",
        "spending_base_run_end_usd": 4_000_000.0,
        "spending_base_initial_usd": 4_000_000.0,
        "total_nav_run_end_usd": 100_000_000.0,
        "excluded_nav_by_tier_usd": {},
        "excluded_nav_by_income_flag_usd": {},
        "withdrawal_rate_vs_total_nav": 0.04,
        "withdrawal_rate_vs_spending_base": 1.00,
        "material_illiquid_share": 0.0,
        "trailing_distributable_income_usd": 4_000_000.0,
        "distributable_income_by_source_usd": {
            "distribution:real_estate:bldg_a": 3_000_000.0,
            "distribution:opco:liv_holding": 1_000_000.0,
        },
        "used_bootstrap_at_run_end": False,
    }
    producer_diag = DistributionProducerDiagnostics(
        emitted_by_domain_usd={
            "real_estate": 12_000_000.0,
            "opco": 4_000_000.0,
        },
        emitted_by_source_usd={
            "distribution:real_estate:bldg_a": 12_000_000.0,
            "distribution:opco:liv_holding": 4_000_000.0,
        },
        emitted_by_recurrence_usd={"recurring": 16_000_000.0},
        emitted_by_confidence_usd={"contractual": 16_000_000.0},
    )
    L = QuarterlyLedger(
        "test_p13_compose",
        initial_nav={
            b: 25_000_000.0 for b in ("cash", "public_bond", "public_equity", "pe_buyout")
        },
        start_quarter=_q("2026Q1"),
    )
    L.finalize()
    out = tmp_path / "report.md"
    write_markdown_report(
        out,
        cfg=cfg,
        ledger=L,
        run_id="test_phase13_compose",
        config_hash="0" * 12,
        fixtures_hash="0" * 12,
        spending_diagnostics=spending_diag,
        distribution_producer_diagnostics=producer_diag,
    )
    text = out.read_text(encoding="utf-8")
    # Both sections rendered.
    assert "## Owl spending base (advisory)" in text
    assert "## Distribution producer (advisory)" in text
    # Consumer side reads sources.
    assert "distribution:real_estate:bldg_a" in text
    # Producer side reads same sources.
    assert "real_estate: $12,000,000" in text
    assert "opco: $4,000,000" in text
