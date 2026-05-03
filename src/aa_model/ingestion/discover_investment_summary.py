"""Phase 15 — Investment Summary discovery CLI entry.

Usage::

    python -m aa_model.ingestion.discover_investment_summary \\
        --workbook /path/to/Investment\\ Summary.xlsx \\
        --mode privacy_safe \\
        --dry-run

    python -m aa_model.ingestion.discover_investment_summary \\
        --workbook /path/to/Investment\\ Summary.xlsx \\
        --out configs/investment_summary_manifest_local.yaml \\
        --mode local_private \\
        --workbook-version v1

Privacy posture
===============

* Default mode: ``privacy_safe`` — redacts non-structural sheet names;
  suitable for chat / committed scaffold / public report.
* ``local_private`` — preserves real sheet names; the CLI refuses to
  write the output to anything other than a path ending in
  ``_local.yaml`` or under ``data/external/``. Those paths are
  gitignored per Phase 14.x conventions.

The CLI never prints cell contents, dollar values, or position-level
content. ``--dry-run`` prints aggregate structural diagnostics only.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from aa_model.ingestion.discovery_position import (
    build_draft_position_manifest,
    discover_investment_summary,
    render_position_diagnostics,
)

_LOCAL_PRIVATE_PATH_HINTS: tuple[str, ...] = (
    "_local.yaml",
    "data/external/",
    "data\\external\\",
)


def _check_local_private_path(out: Path) -> None:
    s = str(out)
    if not any(hint in s for hint in _LOCAL_PRIVATE_PATH_HINTS):
        raise SystemExit(
            f"refusing to write local_private output to {out!s}: "
            f"path must end in '_local.yaml' or live under data/external/ "
            f"so the manifest stays gitignored."
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="aa_model.ingestion.discover_investment_summary",
        description=(
            "Phase 15 Investment Summary discovery. Reads the workbook "
            "read-only, discovers structural layout, and emits a draft "
            "manifest YAML or aggregate diagnostics."
        ),
    )
    parser.add_argument("--workbook", required=True, help="Path to the .xlsx workbook.")
    parser.add_argument(
        "--mode",
        choices=["privacy_safe", "local_private"],
        default="privacy_safe",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Output path for the draft manifest YAML.",
    )
    parser.add_argument(
        "--workbook-version",
        default="v_unknown",
        help="workbook_version string for the draft manifest.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print aggregate structural diagnostics; do not write any file.",
    )
    args = parser.parse_args(argv)

    workbook = Path(args.workbook)
    discovery = discover_investment_summary(workbook)
    draft = build_draft_position_manifest(
        discovery,
        mode=args.mode,
        workbook_version=args.workbook_version,
    )
    diag = render_position_diagnostics(discovery, draft)

    if args.dry_run:
        print(diag)
        return 0

    if args.out is None:
        print(diag)
        print()
        print(
            "(No --out path provided; manifest not emitted. Re-run with "
            "--out PATH to write the draft.)"
        )
        return 0

    out = Path(args.out)
    if args.mode == "local_private":
        _check_local_private_path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(draft.manifest_yaml_text, encoding="utf-8")

    print(diag)
    print()
    print(f"draft manifest written: {out}")
    if args.mode == "privacy_safe":
        print("  (privacy_safe mode — sheet names redacted.)")
    else:
        print("  (local_private mode — real names preserved; do NOT commit.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
