"""Cross-config invariant checks.

Schema validation guarantees each file is well-formed. Validation here enforces
invariants that span files, e.g. PE target weight matches sleeve target.
"""

from __future__ import annotations

from aa_model.io.schemas import StudyConfig


def validate_study_config(cfg: StudyConfig) -> None:
    """Raise ``ValueError`` on cross-config invariant violations."""

    # Phase 1 only supports the stub allocator.
    if cfg.base.allocation.engine != "stub":
        raise ValueError(
            f"Phase 1 supports only allocation.engine = 'stub'; "
            f"got {cfg.base.allocation.engine!r}"
        )

    stub_buckets = set(cfg.allocation.stub_weights)
    nav_buckets = set(cfg.fixture_scenario.nav_initial)

    missing = stub_buckets - nav_buckets
    if missing:
        raise ValueError(
            f"fixture nav_initial missing buckets present in stub_weights: {sorted(missing)}"
        )
    extra = nav_buckets - stub_buckets
    if extra:
        raise ValueError(
            f"fixture nav_initial has buckets not in stub_weights: {sorted(extra)}"
        )

    return_buckets = set(cfg.fixture_scenario.returns)
    pe_buckets = {b for b in stub_buckets if b.startswith("pe_")}
    expected_return_buckets = stub_buckets - pe_buckets
    missing_returns = expected_return_buckets - return_buckets
    if missing_returns:
        raise ValueError(
            f"fixture returns missing buckets: {sorted(missing_returns)}"
        )

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
