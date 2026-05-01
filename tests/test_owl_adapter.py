"""Phase 3c → 4a Owl tests under realized-NAV semantics.

Phase 4a replaced the forecast-only NAV approach with realized-NAV reads
from the closed ledger view. The Phase 3c forecast-based tests were
either reframed (constants, q0 init, within-year constancy, scale
invariance) or replaced with synthetic-ledger tests that exercise the
realized-NAV path directly.

The numerical anchor for Phase 4a uses a hand-built ledger with explicit
return rows that depress total NAV at year boundaries to a known value;
Owl's response is computed against that known NAV and compared to a
hand-worked closed form.
"""

from __future__ import annotations

import pandas as pd
import pytest
from aa_model.integration.ledger import QuarterlyLedger
from aa_model.io.schemas import GuardrailConfig, SmoothingConfig, SpendingConfig
from aa_model.spending.base import SpendingParams
from aa_model.spending.owl_adapter import OwlRule
from aa_model.spending.rules import FlatRealRule, SmoothingRule, make_rule


def _ledger(initial_nav_total: float = 100_000_000.0) -> QuarterlyLedger:
    return QuarterlyLedger(
        "test",
        initial_nav={"cash": initial_nav_total},
        start_quarter=pd.Period("2026Q1", freq="Q-DEC"),
    )


def _params(
    *,
    rule: str = "owl",
    annual: float = 4_000_000.0,
    inflation: float = 0.025,
    upper: float = 0.20,
    lower: float = 0.20,
    raise_pct: float = 0.10,
    cut_pct: float = 0.10,
    floor: float = 0.0,
    ceiling: float = 1e12,
    num_quarters: int = 12,
) -> SpendingParams:
    cfg = SpendingConfig(
        rule=rule,
        annual_spend_usd=annual,
        inflation_pct=inflation,
        smoothing=SmoothingConfig(window_quarters=12, weight=0.0),
        floor_usd=floor,
        ceiling_usd=ceiling,
        guardrail=GuardrailConfig(
            upper_band_pct=upper,
            lower_band_pct=lower,
            raise_pct=raise_pct,
            cut_pct=cut_pct,
        )
        if rule == "owl"
        else None,
    )
    return SpendingParams(
        config=cfg,
        start_quarter=pd.Period("2026Q1", freq="Q-DEC"),
        num_quarters=num_quarters,
    )


# ---- numerical anchor (hand-worked Phase 4a trip) ---------------------------


def _seed_year(
    ledger: QuarterlyLedger,
    rule: OwlRule,
    params: SpendingParams,
    *,
    start_quarter: pd.Period,
    quarterly_return_rate: float,
    n_quarters: int,
) -> None:
    """Append per-quarter realized return rows + this rule's own spend rows
    so a downstream year-boundary call sees a known realized-NAV trajectory.
    """
    q = start_quarter
    for _ in range(n_quarters):
        view = ledger.closed_through(q - 1)
        if view.empty:
            nav_start = float(sum(ledger.initial_nav.values()))
        else:
            nav_start = float(view.groupby("bucket").tail(1)["nav_end_usd"].sum())
        ledger.add(
            quarter=q,
            bucket="cash",
            flow_type="return",
            amount_usd=quarterly_return_rate * nav_start,
            source="cma",
        )
        spend_q = rule.quarterly_outflow_at(ledger, params, q)
        ledger.add(
            quarter=q,
            bucket="cash",
            flow_type="spend",
            amount_usd=-spend_q,
            source=rule.SOURCE_ID,
        )
        q = q + 1


def test_numerical_anchor_q4_raise_trigger_realized_nav():
    """Realized-NAV raise anchor (Phase 4a). Inflation 2.5%/yr; returns
    10%/q (extreme but produces a clean trip in 4 quarters). Walk:

        nav_end_q0 = 100M · 1.10 - 1.0M = 109.0M
        nav_end_q1 = 109.0M · 1.10 - 1.0M = 118.9M
        nav_end_q2 = 118.9M · 1.10 - 1.0M = 129.79M
        nav_end_q3 = 129.79M · 1.10 - 1.0M = 141.769M

    At q4 boundary:
        prior_annual  = 1.0M · 4 = 4.0M
        annual_spend  = 4.0M · 1.025 = 4.10M
        rate          = 4.10M / 141.769M = 0.028920
        threshold     = 0.04 · (1 - 0.20) = 0.032
        rate < threshold → raise; annual *= 1.10 = 4.51M; quarterly = $1,127,500.
    """
    p = _params()
    L = _ledger()
    rule = OwlRule()
    _seed_year(
        L,
        rule,
        p,
        start_quarter=pd.Period("2026Q1", freq="Q-DEC"),
        quarterly_return_rate=0.10,
        n_quarters=4,
    )
    out = rule.quarterly_outflow_at(L, p, pd.Period("2027Q1", freq="Q-DEC"))
    assert out == pytest.approx(1_127_500.0, abs=1e-6)


