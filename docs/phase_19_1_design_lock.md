# Phase 19.1 — Design Lock

> Workbook-side capital-call ingestion + PE-pacing reconciliation.
> Follows Phase 19 (`af58dd3`) which implemented the PE-pacing-only obligation
> bridge. Phase 19.1 closes the gap between the standing constraint
> (`docs/phase_19_design_constraints.md`) and the as-shipped Phase 19 by adding
> the workbook source path and reconciliation diagnostics.

## Why this phase exists

Phase 19 implemented `derive_pe_capital_call_obligation` with source taxonomy
`"explicit" | "pe_pacing" | "unavailable"`. That is correct in isolation but
incomplete relative to the standing design constraint
(`CLAUDE.md` § "Standing design constraint: cash-flow worksheet alignment"),
which requires:

- canonical 5-source taxonomy: `explicit_config | cashflow_workbook |
  pe_pacing_model | investment_summary | synthetic_fixture`;
- workbook capital-call lines as a first-class source when classified;
- precedence between sources defined in the design lock;
- per-quarter reconciliation diagnostics (advisory / warning / blocking).

Phase 19.1 adds the workbook side and the reconciliation layer. It does NOT
change Phase 19's pacing-side logic.

## Non-goals (preserved from Phase 19)

- No Monte Carlo. Deterministic only.
- No `unfunded × heuristic_pct` fallback. T4 invariant preserved.
- No live client data in tests / docs. Synthetic fixtures only.
- No new ledger flow type. The bridge populates
  `LiquidityObligationConfig.next_12m_capital_calls_usd` only.
- No mutation of the workbook.
- No inference of legal / tax / entity-governance classification. Workbook
  classification is upstream (manifest-driven via `RowClassificationRule`).

## Schema changes

### `src/aa_model/ingestion/schemas.py`

Add a sibling to `distributable_candidate` on `RowClassificationRule` and on
`CashFlowLineRecord`:

```python
class RowClassificationRule(BaseModel):
    ...
    distributable_candidate: bool = False
    capital_call_candidate: bool = False   # NEW
    domain: _DOMAIN_LITERAL | None = None
```

```python
class CashFlowLineRecord(BaseModel):
    ...
    distributable_candidate: bool = False
    capital_call_candidate: bool = False   # NEW
    restricted: bool = False
```

Validation rules:

- `capital_call_candidate=True` requires `direction == "outflow"`.
- `capital_call_candidate=True` AND `distributable_candidate=True` is an
  error (mutually exclusive).
- A row may be classified `capital_call_candidate=True` without setting
  `domain`; `domain` is a distribution-side concept.

Diagnostics additions on `WorkbookIngestionDiagnostics`:

```python
capital_call_candidates_by_quarter_usd: dict[str, float] = field(default_factory=dict)
capital_call_candidates_count: int = 0
capital_call_candidates_by_entity_usd: dict[str, float] = field(default_factory=dict)
```

Per-quarter aggregation is the core integration point — the bridge needs
`{ "2026Q2": 1_500_000.0, "2026Q3": 0.0, ... }` to align with the pacing
model's per-quarter projection.

### `src/aa_model/ingestion/workbook.py`

Mirror the distribution-candidate aggregation loop for capital calls.
Reuse the manifest-rule re-derive pattern at line ~692 (don't introduce a
parallel classification path). Sign convention: workbook capital-call lines
are negative (outflows), so per-quarter totals are aggregated as
`abs(amount_usd)` for the bridge to consume positive-sign call totals.

## Bridge module changes

### `src/aa_model/pe/call_obligation.py`

Rename source taxonomy to canonical 5-value enum:

```python
SOURCE_EXPLICIT_CONFIG   = "explicit_config"     # was "explicit"
SOURCE_CASHFLOW_WORKBOOK = "cashflow_workbook"   # NEW
SOURCE_PE_PACING_MODEL   = "pe_pacing_model"     # was "pe_pacing"
SOURCE_UNAVAILABLE       = "unavailable"         # unchanged
# investment_summary, synthetic_fixture: not applicable to capital-call bridge,
# documented in module docstring as part of the canonical taxonomy.
```

Backward compatibility: orchestrator wiring already uses the bridge's
`source` field as a string literal in diagnostics. Update the orchestrator
to map old → new at the boundary in the same commit; do not maintain a
parallel old taxonomy.

### New function: `reconcile_capital_call_sources`

Pure function. Inputs:

- `workbook_calls_by_quarter: dict[str, float] | None` — from
  `WorkbookIngestionDiagnostics.capital_call_candidates_by_quarter_usd`,
  filtered to the next-12m window. `None` when no workbook ingestion ran or
  no rows were classified `capital_call_candidate=True`.
