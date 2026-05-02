"""End-to-end orchestrator tests on fixture scenarios."""

from __future__ import annotations

import time

import pandas as pd
import pytest
from aa_model.integration.orchestrator import run_orchestrator

_ALLOWED_FLOW_TYPES = {
    "inflow",
    "return",
    "pe_call",
    "pe_distribution",
    "pe_nav_mark",
    "spend",
    "rebalance",
}


def test_base_scenario_e2e(base_config_path):
    t0 = time.perf_counter()
    result = run_orchestrator(base_config_path, dry_run=False)
    elapsed = time.perf_counter() - t0
    # SPEC §7: end-to-end test runs in <10 seconds on fixtures.
    assert elapsed < 10.0, f"orchestrator too slow: {elapsed:.2f}s"

    assert (result.output_dir / "ledger.parquet").is_file()
    assert (result.output_dir / "report.md").is_file()
    assert (result.output_dir / "manifest.json").is_file()

    df = result.ledger
    assert len(df) > 0
    assert set(df["flow_type"].unique()) <= _ALLOWED_FLOW_TYPES
    # 20 quarters × 4 buckets × full canonical flow set means many rows.
    assert df["quarter"].nunique() == 20


def test_drawdown_scenario_passes_invariants(with_drawdown_config):
    result = run_orchestrator(with_drawdown_config, dry_run=False)
    df = result.ledger
    # Orchestrator already runs ledger.validate(); arrival here implies pass.
    # Spot-check: q8 (2028Q1) should show a -25% public_equity return.
    eq_q8 = df[
        (df["quarter"].astype(str) == "2028Q1")
        & (df["bucket"] == "public_equity")
        & (df["flow_type"] == "return")
    ]
    assert not eq_q8.empty
    nav_start = float(eq_q8.iloc[0]["nav_start_usd"])
    amt = float(eq_q8.iloc[0]["amount_usd"])
    assert nav_start > 0
    assert abs(amt / nav_start - (-0.25)) < 1e-9


def test_dry_run_writes_no_artifacts(base_config_path):
    result = run_orchestrator(base_config_path, dry_run=True)
    # The output_dir is computed but not necessarily populated this call.
    # We assert no ledger.parquet was written by THIS dry-run; if a prior
    # non-dry run created one earlier in the test suite we leave it alone.
    assert result.manifest.outputs == []


def test_pe_call_and_distribution_have_matching_cash_offsets(base_config_path):
    """Per SPEC §5.1 total NAV conservation, every pe_call / pe_distribution row
    on a non-cash bucket must be paired by an equal-magnitude opposite-sign
    row on the cash bucket carrying the same source. The ledger.validate()
    zero-sum check catches aggregate violations; this test pins the
    *per-source* pairing to catch a swapped-sign or missing-leg bug that
    happens to net to zero across funds.
    """
    result = run_orchestrator(base_config_path, dry_run=False)
    df = result.ledger

    # The cash leg always carries the opposite sign of the PE leg:
    #   pe_call:         pe = +call,  cash = -call
    #   pe_distribution: pe = -dist,  cash = +dist
    for ftype in ("pe_call", "pe_distribution"):
        sub = df[df["flow_type"] == ftype]
        if sub.empty:
            continue
        pe_side = sub[sub["bucket"] != "cash"]
        cash_side = sub[sub["bucket"] == "cash"]
        # 1-to-1 row count: each non-cash leg has exactly one cash counterpart.
        assert len(pe_side) == len(
            cash_side
        ), f"{ftype}: {len(pe_side)} non-cash rows vs {len(cash_side)} cash rows"
        # Pairing key: (quarter, source). Cash row's amount must be the
        # negation of the corresponding non-cash row's amount.
        key = ["quarter", "source"]
        pe_amt = pe_side.groupby(key)["amount_usd"].sum().rename("pe_amt")
        cash_amt = cash_side.groupby(key)["amount_usd"].sum().rename("cash_amt")
        joined = pd.concat([pe_amt, cash_amt], axis=1)
        assert not joined.isna().any().any(), f"{ftype}: orphan rows without pair"
        diff = (joined["cash_amt"] + joined["pe_amt"]).abs()
        assert (
            diff < 1e-9
        ).all(), f"{ftype}: per-source cash offset asymmetry, max |diff|={diff.max()}"


