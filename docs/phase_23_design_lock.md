# Phase 23 — Design Lock — PE Real-Data Commitment Input Layer

> **Status: LOCKED, pre-implementation.** Tightenings applied per reviewer
> 2026-05-04. Implementation lands in a separate commit. No live client
> data, no fund/manager names, no dollar values consumed by tests or
> committed artifacts at any point in this phase.

## Standing constraint inheritance

This phase inherits the `docs/phase_19_design_constraints.md` standing
constraint and the worksheet-alignment dimensions (timing / flow / source /
reconciliation). Phase 23 does **not** alter the Phase 20 source precedence:

```
explicit_config > cashflow_workbook > pe_pacing_model > unavailable
```

The commitment book feeds the `pe_pacing_model` side only. Workbook stays
the operating-forecast spine.

## One-line goal

Build a generic, gitignored, client-data-shaped commitment-book input
layer that supplies per-fund commitment **plan**, **current actuals
snapshot**, and **monthly actuals history** to the deterministic pacing
engine, the Phase 19 capital-call bridge, and reports — without changing
Phase 19 / 20 / 21 behavior on existing fixtures, and without consuming
any live client data in this phase.

## Where Phase 23 slots

```
Existing:   cfg.pe_pacing.funds (FundConfig)        → TA/STAIRS adapter → projection
Existing:   workbook capital_call lines              → Phase 20 reconciliation
Existing:   Phase 21 reconciliation gates            → unchanged
Phase 23:   PE commitment book (new, gitignored loader, optional)
              ├─ plan layer:           per-fund commitment / vintage / sleeve / entity / curves
              ├─ actuals snapshot:     current as-of state (called/distributed/NAV/unfunded)
              └─ monthly actuals:      historical month × fund × entity series
            EntityRegistry adapter (new, in scope, gitignored sources)
```

The book is the **client truth surface** when supplied. `FundConfig`
remains the **synthetic-fixture twin** for tests. When the book is absent,
all Phase 19 / 20 / 21 behavior is byte-stable on existing fixtures.

## Schema additions (new module: `src/aa_model/ingestion/schemas_pe_commitments.py`)

All dollar amounts use `Decimal` in the new schema. Conversion to `float`
happens **only** at the boundary into existing PE projection code that
requires `float`. Existing `FundConfig` is **not refactored** in this
phase.

### Stable identity

```
fund_key: str                       # required, URL-safe, globally unique within book
fund_id: str | None                 # external/client-system id, optional, globally unique when set
fund_name: str | None               # display only, local/private
manager_id: str | None              # external/client-system id, optional, globally unique when set
manager_name: str | None            # display only, local/private
entity_id: str                      # required, must resolve via EntityRegistry adapter
```

`fund_key` is the primary key for cross-table joins (plan ↔ actuals
snapshot ↔ monthly history). Display names are not stable identifiers and
are never used as keys.

### `PEFundCommitmentRecord` (plan table — one row per fund)

| field | type | constraint |
|---|---|---|
| `fund_key` | str | required, URL-safe, globally unique |
| `fund_id` | str \| None | globally unique when set |
| `fund_name` | str \| None | display only |
| `manager_id` | str \| None | globally unique when set |
| `manager_name` | str \| None | display only |
| `entity_id` | str | required, resolves via EntityRegistry |
| `vintage_year` | int | 1990 ≤ y ≤ 2060 (matches workbook epoch guard) |
| `commitment_usd` | Decimal | gt 0 |
| `sleeve` | Literal[pe_buyout/pe_venture/pe_growth/pe_credit/pe_re/pe_infra/pe_secondary] | matches Phase 9 strategy↔sleeve table |
| `commitment_period_start` | date \| None | required if `commitment_period_end` present |
| `commitment_period_end` | date \| None | ≥ start when both set |
| `expected_final_liquidation_date` | date \| None | ≥ commitment_period_end when both set |
| `status` | Literal[active/committed/exited/planned] | matches Phase 9 semantics |

### `PEFundActualsSnapshot` (current-snapshot table — one row per fund per as-of date)

| field | type | constraint |
|---|---|---|
| `fund_key` | str | foreign-key into commitment-book |
| `as_of_date` | date | snapshot grain |
| `called_to_date_usd` | Decimal \| None | ≥ 0; ≤ commitment_usd |
| `distributed_to_date_usd` | Decimal \| None | ≥ 0 |
| `nav_usd` | Decimal \| None | ≥ 0 |
| `unfunded_commitment_usd` | Decimal \| None | computed = commitment - called when both present, else None — never synthesized |
| `source` | Literal[client_statement/manager_portal/k1/audit/internal] | provenance |
| `confidence` | Literal[actual/contractual/estimated] | authored |
| `notes` | str \| None | free-text, never propagated to ledger |

