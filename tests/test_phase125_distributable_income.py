"""Phase 12.5 / L19 — distributable_income spending base tests.

13 tests across schema, ledger flow type, base computation, bootstrap
path, zero-income guard, and end-to-end report rendering. See
MODEL_DOCUMENTATION.md §Phase 12.5 design + §Use-case context.

Schema (3):
1. GuardrailConfig.distribution_window_quarters defaults None; accepts
   [1, 20]; rejects 0 and 21+.
2. GuardrailConfig.bootstrap_distributable_income_usd defaults None;
   accepts strictly positive finite; rejects zero, negative, non-finite.
3. StudyConfig / GuardrailConfig cross-validation matrix.

Ledger flow type (2):
4. distribution_inflow row admitted with cash bucket + amount > 0;
   chain extends cash NAV.
5. Negative or zero amount, or non-cash bucket, fails at add-time.

Base computation (4):
6. Default-off byte-stability (existing 252-test baseline still green).
7. distribution_inflow rows summed correctly over trailing window.
8. PE distribution rows excluded (NOT in distribution_inflow rollup).
9. By-source rollup correct (RE / OpCo / portfolio split).

Bootstrap path (2):
10. Run-age < window → bootstrap value used; is_bootstrap=True.
11. Window-completion handoff: realized takes over; is_bootstrap=False.

Zero-income guard (1):
12. Realized zero after bootstrap window elapsed → ValueError.

End-to-end (1):
13. Report renders the third advisory mode with by-source breakdown,
    dual rates, regime classification, recurring-vs-one-time CAVEAT.
"""

from __future__ import annotations

import pandas as pd
import pytest
from aa_model.integration.ledger import FLOW_ORDER, QuarterlyLedger
from aa_model.io.schemas import (
    GuardrailConfig,
    SmoothingConfig,
    SpendingConfig,
)
from aa_model.spending.base import SpendingParams
from aa_model.spending.owl_adapter import OwlRule
from aa_model.spending.spending_base import (
    compute_distributable_income_base,
    compute_spending_base,
)
from pydantic import ValidationError


def _q(s: str) -> pd.Period:
    return pd.Period(s, freq="Q-DEC")


def _spending_cfg(
    *,
    annual_spend_usd: float = 4_000_000.0,
    spending_base: str | None = "distributable_income",
    window_quarters: int | None = 4,
    bootstrap_usd: float | None = 4_000_000.0,
) -> SpendingConfig:
    """Owl spending config with Phase 12.5 distributable_income fields."""
    gr_kwargs: dict = dict(
        upper_band_pct=0.20,
        lower_band_pct=0.20,
        raise_pct=0.10,
        cut_pct=0.10,
        spending_base=spending_base,
    )
    if spending_base == "distributable_income":
        gr_kwargs["distribution_window_quarters"] = window_quarters
        gr_kwargs["bootstrap_distributable_income_usd"] = bootstrap_usd
    return SpendingConfig(
        rule="owl",
        annual_spend_usd=annual_spend_usd,
        inflation_pct=0.025,
        smoothing=SmoothingConfig(window_quarters=4, weight=0.5),
        floor_usd=0.0,
        ceiling_usd=1.0e12,
        guardrail=GuardrailConfig(**gr_kwargs),
    )


# ---- 1-3. Schema-level validation ------------------------------------------


def test_window_quarters_field_validation():
    """Phase 12.5 #1: window in [1, 20]; rejects 0 and 21+."""
    # Valid range.
    for w in (1, 4, 12, 20):
        gr = GuardrailConfig(
            upper_band_pct=0.2,
            lower_band_pct=0.2,
            raise_pct=0.1,
            cut_pct=0.1,
            spending_base="distributable_income",
            distribution_window_quarters=w,
            bootstrap_distributable_income_usd=1_000_000.0,
        )
        assert gr.distribution_window_quarters == w

    # Reject 0.
    with pytest.raises(ValidationError):
        GuardrailConfig(
            upper_band_pct=0.2,
            lower_band_pct=0.2,
            raise_pct=0.1,
            cut_pct=0.1,
            spending_base="distributable_income",
            distribution_window_quarters=0,
            bootstrap_distributable_income_usd=1_000_000.0,
        )
    # Reject > 20.
    with pytest.raises(ValidationError):
        GuardrailConfig(
            upper_band_pct=0.2,
            lower_band_pct=0.2,
            raise_pct=0.1,
            cut_pct=0.1,
            spending_base="distributable_income",
            distribution_window_quarters=21,
            bootstrap_distributable_income_usd=1_000_000.0,
        )


