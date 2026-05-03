"""Phase 15 — Phase 15 liquidity_bucket → Phase 12 spending-base tier mapping.

Reviewer tightening T3: the Phase 15 bucket taxonomy is richer than
Phase 12's four-tier vocabulary. This module provides the deterministic,
configurable mapping between them.

Default mapping (locked; only ``re_stabilized`` is configurable):

    cash_equivalent   → liquid
    daily_liquid      → liquid
    semi_liquid       → semi_liquid
    illiquid          → illiquid
    locked_strategic  → locked_strategic
    re_stabilized     → illiquid          (configurable)
    re_development    → locked_strategic
    re_land           → locked_strategic
    opco_strategic    → locked_strategic

``re_stabilized`` may be overridden to ``locked_strategic`` via
``PositionManifestConfig.liquidity_tier_overrides``. Income-producing
status of stabilized RE never silently upgrades its tier to ``liquid``.
"""

from __future__ import annotations

_DEFAULT_MAPPING: dict[str, str] = {
    "cash_equivalent": "liquid",
    "daily_liquid": "liquid",
    "semi_liquid": "semi_liquid",
    "illiquid": "illiquid",
    "locked_strategic": "locked_strategic",
    "re_stabilized": "illiquid",
    "re_development": "locked_strategic",
    "re_land": "locked_strategic",
    "opco_strategic": "locked_strategic",
}


def resolve_phase12_tier(
    bucket: str,
    overrides: dict[str, str] | None,
) -> str:
    """Return the Phase 12 liquidity tier for a Phase 15 bucket.

    Applies ``overrides`` if present for this bucket; falls back to
    ``_DEFAULT_MAPPING``. Raises ``ValueError`` for unknown buckets.
    """
    if bucket not in _DEFAULT_MAPPING:
        raise ValueError(
            f"Unknown Phase 15 liquidity_bucket: {bucket!r}. " f"Valid: {sorted(_DEFAULT_MAPPING)}"
        )
    if overrides and bucket in overrides:
        return overrides[bucket]
    return _DEFAULT_MAPPING[bucket]


def build_effective_mapping(
    overrides: dict[str, str] | None,
) -> dict[str, str]:
    """Return the full effective Phase 15 → Phase 12 mapping.

    Merges ``_DEFAULT_MAPPING`` with ``overrides``. Suitable for
    report rendering so the effective mapping is always visible.
    """
    mapping = dict(_DEFAULT_MAPPING)
    if overrides:
        mapping.update(overrides)
    return mapping
