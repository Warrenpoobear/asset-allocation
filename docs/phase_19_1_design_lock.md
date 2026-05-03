# Phase 19.1 — Design Lock — SUPERSEDED

> **Status: SUPERSEDED by Phase 20 (`a5114f6`, 2026-05-03).** Kept for
> traceability only. Do not implement from this file.

## Disposition

The reconciliation work specified here was implemented as **Phase 20** with
a different naming and a leaner schema approach. The standing
worksheet-alignment constraint goals from this lock were met; specific
mechanics differ.

### What Phase 20 delivered (as shipped, `a5114f6`)

- New module: `src/aa_model/pe/call_reconciliation.py`
  (this lock proposed extending `pe/call_obligation.py` instead).
- Source precedence: `explicit_config > cashflow_workbook > pe_pacing_model > unavailable`
  (matches this lock).
- Workbook source path uses `CashFlowLineRecord.category == "capital_call"` +
  `direction == "outflow"` as the classification boundary.
  **Different from this lock**, which proposed a new
  `capital_call_candidate: bool` field on `RowClassificationRule` +
  `CashFlowLineRecord` mirroring `distributable_candidate`. Phase 20 chose
  the existing free-form `category` string as the boundary instead of
  adding a new schema field.
- Reconciliation classification: `advisory < 10% / warning 10–25% /
  blocking >= 25% / n/a (one source)`.
  **Different from this lock**, which proposed a 5% advisory tolerance and
  defined `blocking` as sign-mismatch. Phase 20's bands are wider and
  blocking is delta-magnitude based.
- Orchestrator: 11th `_build_ledger` return element
  (`WorkbookCallReconciliationDiagnostics | None`).
- Report: dedicated "PE call-obligation reconciliation" section replaces
  the Phase 19 bridge section; renders both sources, per-quarter delta
  table, and delta classification.
- 6 synthetic tests added (this lock proposed 9; the 3 missing are
  worth back-checking — see "Open follow-ups" below).

### Standing-constraint compliance check (post-Phase-20)

| Dimension | Required by `CLAUDE.md` § standing constraint | Phase 20 status |
| --- | --- | --- |
| Timing alignment | per-quarter over next-12m window | ✓ |
| Flow alignment | maps to workbook lines or pacing projections, no synthetic heuristic | ✓ |
| Source alignment | canonical 5-value enum on every emitted obligation | partial — uses 4 of 5 (no `investment_summary` or `synthetic_fixture`; not applicable to capital-call bridge) |
| Reconciliation alignment | per-quarter and overall delta + classification, surfaced in report | ✓ |

All four substantive dimensions met. The 5-value taxonomy gap is
acceptable because `investment_summary` and `synthetic_fixture` do not
emit capital-call obligations in the current architecture.

## Open follow-ups (post-Phase 20)

These are items where Phase 20's choices differ from this lock and may
warrant either a back-check or a future doc-line entry:

1. **Tolerance bands** — Phase 20 uses 10% / 25% bands; this lock proposed
   5% advisory + sign-mismatch blocking. Phase 20's bands are wider; if
   real-workbook validation later shows a tighter band is needed,
   tolerance is config-shaped per `CapitalCallReconciliationConfig`
   (not yet a real type — would need to be added).
2. **Sign-mismatch handling** — this lock specified sign-mismatch as
   `blocking` regardless of delta magnitude. Phase 20's classifier is
   pure-delta-pct, so a small-magnitude sign mismatch (e.g. workbook
   shows +$10k, pacing shows -$5k) would classify as `n/a (one source)`
   or low-pct. Verify with a synthetic test if the family-office workbook
   ever produces signed-capital-call edge cases.
3. **Schema approach** — this lock proposed a structured
   `capital_call_candidate: bool` field with manifest-side classification
   rule; Phase 20 uses the free-form `category` string. The free-form
   approach is leaner but couples classification to typo-prone string
   constants. Consider tightening to `Literal["capital_call", ...]` later
   if drift surfaces.
4. **Test coverage** — this lock specified 9 tests; Phase 20 shipped 6.
   Missing scenarios (worth back-checking):
   - explicit user value + both other sources present (deltas reported,
     `selected_source = explicit_config`)
   - per-quarter resolution with mixed window coverage
     (workbook has quarters 1+3, pacing has 1+2+3+4; per-quarter
     classifications vary, overall = worst quarter)
   - determinism: byte-identical reconciliation output for identical inputs

## Original design lock (kept for reference)

The full original design lock content followed below. As of Phase 20 it is
non-authoritative; the live spec is in `MODEL_DOCUMENTATION.md` Phase 20
section and the implementation in `src/aa_model/pe/call_reconciliation.py`.

---

# (Original) Phase 19.1 — Design Lock

> Workbook-side capital-call ingestion + PE-pacing reconciliation.
> Follows Phase 19 (`af58dd3`) which implemented the PE-pacing-only obligation
> bridge. Phase 19.1 closes the gap between the standing constraint
> (`docs/phase_19_design_constraints.md`) and the as-shipped Phase 19 by adding
> the workbook source path and reconciliation diagnostics.

[remaining original-lock content removed for brevity — see git history at
`e7e81ec` for the full text. Phase 20 (`a5114f6`) is the authoritative
implementation; this file is retained as a SUPERSEDED traceability record.]
