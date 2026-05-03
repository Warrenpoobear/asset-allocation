# Phase 14 — Workbook Cross-walk Workflow

> Generic operational workflow for turning the committed manifest scaffold
> into a runnable, locally-classified workbook ingestion. Carries no live
> data. Future sessions should follow this rather than re-deriving the steps
> from chat history.

## Purpose

The Phase 14 ingestor turns a workbook of entity-level cash-flow forecasts
into a normalized stream of `CashFlowLineRecord` rows that downstream phases
consume:

```
workbook.xlsx
  → Phase 14 ingestor (governed by WorkbookManifestConfig)
    → list[EntityRecord] + list[CashFlowLineRecord]
      → Phase 13 producer bridge (when distributable_candidate=True)
        → distribution_inflow ledger rows
          → Phase 12.5 distributable_income spending base
            → Phase 18 SpendingBaseBreakdown bridge
              → Phase 16 liquidity coverage
                → Phase 20 worksheet ↔ PE-pacing reconciliation
                  → Phase 21 reconciliation gates
```

The model is the structure-and-validation engine. The workbook is the
operating-forecast spine. The model never infers economic meaning — every
classification is human-authored upstream.

## Privacy posture (PROJECT_SCOPE §5.3 + Phase 14 RT4)

These rules are non-negotiable:

1. **The workbook is never committed.** It lives outside the repository.
2. **Live values, row labels, and person/entity identifiers stay local.** The
   committed scaffold uses `<TODO_*>` placeholders for sheet names that may
   decode to family-internal identifiers. Real names are filled in only in
   the gitignored `_local.yaml` variant.
3. **Tests use synthetic fixtures only.** No real workbook fragment is ever
   committed to `tests/` or referenced from a tracked test fixture.
4. **Discovery / cross-walk artifacts go to `data/external/`** (gitignored
   per `.gitignore`).
5. **Chat output is aggregate-only.** When reporting from a discovery or
   classification run, share only counts, not row labels, sheet names,
   values, or identifiers — unless the user explicitly opts into a
   higher-visibility posture.

## Phase 14 schema reference

Allowed enum values from `src/aa_model/ingestion/schemas.py`:

| field | values |
|---|---|
| `entity_type` | `operating_llc`, `holding_llc`, `trust_crut`, `trust_family`, `trust_gift`, `trust_gst`, `individual_account`, `real_estate_partnership`, `opco`, `family_aggregate` |
| `cash_flow_role` | `operating`, `investment`, `distribution_only` |
| `layout_type` | `horizontal_quarter`, `display_only` |
| `direction` | `inflow`, `outflow` (sign-consistent with `amount_usd`) |
| `domain` | `real_estate`, `opco`, `land`, `development`, `portfolio`, `entity` (REQUIRED when `distributable_candidate=True`) |
| `recurrence_type` | `recurring`, `one_time`, `unknown` |
| `certainty` | `actual`, `contractual`, `forecast`, `scenario` |
| `period_header_format` | `yyyy_q`, `q_yy`, `q_yyyy`, `calendar_qe` |

Hard constraints:

- `entity_id` and `asset_id` must be URL-safe and contain no colons (reserved
  for the Phase 12.5 source convention `distribution:<domain>:<id>`).
- `entity_id` must be globally unique across `entity_sheets` +
  `re_partnership_sheets`.
- Sheet names must be globally unique across all role buckets.
- `distributable_candidate=True` REQUIRES `domain` to be set on the rule.
- The ingestor enforces direction × sign consistency at construction
  (`inflow` requires `amount_usd >= 0`; `outflow` requires `amount_usd <= 0`).
- Subtotal-pattern rows (matching `total | subtotal | sum | grand total |
  net cash`) are auto-excluded; do NOT add classification rules for them.

## The six-step workflow

```
1. Discovery probe         (Phase 14.2 scraper, structural)
   ↓
2. Cross-walk authoring    (sheet → entity_id mapping)
   ↓
3. Local manifest          (configs/*_local.yaml, gitignored)
   ↓
4. Row classification      (RowClassificationRule per sheet)
   ↓
5. Schema validation       (WorkbookManifestConfig.model_validate)
   ↓
6. Pilot ingestion         (StudyConfig.workbook_ingestion + report)
```

### Step 1 — Discovery probe

Run the Phase 14.2 discovery scraper to detect sheet structure:

```bash
.venv/bin/python -m aa_model.ingestion.discover_workbook \
    --workbook "<absolute path to workbook>" \
    --out configs/workbook_v7_manifest_local.yaml \
    --mode local_private \
    --workbook-version v7
```

Modes:

- `privacy_safe` — redacts non-structural sheet names; safe to share an
  aggregate summary in chat.