- `pacing_diag: PECallObligationBridgeDiagnostics` — Phase 19's existing
  output (renamed source field).
- `coverage_quarter: pd.Period`
- `selection_policy: Literal["workbook_wins", "pe_pacing_wins"] = "workbook_wins"`
- `tolerance_pct: float = 5.0` — within-tolerance bound for advisory tier.

Output:

```python
@dataclass
class CapitalCallReconciliation:
    next_12m_capital_calls_usd: float | None
    selected_source: str               # canonical taxonomy
    workbook_total: float | None
    pacing_total: float | None
    delta_abs: float | None            # workbook - pacing
    delta_pct: float | None            # delta_abs / max(|workbook|, |pacing|)
    classification: str                # "advisory" | "warning" | "blocking"
    by_quarter: dict[str, dict[str, float | None]]
    # by_quarter["2026Q2"] = {"workbook": 1_500_000.0, "pacing": 1_200_000.0,
    #                        "selected": 1_500_000.0, "delta_abs": 300_000.0,
    #                        "delta_pct": 20.0, "classification": "warning"}
    advisories: list[str]
```

## Precedence and classification

### Selection policy

- `workbook_wins` (default): if workbook has classified lines for any quarter
  in the window, workbook total is selected; pacing becomes cross-check.
- `pe_pacing_wins`: explicit user opt-in via config; pacing wins, workbook
  becomes cross-check.

### Both-source-present case

| State | `selected_source` | `next_12m_capital_calls_usd` |
| --- | --- | --- |
| workbook present, pacing present, `workbook_wins` | `cashflow_workbook` | workbook total |
| workbook present, pacing present, `pe_pacing_wins` | `pe_pacing_model` | pacing total |
| workbook present, pacing absent | `cashflow_workbook` | workbook total |
| workbook absent, pacing present | `pe_pacing_model` | pacing total |
| both absent | `unavailable` | `None` |
| explicit user value present | `explicit_config` | user value (workbook + pacing both demoted to cross-check) |

### Tolerance classification

When both sources are present, classify per-quarter and overall:

- `advisory`: `delta_pct <= tolerance_pct` AND signs agree.
- `warning`: `delta_pct > tolerance_pct` AND signs agree.
- `blocking`: signs disagree (one positive, one negative — irreconcilable),
  OR one source has a non-zero value and the other is structurally absent
  (`None` not `0.0`) when both should have data.

`blocking` does NOT zero out `next_12m_capital_calls_usd`. The selection
policy still applies; the classification is surfaced in the diagnostics for
the report. This is consistent with Phase 16's board-snapshot reconciliation
posture: surface, don't suppress.

## Orchestrator wiring

