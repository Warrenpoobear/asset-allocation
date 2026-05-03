"""Phase 10 / L14 — Transaction cost summary tests.

Five tests:

1. Section omitted under stub implementation engine (no
   ``transaction_cost`` rows in the ledger).
2. Section renders under cvxportfolio + non-zero bps with all four
   metric lines + an advisory + the load-bearing
   "diagnostic heuristics, not validation failures" note.
3. All-clear advisory at low turnover (default fixture + 5 bps stays
   under both heuristic thresholds).
4. Threshold-trigger anchor: a constructed scenario where
   max-quarterly-liquid-turnover exceeds 25% of NAV produces the
   "may underprice market impact" advisory.
5. No ledger schema change — ``transaction_cost`` row format is
   byte-identical to pre-Phase-10.

See MODEL_DOCUMENTATION.md §Phase 10 design.
"""

from __future__ import annotations

from pathlib import Path

import yaml

# ---- 1. Section omitted under stub -----------------------------------------


def test_section_omitted_under_stub_engine(base_config_path):
    """Default base.yaml ships implementation.engine=stub with bps=0;
    no transaction_cost rows exist; the new section must be omitted.
    """
    from aa_model.integration.orchestrator import run_orchestrator

    result = run_orchestrator(base_config_path, dry_run=False)
    text = (result.output_dir / "report.md").read_text(encoding="utf-8")
    assert "## Transaction cost summary" not in text


# ---- 2. Section renders under cvxportfolio + non-zero bps ------------------


def test_section_renders_under_cvxportfolio_with_bps(repo_root: Path):
    """Switching implementation.engine to cvxportfolio with non-zero
    bps produces the new section with all four metrics, an advisory,
    and the load-bearing diagnostic-heuristics-not-validation-failures
    note.
    """
    from aa_model.integration.orchestrator import run_orchestrator

    configs = repo_root / "configs"
    base = yaml.safe_load((configs / "base.yaml").read_text(encoding="utf-8"))
    base["implementation"] = {"engine": "cvxportfolio", "bps_per_trade": 5.0}
    base_path = configs / "_test_p10_cvx_base.yaml"
    base_path.write_text(yaml.safe_dump(base), encoding="utf-8")
    try:
        result = run_orchestrator(base_path, dry_run=False)
        text = (result.output_dir / "report.md").read_text(encoding="utf-8")

        # Section header + four metric labels + advisory line + the
        # required tightening's verbatim text.
        assert "## Transaction cost summary" in text
        assert "engine: `cvxportfolio` @ 5 bps" in text
        assert "cumulative transaction_cost: $" in text
        assert "as % of initial NAV:" in text
        assert "liquid rebalance turnover" in text
        assert "max single-quarter liquid turnover as % of NAV:" in text
        assert "advisory:" in text
        # The load-bearing tightening — must appear verbatim.
        assert (
            "diagnostic heuristics, not validation failures" in text
        ), "required tightening missing from report"
        assert "Crossing them does not invalidate the run" in text
    finally:
        base_path.unlink(missing_ok=True)


# ---- 3. All-clear advisory at low turnover ---------------------------------


def test_all_clear_advisory_at_low_turnover(repo_root: Path):
    """Default fixture + cvxportfolio + 5 bps stays under both
    heuristic thresholds (cumulative cost ≪ 1% of NAV; max quarterly
    liquid turnover ≪ 25% of NAV) → advisory says
    'covers this regime'.
    """
    from aa_model.integration.orchestrator import run_orchestrator

    configs = repo_root / "configs"
    base = yaml.safe_load((configs / "base.yaml").read_text(encoding="utf-8"))
    base["implementation"] = {"engine": "cvxportfolio", "bps_per_trade": 5.0}
    base_path = configs / "_test_p10_lowturn_base.yaml"
    base_path.write_text(yaml.safe_dump(base), encoding="utf-8")
    try:
        result = run_orchestrator(base_path, dry_run=False)
        text = (result.output_dir / "report.md").read_text(encoding="utf-8")
        assert "covers this regime" in text
        assert "may underprice market impact" not in text
        assert "cost is material" not in text
    finally:
        base_path.unlink(missing_ok=True)


