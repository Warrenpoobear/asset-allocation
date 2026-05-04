# Hermes Tracking — Asset Allocation Model

> Stable entry point for Hermes/OpenWebUI dashboards. Update sections marked
> `<!-- auto -->` from CI/cron; update prose sections by hand at phase boundaries.
> Last manual sync: 2026-05-02.

---

## Current State <!-- auto -->

- Current phase: **Phase 23 design lock — PE real-data commitment input layer (locked; implementation pending)**
- Latest commit: `f81ff43` — docs(model): Phase 23 design lock — PE real-data commitment input layer
- Branch: `main` (0 ahead, 0 behind origin)
- Last pushed: 2026-05-04 15:25:32 -0400 (`f81ff43`)
- Working tree: clean
- Tests: **371 passed** (`.venv/bin/pytest -p no:warnings --ignore=tests/test_transaction_cost_summary.py`; 4 cvxportfolio-gated omitted)
- Ruff: clean across `src tests scripts` (post-Phase-22/14.3 lint sweep at `82f84b4`)
- Latest run set: `data/processed/runs/aa-dc07a16dffa9-96451d89bace-20260504T204759Z-3e37-crisis_correlation`

Recent series (10 commits since last tracker sync at `303c73e`):
- `f81ff43` docs(model): Phase 23 design lock — PE real-data commitment input layer
- `07327e1` docs(sync): record lint sweep 82f84b4; correct Phase 14.3 test count
- `82f84b4` chore(lint): sweep Phase 14.3 + Phase 22
- `c92642a` docs(roadmap): model buildout sequencing and deferred gates
- `01ff365` docs(pe-pacing): design constraints for three-layer pacing model
- `5fdf68b` docs(l19): tighten Phase B validation spec for pilot worksheet
- `f3169a9` docs(l19): L19 completion workflow — row classification plan
- `d66e01a` docs(l20): mark L20 resolved — workbook-driven capital-call obligation
- `82ce2ec` feat(phase-14.3): workbook row_range / data-region scoping
- `a6db960` feat(phase-22): manager terms consumer / diagnostic layer

## Open Gates

- [ ] **Phase 23 implementation** — design locked at `f81ff43` (PE real-data commitment input layer). Implementation pending.
- [ ] **Phase 7 STAIRS PE adapter** — design locked at `993a751`. Implementation
      blocked until tests + invariants drafted alongside `pe/stairs_adapter.py`.
- [ ] **Phase 10 L14 transaction-cost diagnostics** — partially resolved at
      `49544f7` (report section); 4 cvxportfolio-gated tests still skipped /
      ModuleNotFoundError when extra not installed.
- [ ] **Phase 19.1 design lock** — SUPERSEDED by Phase 20 (`a5114f6`) +
      Phase 21 (`412e1ee`). `docs/phase_19_1_design_lock.md` retained as
      traceability record. Phase 20 implemented reconciliation; Phase 21
      added configurable gates (advisory / warning / requires_override /
      hard_fail). The 3 missing-test follow-ups from the superseded lock
      remain open until back-checked against Phase 21's gate behavior.
- [ ] **L19 spending-base realism** — partially resolved (Phase 12/12.5/13/14
      at MODEL_DOCUMENTATION.md:1376). Remaining gap: real-workbook
      validation pending.
- [ ] **MODEL_DOCUMENTATION.md sweep** — confirm L16 status flips to
      `[RESOLVED 2026-05-02, Phase 11]` and that L19 caveat references are linked.
- [ ] **Determinism check** — re-run identical inputs must produce byte-identical
      `ledger.parquet` (SPEC §determinism).

## Active Limitations (open)

| ID  | Title                                                          | Doc line |
| --- | -------------------------------------------------------------- | -------- |
| L1  | PE timing scenarios mechanically affect returns                | 475      |
| L2  | Returns are NAV-dependent, not regime-dependent                | 490      |
| L3  | Stub-vs-riskfolio weights are not numerically comparable       | 502      |
| L5  | `source` as a PE-leg pairing key is fragile                    | 543      |
| L7  | Smoothing rule with `weight=0` freezes spending                | 586      |
| L9  | Heavy install footprint for `riskfolio` extra                  | 615      |
| L10 | `/mnt/c` filesystem unsuitable for `.venv`                     | 626      |
| L11 | Synthetic 2-row dummy returns frame in Riskfolio adapter       | 637      |
| L12 | Non-fatal "convert cov to PSD" warning                         | 694      |
| L14 | Only linear transaction cost is modeled (partial resolve)      | 984      |
| L17 | Cross-engine metric comparability is not meaningful            | 839      |
| L19 | Spending-base realism — partial; pending real-workbook validation | 1376  |

## Resolved Limitations

