# Hermes Tracking — Asset Allocation Model

> Stable entry point for Hermes/OpenWebUI dashboards. Update sections marked
> `<!-- auto -->` from CI/cron; update prose sections by hand at phase boundaries.
> Last manual sync: 2026-05-02.

---

## Current State <!-- auto -->

- Current phase: **Phase 20 — PE call-obligation reconciliation to cash-flow worksheet (landed)**
- Latest commit: `2be73ca` — chore(lint): ruff --fix + format sweep for Phase 20 (a5114f6)
- Branch: `main` (0 ahead, 0 behind origin)
- Last pushed: 2026-05-03 14:00:xx -0400 (pending push)
- Working tree: clean
- Tests: **347 passed** (`.venv/bin/pytest -p no:warnings`; +6 from Phase 20)
- Ruff: clean across `src tests scripts` (post-Phase-20 lint sweep at `2be73ca`)
- Latest run set: `data/processed/runs/aa-e3f6ab2337ad-96451d89bace-20260503T180424Z-*`

Recent series (7 commits since 2026-05-03 12:00 ET):
- `2be73ca` chore(lint): ruff --fix + format sweep for Phase 20 (a5114f6)
- `5ff335f` docs(tracking): post-Phase-19 / Phase-19.1-locked sync
- `a5114f6` Phase 20 / L20: PE call-obligation reconciliation to cash-flow worksheet
- `e7e81ec` docs(model): Phase 19.1 design lock — workbook capital-call reconciliation [SUPERSEDED by Phase 20]
- `9c250f4` chore(lint): ruff --fix + format sweep for Phase 19 (af58dd3)
- `0ad2420` docs(model): Phase 19 design prompt — PE call-obligation reconciled to workbook
- `8a150c5` docs(claude): standing constraint — worksheet-aligned spending/liquidity/PE pacing

## Open Gates

- [ ] **Phase 7 STAIRS PE adapter** — design locked at `993a751`. Implementation
      blocked until tests + invariants drafted alongside `pe/stairs_adapter.py`.
- [ ] **Phase 10 L14 transaction-cost diagnostics** — partially resolved at
      `49544f7` (report section); 4 cvxportfolio-gated tests still skipped /
      ModuleNotFoundError when extra not installed.
- [ ] **Phase 19.1 design lock** — SUPERSEDED by Phase 20 (`a5114f6`).
      `docs/phase_19_1_design_lock.md` retained as traceability record;
      Phase 20 chose `category="capital_call"` string boundary instead
      of new `capital_call_candidate` field, and 10/25% delta bands
      instead of 5%/sign-mismatch. Open follow-ups (3 missing tests,
      sign-mismatch handling, schema tightening) noted in the
      superseded lock file.
- [ ] **L20 doc-line** — Phase 20 added `### L20 —` heading to
      MODEL_DOCUMENTATION.md (verify in next sweep).
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
| L20 | PE call obligation — workbook reconciliation (resolved Phase 20; 3 follow-up tests pending) | (Ph20) |

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
Current phase:        Phase 20 landed (PE call-obligation reconciled to workbook)
Last pushed commit:   2be73ca  (Phase 20 lint sweep)
Tests:                347 passed
Ruff errors:          0 (clean across src/tests/scripts post-Phase-20 sweep at 2be73ca)
Open limitations:     13  (L20 effectively resolved; 3 follow-up tests pending)
Resolved limitations: 7
Next gated task:      L20 follow-up tests (sign-mismatch, per-quarter mixed window, determinism); confirm L20 status line in MODEL_DOCUMENTATION.md
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