# ---- 4. Threshold trigger anchor: high-turnover advisory -------------------


def test_high_turnover_triggers_market_impact_advisory(repo_root: Path):
    """Construct a config where policy weights diverge sharply from the
    fixture's initial NAV, so the q0 rebalance is forced to move > 25%
    of NAV across liquid sleeves. The advisory must contain the 'may
    underprice market impact' warning.

    Approach: the shipped fixture's nav_initial is roughly
    65/20/15/0 (equity/bond/cash/pe_buyout). Override the public-
    allocation policy to 25/10/40/25 so the q0 rebalance must sell
    ~$32M of equity, ~$7M of bond, and buy ~$39M of cash — well over
    25% of NAV one-side.
    """
    from aa_model.integration.orchestrator import run_orchestrator

    configs = repo_root / "configs"
    public_alloc = yaml.safe_load((configs / "public_allocation.yaml").read_text(encoding="utf-8"))
    # New policy weights: pe_buyout stays 0.25 (sleeve_target_pct
    # constraint); liquid weights shifted to force a large rebalance
    # away from the fixture's equity-heavy initial NAV.
    public_alloc["stub_weights"] = {
        "cash": 0.40,
        "public_bond": 0.10,
        "public_equity": 0.25,
        "pe_buyout": 0.25,
    }
    pa_path = configs / "_test_p10_highturn_public_alloc.yaml"
    pa_path.write_text(yaml.safe_dump(public_alloc), encoding="utf-8")

    base = yaml.safe_load((configs / "base.yaml").read_text(encoding="utf-8"))
    base["implementation"] = {"engine": "cvxportfolio", "bps_per_trade": 5.0}
    base["allocation"] = {
        "engine": "stub",
        "config": "configs/_test_p10_highturn_public_alloc.yaml",
    }
    base_path = configs / "_test_p10_highturn_base.yaml"
    base_path.write_text(yaml.safe_dump(base), encoding="utf-8")
    try:
        result = run_orchestrator(base_path, dry_run=False)
        text = (result.output_dir / "report.md").read_text(encoding="utf-8")
        assert "may underprice market impact" in text, (
            f"high-turnover advisory not triggered; relevant lines:\n"
            f"{[ln for ln in text.split(chr(10)) if 'turnover' in ln or 'advisory' in ln]}"
        )
    finally:
        pa_path.unlink(missing_ok=True)
        base_path.unlink(missing_ok=True)


# ---- 5. No ledger schema change --------------------------------------------


def test_transaction_cost_row_schema_unchanged(repo_root: Path):
    """transaction_cost rows must still match the Phase 3b schema:
    bucket=cash, amount_usd<=0, source=impl:<engine>. Phase 10 adds
    no new columns and changes no existing values.
    """
    from aa_model.integration.orchestrator import run_orchestrator

    configs = repo_root / "configs"
    base = yaml.safe_load((configs / "base.yaml").read_text(encoding="utf-8"))
    base["implementation"] = {"engine": "cvxportfolio", "bps_per_trade": 5.0}
    base_path = configs / "_test_p10_schema_base.yaml"
    base_path.write_text(yaml.safe_dump(base), encoding="utf-8")
    try:
        result = run_orchestrator(base_path, dry_run=True)
        df = result.ledger
        tx = df[df["flow_type"] == "transaction_cost"]
        assert not tx.empty, "no transaction_cost rows under cvxportfolio + 5 bps"
        assert (tx["bucket"] == "cash").all()
        assert (tx["amount_usd"] <= 0.0).all()
        assert (tx["source"] == "impl:cvxportfolio").all()
        # Column set unchanged from §5.1.
        expected_cols = {
            "quarter",
            "bucket",
            "flow_type",
            "amount_usd",
            "nav_start_usd",
            "nav_end_usd",
            "source",
            "run_id",
        }
        assert set(df.columns) == expected_cols, (
            f"ledger columns changed: {set(df.columns) - expected_cols} added; "
            f"{expected_cols - set(df.columns)} removed"
        )
    finally:
        base_path.unlink(missing_ok=True)