- `local_private` — keeps real sheet names; refuses to write to non-gitignored
  paths.

The probe writes:

- a draft local manifest (with structural mappings auto-detected)
- aggregate diagnostics about layout, header rows, period formats, and
  unresolved sheets

### Step 2 — Cross-walk authoring

Build a structural mapping document that links each workbook sheet to a
proposed `entity_id` slot in the scaffold. The cross-walk is local-only.

Recommended artifacts under `data/external/` (all gitignored):

| artifact | purpose |
|---|---|
| `workbook_<v>_layout_summary.csv` | per-sheet header row, header count, format, label rows, subtotal counts |
| `workbook_<v>_row_label_inventory.csv` | every row label with auto-flagged subtotal/ignore/TODO disposition |
| `workbook_<v>_sheet_to_entity.csv` | sheet → proposed entity_id / entity_type / cash_flow_role / role bucket |
| `workbook_<v>_mapping_crosswalk.md` | human-readable cross-walk: scaffold reconciliation, recommended workflow, open risks |

The cross-walk should:

- Match every workbook sheet to one of the scaffold's role buckets.
- Identify which `<TODO_*>` placeholder slot corresponds to each real sheet.
- Flag layout outliers (e.g., a sheet whose header is on a non-default row).
- Flag entity-type ambiguities (e.g., a generic `Trust` suffix that could be
  `trust_family`, `trust_crut`, or `trust_gst`).
- Flag any sheet that the heuristic role detection mis-classified.

### Step 3 — Local manifest

Copy the scaffold and fill placeholders locally:

```bash
cp configs/workbook_v7_manifest.yaml configs/workbook_v7_manifest_local.yaml
git check-ignore -v configs/workbook_v7_manifest_local.yaml
# ↳ confirm it's gitignored before editing
```

In the local manifest:

- Replace each `<TODO_*>` `sheet_name` with the real workbook sheet name.
- Adjust `entity_type` and `cash_flow_role` if the cross-walk flagged
  ambiguity.
- Add per-sheet overrides where layout differs from the manifest defaults:

  ```yaml
  - sheet_name: "<that sheet>"
    entity_id: "<id>"
    entity_type: "<type>"
    display_name: "<label>"
    cash_flow_role: "<role>"
    header_row_index: 3              # per-sheet override
    period_header_format: "yyyy_q"   # per-sheet override
    layout_type: "horizontal_quarter"
  ```

- Do NOT change committed `entity_id` values. They anchor deterministic
  Phase 14 → Phase 13 producer_ids.

### Step 4 — Row classification

For each `horizontal_quarter` entity sheet, author `row_classification_rules`
locally using the row-label inventory artifact as the working file.

Per row, decide:

- **action**: `classify` | `exclude` | `ignore`
  - `classify` — the row becomes a `CashFlowLineRecord` and may feed the
    Phase 13 producer if `distributable_candidate=True`.
  - `exclude` — subtotal / total / sum / net-cash row. Already auto-excluded
    via `subtotal_label_patterns`; explicit excludes are belt-and-braces.
  - `ignore` — section header / spacer row with no numeric data.

For each `classify` row, set:

- `direction`: `inflow` | `outflow`
- `category`: free-text label (operating description)
- `domain`: required when `distributable_candidate=True`; otherwise optional
- `distributable_candidate`: `true` | `false`
- `restricted`: `true` | `false`
- `recurrence_type`: `recurring` | `one_time` | `unknown`
- `certainty`: `actual` | `contractual` | `forecast` | `scenario`

Each rule becomes one `RowClassificationRule` entry on the
`EntitySheetSpec`'s `row_classification_rules` list.

**Authoring order recommendation.** Start with 2–3 sheets that have the
smallest TODO count to validate rule mechanics before scaling to the full
sheet set.

### Step 5 — Schema validation

Validate the local manifest before any ingestion run:

```python
import yaml
from aa_model.ingestion.schemas import WorkbookManifestConfig

data = yaml.safe_load(open("configs/workbook_v7_manifest_local.yaml"))
cfg = WorkbookManifestConfig.model_validate(data)
print("VALID")
print(f"  entity_sheets: {len(cfg.entity_sheets)}")
```

The validator catches:

- duplicate `entity_id` values
- duplicate sheet names across role buckets
- colons in `entity_id` or `asset_id` (reserved for source convention)
- `distributable_candidate=True` without a `domain`
- malformed enum values
- bad `header_row_index` (must be ≥ 1)

### Step 6 — Pilot ingestion

Wire the local manifest into a `StudyConfig.workbook_ingestion` block and run
a study locally. Inspect the `## Workbook ingestion (advisory)` section of
the report for:

- `unmatched_lines_count` — rows that didn't match any classification rule
- `excluded_subtotal_rows` — sanity check vs the row-inventory total
- `unparseable_period_headers` — sheets where the parser couldn't read
  quarter columns (likely needs a per-sheet `header_row_index` override)
- `stale_formula_warnings` — Phase 14 RT1 caveat (cached formula values)
- `board_snapshot_reconciliations` — advisory deltas vs board-snapshot tabs
- `distribution_candidates_by_domain_usd` — what's flowing into Phase 13

## Standing principles to honor

1. **NAV is not liquidity.** Appraisal value is not spending capacity. OpCo
   equity is not automatically distributable. Development and land assets
   require separate capital-need and monetization assumptions.
2. **Upstream classification only.** The model never infers
   `distributable_candidate`, `restricted`, `recurrence_type`, or `domain`
   from row labels. Each rule is human-authored.
3. **The workbook is the operating-forecast spine.** PE pacing, allocation,
   and liquidity coverage align to the workbook, not to a parallel
   theoretical model.
4. **Sign convention is enforced.** Mismatches fail at construction.
5. **Distributable rows must carry a domain.** Required for the Phase 12.5
   source convention `distribution:<domain>:<id>`.
6. **Restricted distributions are excluded** from `distribution_inflow`
   candidates but surfaced in diagnostics.
7. **The workbook is never mutated.** Read-only via
   `openpyxl(read_only=True, data_only=True, keep_links=False)`.

## Open-risks template

For each cross-walk pass, use this checklist when populating the
`## Open items & risks` section of `workbook_<v>_mapping_crosswalk.md`:

- [ ] Layout outliers — sheets whose `header_row_index` differs from the
      manifest default
- [ ] Entity-type ambiguity — generic suffixes (e.g., bare `T`) that could
      decode to multiple `entity_type` values
- [ ] Joint vs individual accounts — sheet names that may represent joint
      accounts (set `entity_type` accordingly)
- [ ] Project rollup vs entity sheet — sheets with duplicate row labels
      across sub-sections (often `display_only` until rules can disambiguate)
- [ ] Capital account vs operating LLC — `*_Cap` style sheets where legal
      type is ambiguous
- [ ] Aggregate snapshot vs entity tab — year/period-prefixed sheet names
      flagged by single-token heuristics

## Phase 14 → Phase 13 producer convention reminder

When a `RowClassificationRule` has `distributable_candidate=True`, the
ingestor's bridge function emits a `distribution_inflow` row whose `source`
follows the convention:

```
source       = f"distribution:{domain}:{asset_id_or_entity_id}"
producer_id  = f"{workbook_version}__{sheet_name}__{row_label}__{quarter}"
```

`workbook_version` is committed in the manifest and anchors deterministic
producer_ids across runs. `producer_id` does NOT include the workbook hash;
the hash is captured separately in `IngestionDiagnostics.workbook_hash` for
provenance only.

Use `REPartnershipSheetSpec.asset_id_by_row_label` when one entity sheet
carries multiple stabilized-RE assets and you want each row to emit a
finer-grained `asset_id` rather than the bare `entity_id`.

## L19 / L20 resolution gates

This workflow is the prerequisite for closing two open limitations:

- **L19** — full distributable-income realism. Requires the local manifest
  authored, classification rules in place, and a clean ingestion run that
  produces meaningful `distribution_inflow` rows.
- **L20** — full liquidity coverage realism. Requires a study run where
  `source_used = "cashflow_workbook"` for the PE call-obligation
  reconciliation, with a manageable advisory-only delta vs PE pacing
  projections.

Neither L19 nor L20 closes from synthetic-fixture coverage alone; both
require the live workbook to validate cleanly through Steps 5–6.

## When to re-run discovery

Re-run the Phase 14.2 discovery scraper (Step 1) when:

- A new workbook version is published (e.g., `v8.xlsx`).
- Sheets are added, renamed, or restructured.
- The board snapshot calendar advances and new snapshot tabs appear.
- Header layout changes (e.g., quarterly headers move from row 4 to row 5).

The scraper is idempotent and structural; running it does not classify
anything. It only detects layout. Manual classification (Step 4) must be
re-applied to any new horizontal_quarter sheets.

## Out of scope for this workflow

- Tax / legal / entity-governance distributability inference.
- Inter-entity transfer mechanics.
- Manager / fund liquidity terms (separate roadmap layer — see
  `Investment Summary` ingestion under Phase 15).
- Monte Carlo / stochastic forecasting (gated by L2).
- Workbook mutation of any kind.
- Committing the workbook, derived live values, or row labels to the repo.