def test_numerical_anchor_q8_cut_trigger_realized_nav():
    """Realized-NAV cut anchor (Phase 4a). Inflation 10%/yr (large to trip
    in 8 quarters); zero returns; spending alone depletes NAV.

        q0..q3:   $1M each.            nav_end_q3 = 100M - 4M = 96M
        q4 boundary:
          prior_annual = 4M; inflated = 4.4M; rate = 4.4 / 96 = 0.04583;
          threshold = 0.04·1.20 = 0.048;  rate < threshold → no trigger.
          quarterly = 4.4M / 4 = 1.1M.
        q4..q7:   $1.1M each.          nav_end_q7 = 96M - 4·1.1M = 91.6M
        q8 boundary:
          prior_annual = 4.4M; inflated = 4.84M; rate = 4.84 / 91.6 = 0.05284;
          rate > threshold → cut; annual = 4.84 · 0.90 = 4.356M;
          quarterly = $1,089,000.
    """
    p = _params(inflation=0.10, num_quarters=12)
    L = _ledger()
    rule = OwlRule()
    _seed_year(
        L,
        rule,
        p,
        start_quarter=pd.Period("2026Q1", freq="Q-DEC"),
        quarterly_return_rate=0.0,
        n_quarters=8,
    )
    out = rule.quarterly_outflow_at(L, p, pd.Period("2028Q1", freq="Q-DEC"))
    assert out == pytest.approx(1_089_000.0, abs=1e-6)


# ---- q0 initialization ------------------------------------------------------


def test_q0_returns_initial_quarterly_with_no_guardrail_check():
    """q0 is initialization, not a guardrail decision. With non-trivial
    initial NAV and tight bands, q0 still returns annual_spend / 4
    regardless of what the rate looks like.
    """
    p = _params(annual=8_000_000.0)  # implies 8% initial rate, way outside bands
    L = _ledger(100_000_000.0)
    out = OwlRule().quarterly_outflow_at(L, p, p.start_quarter)
    assert out == pytest.approx(2_000_000.0, abs=1e-9)


def test_q0_init_independent_of_ledger_state():
    """The q0 path returns annual_spend/4 even if the ledger somehow
    contains rows (it shouldn't in normal flow, but the rule should not
    inspect the ledger at q0).
    """
    p = _params()
    L = _ledger()
    L.add(
        quarter=pd.Period("2025Q4", freq="Q-DEC"),
        bucket="cash",
        flow_type="return",
        amount_usd=999.0,
        source="cma",
    )
    out = OwlRule().quarterly_outflow_at(L, p, p.start_quarter)
    assert out == pytest.approx(1_000_000.0, abs=1e-9)


# ---- structural / boundary --------------------------------------------------


def test_within_year_spending_is_constant_via_wrapper():
    """Within any calendar year (4 quarters from start), Owl spending is
    constant. The wrapper threads its own spend rows so the rule's
    within-year reads find a stable prior.
    """
    p = _params()
    out = OwlRule().quarterly_outflows(_ledger(), p)
    for year in range(3):
        year_slice = out.iloc[year * 4 : (year + 1) * 4]
        assert year_slice.nunique() == 1, f"spending varied within year {year}"


def test_no_nan_no_negative_no_inf():
    p = _params(num_quarters=20)
    out = OwlRule().quarterly_outflows(_ledger(), p)
    import math

    assert not out.isna().any()
    assert (out >= 0.0).all()
    assert all(math.isfinite(v) for v in out.values)


def test_floor_clip_applied():
    p = _params(annual=0.0, floor=500.0, num_quarters=4)
    out = OwlRule().quarterly_outflows(_ledger(), p)
    assert (out == 500.0).all()


def test_ceiling_clip_applied():
    p = _params(annual=4_000_000.0, ceiling=900_000.0, num_quarters=4)
    out = OwlRule().quarterly_outflows(_ledger(), p)
    assert (out == 900_000.0).all()


# ---- determinism + path dependence ------------------------------------------


def test_deterministic_two_runs_match():
    p = _params()
    a = OwlRule().quarterly_outflows(_ledger(), p)
    b = OwlRule().quarterly_outflows(_ledger(), p)
    pd.testing.assert_series_equal(a, b)