def test_bootstrap_field_validation():
    """Phase 12.5 #2: bootstrap > 0; rejects zero, negative, non-finite."""
    # Valid.
    gr = GuardrailConfig(
        upper_band_pct=0.2,
        lower_band_pct=0.2,
        raise_pct=0.1,
        cut_pct=0.1,
        spending_base="distributable_income",
        distribution_window_quarters=4,
        bootstrap_distributable_income_usd=1_000_000.0,
    )
    assert gr.bootstrap_distributable_income_usd == 1_000_000.0

    # Reject zero.
    with pytest.raises(ValidationError):
        GuardrailConfig(
            upper_band_pct=0.2,
            lower_band_pct=0.2,
            raise_pct=0.1,
            cut_pct=0.1,
            spending_base="distributable_income",
            distribution_window_quarters=4,
            bootstrap_distributable_income_usd=0.0,
        )
    # Reject negative.
    with pytest.raises(ValidationError):
        GuardrailConfig(
            upper_band_pct=0.2,
            lower_band_pct=0.2,
            raise_pct=0.1,
            cut_pct=0.1,
            spending_base="distributable_income",
            distribution_window_quarters=4,
            bootstrap_distributable_income_usd=-100.0,
        )
    # Reject non-finite.
    with pytest.raises(ValidationError):
        GuardrailConfig(
            upper_band_pct=0.2,
            lower_band_pct=0.2,
            raise_pct=0.1,
            cut_pct=0.1,
            spending_base="distributable_income",
            distribution_window_quarters=4,
            bootstrap_distributable_income_usd=float("inf"),
        )


def test_studyconfig_phase125_cross_validation_matrix():
    """Phase 12.5 #3: every cross-validation path."""
    # 3a: distributable_income without window → fail.
    with pytest.raises(ValidationError):
        GuardrailConfig(
            upper_band_pct=0.2,
            lower_band_pct=0.2,
            raise_pct=0.1,
            cut_pct=0.1,
            spending_base="distributable_income",
            bootstrap_distributable_income_usd=1_000_000.0,
        )
    # 3b: distributable_income without bootstrap → fail.
    with pytest.raises(ValidationError):
        GuardrailConfig(
            upper_band_pct=0.2,
            lower_band_pct=0.2,
            raise_pct=0.1,
            cut_pct=0.1,
            spending_base="distributable_income",
            distribution_window_quarters=4,
        )
    # 3c: window present but spending_base != distributable_income → fail.
    with pytest.raises(ValidationError):
        GuardrailConfig(
            upper_band_pct=0.2,
            lower_band_pct=0.2,
            raise_pct=0.1,
            cut_pct=0.1,
            spending_base="liquid_nav",
            distribution_window_quarters=4,
        )
    # 3d: bootstrap present but spending_base != distributable_income → fail.
    with pytest.raises(ValidationError):
        GuardrailConfig(
            upper_band_pct=0.2,
            lower_band_pct=0.2,
            raise_pct=0.1,
            cut_pct=0.1,
            spending_base="liquid_nav",
            bootstrap_distributable_income_usd=1_000_000.0,
        )
    # 3e: both fields with default base → fail.
    with pytest.raises(ValidationError):
        GuardrailConfig(
            upper_band_pct=0.2,
            lower_band_pct=0.2,
            raise_pct=0.1,
            cut_pct=0.1,
            distribution_window_quarters=4,
            bootstrap_distributable_income_usd=1_000_000.0,
        )
    # 3f: positive case validates cleanly.
    gr_ok = GuardrailConfig(
        upper_band_pct=0.2,
        lower_band_pct=0.2,
        raise_pct=0.1,
        cut_pct=0.1,
        spending_base="distributable_income",
        distribution_window_quarters=4,
        bootstrap_distributable_income_usd=1_000_000.0,
    )
    assert gr_ok.spending_base == "distributable_income"


# ---- 4-5. Ledger flow type --------------------------------------------------


