"""TA model golden-CSV regression test (SPEC §6 Phase 1, §7).

Asserts byte-equality between a freshly generated DataFrame and the
committed ``tests/golden/ta_single_fund.csv``. Drift in the projection
math, rounding, or output format flips this test.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from aa_model.io.schemas import FundConfig, TADefaultsConfig
from aa_model.pe.ta_model import project_fund

GOLDEN_PATH = Path(__file__).parent / "golden" / "ta_single_fund.csv"


def _golden_inputs() -> tuple[FundConfig, TADefaultsConfig]:
    fund = FundConfig(
        name="GoldenFund_2024Q1",
        commitment_usd=100_000_000.0,
        vintage="2024Q1",
        sleeve="pe_buyout",
    )
    defaults = TADefaultsConfig(
        lifetime_years=12,
        commitment_period_years=4,
        rate_of_contribution=[0.25, 0.30, 0.25, 0.20],
        bow=2.5,
        yield_pct=0.0,
        growth_pct=0.13,
    )
    return fund, defaults


def test_golden_byte_equality(tmp_path: Path) -> None:
    fund, defaults = _golden_inputs()
    df = project_fund(fund, defaults)
    out = tmp_path / "ta_single_fund.csv"
    df.to_csv(out, index=False, float_format="%.10f", lineterminator="\n")
    assert out.read_bytes() == GOLDEN_PATH.read_bytes()


def test_projection_length_matches_lifetime() -> None:
    fund, defaults = _golden_inputs()
    df = project_fund(fund, defaults)
    assert len(df) == 4 * defaults.lifetime_years


def test_calls_sum_to_commitment() -> None:
    fund, defaults = _golden_inputs()
    df = project_fund(fund, defaults)
    total_called = df["call_usd"].sum()
    assert pytest.approx(total_called, rel=1e-12) == fund.commitment_usd


def test_final_quarter_fully_liquidates() -> None:
    fund, defaults = _golden_inputs()
    df = project_fund(fund, defaults)
    last = df.iloc[-1]
    # Final quarter winds the fund down: distribution = nav going in,
    # nav_end = 0. Without this, the pacing curve leaves a residual
    # NAV at lifetime end (the bug fixed in this change).
    assert last["distribution_usd"] == pytest.approx(last["nav_start_usd"])
    assert last["nav_end_usd"] == pytest.approx(0.0)