### `PEActualsMonthlyRecord` (historical monthly series — one row per fund × month)

Designed for Archway monthly Position Reports and equivalent monthly
sources. Aggregation to quarter happens downstream; the loader stores
monthly grain.

| field | type | constraint |
|---|---|---|
| `fund_key` | str | foreign-key into commitment-book |
| `entity_id` | str | resolves via EntityRegistry |
| `period_month` | date | first day of month, `YYYY-MM-01` |
| `call_usd` | Decimal \| None | ≥ 0 |
| `distribution_usd` | Decimal \| None | ≥ 0 |
| `nav_usd` | Decimal \| None | ≥ 0 |
| `unfunded_usd` | Decimal \| None | ≥ 0; never synthesized |
| `source` | Literal[client_statement/manager_portal/k1/audit/internal/archway_monthly] | provenance |
| `confidence` | Literal[actual/contractual/estimated] | authored |

`(fund_key, period_month)` is unique. No partial inference: if a field is
unobserved, it stays `None`.

### `CurveAssumptions` (deterministic, optional override)

| field | type | constraint |
|---|---|---|
| `rate_of_contribution` | list[Decimal] | sums ≤ 1.0; first 4 quarters' weights |
| `bow` | Decimal | gt 0 |
| `yale_distribution` | bool | TA shape selector |
| `lifetime_quarters` | int | 20 ≤ q ≤ 60 |

Curve precedence:

```
fund-level explicit override  >  book-level default curve  >  existing pe_pacing defaults
```

**No partial curve inference.** A fund-level override must supply the
complete `CurveAssumptions` record; partial override is a validation
error.

No stochastic fields. No Monte Carlo seeds. No fee-economics consumption
(Phase 9 keeps `_FeeModelConfig` as metadata-only).

### `PECommitmentBookManifest` (top-level)

| field | type | constraint |
|---|---|---|
| `book_version` | str | URL-safe; goes into `config_hash` |
| `as_of_date` | date | book-level snapshot date; goes into `config_hash` |
| `expected_filenames` | list[str] | matches client CSV/YAML names |
| `funds` | list[PEFundCommitmentRecord] | `fund_key` globally unique |
| `actuals_snapshot` | list[PEFundActualsSnapshot] | every `fund_key` foreign-key validates |
| `actuals_monthly` | list[PEActualsMonthlyRecord] | every `fund_key` foreign-key validates; `(fund_key, period_month)` unique |
| `entity_registry_ref` | str | path to local EntityRegistry source |
| `default_pacing_curve` | CurveAssumptions \| None | book-level default |

## EntityRegistry adapter (in Phase 23 scope)

Thin local-private adapter at
`src/aa_model/ingestion/entity_registry.py`. Validates that every
`entity_id` referenced by the commitment book resolves against one of:

```
- Phase 14 local workbook manifest entity_ids (configs/workbook_v7_manifest_local.yaml)
- Phase 15 position manifest entity_ids (configs/investment_summary_manifest_local.yaml)
- Local entity registry CSV/YAML (configs/entity_registry_local.yaml — gitignored)
```

Resolution precedence: workbook manifest → position manifest → local
registry. First hit wins. Multi-source mismatch (same `entity_id`,
inconsistent attributes) surfaces in diagnostics, not raised.

Local files stay gitignored. The adapter is purely a lookup; it does
not re-author entity meaning.

## Loader contract

Three file formats, all gitignored under `data/external/` or `configs/`:

1. **CSV bundle** (preferred for client onboarding):
   - `data/external/pe_commitment_book_local.csv` — plan rows
   - `data/external/pe_actuals_snapshot_local.csv` — current snapshot rows
   - `data/external/pe_actuals_monthly_local.csv` — monthly history rows
2. **YAML** (single file for programmatic edits):
   - `configs/pe_commitment_book_local.yaml`
3. **Mixed** — plan + snapshot in YAML, monthly history in CSV (allowed for
   onboarding workflows where Archway extracts ship as CSV).

Loader at `src/aa_model/ingestion/pe_commitments.py`:

```
def load_pe_commitment_book(
    book_path: Path,
    *,
    actuals_snapshot_path: Path | None = None,
    actuals_monthly_path: Path | None = None,
    entity_registry: EntityRegistry,
) -> PECommitmentBookManifest: ...
```

Path-safety: refuses non-`_local.{csv,yaml}` and refuses outputs outside
`data/external/` / `configs/`. Mirrors `discover_workbook` local-private
guard.