def test_distribution_inflow_admits_cash_positive_row():
    """Phase 12.5 #4: distribution_inflow row joins ledger; cash NAV grows."""
    q = _q("2026Q1")
    L = QuarterlyLedger("test", initial_nav={"cash": 100_000.0}, start_quarter=q)
    L.add(
        quarter=q,
        bucket="cash",
        flow_type="distribution_inflow",
        amount_usd=25_000.0,
        source="distribution:real_estate:building_a",
    )
    df = L.finalize()
    di_rows = df[df["flow_type"] == "distribution_inflow"]
    assert len(di_rows) == 1
    assert float(di_rows.iloc[0]["amount_usd"]) == 25_000.0
    # Cash NAV chain: 100,000 + 25,000 = 125,000.
    cash_rows = df[df["bucket"] == "cash"]
    assert float(cash_rows.iloc[-1]["nav_end_usd"]) == 125_000.0
    # And FLOW_ORDER must include the new flow type at the right slot.
    assert "distribution_inflow" in FLOW_ORDER
    assert FLOW_ORDER.index("distribution_inflow") == FLOW_ORDER.index("inflow") + 1


def test_distribution_inflow_structural_constraints():
    """Phase 12.5 #5: amount must be > 0; bucket must be cash."""
    q = _q("2026Q1")
    L = QuarterlyLedger("test", initial_nav={"cash": 100_000.0, "re": 0.0}, start_quarter=q)
    # Zero amount fails.
    with pytest.raises(ValueError, match="amount_usd must be > 0"):
        L.add(
            quarter=q,
            bucket="cash",
            flow_type="distribution_inflow",
            amount_usd=0.0,
            source="distribution:real_estate:zero",
        )
    # Negative amount fails.
    with pytest.raises(ValueError, match="amount_usd must be > 0"):
        L.add(
            quarter=q,
            bucket="cash",
            flow_type="distribution_inflow",
            amount_usd=-100.0,
            source="distribution:real_estate:neg",
        )
    # Non-cash bucket fails.
    with pytest.raises(ValueError, match="must target bucket='cash'"):
        L.add(
            quarter=q,
            bucket="re",
            flow_type="distribution_inflow",
            amount_usd=100.0,
            source="distribution:real_estate:wrong_bucket",
        )


# ---- 6-9. Base computation -------------------------------------------------


def _seed_4q_distributions(
    L: QuarterlyLedger,
    start_q: pd.Period,
    *,
    by_source: dict[str, list[float]],
) -> None:
    """Seed N quarters of distribution_inflow rows. by_source maps source-string to per-quarter dollar amounts."""
    for offset in range(max(len(v) for v in by_source.values())):
        q = start_q + offset
        for source, amounts in by_source.items():
            if offset < len(amounts) and amounts[offset] > 0.0:
                L.add(
                    quarter=q,
                    bucket="cash",
                    flow_type="distribution_inflow",
                    amount_usd=amounts[offset],
                    source=source,
                )


def test_default_off_byte_stable_with_phase125_schema():
    """Phase 12.5 #6: with the new flow type + fields installed, the
    default Phase 12 path remains byte-stable. Compute spending base
    with spending_base=None vs total_nav vs no Phase 12.5 args set —
    all return identical results."""
    nav = pd.Series({"cash": 25e6, "hf": 25e6, "re": 25e6, "land": 25e6}, dtype=float)
    a = compute_spending_base(nav, None, None, None, None)
    b = compute_spending_base(nav, None, None, "total_nav", None)
    assert a.base_usd == b.base_usd == 100e6
    assert a.distributable_income_by_source_usd == {}
    assert a.is_bootstrap is False


def test_distribution_rows_summed_over_trailing_window():
    """Phase 12.5 #7: trailing-4q sum across realized window."""
    start_q = _q("2026Q1")
    L = QuarterlyLedger(
        "t",
        initial_nav={"cash": 1_000_000.0},
        start_quarter=start_q,
    )
    _seed_4q_distributions(
        L,
        start_q,
        by_source={
            "distribution:real_estate:bldg_a": [200_000.0, 200_000.0, 200_000.0, 200_000.0],
            "distribution:opco:liv": [50_000.0, 50_000.0, 50_000.0, 50_000.0],
        },
    )
    L.finalize()
    base, by_source, is_boot = compute_distributable_income_base(
        L,
        prior_quarter=start_q + 3,
        window_quarters=4,
        bootstrap_usd=1.0,
    )
    # 4 quarters × ($200k + $50k) = $1,000,000.
    assert base == pytest.approx(1_000_000.0)
    assert is_boot is False
    assert by_source["distribution:real_estate:bldg_a"] == pytest.approx(800_000.0)
    assert by_source["distribution:opco:liv"] == pytest.approx(200_000.0)


