"""Phase 2 scenario builder.

A ``Scenario`` is a bundle of optional overrides that perturb the resolved
study config. The orchestrator consumes scenarios via
``run_orchestrator(..., scenario=...)`` and applies overrides through
``cfg.model_copy(update=...)`` — there is no scenario-aware branching
anywhere else in the engine.

Five canonical perturbations (SPEC §6 Phase 2):

1. ``base`` — no overrides; reference run.
2. ``public_drawdown`` — -25% public_equity at q8 with 4-quarter linear
   recovery (overrides on ``fixture_scenario.returns``).
3. ``delayed_pe_distributions`` — TA bow raised 2.5 → 4.0 so distributions
   concentrate later in fund lifetime (override on
   ``pe_pacing.ta_defaults.bow``).
4. ``clustered_calls`` — ``rate_of_contribution`` front-loaded to
   [0.50, 0.30, 0.15, 0.05] (override on
   ``pe_pacing.ta_defaults.rate_of_contribution``).
5. ``inflation_shock`` — spending ``inflation_pct`` 2.5% → 6.0% per year
   (override on ``spending.inflation_pct``).

Correlation shock (also listed in §6) is intentionally omitted: Phase 1
does not model bucket-level correlation, so a correlation override has
no place to land. A correlation scenario will be added when the CMA
gains a covariance matrix.

Model limitation — PE timing scenarios
======================================

``clustered_calls`` and ``delayed_pe_distributions`` shift the timing of
PE cash flows but do **not** model the opportunity cost of capital
parked in PE versus deployed in public markets. PE NAV growth
(``ta_defaults.growth_pct``) is a deterministic constant that does not
respond to which side of the portfolio holds capital, and the
zero-cost rebalancer recycles distributions back into the same target
weights without slippage. As a result, timing shifts can mechanically
increase or decrease cumulative return — e.g. ``clustered_calls``
deploys more capital earlier into a higher-modeled-growth sleeve and
appears to outperform ``base``. **Do not read this as alpha.** Treat
PE-timing scenarios as stress on liquidity and pacing, not on returns,
until the model gains a public-vs-private opportunity-cost link
(stochastic CMA + per-bucket return paths driven from a shared regime).
"""

from __future__ import annotations

from dataclasses import dataclass

from aa_model.io.schemas import (
    FixtureScenarioConfig,
    PEPacingConfig,
    ReturnOverride,
    ReturnPath,
    SpendingConfig,
)


@dataclass(frozen=True)
class Scenario:
    """A bundle of optional overrides on top of the base study config.

    Any field set to ``None`` keeps the base value. ``name`` is the
    scenario's identifier in the comparison report and the suffix on
    the per-scenario invocation id.
    """

    name: str
    description: str
    fixture_scenario: FixtureScenarioConfig | None = None
    pe_pacing: PEPacingConfig | None = None
    spending: SpendingConfig | None = None


def _override_returns(
    base: FixtureScenarioConfig,
    *,
    bucket: str,
    overrides: list[ReturnOverride],
    name: str,
    description: str,
) -> FixtureScenarioConfig:
    new_returns = dict(base.returns)
    base_path = new_returns[bucket]
    new_returns[bucket] = ReturnPath(quarterly=base_path.quarterly, overrides=list(overrides))
    return base.model_copy(
        update={"name": name, "description": description, "returns": new_returns}
    )


def make_scenarios(
    base_fixture: FixtureScenarioConfig,
    base_pe_pacing: PEPacingConfig,
    base_spending: SpendingConfig,
) -> list[Scenario]:
    """Build the five canonical Phase 2 scenarios from base configs."""
    scenarios: list[Scenario] = []

    # 1. Base — pure reference run.
    scenarios.append(Scenario(name="base", description="Reference scenario; no perturbations."))

    # 2. Public drawdown.
    fix_dd = _override_returns(
        base_fixture,
        bucket="public_equity",
        overrides=[
            ReturnOverride(quarter_index=8, value=-0.25),
            ReturnOverride(quarter_index=9, value=0.0987),
            ReturnOverride(quarter_index=10, value=0.0987),
            ReturnOverride(quarter_index=11, value=0.0987),
            ReturnOverride(quarter_index=12, value=0.0987),
        ],
        name="public_drawdown",
        description="-25% public_equity shock at q8 with 4-quarter linear recovery.",
    )
    scenarios.append(
        Scenario(
            name="public_drawdown",
            description=fix_dd.description,
            fixture_scenario=fix_dd,
        )
    )

    # 3. Delayed PE distributions — bow 2.5 → 4.0.
    pe_delayed = base_pe_pacing.model_copy(
        update={
            "ta_defaults": base_pe_pacing.ta_defaults.model_copy(update={"bow": 4.0}),
        }
    )
    scenarios.append(
        Scenario(
            name="delayed_pe_distributions",
            description="TA bow raised 2.5 → 4.0; distributions concentrate later in fund lifetime.",
            pe_pacing=pe_delayed,
        )
    )

    # 4. Clustered calls — front-loaded contribution schedule.
    pe_clustered = base_pe_pacing.model_copy(
        update={
            "ta_defaults": base_pe_pacing.ta_defaults.model_copy(
                update={"rate_of_contribution": [0.50, 0.30, 0.15, 0.05]}
            ),
        }
    )
    scenarios.append(
        Scenario(
            name="clustered_calls",
            description="rate_of_contribution front-loaded to [0.50, 0.30, 0.15, 0.05].",
            pe_pacing=pe_clustered,
        )
    )

    # 5. Inflation shock — spending inflation 2.5% → 6.0% per year.
    sp_shock = base_spending.model_copy(update={"inflation_pct": 0.06})
    scenarios.append(
        Scenario(
            name="inflation_shock",
            description="Spending inflation steps from 2.5% to 6.0% per year.",
            spending=sp_shock,
        )
    )

    return scenarios
