"""Generate the committed golden CSV used by the TA regression test.

Run from repo root::

    python tests/generate_ta_golden.py

Pinned defaults from SPEC §6 Phase 1:

* lifetime_years = 12
* commitment_period_years = 4
* rate_of_contribution = [0.25, 0.30, 0.25, 0.20]
* bow = 2.5
* yield_pct = 0.0
* growth_pct = 0.13

Single fund: commitment $100M, vintage 2024Q1, projected 48 quarters.
The regression test in ``tests/test_pe_ta.py`` asserts byte-equality between
a freshly generated DataFrame and the committed CSV.
"""

from __future__ import annotations

from pathlib import Path

from aa_model.io.schemas import FundConfig, TADefaultsConfig
from aa_model.pe.ta_model import project_fund


def build_defaults() -> TADefaultsConfig:
    return TADefaultsConfig(
        lifetime_years=12,
        commitment_period_years=4,
        rate_of_contribution=[0.25, 0.30, 0.25, 0.20],
        bow=2.5,
        yield_pct=0.0,
        growth_pct=0.13,
    )


def build_fund() -> FundConfig:
    return FundConfig(
        name="GoldenFund_2024Q1",
        commitment_usd=100_000_000.0,
        vintage="2024Q1",
        sleeve="pe_buyout",
    )


def main() -> Path:
    df = project_fund(build_fund(), build_defaults())
    out = Path(__file__).parent / "golden" / "ta_single_fund.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False, float_format="%.10f", lineterminator="\n")
    return out


if __name__ == "__main__":
    print(f"wrote {main()}")