def test_cvxportfolio_engine_preserves_invariants_under_nonzero_bps(
    with_cvxportfolio_config,
):
    """Phase 3b end-to-end: switching to cvxportfolio + 5 bps must not break
    any ledger invariant. Total NAV conservation now includes
    transaction_cost in the contributing-flows set; arrival here means
    QuarterlyLedger.validate() still passed.
    """
    pytest.importorskip("cvxportfolio")
    result = run_orchestrator(with_cvxportfolio_config, dry_run=False)
    df = result.ledger
    # Sanity checks beyond the invariant battery already run by the orchestrator:
    assert "transaction_cost" in df["flow_type"].unique()
    tc = df[df["flow_type"] == "transaction_cost"]
    # Every transaction_cost row should land on cash, be non-positive,
    # and appear at most once per quarter.
    assert (tc["bucket"] == "cash").all()
    assert (tc["amount_usd"] <= 0.0).all()
    assert tc.groupby("quarter").size().max() == 1
    # And totals: cumulative cost should be positive but small (we used 5 bps
    # on rebalance volume against a $100M portfolio with mostly small drift).
    cum_cost = float(-tc["amount_usd"].sum())
    assert 0.0 < cum_cost < 1_000_000.0


def test_crisis_correlation_scenario_end_to_end(base_config_path):
    """Phase 6 / L6: the crisis_correlation scenario shocks the CMA's
    correlation matrix via override; orchestrator must apply the shock
    before fitting the allocator and the report must surface a
    'Correlation shock (scenario)' section with the right diagnostics.
    Ledger invariants must still hold — the shock is upstream of any
    flow logic.
    """
    from aa_model.assumptions.scenario_builder import make_scenarios
    from aa_model.io.loaders import load_study_config

    cfg = load_study_config(base_config_path)
    sc = next(
        s
        for s in make_scenarios(cfg.fixture_scenario, cfg.pe_pacing, cfg.spending)
        if s.name == "crisis_correlation"
    )
    result = run_orchestrator(base_config_path, scenario=sc, dry_run=False)
    text = (result.output_dir / "report.md").read_text(encoding="utf-8")

    assert "## Correlation shock (scenario)" in text
    assert "type: `override`" in text
    assert "pairwise replacements: 2" in text
    assert "max |Δρ| vs baseline" in text
    assert "PSD: pass" in text
    assert "CMA baseline preserved" in text


def test_correlation_shock_changes_run_id_hash(base_config_path):
    """The shock must propagate into config_hash so two runs differing only
    in the shock produce distinct run_ids. This is the architectural
    contract that makes scenario substitution into ``cfg.cma`` (Phase 6
    refactor) the right design.
    """
    from aa_model.assumptions.scenario_builder import make_scenarios
    from aa_model.io.loaders import load_study_config

    cfg = load_study_config(base_config_path)
    scenarios = make_scenarios(cfg.fixture_scenario, cfg.pe_pacing, cfg.spending)
    base_sc = next(s for s in scenarios if s.name == "base")
    crisis_sc = next(s for s in scenarios if s.name == "crisis_correlation")

    rr_base = run_orchestrator(base_config_path, scenario=base_sc, dry_run=True)
    rr_crisis = run_orchestrator(base_config_path, scenario=crisis_sc, dry_run=True)
    assert rr_base.manifest.config_hash != rr_crisis.manifest.config_hash, (
        "shock did not propagate into config_hash; "
        f"base={rr_base.manifest.config_hash}, "
        f"crisis={rr_crisis.manifest.config_hash}"
    )


def test_report_contains_capital_market_assumptions_section(base_config_path):
    """Phase 5: report.md gains a 'Capital market assumptions' section
    rendered from the loaded CMA. Skipped when CMA is absent (test
    paths) — covered here because the default base.yaml ships with a
    CMA reference.
    """
    result = run_orchestrator(base_config_path, dry_run=False)
    text = (result.output_dir / "report.md").read_text(encoding="utf-8")
    assert "## Capital market assumptions" in text
    # Per-bucket header table.
    assert "expected return (annual)" in text
    assert "volatility (annual)" in text
    # Portfolio-level prior block.
    assert "Portfolio priors at policy weights" in text
    assert "expected return (annual):" in text
    assert "expected volatility (annual):" in text
    # Liquidity counts (shipped CMA has liquidity tags).
    assert "Liquidity bucket counts" in text


