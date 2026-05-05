"""Phase 16 / L20 — Liquidity coverage diagnostics.

Computes whether the SFO has enough liquid resources to meet spending,
capital calls, taxes, entity obligations, and stress needs.

Architecture
============

Pure function: ``compute_liquidity_coverage`` takes positions +
obligations + optional metadata and returns a ``LiquidityCoverageResult``
deterministically. No ledger reads. No side effects. No module state.
Same inputs → same output byte-for-byte.

Reviewer tightenings enforced here
===================================

* T1: ``LiquidityObligationConfig`` is standalone; not wired into
  ``StudyConfig`` in Phase 16.
* T2: ``LiquidityCoverageConfig`` thresholds are configurable from the
  start; schema defaults match the Phase 16 design-lock policy but may
  be overridden per study.
* T3: Semi-liquid NAV is advisory-only. It is NOT included in breach
  coverage, liquidity runway, or next-12m obligation ratios. A future
  phase may model notice-period access.
* T4: ``next_12m_capital_calls_usd`` is never inferred from unfunded
  commitments. If total unfunded exists and next-12m calls are unknown,
  an advisory is emitted.
* T5: ``liquid_nav_to_annual_income_estimate`` is the stock-to-flow
  metric for ``distributable_income`` spending-base mode.
  ``liquid_to_spending_base`` is set to ``None`` when the spending base
  is flow-type to prevent stock/flow confusion.
* T6: ``total_unfunded_commitments_usd`` is captured in the result.

Standing principle
==================

NAV is not liquidity.
Appraisal value is not spending capacity.
Semi-liquid availability depends on gates, notice periods, and fund
conditions the model cannot assert.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from aa_model.ingestion.liquidity_mapping import resolve_phase12_tier
from aa_model.ingestion.schemas_position import ManagerTermsRecord, PositionRecord

if TYPE_CHECKING:
    from aa_model.spending.spending_base import SpendingBaseBreakdown

_STRICT = ConfigDict(extra="forbid")

_NAV_CAVEAT_TEXT: str = (
    "Coverage ratios reflect manager-reported NAV and human-authored "
    "liquidity_bucket tags. Legal redemption availability may differ "
    "due to gates, side pockets, lockups, or notice periods. "
    "Semi-liquid NAV is excluded from breach coverage and runway. "
    "This section is advisory only."
)


# ---- input schemas ---------------------------------------------------------


class LiquidityObligationConfig(BaseModel):
    """Near-term obligation inputs. All values >= 0 or None.

    T1: standalone config; not wired into StudyConfig in Phase 16.
    Unknown obligations are surfaced as advisory gaps, never zeroed.
    """

    model_config = _STRICT

    annual_spend_usd: float | None = None
    next_12m_capital_calls_usd: float | None = None
    next_12m_tax_obligations_usd: float | None = None
    next_12m_entity_obligations_usd: float | None = None
    note: str | None = None

    @field_validator(
        "annual_spend_usd",
        "next_12m_capital_calls_usd",
        "next_12m_tax_obligations_usd",
        "next_12m_entity_obligations_usd",
    )
    @classmethod
    def _non_negative(cls, v: float | None) -> float | None:
        if v is not None and v < 0:
            raise ValueError(f"Obligation values must be >= 0; got {v}")
        return v


class LiquidityCoverageConfig(BaseModel):
    """Policy thresholds for coverage warnings and breaches.

    T2: configurable from the start; defaults match Phase 16 design-lock.
    Override per study when IPS, generation, or board policy differs.
    """

    model_config = _STRICT

    liquid_coverage_breach_threshold: float = Field(default=1.0, ge=0.0)
    liquid_coverage_warning_threshold: float = Field(default=2.0, ge=0.0)
    illiquid_concentration_warning_pct: float = Field(default=0.60, ge=0.0, le=1.0)
    capital_call_coverage_warning_ratio: float = Field(default=1.0, ge=0.0)
    missing_bucket_warning_threshold: int = Field(default=1, ge=0)
    runway_horizon_quarters: int = Field(default=8, ge=1)

    @model_validator(mode="after")
    def _thresholds_well_ordered(self) -> LiquidityCoverageConfig:
        if self.liquid_coverage_warning_threshold < self.liquid_coverage_breach_threshold:
            raise ValueError(
                f"liquid_coverage_warning_threshold "
                f"({self.liquid_coverage_warning_threshold}) must be >= "
                f"liquid_coverage_breach_threshold "
                f"({self.liquid_coverage_breach_threshold})"
            )
        return self


# ---- output types ----------------------------------------------------------


@dataclass
class LiquidityCoverageDiagnostics:
    breaches: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    advisories: list[str] = field(default_factory=list)
    missing_obligation_inputs: list[str] = field(default_factory=list)
    positions_untagged: int = 0
    positions_stale_nav: int = 0
    nav_caveat: str = _NAV_CAVEAT_TEXT


@dataclass
class LiquidityCoverageResult:
    # Tier NAV aggregates
    liquid_nav: float
    semi_liquid_nav: float
    illiquid_nav: float
    locked_strategic_nav: float
    total_position_nav: float
    untagged_nav: float
    total_unfunded_commitments_usd: float  # T6

    # Coverage ratios (NAV-denominator modes)
    liquid_to_annual_spend: float | None
    liquid_to_spending_base: float | None  # T5: None when base is flow-type
    liquid_to_next12m_obligations: float | None
    capital_call_coverage: float | None
    liquid_fraction_of_nav: float | None
    illiquid_fraction_of_nav: float | None
    liquidity_runway_quarters: int | None  # T3: liquid-only; semi-liquid excluded

    # T5: stock-to-flow metric for distributable_income mode
    liquid_nav_to_annual_income_estimate: float | None

    # Semi-liquid advisory (T3: not in breach coverage or runway)
    semi_liquid_nav_with_known_terms: float
    semi_liquid_nav_terms_unknown: float
    semi_liquid_earliest_notice_days: int | None

    diagnostics: LiquidityCoverageDiagnostics


# ---- pure helper -----------------------------------------------------------


def _safe_divide(numerator: float, denominator: float | None) -> float | None:
    if denominator is None or denominator == 0.0 or not math.isfinite(denominator):
        return None
    return numerator / denominator


# ---- entry point -----------------------------------------------------------


def compute_liquidity_coverage(
    positions: list[PositionRecord],
    obligations: LiquidityObligationConfig,
    *,
    tier_overrides: dict[str, str] | None = None,
    manager_terms: list[ManagerTermsRecord] | None = None,
    spending_base: SpendingBaseBreakdown | None = None,
    spending_base_is_flow: bool = False,
    stale_nav_count: int = 0,
    untagged_position_count: int = 0,
    config: LiquidityCoverageConfig | None = None,
) -> LiquidityCoverageResult:
    """Compute liquidity coverage metrics from position records.

    Pure function. No ledger reads. No side effects.

    Parameters
    ----------
    positions:
        List of ``PositionRecord`` from Phase 15 ingestion or synthetic
        config.
    obligations:
        Near-term spending and obligation estimates.
    tier_overrides:
        Phase 15 → Phase 12 tier overrides (from
        ``PositionManifestConfig.liquidity_tier_overrides``).
    manager_terms:
        ``ManagerTermsRecord`` list for semi-liquid advisory (T3).
    spending_base:
        Phase 12 ``SpendingBaseBreakdown`` for ``liquid_to_spending_base``
        and ``liquid_nav_to_annual_income_estimate`` (T5).
    spending_base_is_flow:
        Set ``True`` when spending base was computed in
        ``distributable_income`` mode. Triggers T5 labeling logic.
    stale_nav_count:
        Pass-through from ``PositionIngestionDiagnostics`` for advisory.
    untagged_position_count:
        Pass-through from ``PositionIngestionDiagnostics`` for warning.
    config:
        Policy thresholds. Uses ``LiquidityCoverageConfig()`` defaults
        when ``None``.
    """
    cfg = config or LiquidityCoverageConfig()
    manager_by_id: dict[str, ManagerTermsRecord] = {m.manager_id: m for m in (manager_terms or [])}

    # ---- tier NAV aggregates -----------------------------------------------

    tier_nav: dict[str, float] = defaultdict(float)
    total_unfunded = 0.0
    semi_liquid_known = 0.0
    semi_liquid_unknown = 0.0
    notice_days_list: list[int] = []

    for pos in positions:
        tier = resolve_phase12_tier(pos.liquidity_bucket, tier_overrides)
        tier_nav[tier] += pos.market_value_usd
        if pos.unfunded_commitment_usd:
            total_unfunded += pos.unfunded_commitment_usd

        # T3: semi-liquid advisory breakdown
        if tier == "semi_liquid":
            mgr = manager_by_id.get(pos.manager_id) if pos.manager_id else None
            if mgr and mgr.confidence != "unknown":
                semi_liquid_known += pos.market_value_usd
                if mgr.notice_days is not None:
                    notice_days_list.append(mgr.notice_days)
            else:
                semi_liquid_unknown += pos.market_value_usd

    liquid_nav = tier_nav.get("liquid", 0.0)
    semi_liquid_nav = tier_nav.get("semi_liquid", 0.0)
    illiquid_nav = tier_nav.get("illiquid", 0.0)
    locked_nav = tier_nav.get("locked_strategic", 0.0)
    total_nav = sum(tier_nav.values())
    earliest_notice = min(notice_days_list) if notice_days_list else None

    # ---- coverage ratios ---------------------------------------------------

    annual_spend = obligations.annual_spend_usd

    liquid_to_annual_spend = _safe_divide(liquid_nav, annual_spend)

    # T5: liquid_to_spending_base is None when spending base is flow-type
    if spending_base is not None and not spending_base_is_flow:
        liquid_to_spending_base = _safe_divide(liquid_nav, spending_base.base_usd)
    else:
        liquid_to_spending_base = None

    # T5: stock-to-flow metric for distributable_income mode
    if spending_base is not None and spending_base_is_flow:
        liquid_nav_to_annual_income_estimate = _safe_divide(liquid_nav, spending_base.base_usd)
    else:
        liquid_nav_to_annual_income_estimate = None

    # next-12m total obligations
    next12m_parts = [
        obligations.next_12m_capital_calls_usd,
        obligations.next_12m_tax_obligations_usd,
        obligations.next_12m_entity_obligations_usd,
    ]
    known_parts = [v for v in next12m_parts if v is not None]
    next12m_total: float | None = sum(known_parts) if known_parts else None

    liquid_to_next12m = _safe_divide(liquid_nav, next12m_total)
    capital_call_coverage = _safe_divide(liquid_nav, obligations.next_12m_capital_calls_usd)

    liquid_fraction = _safe_divide(liquid_nav, total_nav)
    illiquid_fraction = _safe_divide(illiquid_nav + locked_nav, total_nav)

    # T3: runway uses liquid-only NAV
    if annual_spend and annual_spend > 0:
        quarterly_spend = annual_spend / 4.0
        runway_quarters: int | None = int(liquid_nav / quarterly_spend)
    else:
        runway_quarters = None

    # ---- diagnostics -------------------------------------------------------

    diag = _build_diagnostics(
        liquid_nav=liquid_nav,
        semi_liquid_nav=semi_liquid_unknown,
        total_nav=total_nav,
        liquid_to_annual_spend=liquid_to_annual_spend,
        liquid_to_next12m=liquid_to_next12m,
        capital_call_coverage=capital_call_coverage,
        illiquid_fraction=illiquid_fraction,
        obligations=obligations,
        total_unfunded=total_unfunded,
        stale_nav_count=stale_nav_count,
        untagged_position_count=untagged_position_count,
        runway_quarters=runway_quarters,
        cfg=cfg,
    )

    return LiquidityCoverageResult(
        liquid_nav=liquid_nav,
        semi_liquid_nav=semi_liquid_nav,
        illiquid_nav=illiquid_nav,
        locked_strategic_nav=locked_nav,
        total_position_nav=total_nav,
        untagged_nav=0.0,
        total_unfunded_commitments_usd=total_unfunded,
        liquid_to_annual_spend=liquid_to_annual_spend,
        liquid_to_spending_base=liquid_to_spending_base,
        liquid_to_next12m_obligations=liquid_to_next12m,
        capital_call_coverage=capital_call_coverage,
        liquid_fraction_of_nav=liquid_fraction,
        illiquid_fraction_of_nav=illiquid_fraction,
        liquidity_runway_quarters=runway_quarters,
        liquid_nav_to_annual_income_estimate=liquid_nav_to_annual_income_estimate,
        semi_liquid_nav_with_known_terms=semi_liquid_known,
        semi_liquid_nav_terms_unknown=semi_liquid_unknown,
        semi_liquid_earliest_notice_days=earliest_notice,
        diagnostics=diag,
    )


def _build_diagnostics(
    *,
    liquid_nav: float,
    semi_liquid_nav: float,
    total_nav: float,
    liquid_to_annual_spend: float | None,
    liquid_to_next12m: float | None,
    capital_call_coverage: float | None,
    illiquid_fraction: float | None,
    obligations: LiquidityObligationConfig,
    total_unfunded: float,
    stale_nav_count: int,
    untagged_position_count: int,
    runway_quarters: int | None,
    cfg: LiquidityCoverageConfig,
) -> LiquidityCoverageDiagnostics:
    breaches: list[str] = []
    warnings: list[str] = []
    advisories: list[str] = []
    missing: list[str] = []

    # Breaches
    if (
        liquid_to_annual_spend is not None
        and liquid_to_annual_spend < cfg.liquid_coverage_breach_threshold
    ):
        breaches.append(
            f"liquid_to_annual_spend={liquid_to_annual_spend:.2f} < "
            f"breach threshold {cfg.liquid_coverage_breach_threshold:.1f}"
        )

    if liquid_to_next12m is not None and liquid_to_next12m < cfg.liquid_coverage_breach_threshold:
        breaches.append(
            f"liquid_to_next12m_obligations={liquid_to_next12m:.2f} < "
            f"breach threshold {cfg.liquid_coverage_breach_threshold:.1f}"
        )

    # Warnings
    if (
        liquid_to_annual_spend is not None
        and cfg.liquid_coverage_breach_threshold
        <= liquid_to_annual_spend
        < cfg.liquid_coverage_warning_threshold
    ):
        warnings.append(
            f"liquid_to_annual_spend={liquid_to_annual_spend:.2f} < "
            f"warning threshold {cfg.liquid_coverage_warning_threshold:.1f}"
        )

    if (
        capital_call_coverage is not None
        and capital_call_coverage < cfg.capital_call_coverage_warning_ratio
    ):
        warnings.append(
            f"capital_call_coverage={capital_call_coverage:.2f} < "
            f"{cfg.capital_call_coverage_warning_ratio:.1f}"
        )

    if illiquid_fraction is not None and illiquid_fraction > cfg.illiquid_concentration_warning_pct:
        warnings.append(
            f"illiquid+locked_strategic fraction={illiquid_fraction:.1%} > "
            f"{cfg.illiquid_concentration_warning_pct:.0%} concentration threshold"
        )

    if untagged_position_count >= cfg.missing_bucket_warning_threshold:
        warnings.append(f"{untagged_position_count} position(s) missing liquidity_bucket tag")

    if runway_quarters is not None and runway_quarters < cfg.runway_horizon_quarters:
        warnings.append(
            f"liquidity_runway={runway_quarters} quarters < "
            f"horizon {cfg.runway_horizon_quarters} quarters"
        )

    # Advisories
    if obligations.annual_spend_usd is None:
        missing.append("annual_spend_usd")
        advisories.append(
            "annual_spend_usd not provided — liquid_to_annual_spend and "
            "runway_quarters unavailable"
        )

    if obligations.next_12m_capital_calls_usd is None:
        missing.append("next_12m_capital_calls_usd")
        # T4: advisory if total unfunded exists but next-12m calls unknown
        if total_unfunded > 0:
            advisories.append(
                f"total_unfunded_commitments={total_unfunded:,.0f} exists but "
                f"next_12m_capital_calls_usd not provided — capital call "
                f"coverage unavailable"
            )

    if obligations.next_12m_tax_obligations_usd is None:
        missing.append("next_12m_tax_obligations_usd")

    if obligations.next_12m_entity_obligations_usd is None:
        missing.append("next_12m_entity_obligations_usd")

    if semi_liquid_nav > 0:
        advisories.append(
            f"semi_liquid NAV with unknown terms={semi_liquid_nav:,.0f} — "
            f"redemption availability not modeled (T3)"
        )

    if stale_nav_count > 0:
        advisories.append(f"{stale_nav_count} position(s) have stale NAV (> 90 days old)")

    return LiquidityCoverageDiagnostics(
        breaches=breaches,
        warnings=warnings,
        advisories=advisories,
        missing_obligation_inputs=missing,
        positions_untagged=untagged_position_count,
        positions_stale_nav=stale_nav_count,
    )


# ---- report renderer -------------------------------------------------------


def render_coverage_report_section(
    result: LiquidityCoverageResult,
    spending_base_mode: str | None = None,
) -> str:
    """Render the ## Liquidity coverage (Phase 16, advisory) report section.

    Parameters
    ----------
    result:
        ``LiquidityCoverageResult`` from ``compute_liquidity_coverage``.
    spending_base_mode:
        Phase 17 reviewer tightening 5 — explicit parameter set by the
        orchestrator from ``cfg.spending.guardrail.spending_base``.
        Used to label the ``liquid_to_spending_base`` ratio line.
        ``None`` renders the label as ``"liquid / spending_base"``.
    """

    def _fmt_nav(v: float) -> str:
        return f"${v:>14,.0f}"

    def _fmt_ratio(v: float | None) -> str:
        if v is None:
            return "n/a"
        return f"{v:.2f}x"

    def _pct(v: float | None) -> str:
        if v is None:
            return "n/a"
        return f"{v:.1%}"

    spending_base_label = (
        f"liquid / spending_base ({spending_base_mode})"
        if spending_base_mode is not None
        else "liquid / spending_base"
    )

    total = result.total_position_nav
    lines = [
        "## Liquidity coverage (Phase 16, advisory)",
        "",
        f"  total_position_nav:          {_fmt_nav(total)}",
        f"  liquid_nav:                  {_fmt_nav(result.liquid_nav)}"
        f"  ({_pct(result.liquid_fraction_of_nav)} of NAV)",
        f"  semi_liquid_nav:             {_fmt_nav(result.semi_liquid_nav)}"
        f"  (advisory only — T3)",
        f"  illiquid_nav:                {_fmt_nav(result.illiquid_nav)}",
        f"  locked_strategic_nav:        {_fmt_nav(result.locked_strategic_nav)}"
        f"  ({_pct(result.illiquid_fraction_of_nav)} illiquid+locked)",
        f"  total_unfunded_commitments:  {_fmt_nav(result.total_unfunded_commitments_usd)}",
        "",
        "  Coverage ratios:",
        f"    liquid / annual_spend:     {_fmt_ratio(result.liquid_to_annual_spend)}",
        f"    {spending_base_label}:".ljust(38) + f"{_fmt_ratio(result.liquid_to_spending_base)}",
        f"    liquid / next12m_oblig:    {_fmt_ratio(result.liquid_to_next12m_obligations)}",
        f"    capital_call_coverage:     {_fmt_ratio(result.capital_call_coverage)}",
        "    liquidity_runway:          "
        + (
            f"{result.liquidity_runway_quarters} quarters"
            if result.liquidity_runway_quarters is not None
            else "n/a"
        ),
    ]

    if result.liquid_nav_to_annual_income_estimate is not None:
        lines.append(
            f"    liquid/annual_income_est:  {_fmt_ratio(result.liquid_nav_to_annual_income_estimate)}"
            f"  (stock-to-flow; distributable_income mode)"
        )

    lines += [
        "",
        "  Semi-liquid advisory (T3 — not in breach coverage or runway):",
        f"    known_terms:               {_fmt_nav(result.semi_liquid_nav_with_known_terms)}",
        f"    terms_unknown:             {_fmt_nav(result.semi_liquid_nav_terms_unknown)}",
        "    earliest_notice_days:      "
        + (
            str(result.semi_liquid_earliest_notice_days)
            if result.semi_liquid_earliest_notice_days is not None
            else "n/a"
        ),
        "",
    ]

    d = result.diagnostics
    lines.append(
        f"  BREACHES ({len(d.breaches)}): " + ("; ".join(d.breaches) if d.breaches else "none")
    )
    lines.append(
        f"  WARNINGS ({len(d.warnings)}): " + ("; ".join(d.warnings) if d.warnings else "none")
    )
    lines.append(
        f"  ADVISORIES ({len(d.advisories)}): "
        + ("; ".join(d.advisories) if d.advisories else "none")
    )
    lines += [
        "",
        f"  CAVEAT: {d.nav_caveat}",
    ]
    return "\n".join(lines)
