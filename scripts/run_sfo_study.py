"""End-to-end SFO study runner for Phase 1.

Usage::

    python scripts/run_sfo_study.py --config configs/base.yaml
    python scripts/run_sfo_study.py --config configs/base.yaml --dry-run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow direct ``python scripts/run_sfo_study.py`` from the repo root without
# requiring an editable install.
_REPO_SRC = Path(__file__).resolve().parent.parent / "src"
if _REPO_SRC.is_dir() and str(_REPO_SRC) not in sys.path:
    sys.path.insert(0, str(_REPO_SRC))

from aa_model.integration.orchestrator import run_orchestrator  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the SFO Phase 1 study end-to-end.")
    parser.add_argument("--config", required=True, type=Path, help="path to base.yaml")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate configs and print manifest preview without writing outputs",
    )
    parser.add_argument(
        "--invocation-id",
        default=None,
        help="explicit per-invocation suffix for run_id (default = UTC ts + nonce)",
    )
    args = parser.parse_args(argv)

    result = run_orchestrator(args.config, dry_run=args.dry_run, invocation_id=args.invocation_id)
    print(f"run_id:     {result.run_id}")
    print(f"output_dir: {result.output_dir}")
    print(f"rows:       {len(result.ledger)}")
    print(f"config_hash:   {result.manifest.config_hash}")
    print(f"fixtures_hash: {result.manifest.fixtures_hash}")
    if args.dry_run:
        print("(dry run — no files written)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