def test_cvxportfolio_allocation_engine_preserves_invariants_end_to_end(
    with_cvxportfolio_allocation_config,
):
    """Phase 4b end-to-end: allocation.engine = cvxportfolio + non-zero bps
    must not break any ledger invariant. The cost-aware allocator's
    target_at runs once per quarter; the orchestrator must hand it
    pre-rebalance current_dollars and consume the canonicalized target.
    """
    pytest.importorskip("cvxpy")
    result = run_orchestrator(with_cvxportfolio_allocation_config, dry_run=False)
    df = result.ledger
    assert "rebalance" in df["flow_type"].unique()
    assert "transaction_cost" in df["flow_type"].unique()
    # rebalance per quarter still zero-sum (invariant 5.1) — already checked
    # by the orchestrator's validate(); this is a redundant smoke.
    rb = df[df["flow_type"] == "rebalance"]
    for q, sub in rb.groupby("quarter", sort=False):
        assert abs(float(sub["amount_usd"].sum())) < 1e-6


def test_cvxportfolio_allocation_report_contains_calibration_section(
    with_cvxportfolio_allocation_config,
):
    """Cost-aware allocator runs emit a 'Cost-aware allocator calibration
    (advisory)' section in report.md with the rule-of-thumb suggested
    λ_norm vs the configured value. Diagnostic only — no behavior change.
    """
    pytest.importorskip("cvxpy")
    result = run_orchestrator(with_cvxportfolio_allocation_config, dry_run=False)
    report_path = result.output_dir / "report.md"
    assert report_path.exists()
    text = report_path.read_text(encoding="utf-8")
    assert "Cost-aware allocator calibration" in text
    assert "policy_loss_lambda_norm (used)" in text
    assert "suggested_policy_loss_lambda_norm" in text
    assert "ratio used / suggested" in text


def test_owl_spending_rule_preserves_invariants_end_to_end(with_owl_spending_config):
    """Phase 3c end-to-end: switching spending.rule to 'owl' with a guardrail
    block must not break any ledger invariant. spend rows are still the
    canonical external-outflow channel (no schema change needed for Owl).
    """
    result = run_orchestrator(with_owl_spending_config, dry_run=False)
    df = result.ledger
    spend = df[df["flow_type"] == "spend"]
    # Spending rows are present, on cash, all non-positive (outflows).
    assert not spend.empty
    assert (spend["bucket"] == "cash").all()
    assert (spend["amount_usd"] <= 0.0).all()
    # One spend row per quarter.
    assert spend.groupby("quarter").size().max() == 1


def test_owl_cuts_spending_under_realized_drawdown(
    with_owl_on_drawdown_config, with_owl_spending_config
):
    """Phase 4a exit gate (L18 fix). Owl runs against the drawdown fixture
    and reads realized prior-quarter NAV. After the -25% public_equity
    shock at q8 (and 4-quarter recovery), realized NAV is briefly
    depressed; Owl's first guardrail check after the shock (year-3
    boundary at q12) must produce **lower spending** than the same Owl
    run on the base fixture for the same year.

    Rationale: Phase 3c Owl using forecast NAV would not see the shock
    and would raise spending under nominal forecast growth (L18 in
    MODEL_DOCUMENTATION). Phase 4a Owl reads realized NAV and responds
    correctly. We assert the directional outcome — drawdown spending
    is not greater than base spending — rather than a hand-worked
    closed form, because total NAV at q11 depends on bucket-weighted
    returns, PE pacing, and rebalance feedback combined.
    """
    rr_drawdown = run_orchestrator(with_owl_on_drawdown_config, dry_run=False)
    rr_base = run_orchestrator(with_owl_spending_config, dry_run=False)
    df_dd = rr_drawdown.ledger
    df_base = rr_base.ledger
    spend_dd = df_dd[(df_dd["flow_type"] == "spend") & (df_dd["source"] == "spending:owl")]
    spend_base = df_base[(df_base["flow_type"] == "spend") & (df_base["source"] == "spending:owl")]
    # Cumulative Owl spending under drawdown must be ≤ under base.
    cum_dd = float(-spend_dd["amount_usd"].sum())
    cum_base = float(-spend_base["amount_usd"].sum())
    assert cum_dd <= cum_base, (
        f"Owl raised spending under drawdown (cum_dd={cum_dd:,.0f} vs "
        f"cum_base={cum_base:,.0f}) — L18 regression"
    )


