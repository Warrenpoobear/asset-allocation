"""Reproducibility manifest. SPEC §5.3 + §8.

Each orchestrator run writes ``manifest.json`` next to the ledger.

``run_id`` format::

    aa-<config_hash[:12]>-<fixtures_hash[:12]>-<UTC_timestamp>-<nonce>

The two hash segments are deterministic in the inputs; the timestamp +
nonce make every invocation unique so two consecutive runs land in
distinct directories (SPEC §8 "never overwritten"). Determinism applies
to ledger *content* (compare with the ``run_id`` column dropped), not the
parquet file path.
"""

from __future__ import annotations

import importlib.metadata as md
import json
import secrets
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

_TRACKED_LIBS = ("aa_model", "numpy", "pandas", "pydantic", "pyyaml", "pyarrow", "jinja2")


def _library_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    for lib in _TRACKED_LIBS:
        try:
            versions[lib] = md.version(lib)
        except md.PackageNotFoundError:
            versions[lib] = "unknown"
    return versions


def utcnow_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utcnow_compact() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def make_invocation_id() -> str:
    """Per-invocation suffix: compact UTC timestamp + 4-char hex nonce.

    The nonce protects against same-second collisions when two runs are
    launched concurrently.
    """
    return f"{_utcnow_compact()}-{secrets.token_hex(2)}"


def make_run_id(
    config_hash: str,
    fixtures_hash: str,
    *,
    invocation_id: str | None = None,
) -> str:
    """``aa-<cfg[:12]>-<fix[:12]>-<invocation>``.

    ``invocation_id`` is generated via :func:`make_invocation_id` if not
    supplied. Pass an explicit value (e.g., from a CLI flag) to reproduce
    a specific historical run dir.
    """
    cfg = config_hash.split(":", 1)[-1][:12]
    fix = fixtures_hash.split(":", 1)[-1][:12]
    inv = invocation_id if invocation_id is not None else make_invocation_id()
    return f"aa-{cfg}-{fix}-{inv}"


@dataclass(frozen=True)
class Manifest:
    run_id: str
    config_hash: str
    fixtures_hash: str
    library_versions: dict[str, str]
    seed: int
    started_at: str
    finished_at: str
    outputs: list[str] = field(default_factory=list)

    @classmethod
    def build(
        cls,
        *,
        run_id: str,
        config_hash: str,
        fixtures_hash: str,
        seed: int,
        started_at: str,
        finished_at: str,
        outputs: list[str],
    ) -> Manifest:
        return cls(
            run_id=run_id,
            config_hash=config_hash,
            fixtures_hash=fixtures_hash,
            library_versions=_library_versions(),
            seed=seed,
            started_at=started_at,
            finished_at=finished_at,
            outputs=outputs,
        )

    def to_dict(self) -> dict:
        return asdict(self)

    def write(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, sort_keys=True, indent=2)
            f.write("\n")