## Reconciliation grain

`(quarter, fund_key, entity_id, source)` is the four-tuple that pins
every cross-source delta. Sources:

```
- client_fund_commitments     (Phase 23 — new)
- pe_pacing_model              (existing — fed by client_fund_commitments when book loaded)
- cashflow_workbook            (existing — Phase 20)
- explicit_config              (existing — Phase 19)
```

Phase 23 adds **one** new diagnostic surface (does NOT modify Phase 20
reconciliation):

`PECommitmentBookDiagnostics`:

| field | meaning |
|---|---|
| `book_loaded` | bool — was a book supplied |
| `book_version` | str \| None |
| `as_of_date` | date \| None — book's snapshot |
| `funds_count` | int |
| `funds_with_snapshot_count` | int |
| `funds_with_monthly_history_count` | int |
| `monthly_history_period_min` / `_max` | dates |
| `unfunded_sum_usd` | Decimal \| None — Σ unfunded across active+committed; None if any missing |
| `entity_resolution_failures` | list[(fund_key, entity_id)] — fail-loud |
| `commitment_period_violations` | list[(fund_key, reason)] |
| `actuals_snapshot_freshness_days` | dict[fund_key → days_since_as_of] (derived) |
| `staleness_flags` | list[(fund_key, days)] — derived where freshness > threshold (config-shaped, default 90d) |
| `pacing_curve_overrides_count` | int |
| `monthly_history_completeness` | dict[fund_key → (months_present, months_expected, gaps)] |

These flow into the report under a new `## PE commitment book (advisory)`
section. Section is omitted entirely when `book_loaded=False`.
Default-fixture runs stay byte-stable.

## Confidence and staleness

`confidence` enum (authored):

```
actual         — observed flow / mark from authoritative source
contractual    — known per executed agreement
estimated      — best-available approximation
```

`stale` is **not** an authored confidence value. Staleness is a derived
condition, computed from:

```
staleness_flag = (run_as_of_date - actuals_snapshot.as_of_date) > staleness_threshold_days
```

Default threshold: 90 days. Configurable per
`PECommitmentBookDiagnosticsConfig` (added in same phase). Threshold
goes into `config_hash`.

## Decimal vs float boundary

```
PE commitment book schema:                Decimal everywhere
EntityRegistry adapter:                    Decimal pass-through
PECommitmentBookDiagnostics:               Decimal
Conversion → float:                        ONLY when feeding
                                           cfg.pe_pacing.funds shape
                                           into existing TA/STAIRS adapter
```

Existing `FundConfig` (currently `float`) is **not** refactored in this
phase. A future tightening pass may converge the two; not in scope here.

## Determinism contract

- Schema is pure data; CSV/YAML parsing produces a canonical dict.
- `book_version` and `as_of_date` are folded into `config_hash`. Renaming
  the book or shifting the snapshot date invalidates `run_id` correctly.
- Loader is idempotent: same files in → byte-identical manifest dump.
- No clock reads (`datetime.now()` not used). The only time anchor is
  `as_of_date` from the book.
- Monthly history is sorted by `(fund_key, period_month)` before manifest
  emission; ordering is deterministic.

## Projection anchoring (deferred from Phase 23)

Phase 23 **loads and diagnoses** actuals history. The pacing engine is
**not** modified to seed/anchor projections from observed historical
calls/distributions in this phase. That seeding is a follow-up phase
(provisionally Phase 24), to be designed after the book loader is in
production and at least one monthly extract has been ingested locally.

If, during implementation, the seeding turns out to be small enough to
land alongside the loader without extending blast radius, the design
will be re-tightened before merging that piece — not silently scoped in.

## What Phase 23 is **NOT**

- Not a Phase 19 / 20 / 21 status change. L19 stays PARTIALLY RESOLVED.
  L20 stays RESOLVED.
- Not a Monte Carlo input layer.
- Not a stochastic pacing upgrade.
- Not a fee-economics consumption layer (Phase 9 metadata stance preserved).
- Not a recommitment optimizer.
- Not a manager-level coupling override.
- Not a secondary-sale / haircut model.
- Not a tax-aware layer.
- Not a workbook mutation. Workbook stays operating-forecast spine.
- Not a `FundConfig` refactor.
- Not a projection anchoring change (deferred — see above).
- Not a live-data ingestion phase. Only synthetic fixtures committed.

## Tests planned (synthetic fixtures only)

Schema (10):