def test_owl_does_not_raise_under_inflation_shock_end_to_end(repo_root):
    """Phase 4a exit gate (L18 fix). Under inflation_shock the user-facing
    inflation_pct is 6.0% (vs 2.5% base) — applied via the Scenario
    builder's spending override. With Phase 4a Owl reading realized
    rather than forecast NAV, an inflation-only shock (no return
    perturbation) must produce a guardrail **cut** at the year boundary
    where the inflated rate exceeds the upper band, NOT a raise.
    """
    import yaml
    from aa_model.assumptions.scenario_builder import make_scenarios
    from aa_model.io.loaders import load_study_config

    configs = repo_root / "configs"
    spending_path = configs / "_test_owl_infl.yaml"
    spending_path.write_text(
        yaml.safe_dump(
            {
                "rule": "owl",
                "annual_spend_usd": 4_000_000.0,
                "inflation_pct": 0.025,  # base inflation; scenario lifts to 6%
                "smoothing": {"window_quarters": 12, "weight": 0.0},
                "floor_usd": 0.0,
                "ceiling_usd": 1.0e12,
                "guardrail": {
                    "upper_band_pct": 0.20,
                    "lower_band_pct": 0.20,
                    "raise_pct": 0.10,
                    "cut_pct": 0.10,
                },
            }
        ),
        encoding="utf-8",
    )
    base_cfg = yaml.safe_load((configs / "base.yaml").read_text(encoding="utf-8"))
    base_cfg["spending"]["config"] = "configs/_test_owl_infl.yaml"
    base_path = configs / "_test_owl_infl_base.yaml"
    base_path.write_text(yaml.safe_dump(base_cfg), encoding="utf-8")
    try:
        cfg = load_study_config(base_path)
        scenarios = make_scenarios(cfg.fixture_scenario, cfg.pe_pacing, cfg.spending)
        infl_scenario = next(s for s in scenarios if s.name == "inflation_shock")
        base_scenario = next(s for s in scenarios if s.name == "base")
        rr_infl = run_orchestrator(base_path, scenario=infl_scenario, dry_run=False)
        rr_base = run_orchestrator(base_path, scenario=base_scenario, dry_run=False)
        df_infl = rr_infl.ledger
        df_base = rr_base.ledger

        # Under Phase 4a Owl: at each year boundary the quarterly spending
        # changes by exactly the inflation factor (no raise, no cut). A
        # year-over-year ratio strictly above (1 + inflation_pct) would
        # indicate an erroneous raise; below 1.0 would indicate a cut.
        # Check both the inflation-shock and base scenarios — neither
        # should ratchet beyond pure inflation under the toy fixtures.
        infl_pct = infl_scenario.spending.inflation_pct  # 0.06
        base_pct = (
            base_scenario.spending.inflation_pct
            if base_scenario.spending
            else cfg.spending.inflation_pct
        )  # 0.025

        def _year_quarterlies(df):
            own_df = df[
                (df["flow_type"] == "spend") & (df["source"] == "spending:owl")
            ].sort_values("quarter")
            # Pick q0, q4, q8, q12, q16 representatives (each year's quarterly).
            n_q = cfg.base.horizon.num_quarters
            start = cfg.base.horizon.start_quarter
            import pandas as _pd

            qs = [_pd.Period(start, freq="Q-DEC") + 4 * y for y in range(n_q // 4)]
            return [float(-own_df[own_df["quarter"] == q]["amount_usd"].iloc[0]) for q in qs]

        for label, ys, pct in [
            ("inflation_shock", _year_quarterlies(df_infl), infl_pct),
            ("base", _year_quarterlies(df_base), base_pct),
        ]:
            for n in range(1, len(ys)):
                ratio = ys[n] / ys[n - 1]
                expected_no_trigger = 1.0 + pct
                # Allow a tiny floating-point slack but reject any
                # year-on-year ratio above the inflation factor.
                assert ratio <= expected_no_trigger + 1e-9, (
                    f"{label}: Owl raised at year {n} "
                    f"(ratio={ratio:.6f} > 1 + inflation={expected_no_trigger:.6f}) — "
                    f"L18 regression"
                )
    finally:
        base_path.unlink(missing_ok=True)
        spending_path.unlink(missing_ok=True)


def test_input_hashes_are_deterministic_run_ids_are_unique(base_config_path):
    r1 = run_orchestrator(base_config_path, dry_run=True)
    r2 = run_orchestrator(base_config_path, dry_run=True)
    # Hashes are deterministic in inputs.
    assert r1.manifest.config_hash == r2.manifest.config_hash
    assert r1.manifest.fixtures_hash == r2.manifest.fixtures_hash
    # run_id includes a per-invocation suffix, so two consecutive runs differ.
    assert r1.run_id != r2.run_id
    # Both run_ids share the same hash prefix though.
    cfg = r1.manifest.config_hash.split(":", 1)[-1][:12]
    fix = r1.manifest.fixtures_hash.split(":", 1)[-1][:12]
    prefix = f"aa-{cfg}-{fix}-"
    assert r1.run_id.startswith(prefix)
    assert r2.run_id.startswith(prefix)