Resolution order (extends Phase 19's three-step into five):

1. `liquidity_obligations.next_12m_capital_calls_usd` explicitly set →
   `selected_source = "explicit_config"`.
2. Workbook capital-call diagnostics present and non-empty in window →
   workbook value via reconciliation function (which also runs pacing
   if available, for the cross-check delta).
3. Pacing-only (workbook absent) → `derive_pe_capital_call_obligation` as
   today; `selected_source = "pe_pacing_model"`.
4. Pacing absent (empty `pe_proj`) → `selected_source = "unavailable"`,
   `next_12m_capital_calls_usd = None`.
5. The reconciliation result (`CapitalCallReconciliation`) is carried
   alongside the existing `PECallObligationBridgeDiagnostics` in
   `_build_ledger`'s return tuple. `_build_ledger` becomes a 12-element
   tuple. Single-source paths produce a `CapitalCallReconciliation` with
   the absent side recorded as `None`.

## Report wiring

`integration/report.py` — extend the Phase 19 PE-call diagnostics block:

- After the existing `top contributors` section, add a `Reconciliation`
  block with: `selected_source`, `workbook_total`, `pacing_total`,
  `delta_abs`, `delta_pct`, `classification` (overall + per-quarter table).
- `classification == "warning"` renders with `**WARNING (>tolerance)**`
  marker, mirroring Phase 16 board-snapshot wording.
- `classification == "blocking"` renders with
  `**BLOCKING (sign mismatch)**` and emits a top-of-section advisory.

## Test plan

New tests in `tests/test_phase19_1_call_obligation_reconciliation.py`:

1. workbook-only (pacing empty): selected=`cashflow_workbook`, pacing fields
   `None`, classification=`advisory`.
2. pacing-only (workbook absent): selected=`pe_pacing_model`,
   workbook fields `None`, classification=`advisory` with workbook-source
   unavailable note.
3. both present, within tolerance (5%), `workbook_wins`:
   selected=`cashflow_workbook`, classification=`advisory`,
   `delta_pct < 5.0`.
4. both present, outside tolerance (20% delta), `workbook_wins`:
   selected=`cashflow_workbook`, classification=`warning`,
   `delta_pct > 5.0`.
5. both present, outside tolerance, `pe_pacing_wins`:
   selected=`pe_pacing_model`, classification=`warning`.
6. both present, signs disagree:
   classification=`blocking`,
   `next_12m_capital_calls_usd = workbook_total` (selection policy
   still applies; blocking is a diagnostic not a suppression).
7. explicit user value + both present:
   selected=`explicit_config`,
   workbook + pacing demoted to cross-check, deltas reported.
8. determinism: identical inputs produce byte-identical
   `CapitalCallReconciliation` field-by-field (json-roundtrip equal).
9. per-quarter resolution: window has 4 quarters, only 2 quarters have
   workbook entries, all 4 have pacing entries → per-quarter classification
   varies, overall classification = worst quarter.

Total Phase 19.1 test count: 9 new tests. Existing Phase 19 tests must be
updated for renamed source enum (mechanical: `"pe_pacing"` →
`"pe_pacing_model"`, `"explicit"` → `"explicit_config"`).

## Reverse-compatibility

This phase renames source-string values. There is no on-disk artifact that
persists the old values:

- `manifest.json` from determinism runs does not include the source enum.
- `LiquidityCoverageResult` does not currently carry the source string
  (only the bridge diagnostics, which are in-memory only).

If a downstream artifact is later found to persist the old strings, this
phase's commit will need to be amended with a migration; flag during
implementation.

## Limitation update

This phase does NOT mint a new L-item. It closes the implicit gap between
Phase 19 and the standing constraint. If implementation uncovers a residual
gap (e.g. workbook calls that classify by *category* rather than *quarter*,
or category-coded calls that need legal-restricted filtering analogous to
distribution restricted=True), mint as L21 in the implementation commit.

## Affected files

```
src/aa_model/ingestion/schemas.py              (+ capital_call_candidate field)
src/aa_model/ingestion/workbook.py             (+ aggregation loop)
src/aa_model/pe/call_obligation.py             (renamed enum + reconcile fn)
src/aa_model/integration/orchestrator.py       (resolution order)
src/aa_model/integration/report.py             (reconciliation block)
src/aa_model/io/schemas.py                     (CapitalCallReconciliationConfig
                                                — selection_policy, tolerance_pct)
configs/base.yaml                              (defaults under liquidity_coverage)
tests/test_phase19_pe_call_obligation.py       (rename-only, mechanical)
tests/test_phase19_1_call_obligation_reconciliation.py  (NEW, 9 tests)
MODEL_DOCUMENTATION.md                         (Phase 19.1 § + L20 status)
HERMES_TRACKING.md                             (post-impl auto-section refresh)
```

## Commit series (planned)

1. `docs(model): lock Phase 19.1 — workbook capital-call reconciliation`
   (this file moves to MODEL_DOCUMENTATION.md, design-input file removed).
2. `phase 19.1: workbook capital_call_candidate classification + diagnostics`
   (schema + workbook ingestion + tests for ingestion side only).
3. `phase 19.1: source taxonomy rename + reconciliation function`
   (call_obligation.py, mechanical test rename, reconcile fn + 9 tests).
4. `phase 19.1: orchestrator + report wiring for reconciliation`
   (full integration; report block; end-to-end test).
5. `docs(tracking): post-Phase-19.1 sync` (auto sections only).

Each commit must independently pass `ruff check` + `pytest -p no:warnings`.
No `git add -A`; explicit paths only (concurrent-author rule from
`aa-model-tracker` skill).

## Standing-constraint compliance check

After implementation, this phase MUST satisfy all four alignment dimensions
from `CLAUDE.md` § "Standing design constraint":

- [x] Timing alignment — bridge resolves per-quarter over the 4-quarter
      next-12m window using `coverage_quarter + 1..4`.
- [x] Flow alignment — `next_12m_capital_calls_usd` maps to either
      workbook capital-call lines or PE pacing projections; no synthetic
      heuristic path.
- [x] Source alignment — canonical 5-value enum on every emitted
      obligation; `selected_source` always set.
- [x] Reconciliation alignment — `CapitalCallReconciliation` exposes
      delta + classification per quarter and overall; surfaced in report.

If any of these regress during implementation, STOP and re-open the design
lock. Do not silently relax the constraint.