def test_pe_distribution_rows_excluded_from_rollup():
    """Phase 12.5 #8: pe_distribution rows DO NOT count toward trailing income."""
    start_q = _q("2026Q1")
    L = QuarterlyLedger(
        "t",
        initial_nav={"cash": 1_000_000.0, "pe_buyout": 5_000_000.0},
        start_quarter=start_q,
    )
    # Real distribution_inflow rows in 4 quarters.
    _seed_4q_distributions(
        L,
        start_q,
        by_source={"distribution:portfolio:dividends": [50_000.0] * 4},
    )
    # Add pe_distribution rows — these must NOT leak into the trailing
    # distributable_income rollup. pe_call zero-sum requires offsets, so
    # we model a $1M pe_distribution paid into cash from pe_buyout.
    for i in range(4):
        q = start_q + i
        L.add(
            quarter=q,
            bucket="cash",
            flow_type="pe_distribution",
            amount_usd=+1_000_000.0,
            source="pacing:fund_x",
        )
        L.add(
            quarter=q,
            bucket="pe_buyout",
            flow_type="pe_distribution",
            amount_usd=-1_000_000.0,
            source="pacing:fund_x",
        )
    L.finalize()
    base, by_source, _ = compute_distributable_income_base(
        L,
        prior_quarter=start_q + 3,
        window_quarters=4,
        bootstrap_usd=1.0,
    )
    # Only the 4 × $50k = $200k of real distribution_inflow should count.
    assert base == pytest.approx(200_000.0)
    assert "distribution:portfolio:dividends" in by_source
    assert "pacing:fund_x" not in by_source


def test_by_source_rollup_correct():
    """Phase 12.5 #9: per-source breakdown is accurate."""
    start_q = _q("2026Q1")
    L = QuarterlyLedger("t", initial_nav={"cash": 1_000_000.0}, start_quarter=start_q)
    _seed_4q_distributions(
        L,
        start_q,
        by_source={
            "distribution:real_estate:bldg_a": [100_000.0, 100_000.0, 100_000.0, 100_000.0],
            "distribution:real_estate:bldg_b": [50_000.0, 50_000.0, 50_000.0, 50_000.0],
            "distribution:opco:liv": [25_000.0, 25_000.0, 25_000.0, 25_000.0],
            "distribution:portfolio:dividends": [10_000.0, 10_000.0, 10_000.0, 10_000.0],
        },
    )
    L.finalize()
    _, by_source, _ = compute_distributable_income_base(
        L,
        prior_quarter=start_q + 3,
        window_quarters=4,
        bootstrap_usd=1.0,
    )
    assert by_source["distribution:real_estate:bldg_a"] == pytest.approx(400_000.0)
    assert by_source["distribution:real_estate:bldg_b"] == pytest.approx(200_000.0)
    assert by_source["distribution:opco:liv"] == pytest.approx(100_000.0)
    assert by_source["distribution:portfolio:dividends"] == pytest.approx(40_000.0)


# ---- 10-11. Bootstrap path -------------------------------------------------


def test_insufficient_history_uses_bootstrap():
    """Phase 12.5 #10: short run uses bootstrap value; is_bootstrap=True."""
    start_q = _q("2026Q1")
    L = QuarterlyLedger("t", initial_nav={"cash": 1_000_000.0}, start_quarter=start_q)
    # Seed only 2 quarters; window = 4. Realized window extends earlier
    # than start_q → bootstrap.
    _seed_4q_distributions(
        L,
        start_q,
        by_source={"distribution:real_estate:bldg_a": [100_000.0, 100_000.0]},
    )
    L.finalize()
    base, by_source, is_boot = compute_distributable_income_base(
        L,
        prior_quarter=start_q + 1,
        window_quarters=4,
        bootstrap_usd=4_000_000.0,
    )
    assert base == pytest.approx(4_000_000.0)
    assert is_boot is True
    assert by_source == {}  # bootstrap path doesn't expose realized rows


