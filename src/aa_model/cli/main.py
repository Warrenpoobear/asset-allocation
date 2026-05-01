"""``aa-model`` CLI entry point. Registered in pyproject.toml [project.scripts]."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from aa_model.integration.orchestrator import run_orchestrator


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aa-model",
        description="SFO asset allocation study runner.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the study end-to-end")
    run.add_argument("--config", required=True, type=Path, help="path to base.yaml")
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="validate configs + compute hashes, print manifest preview, write nothing",
    )
    run.add_argument(
        "--invocation-id",
        default=None,
        help=(
            "explicit per-invocation suffix for run_id; default = UTC timestamp + "
            "4-char hex nonce. Override to reproduce a specific historical run dir."
        ),
    )
    return parser


def _run(args: argparse.Namespace) -> int:
    result = run_orchestrator(args.config, dry_run=args.dry_run, invocation_id=args.invocation_id)
    print(f"run_id:        {result.run_id}")
    print(f"output_dir:    {result.output_dir}")
    print(f"rows:          {len(result.ledger)}")
    print(f"config_hash:   {result.manifest.config_hash}")
    print(f"fixtures_hash: {result.manifest.fixtures_hash}")
    if args.dry_run:
        print("--- manifest preview (dry run) ---")
        print(json.dumps(result.manifest.to_dict(), sort_keys=True, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        return _run(args)
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