| ID  | Phase    | Resolved    | Title                                                  |
| --- | -------- | ----------- | ------------------------------------------------------ |
| L4  | Phase 5  | 2026-05-02  | Riskfolio default CMA fallback placeholder             |
| L6  | Phase 6  | 2026-05-02  | `correlation_shock` scenario omitted                   |
| L8  | Phase 8  | 2026-05-02  | Rebalancer treated PE as a liquid sleeve               |
| L13 | Phase 4b | 2026-05-02  | Cvxportfolio adapter had no path dependence            |
| L15 | Phase 4a | 2026-05-01  | Owl reacted to forecasted NAV, not realized NAV        |
| L16 | Phase 11 | 2026-05-02  | Owl scale-invariant in initial NAV (absolute guardrail)|
| L18 | Phase 4a | 2026-05-01  | Owl misread inflation shock as headroom                |
| L20 | Phase 20+21 | 2026-05-03 | PE call obligation — workbook reconciliation + gates  |

## Do Not Violate (governance invariants)

- **Ledger is sole state spine.** No sidecars, no hidden state.
- **CMA is baseline prior; scenarios are perturbations.** CMA baseline immutable.
- **No implementation before design lock.** Each phase ships a `docs(model): lock Phase N`
  commit before any implementation commit.
- **MODEL_DOCUMENTATION.md must be updated for any behavior change.** Doc-as-spec.
- **Determinism: identical inputs → byte-identical `ledger.parquet`.**
- **No overwriting run directories** — every run gets a new `run_id`.
- **No optimizer libs in Phase 1.** Phase-1 stub allocator only.
- **No STAIRS before L6 correlation_shock.** [SATISFIED — L6 resolved Phase 6.]
- **PROJECT_SCOPE.md is authoritative** for Wake Robin reference architecture
  (locked 2026-05-02 at `69cae5c`).

## Standard Commands

```bash
# Tests (clean, no warnings — 225 expected; 4 cvxportfolio-gated tests need extra)
.venv/bin/pytest -p no:warnings

# Lint
.venv/bin/ruff check src tests scripts
.venv/bin/ruff format --check src tests scripts

# Run scenario sweep
.venv/bin/python scripts/run_sfo_study.py --config configs/base.yaml

# Determinism: rerun and diff manifest hashes
.venv/bin/python scripts/run_sfo_study.py --config configs/base.yaml
# then compare two manifest.json files under data/processed/runs/
```

## Hermes Automations

| Cadence            | What runs                                                    |
| ------------------ | ------------------------------------------------------------ |
| Daily 18:00 ET     | repo health: git status, pytest -q, ruff check, doc-diff     |
| On every push      | new-commits summary, behavior-change → doc-update enforcement|
| Before phase gate  | design-section presence check, open-L items, required tests  |

Cron jobs are registered separately in Hermes (see `cronjob list`).

## Dashboard Cards (for Open WebUI / Hermes UI)

### Asset Allocation Model — Status
```
Current phase:        Phase 21 landed (reconciliation gates / policy thresholds)
Last pushed commit:   c142ffd  (Phase 21 lint sweep, +1 ahead pending push)
Tests:                353 passed
Ruff errors:          0 (clean across src/tests/scripts post-Phase-21 sweep at c142ffd)
Open limitations:     13  (L20 effectively resolved across Phase 20+21)
Resolved limitations: 7
Next gated task:      L20 follow-up tests (sign-mismatch, per-quarter mixed window, determinism); MODEL_DOCUMENTATION.md L20 status verification
Last model-doc update: 2026-05-03 (L19 status under Phase 14)
Latest run:           20260503T045900Z (crisis_correlation)
```

### Governance Gates
```
[x] Design lock before implementation       (Phase 7,8,9,10,11 all locked)
[x] PROJECT_SCOPE.md authoritative          (locked 69cae5c)
[ ] MODEL_DOCUMENTATION updated post-impl   (pending L16/L19 sweep)
[x] CMA baseline immutable                  (verified L4 resolution)
[x] Ledger remains sole state spine         (architectural)
[x] L6 correlation_shock before STAIRS      (Phase 6 resolved L6)
```

### Numerical Health
```
[x] Determinism check         (Phase 1 gate, verified)
[x] Ledger invariants         (test_schemas.py, test_orchestrator.py)
[x] Spend uniqueness          (test_spending_rules.py)
[ ] PSD validation            (L12 warning still emitted — non-fatal)
[x] Cost-aware λ diagnostics  (Phase 4b)
[x] Scenario shock validation (test_scenario_builder.py, test_sweep.py)
[x] Owl scale-invariance      (Phase 11 — absolute-dollar guardrail)
```

---

## Update protocol

- **Auto sections** (`<!-- auto -->`) — overwritten by daily Hermes cron and post-push hook.
- **Active/Resolved Limitations** — updated when a commit message contains `resolves L#`
  or doc edit changes the `Status:` line of an `L#` heading.
- **Gates / Do Not Violate** — only edited when SPEC.md changes. Treat as protected.
- **Phase prose** — edit at phase-boundary commits (`docs(model): lock Phase N`).
