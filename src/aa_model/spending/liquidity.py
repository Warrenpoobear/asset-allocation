"""Liquidity / drawdown metrics for Phase 2 scenario reporting.

Three measures (SPEC §6 Phase 2):

* **Coverage** — months of forward spending covered by liquid (cash +
  short-bond) NAV. Reported per quarter, plus min and mean across the
  horizon.
* **Reserve shortfall frequency** — fraction of quarters where coverage
  drops below ``liquidity.floor_months`` (default 18, from base config).
* **Worst-draw window** — peak-to-trough decline in total NAV
  (``max_drawdown_pct``) and its length in quarters.

All metrics are computed from the ledger's end-of-quarter NAV grid plus the
spending series the orchestrator already generates. No new state objects.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

LIQUID_BUCKETS_DEFAULT: tuple[str, ...] = ("cash", "public_bond")


@dataclass(frozen=True)
class LiquidityMetrics:
    final_nav_usd: float
    cumulative_return_pct: float
    min_coverage_months: float
    mean_coverage_months: float
    shortfall_frequency: float  # in [0, 1]
    max_drawdown_pct: float  # ≤ 0
    drawdown_quarters: int


def coverage_months_per_quarter(
    end_nav_by_quarter: pd.DataFrame,
    annual_spend_by_quarter: pd.Series,
    liquid_buckets: tuple[str, ...] = LIQUID_BUCKETS_DEFAULT,
) -> pd.Series:
    """Coverage per quarter = liquid NAV / monthly spend at that quarter.

    ``annual_spend_by_quarter`` is the per-quarter annualized spend rate
    — i.e. the spending the household is currently running at, expressed
    as an annual figure. (For Phase 1 this is just the quarterly outflow
    × 4.) Coverage is then ``liquid_nav / (annual / 12)``.
    """
    present = [b for b in liquid_buckets if b in end_nav_by_quarter.columns]
    if not present:
        return pd.Series(0.0, index=end_nav_by_quarter.index, name="coverage_months")
    liquid_nav = end_nav_by_quarter[list(present)].sum(axis=1)
    monthly = annual_spend_by_quarter / 12.0
    # Align indexes; if spend is zero in a quarter, coverage is infinite.
    aligned_monthly = monthly.reindex(liquid_nav.index)
    coverage = liquid_nav / aligned_monthly.replace(0.0, float("nan"))
    coverage = coverage.fillna(float("inf"))
    coverage.name = "coverage_months"
    return coverage


def shortfall_frequency(coverage: pd.Series, floor_months: float) -> float:
    """Fraction of quarters where ``coverage < floor_months``."""
    if coverage.empty:
        return 0.0
    return float((coverage < floor_months).mean())


def max_drawdown(total_nav: pd.Series) -> tuple[float, int]:
    """Worst peak-to-trough decline + window length (in quarters).

    Returns ``(max_dd_pct, length)``. ``max_dd_pct`` is non-positive;
    ``length`` is 0 when no drawdown ever occurs (monotonically rising
    NAV).
    """
    if total_nav.empty:
        return 0.0, 0
    cummax = total_nav.cummax()
    dd = total_nav / cummax - 1.0
    max_dd = float(dd.min())
    if max_dd >= 0.0:
        return 0.0, 0
    trough_idx = dd.idxmin()
    pre = total_nav.loc[:trough_idx]
    peak_value = pre.max()
    peak_idx = pre[pre == peak_value].index[-1]
    length = total_nav.index.get_loc(trough_idx) - total_nav.index.get_loc(peak_idx)
    return max_dd, int(length)


def compute_liquidity_metrics(
    end_nav_by_quarter: pd.DataFrame,
    annual_spend_by_quarter: pd.Series,
    *,
    floor_months: float,
    initial_nav_usd: float,
    liquid_buckets: tuple[str, ...] = LIQUID_BUCKETS_DEFAULT,
) -> LiquidityMetrics:
    coverage = coverage_months_per_quarter(
        end_nav_by_quarter, annual_spend_by_quarter, liquid_buckets
    )
    total_nav = end_nav_by_quarter.sum(axis=1)
    final = float(total_nav.iloc[-1]) if not total_nav.empty else 0.0
    cum_ret = ((final / initial_nav_usd) - 1.0) if initial_nav_usd > 0 else 0.0
    max_dd, dd_qs = max_drawdown(total_nav)
    return LiquidityMetrics(
        final_nav_usd=final,
        cumulative_return_pct=cum_ret * 100.0,
        min_coverage_months=float(coverage.min()),
        mean_coverage_months=float(coverage.mean()),
        shortfall_frequency=shortfall_frequency(coverage, floor_months),
        max_drawdown_pct=max_dd * 100.0,
        drawdown_quarters=dd_qs,
    )
