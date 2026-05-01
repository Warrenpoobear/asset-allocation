# Session Conventions for asset-allocation

This repo is built per [SPEC.md](SPEC.md). Read it before making changes.

## Phase
Phase 1 — Spine. No external optimizers (only stub allocator). Allowed deps: numpy, pandas, pydantic>=2, pyyaml, pyarrow, jinja2, pytest, ruff.

## Architecture rules
- Quarterly ledger is the spine. Every flow lands on it.
- Schemas first (pydantic v2). Configs are validated; failure is loud.
- Adapter contracts in §9 of SPEC are mandated; stubs are reference implementations.
- Determinism: every run writes `data/processed/runs/<run_id>/manifest.json`. Reruns with identical inputs produce byte-identical `ledger.parquet`.
- Phase gates are real. Do not start Phase 2 until Phase 1 exit gate is green.

## Local commands

```
pip install -e .
pip install -r requirements-dev.txt
pytest -q
python scripts/run_sfo_study.py --config configs/base.yaml
```

## What NOT to do
- Don't hard-code 60/40 anywhere. Stub allocator reads `configs/public_allocation.yaml::stub_weights`.
- Don't introduce a base class for a single subclass beyond what §9 of SPEC mandates.
- Don't pull in optimizer libs in Phase 1.
- Don't overwrite an existing run directory; reruns create a new `run_id`.