def test_window_completion_handoff_to_realized():
    """Phase 12.5 #11: at window-completion, realized takes over."""
    start_q = _q("2026Q1")
    L = QuarterlyLedger("t", initial_nav={"cash": 1_000_000.0}, start_quarter=start_q)
    _seed_4q_distributions(
        L,
        start_q,
        by_source={"distribution:portfolio:dividends": [200_000.0] * 4},
    )
    L.finalize()
    # prior_quarter = start_q + 3 means realized window covers
    # start_q .. start_q+3 (4 quarters) — window complete.
    base, _, is_boot = compute_distributable_income_base(
        L,
        prior_quarter=start_q + 3,
        window_quarters=4,
        bootstrap_usd=4_000_000.0,
    )
    assert base == pytest.approx(800_000.0)  # 4 × 200k = realized
    assert is_boot is False


# ---- 12. Zero-income guard -------------------------------------------------


def test_owl_raises_on_zero_realized_after_bootstrap_window():
    """Phase 12.5 #12: window complete + realized sum = 0 → ValueError."""
    start_q = _q("2026Q1")
    cfg = _spending_cfg(window_quarters=4, bootstrap_usd=4_000_000.0)
    rule = OwlRule()
    L = QuarterlyLedger("t", initial_nav={"cash": 100_000_000.0}, start_quarter=start_q)
    params = SpendingParams(
        config=cfg,
        start_quarter=start_q,
        num_quarters=8,
    )
    # Drive q0..q3 with NO distribution_inflow rows — bootstrap covers
    # the first year. Then year-boundary at q4: window complete (start_q
    # .. start_q+3), but realized sum = 0 → ValueError.
    for i in range(4):
        q = start_q + i
        quarterly = rule.quarterly_outflow_at(L, params, q)
        L.add(
            quarter=q,
            bucket="cash",
            flow_type="spend",
            amount_usd=-quarterly,
            source=rule.SOURCE_ID,
        )
    with pytest.raises(ValueError, match="realized trailing distributable income"):
        rule.quarterly_outflow_at(L, params, start_q + 4)


# ---- 13. End-to-end report rendering ---------------------------------------


def test_report_renders_distributable_income_advisory(tmp_path, repo_root):
    """Phase 12.5 #13: third advisory render mode includes by-source
    breakdown, dual rates, regime classification, recurring-vs-one-time
    CAVEAT, and producer-dependent paragraph."""
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

    diagnostics = {
        "engine": "OwlRule",
        "min_clamp_activations": 0,
        "max_clamp_activations": 0,
        "spending_base_mode": "distributable_income",
        "spending_base_run_end_usd": 3_500_000.0,
        "spending_base_initial_usd": 4_000_000.0,
        "total_nav_run_end_usd": 100_000_000.0,
        "excluded_nav_by_tier_usd": {},
        "excluded_nav_by_income_flag_usd": {},
        "withdrawal_rate_vs_total_nav": 0.04,
        "withdrawal_rate_vs_spending_base": 1.20,  # 120% — STRONG WARNING
        "material_illiquid_share": 0.0,
        "trailing_distributable_income_usd": 3_500_000.0,
        "distributable_income_by_source_usd": {
            "distribution:real_estate:bldg_a": 2_000_000.0,
            "distribution:opco:liv": 800_000.0,
            "distribution:portfolio:dividends": 700_000.0,
        },
        "used_bootstrap_at_run_end": False,
    }
    L = QuarterlyLedger(
        "t",
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
        run_id="test_phase125",
        config_hash="0" * 12,
        fixtures_hash="0" * 12,
        spending_diagnostics=diagnostics,
    )
    text = out.read_text(encoding="utf-8")
    assert "## Owl spending base (advisory)" in text
    assert "selected base: distributable_income" in text
    assert "trailing distributable income" in text
    assert "rate vs total NAV" in text
    assert "rate vs distributable-income base" in text
    assert "STRONG WARNING" in text  # rate >= 100%
    assert "distribution:real_estate:bldg_a" in text
    assert "distribution:opco:liv" in text
    assert "distribution:portfolio:dividends" in text
    # Reviewer tightening 3 — recurring/one-time CAVEAT is rendered.
    assert "CAVEAT" in text
    assert "Recurring vs" in text or "recurring vs" in text
    assert "asset sales" in text or "one-time" in text
    # Reviewer tightening 4 — producer-dependent framing in the closing
    # paragraph (NOT "fully resolves").
    assert "Production-grade distributable-income realism remains" in text
    assert "Phase 13" in text or "Phase 14" in text
    # Reviewer tightening 1 — explicit non-determination of
    # legal/tax/governance distributability.
    assert "legal" in text or "governance" in text