1. `commitment_usd > 0` enforced.
2. `vintage_year` epoch guard (1990 ≤ y ≤ 2060).
3. `sleeve` ↔ `strategy` table consistency (re-uses Phase 9 table).
4. `status` enum accepts the four Phase 9 values.
5. `fund_key` global uniqueness within a book.
6. `fund_id` global uniqueness when any fund sets it.
7. `manager_id` global uniqueness when any fund sets it.
8. `commitment_period_start ≤ commitment_period_end` when both set.
9. `expected_final_liquidation_date ≥ commitment_period_end` when both set.
10. Actuals foreign-key validation: every snapshot / monthly row's
    `fund_key` resolves into the plan table.

EntityRegistry (4):

11. Resolution precedence: workbook → position → local.
12. Multi-source mismatch surfaces as diagnostic, does not raise.
13. Unresolvable `entity_id` surfaces in
    `entity_resolution_failures`.
14. Local registry `_local` file path-safety (refuses non-`_local`
    paths).

Loader (5):

15. CSV bundle load (plan + snapshot + monthly).
16. YAML book load (plan + snapshot inline; monthly absent).
17. Mixed-format load (YAML plan + CSV monthly).
18. Path-safety refusal of non-`_local.{csv,yaml}` outputs.
19. Missing-file diagnostics (snapshot absent → `funds_with_snapshot_count=0`).

Diagnostics (5):

20. `book_loaded=False` → report section omitted.
21. Partial actuals → `unfunded_sum_usd=None`, listed as missing.
22. Staleness derivation: `staleness_threshold_days=90` default; values
    older than threshold flagged in `staleness_flags`.
23. `monthly_history_completeness` reports per-fund gaps correctly.
24. `pacing_curve_overrides_count` matches funds with explicit curves.

Determinism (2):

25. Same input → byte-identical manifest dump.
26. Different `book_version` or `as_of_date` → different `config_hash`.

End-to-end (2):

27. Default fixture (no book) → byte-identical pre-Phase-23 ledger /
    manifest / report.
28. Book supplied with synthetic fixture → new section renders, ledger
    schema unchanged, Phase 19 / 20 / 21 outputs unchanged.

Total: ~28 tests.

## L-status implications

- L1 — unchanged.
- L19 — unchanged. Stays PARTIALLY RESOLVED. Phase 23 is independent of
  workbook row-classification authoring.
- L20 — unchanged. Stays RESOLVED. Workbook still wins for
  `next_12m_capital_calls_usd`.
- L21 (PE pacing realism) — narrows. The standing limitation that PE
  pacing inputs are config-driven synthetic gets one rung less abstract:
  with a book loaded, pacing inputs are client-derived. Curves remain
  deterministic. **Not** marked RESOLVED — full resolution requires
  projection anchoring (deferred phase).

## Privacy posture (load-bearing)

- All client commitment files are gitignored under `data/external/` or
  `configs/`.
- Live values, dollar amounts, fund names, manager names, person
  identifiers **never paste into chat**.
- Tests use synthetic fixtures only (e.g.
  `synthetic_fund_alpha`, `synthetic_manager_a`).
- Committed docs (this file, `MODEL_DOCUMENTATION.md` updates) carry no
  client values, no real fund/manager names, and no real entity_ids.
- The loader confirms gitignore membership BEFORE reading or writing any
  local-private path.

## Locked design choices

- `fund_key` is the stable primary key. `fund_name` and `manager_name`
  are display/local-private fields only.
- Plan and actuals are separate tables. Actuals split into current
  snapshot and monthly history.
- `Decimal` in the new schema; conversion to `float` only at adapter
  boundary; existing `FundConfig` not refactored.
- `confidence` enum is `actual` / `contractual` / `estimated`. Staleness
  is derived from `as_of_date` and threshold, not authored.
- Curve precedence: fund-level explicit override > book-level default >
  existing `pe_pacing` defaults. No partial curve inference.
- EntityRegistry adapter is in scope; sources stay gitignored.
- Phase 20 source precedence (`explicit_config > cashflow_workbook >
  pe_pacing_model > unavailable`) **unchanged**. Book feeds the
  `pe_pacing_model` side only.
- Projection anchoring from monthly actuals is **deferred** from
  Phase 23.
- No live client data consumed at any point in this phase.

## Implementation gating

Implementation is gated on:

1. This design lock committed (docs-only).
2. No reviewer tightening in flight.
3. Synthetic fixtures authored before any production code.
4. EntityRegistry adapter implemented before book loader (loader depends
   on registry).

After implementation, a separate `chore(lint)` sweep follows the
project's standard cadence; report-line wording stabilization may occur
in a docs-only follow-up commit.
