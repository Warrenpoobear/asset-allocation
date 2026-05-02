"""Cross-config invariant checks.

Schema validation guarantees each file is well-formed. Validation here enforces
invariants that span files, e.g. PE target weight matches sleeve target.
"""

from __future__ import annotations

from aa_model.io.schemas import StudyConfig

_KNOWN_ALLOCATION_ENGINES: frozenset[str] = frozenset({"stub", "riskfolio", "cvxportfolio"})
_KNOWN_IMPLEMENTATION_ENGINES: frozenset[str] = frozenset({"stub", "cvxportfolio"})


def validate_study_config(cfg: StudyConfig) -> None:
    """Raise ``ValueError`` on cross-config invariant violations."""

    # Engine must be one of the wired adapters. Schema-level Literal already
    # enforces this; check is repeated here so cross-config validation gives
    # a single, predictable failure mode.
    if cfg.base.allocation.engine not in _KNOWN_ALLOCATION_ENGINES:
        raise ValueError(
            f"unsupported allocation.engine: {cfg.base.allocation.engine!r}; "
            f"known: {sorted(_KNOWN_ALLOCATION_ENGINES)}"
        )
    if cfg.base.implementation.engine not in _KNOWN_IMPLEMENTATION_ENGINES:
        raise ValueError(
            f"unsupported implementation.engine: {cfg.base.implementation.engine!r}; "
            f"known: {sorted(_KNOWN_IMPLEMENTATION_ENGINES)}"
        )
    # Stub rebalancer must run with bps == 0 — non-zero bps would silently
    # mean "I asked for costs but the stub ignored them".
    if cfg.base.implementation.engine == "stub" and cfg.base.implementation.bps_per_trade != 0.0:
        raise ValueError(
            "implementation.engine='stub' requires bps_per_trade=0.0 "
            "(stub is a zero-cost rebalancer); set engine='cvxportfolio' "
            "to apply costs."
        )

    stub_buckets = set(cfg.allocation.stub_weights)
    nav_buckets = set(cfg.fixture_scenario.nav_initial)

    # CMA bucket alignment (Phase 5). The CMAConfig validator already
    # ensures internal consistency; here we enforce that the CMA covers
    # exactly the allocation bucket universe.
    cma_buckets = set(cfg.cma.expected_returns_annual.keys())
    missing_cma = stub_buckets - cma_buckets
    extra_cma = cma_buckets - stub_buckets
    if missing_cma or extra_cma:
        raise ValueError(
            "CMA bucket set does not match allocation.stub_weights — "
            f"missing in CMA: {sorted(missing_cma)}, "
            f"extra in CMA: {sorted(extra_cma)}"
        )

    missing = stub_buckets - nav_buckets
    if missing:
        raise ValueError(
            f"fixture nav_initial missing buckets present in stub_weights: {sorted(missing)}"
        )
    extra = nav_buckets - stub_buckets
    if extra:
        raise ValueError(f"fixture nav_initial has buckets not in stub_weights: {sorted(extra)}")

    return_buckets = set(cfg.fixture_scenario.returns)
    pe_buckets = {b for b in stub_buckets if b.startswith("pe_")}
    expected_return_buckets = stub_buckets - pe_buckets
    missing_returns = expected_return_buckets - return_buckets
    if missing_returns:
        raise ValueError(f"fixture returns missing buckets: {sorted(missing_returns)}")

    for fund in cfg.pe_pacing.funds:
        if fund.sleeve not in stub_buckets:
            raise ValueError(
                f"fund {fund.name!r} sleeve {fund.sleeve!r} not in stub_weights buckets"
            )

    pe_weight = sum(w for b, w in cfg.allocation.stub_weights.items() if b.startswith("pe_"))
    pe_target = cfg.base.pe.sleeve_target_pct
    if abs(pe_weight - pe_target) > 1e-9:
        raise ValueError(
            f"sum of pe_* stub_weights ({pe_weight}) != base.pe.sleeve_target_pct ({pe_target})"
        )

    if cfg.base.horizon.start_quarter != cfg.fixture_scenario.horizon.start_quarter:
        raise ValueError(
            "base.horizon.start_quarter != fixture_scenario.horizon.start_quarter "
            f"({cfg.base.horizon.start_quarter} vs {cfg.fixture_scenario.horizon.start_quarter})"
        )
    if cfg.base.horizon.num_quarters != cfg.fixture_scenario.horizon.num_quarters:
        raise ValueError(
            "base.horizon.num_quarters != fixture_scenario.horizon.num_quarters "
            f"({cfg.base.horizon.num_quarters} vs {cfg.fixture_scenario.horizon.num_quarters})"
        )

    n_q = cfg.base.horizon.num_quarters
    for bucket, path in cfg.fixture_scenario.returns.items():
        for ov in path.overrides:
            if ov.quarter_index >= n_q:
                raise ValueError(
                    f"return override for {bucket!r} at q={ov.quarter_index} exceeds horizon ({n_q})"
                )