def test_path_dependence_via_wrapper_reads_own_prior():
    """The wrapper builds a synthetic working ledger and Owl reads its own
    prior spend row from it. With no realized return rows in the synthetic
    ledger, NAV stays at initial - cumulative spend; bands rarely trip;
    spending tracks pure inflation steps.
    """
    p = _params(num_quarters=12)
    out = OwlRule().quarterly_outflows(_ledger(), p)
    assert out.iloc[0] == pytest.approx(1_000_000.0)
    assert out.iloc[4] == pytest.approx(1_025_000.0)  # year-1 inflation only
    assert out.iloc[8] == pytest.approx(1_050_625.0)  # year-2 inflation only


# ---- comparability ---------------------------------------------------------


def test_owl_with_inactive_bands_matches_flat_real():
    """Bands so wide they can never trigger → Owl reduces to flat_real for
    the wrapper case (same inflation step-up trajectory)."""
    p_owl = _params(upper=10.0, lower=10.0, num_quarters=20)
    p_flat = _params(rule="flat_real", num_quarters=20)
    out_owl = OwlRule().quarterly_outflows(_ledger(), p_owl)
    out_flat = FlatRealRule().quarterly_outflows(_ledger(), p_flat)
    pd.testing.assert_series_equal(out_owl, out_flat)


def test_owl_distinct_from_smoothing_under_realized_drawdown():
    """Build a ledger where realized NAV drops sharply at q4. Owl trips a
    cut at q4; smoothing(w=0.5) doesn't react to NAV at all. Their q4
    outputs differ.
    """
    p_owl = _params()
    L_owl = _ledger()
    rule = OwlRule()
    # Drive a -10%/q realized loss for q0..q3 then emit own spend rows.
    q = pd.Period("2026Q1", freq="Q-DEC")
    for _ in range(4):
        view = L_owl.closed_through(q - 1)
        nav_start = (
            float(sum(L_owl.initial_nav.values()))
            if view.empty
            else float(view.groupby("bucket").tail(1)["nav_end_usd"].sum())
        )
        L_owl.add(
            quarter=q, bucket="cash", flow_type="return", amount_usd=-0.10 * nav_start, source="cma"
        )
        spend_q = rule.quarterly_outflow_at(L_owl, p_owl, q)
        L_owl.add(
            quarter=q, bucket="cash", flow_type="spend", amount_usd=-spend_q, source=rule.SOURCE_ID
        )
        q = q + 1
    owl_q4 = rule.quarterly_outflow_at(L_owl, p_owl, pd.Period("2027Q1", freq="Q-DEC"))

    p_smooth = _params(rule="smoothing")
    smooth = SmoothingRule().quarterly_outflows(_ledger(), p_smooth)
    smooth_q4 = float(smooth.iloc[4])
    assert owl_q4 != pytest.approx(smooth_q4, rel=1e-6)


# ---- source filter ----------------------------------------------------------


def test_owl_does_not_react_to_other_rule_spend_rows():
    """Phase 4 design: a path-dependent SpendingRule may only read prior
    spend rows where source == its own rule source. Inject a spend row
    from a *different* source and verify Owl ignores it (raises rather
    than reading the wrong source).
    """
    p = _params(num_quarters=8)
    L = _ledger()
    rule = OwlRule()
    # q0 init via wrapper-style: append a foreign-source spend row at q0.
    L.add(
        quarter=pd.Period("2026Q1", freq="Q-DEC"),
        bucket="cash",
        flow_type="spend",
        amount_usd=-1_000_000.0,
        source="spending:flat_real",  # NOT spending:owl
    )
    # Owl at q1 must look for its own source; finds none → raises, not
    # silently reads the foreign row.
    with pytest.raises(RuntimeError, match="prior spend row not found"):
        rule.quarterly_outflow_at(L, p, pd.Period("2026Q2", freq="Q-DEC"))


# ---- factory + schema -------------------------------------------------------


def test_make_rule_factory():
    assert isinstance(make_rule("owl"), OwlRule)


def test_owl_without_guardrail_config_raises_at_validation():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SpendingConfig.model_validate(
            {
                "rule": "owl",
                "annual_spend_usd": 4_000_000.0,
                "inflation_pct": 0.025,
                "smoothing": {"window_quarters": 12, "weight": 0.0},
                "floor_usd": 0.0,
                "ceiling_usd": 1e12,
                # guardrail missing
            }
        )


def test_owl_rule_raises_if_initial_nav_zero():
    p = _params()
    bad_ledger = QuarterlyLedger("x", initial_nav={"cash": 0.0}, start_quarter=p.start_quarter)
    # q0 path returns annual/4 without checking NAV (q0 is initialization,
    # not a guardrail decision); but a year-boundary call should fail.
    with pytest.raises(ValueError, match="positive initial NAV"):
        OwlRule().quarterly_outflow_at(bad_ledger, p, pd.Period("2027Q1", freq="Q-DEC"))
