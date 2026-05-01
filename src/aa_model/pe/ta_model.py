"""Takahashi-Alexander quarterly cash-flow projection.

For each fund the model produces, for every quarter ``t = 0..4*L - 1`` since
vintage, the fund's call, distribution, NAV mark, and NAV trajectory:

    L = lifetime_years
    P = commitment_period_years
    rc[y] = rate_of_contribution[y]              (sums to 1.0; flat within year)
    B = bow                                       (curvature of distribution rate)
    Y = yield_pct                                 (annual rate; floor on dist rate)
    G = growth_pct                                (annual NAV growth post-distribution)
    K = commitment_usd

For quarter index ``t = 0, 1, ..., 4*L - 1``::

    year_index = t // 4
    age_years  = (t + 1) / 4                                # age at quarter end

    call_t     = (rc[year_index] * K) / 4   if year_index < P else 0.0
    N_after_call           = NAV_start + call_t
    annual_dist_rate       = max(Y, (age_years / L) ** B)
    quarterly_dist_rate    = annual_dist_rate / 4
    distribution_t         = quarterly_dist_rate * N_after_call
    N_after_dist           = N_after_call - distribution_t
    nav_mark_t             = N_after_dist * (G / 4)
    NAV_end                = N_after_dist + nav_mark_t

The model is the canonical PE cash-flow generator in Phase 1; it is *not*
behind an adapter (SPEC §9 final paragraph).
"""

from __future__ import annotations

import pandas as pd

from aa_model.io.schemas import FundConfig, TADefaultsConfig

PROJECTION_COLUMNS: tuple[str, ...] = (
    "fund_name",
    "vintage",
    "quarter_index",
    "quarter",
    "age_years",
    "nav_start_usd",
    "call_usd",
    "distribution_usd",
    "nav_mark_usd",
    "nav_end_usd",
)


def project_fund(fund: FundConfig, defaults: TADefaultsConfig) -> pd.DataFrame:
    """Project a single fund quarter-by-quarter for its full lifetime."""
    L = defaults.lifetime_years
    P = defaults.commitment_period_years
    rc = defaults.rate_of_contribution
    B = defaults.bow
    Y = defaults.yield_pct
    G = defaults.growth_pct
    K = fund.commitment_usd
    vintage = pd.Period(fund.vintage, freq="Q-DEC")

    n_quarters = 4 * L
    rows: list[dict] = []
    nav = 0.0

    for t in range(n_quarters):
        year_index = t // 4
        age_years = (t + 1) / 4.0

        call = (rc[year_index] * K) / 4.0 if year_index < P else 0.0

        nav_after_call = nav + call

        annual_dist_rate = max(Y, (age_years / L) ** B)
        # In the final quarter, force full liquidation so the fund winds down
        # cleanly (annual_dist_rate may exceed 1.0 if (age/L)**B does; cap at 1).
        quarterly_dist_rate = min(annual_dist_rate / 4.0, 1.0)
        distribution = quarterly_dist_rate * nav_after_call
        nav_after_dist = nav_after_call - distribution

        nav_mark = nav_after_dist * (G / 4.0)
        nav_end = nav_after_dist + nav_mark

        rows.append(
            {
                "fund_name": fund.name,
                "vintage": str(vintage),
                "quarter_index": t,
                "quarter": str(vintage + t),
                "age_years": age_years,
                "nav_start_usd": nav,
                "call_usd": call,
                "distribution_usd": distribution,
                "nav_mark_usd": nav_mark,
                "nav_end_usd": nav_end,
            }
        )
        nav = nav_end

    df = pd.DataFrame(rows, columns=list(PROJECTION_COLUMNS))
    return df


def project_funds(
    funds: list[FundConfig], defaults: TADefaultsConfig
) -> pd.DataFrame:
    """Stack projections across funds. Empty input returns an empty frame."""
    if not funds:
        return pd.DataFrame(columns=list(PROJECTION_COLUMNS))
    parts = [project_fund(f, defaults) for f in funds]
    return pd.concat(parts, ignore_index=True)
