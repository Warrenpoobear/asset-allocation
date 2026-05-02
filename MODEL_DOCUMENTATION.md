# MODEL_DOCUMENTATION

Authoritative record of model design, assumptions, limitations, and changes for
the SFO asset allocation study model. This file is updated on every commit that
changes model behavior; entries are appended, never rewritten or summarized
away.

---

## Overview

A modular Python research package that lets a single-family-office team study
three coupled questions on a single integrated quarterly cash-flow ledger:

1. **Public-asset allocation** — strategic policy, constraints, rebalancing.
2. **Spending and liquidity** — withdrawal rules, reserve floors, coverage.
3. **PE pacing** — calls, distributions, NAV evolution.

The package is a **research tool**. Outputs are reports and CSV/Parquet
artifacts (`ledger.parquet`, `report.md`, `manifest.json`,
`comparison.html`). It is not a production trading, accounting, or order
management system.

Spec of record: [`SPEC.md`](SPEC.md). When this document and `SPEC.md`
disagree on intent, `SPEC.md` is authoritative; this document records the
implementation's actual behavior.

---

## Architecture

The system has one load-bearing object — the **quarterly ledger** — and
everything else is a producer or consumer of rows on it.

```
configs (YAML, schema-validated)         scenarios (in-memory overrides)
        │                                       │
        └──────► resolved StudyConfig ◄─────────┘
                          │
                          ▼
                  orchestrator (per-quarter loop)
                          │
   ┌──────────────────────┼─────────────────────────┐
   │                      │                         │
allocator              spending rule            PE TA model + pacing
(stub | riskfolio)     (flat_real | smoothing)  (canonical, not behind adapter)
   │                      │                         │
   └──────────────────────┴─────────────────────────┘
                          │ flows
                          ▼
                  QuarterlyLedger
                  (sort + chain + validate)
                          │
                          ▼
              ledger.parquet + report.md + manifest.json
```

Subsystem layout (Phase 3a):

```
src/aa_model/
├─ io/              schemas (pydantic v2), loaders, cross-config validation
├─ assumptions/     CMA dataclass, scenario_builder
├─ allocation/      AllocationAdapter ABC, StubAllocator, RiskfolioAdapter,
│                   factory, Constraints
├─ implementation/  ImplementationAdapter ABC, StubImplementation (zero-cost),
│                   CvxportfolioImplementation (linear cost), factory
├─ spending/        SpendingRule ABC, FlatRealRule, SmoothingRule, liquidity
├─ pe/              ta_model (canonical), pacing
├─ integration/     QuarterlyLedger, orchestrator, manifest, report,
│                   sweep, comparison_report
└─ cli/             aa-model entry point (run, sweep)
```

### Determinism contract

Every orchestrator run writes a `manifest.json` with `config_hash`,
`fixtures_hash`, library versions, seed, started/finished timestamps, and
the list of output artifacts. Hashes are computed from the resolved
study-config objects via canonical JSON (sorted keys, fixed indent), so
they are invariant to whether inputs came from disk or were synthesized in
memory by a scenario.

`run_id` format: `aa-<config_hash[:12]>-<fixtures_hash[:12]>-<UTC_ts>-<nonce>`.
Hashes are deterministic in the inputs; the timestamp + 4-char hex nonce make
each invocation unique so reruns never overwrite a prior run dir. Two
consecutive runs of the same config produce different `run_id`s and different
output dirs but **byte-identical `ledger.parquet` content once the per-row
`run_id` metadata column is dropped**.

---

## Core Invariants

The orchestrator is forbidden from completing a run without satisfying every
invariant below. `QuarterlyLedger.validate()` enforces them and is called
unconditionally at the end of every run.

### Ledger invariants (SPEC §5.1, extended in P3b)

1. **Canonical intra-quarter ordering.** Within each `(quarter, bucket)`,
   rows are sorted by `flow_type` in this order, ties broken by `source`
   ascending:
   `inflow → return → pe_call → pe_distribution → pe_nav_mark → spend → rebalance → transaction_cost`.
   `transaction_cost` is a Phase 3b extension; it lands after rebalance
   because the cost is a function of the just-executed trades.
2. **Per-row consistency.** `nav_end_usd == nav_start_usd + amount_usd`
   for every row (within `1e-6`). Returns and `pe_nav_mark` rows express
   their P&L as the dollar `amount_usd`, so this holds uniformly.
3. **Chain consistency.** Within each `(run_id, bucket)` chain in canonical
   order, `nav_start_usd[n] == nav_end_usd[n-1]`. The first row of each
   bucket has `nav_start_usd` equal to that bucket's `initial_nav` (or
   `0.0` if absent).
4. **Per-bucket per-quarter tie-out.** For each `(run_id, quarter, bucket)`,
   `sum(amount_usd) == nav_end_usd[last_row] - nav_start_usd[first_row]`.
5. **External cash flow tie-out.** For each `(run_id, quarter)`,
   `sum(amount_usd over flow_type ∈ {inflow, spend, transaction_cost})`
   equals the household's net external wire that quarter (passed into
   `validate()` by the orchestrator). `transaction_cost` is included
   because the cost leaves the household — paid to brokers /
   market-makers — and is therefore external cash, not internal flow.
6. **Rebalance is zero-sum.** For each `(run_id, quarter)`,
   `sum(amount_usd over flow_type == "rebalance") == 0` within `1e-6`.
7. **PE call / distribution are zero-sum.** Same statement as rebalance,
   for `flow_type ∈ {pe_call, pe_distribution}`. Capital moving between
   `cash` and a PE sleeve must net to zero across buckets each quarter.
8. **Per-source PE leg pairing.** Every `pe_call` / `pe_distribution` row
   on a non-cash bucket has exactly one paired row on the `cash` bucket
   with the same `source` and the negated amount. Tested explicitly in
   `tests/test_orchestrator.py::test_pe_call_and_distribution_have_matching_cash_offsets`
   to catch a swapped-sign or missing-leg bug that happens to net to zero
   across funds.
9. **Total NAV conservation.** For each `(run_id, quarter)`,
   `Δ(total_NAV_quarter) == sum(return + pe_nav_mark + inflow + spend +
   transaction_cost amounts)`. Equivalently: total portfolio NAV moves
   only via market P&L and external cash (where `transaction_cost` is
   classified as external — see invariant 5), never via internal flows
   like rebalance / pe_call / pe_distribution.
10. **No NaN** in `amount_usd`, `nav_start_usd`, `nav_end_usd`.
11. **Single `run_id`** per ledger object.

### Determinism / reproducibility contract

* Two consecutive runs of the same config (with no scenario override)
  produce identical `config_hash`, identical `fixtures_hash`, and ledger
  content that is byte-identical once the `run_id` column is dropped.
* All seeded randomness uses `numpy.random.default_rng(seed)` derived from
  `configs/base.yaml::seed`. No global `np.random.seed`.
* Output directories are never overwritten — each invocation lands in its
  own dir suffixed by timestamp + nonce.

### Adapter parity contract (Phase 3+)

The stub returns configured weights verbatim; non-stub adapters solve
optimization problems. They are **not** numerically equal. Parity is
**structural**:

* Same bucket index, same dtype.
* Weights sum to 1 within `1e-6`.
* All weights in `[0, 1]` (no shorts under default constraints).
* No NaN / inf.
* Under a *binding* equality constraint (`min == max` for a bucket),
  both adapters produce that exact weight within `1e-6`.

### Adapter discipline contract (Phase 3+)

Every adapter that ships under one of the SPEC §9 ABCs must obey the
discipline below. The phrasing differs by ABC because each ABC hands
the adapter different inputs — but the underlying principle is the
same: pure function of declared inputs, no retention beyond what the
ABC contract requires, no global state, no out-of-channel reads.

* **`AllocationAdapter`** — pure function of
  `(returns, CMA, Constraints)` → weights, plus the
  `fit() → weights() → diagnostics()` lifecycle the ABC defines. No
  retention of inputs across calls beyond what that lifecycle
  requires. No global / shared state. **No ledger access** (the ABC
  does not hand the adapter the ledger).
* **`ImplementationAdapter`** — pure function of
  `(current, target, CostModel)` → `RebalanceResult`. No retention
  of inputs between calls. No global / shared state. **No ledger
  access** (the ABC does not hand the adapter the ledger).
* **`SpendingRule`** — pure function of `(QuarterlyLedger, SpendingParams)`
  → quarterly outflow series. **No ledger mutation. May read the
  ledger passed into the `SpendingRule` interface, but may not retain
  it, mutate it, or access global/shared state.** The current call's
  ledger argument is the only legitimate channel for ledger reads;
  the orchestrator passes it with `initial_nav` set and (today) no
  flows yet recorded.
* **`PEAdapter`** (Phase 7+) — pure function of
  `(PEPacingConfig, horizon_start, num_quarters, CMA, public_equity_path)`
  → PE projection frame conforming to `PROJECTION_COLUMNS`. **No
  ledger access** (the ABC does not hand the adapter the ledger).
  **No mutation of `CMA` or `public_equity_path`** — both are read-only
  inputs; the adapter must not write back to the dataframes / Series /
  CMA dataclass it received. **No hidden / global state** between
  invocations. **No randomness** in any current PE engine. A future
  stochastic variant must declare itself under a separate engine name
  (e.g., `pe.engine="stairs_mc"`) and document its randomness contract
  explicitly — `pe.engine="ta"` and `pe.engine="stairs"` are reserved
  for deterministic engines forever. Output schema must be the
  unmodified `PROJECTION_COLUMNS` (plus the `sleeve` column the
  factory wraps it with); engine-specific extensions belong in
  `adapter.diagnostics()`, not in the projection frame.
* **All adapters** — lazy backend imports if the adapter has an
  optional dependency. Any non-stub adapter ships with: structural
  parity tests against the stub, at least one numerical anchor case,
  documented path-dependence semantics, and an entry in this file's
  Change Log gating its merge.

---

## Model Components

### Allocation

Behind `AllocationAdapter` (SPEC §9):

* `StubAllocator` (Phase 1, reference) — reads
  `configs/public_allocation.yaml::stub_weights` and returns them verbatim.
  Ignores `returns` / `CMA` / `Constraints`, but records their shape in
  `diagnostics()`. Always conformant by construction.
* `RiskfolioAdapter` (Phase 3a, opt-in via `[project.optional-dependencies]
  riskfolio`) — `riskfolio-lib` 7.2.1 + `cvxpy` 1.8.2 backend. Solves
  `model="Classic", rm="MV", obj="MinRisk", hist=False` against the
  caller-provided CMA. Long-only box bounds from the `Constraints` argument.
  When called with an empty CMA (the orchestrator's Phase 1 default), the
  adapter synthesizes a default annualized-vol vector + identity correlation
  — see *Known Limitations* for what this means about output.

The factory `aa_model.allocation.factory.make_allocator(cfg, engine=...)`
dispatches by `allocation.engine`. Non-stub adapters import their backend
lazily so the package runs without optional optimizer deps installed.

`Constraints` (in `allocation/constraints.py`) is a frozen dataclass with
`min_weights` and `max_weights` dicts (per-bucket box bounds). `CMA` (in
`assumptions/cma.py`) carries `expected_returns_annual`, `vol_annual`, and
`corr` as pandas Series / DataFrame. Both default to empty in Phase 1.

### Implementation (rebalancer)

Behind `ImplementationAdapter` (SPEC §9):

* `StubImplementation` (Phase 1, reference) — zero-cost rebalancer.
  `trades = target - current`, `cost_usd = 0.0`, no NaN, trades sum to
  zero by construction.
* `CvxportfolioImplementation` (Phase 3b, opt-in via
  `[project.optional-dependencies] cvxportfolio`) — same trades as the
  stub, plus a linear transaction cost
  `cost_usd = (bps_per_trade / 1e4) · ∑ |trade|` consistent with the
  linear term of cvxportfolio's `StocksTransactionCost(a=bps/1e4)`.
  Quadratic / market-impact / per-share terms are intentionally NOT
  modeled — Phase 3b minimum (see L14). The adapter has **no path
  dependence**: trades depend only on the current and target vectors
  handed in for *this* call, not on any prior call (see L13).

`make_implementation(engine=...)` (in `implementation/factory.py`)
dispatches by `implementation.engine`. Cvxportfolio imports lazily so
the package runs without the optional dep.

`CostModel` (`implementation/base.py`) carries `bps_per_trade`. The
orchestrator builds it from `base.implementation.bps_per_trade` and
hands it to `rebalance(...)`. Cross-config validation rejects
`engine=stub` paired with non-zero `bps_per_trade` because the stub
silently ignores costs and that combination would mean "I asked for
costs but they weren't applied".

`RebalanceResult` (`implementation/base.py`) is a frozen dataclass
holding `trades` (per-bucket signed dollar Series) and `cost_usd`.

When `cost_usd > 0`, the orchestrator emits a single
`transaction_cost` row on the `cash` bucket with `amount_usd =
-cost_usd` and source `impl:<engine>`. The household's net external
cash for the quarter then includes this row alongside `inflow` and
`spend` (see Core Invariants §5).

#### transaction_cost classification — load-bearing decision

> `transaction_cost` is modeled as an external cash outflow.
>
> **Rationale**
> - brokerage fees leave the portfolio to a third party
> - this preserves the invariant: NAV changes only via market P&L
>   and external cash flows
> - treating costs as internal leakage would break deterministic NAV
>   reconciliation
>
> **Implication**
> - reported returns are net of costs
> - external cash accounting includes both spending and transaction
>   costs
>
> **Stability commitment**
> Do NOT reclassify `transaction_cost` as internal leakage in a future
> phase unless the §Core Invariants block is redesigned globally and
> all external-tie-out / NAV-conservation tests are updated together.
> A piecemeal change here would silently invalidate every prior run's
> reconciliation, including this commit's numerical anchor.

### Spending / Liquidity

Behind `SpendingRule` (SPEC §9):

* `FlatRealRule` — annual amount split evenly across 4 quarters; nominal
  steps up by `inflation_pct` at each year boundary. Floor / ceiling clip
  the per-quarter value.
* `SmoothingRule` (EWMA) —
  ```
  spend_0 = target_0
  spend_t = w · target_t + (1 - w) · spend_{t-1}    (t > 0)
  ```
  where `target_t` is the same inflated-quarterly series `FlatRealRule`
  emits and `w = config.smoothing.weight`. `w = 1` tracks `target` exactly
  (equivalent to flat-real); `w = 0` freezes spending at the initial target
  and never re-anchors to inflation; intermediate `w` produces an
  exponentially-weighted lag toward target.
* `OwlRule` (Phase 3c, project codename for the Guyton-Klinger guardrail
  rule; lives at `spending/owl_adapter.py` for §4-layout consistency) —
  ```
  initial_rate    = annual_spend_0 / initial_nav_total
  forecast_nav_t  = initial_nav_total · (1 + forecast_quarterly_return_pct)^t

  for each year boundary  (t > 0, t % 4 == 0):
      annual_spend_t  = annual_spend_{t-1} · (1 + inflation_pct)        # inflation step
      current_rate    = annual_spend_t / forecast_nav_t
      if current_rate < initial_rate · (1 - lower_band_pct):
          annual_spend_t *= (1 + raise_pct)                              # ratchet up
      elif current_rate > initial_rate · (1 + upper_band_pct):
          annual_spend_t *= (1 - cut_pct)                                # ratchet down

  within a year  (t % 4 != 0):
      annual_spend stays at year's level
  quarterly_t       = annual_spend_t / 4    (then floor / ceiling clipped)
  ```
  Guardrail bands and ratchet sizes come from a new
  `GuardrailConfig` block in `spending.guardrail`; cross-config validation
  rejects `rule = owl` without it. Owl is **not** an external library
  adapter — it is the canonical implementation of the missing
  `guardrail` rule referenced in SPEC §4 layout.

`make_rule(name)` is the factory; for `name == "owl"` it imports
`OwlRule` lazily from `spending/owl_adapter.py` to avoid circular
imports between `rules.py` and `owl_adapter.py`.

#### Spending-rule comparison

| | flat_real | smoothing | owl |
|---|---|---|---|
| precomputed at run start | yes | yes | yes |
| inflation-adjusted target | yes | yes | yes (year-by-year) |
| within-year constancy | yes (4q identical) | no (quarterly EWMA) | yes (4q identical) |
| path-dependent | no | yes (1-step back on `spend_{t-1}`) | yes (1-step back on year-prior `annual_spend`) |
| reads ledger NAV | no | no | reads `ledger.initial_nav` only |
| reacts to realized NAV | no | no | **no** — uses `forecast_quarterly_return_pct` (see L15) |
| reacts to scenario shocks | no | no | **no** — `forecast_quarterly_return_pct` is exogenous, not scenario-derived (L15) |
| invariant under NAV scaling | yes (config-driven) | yes (config-driven) | yes — see L16 |
| extra config required | none | `smoothing.weight` | `guardrail` block (5 fields) |

Liquidity metrics (`spending/liquidity.py`) are derived from the ledger
DataFrame after the run completes — no shadow state, no rolling tracker:

* `coverage_months_per_quarter(end_nav_by_quarter, annual_spend_by_quarter,
  liquid_buckets)` — `liquid_nav / (annual_spend / 12)`. `liquid_buckets`
  defaults to `("cash", "public_bond")`.
* `shortfall_frequency(coverage, floor_months)` — fraction of quarters
  with coverage below `floor_months`. Floor defaults to 18 (per
  `base.yaml::liquidity.floor_months`).
* `max_drawdown(total_nav)` — worst peak-to-trough decline + window length
  in quarters. Returns `(0.0, 0)` on a monotone path.
* `compute_liquidity_metrics(...)` aggregates the above into a
  `LiquidityMetrics` dataclass (final NAV, cumulative return, min/mean
  coverage, shortfall freq, max drawdown, drawdown quarters).

### PE Model

The Takahashi-Alexander cash-flow projector lives in `pe/ta_model.py` and is
**not** behind an adapter (SPEC §9 final paragraph). For each fund, given
parameters from `pe_pacing.yaml::ta_defaults`, the model produces a tidy
DataFrame indexed by quarter from vintage with:

```
year_index = t // 4
age_years  = (t + 1) / 4                         # age at quarter end

call_t  = (rc[year_index] · K) / 4               (year_index < commitment_period_years)
N_after_call         = NAV_start + call_t
annual_dist_rate     = max(yield_pct, (age_years / lifetime_years)^bow)
quarterly_dist_rate  = min(annual_dist_rate / 4, 1.0)         # final-period cap
distribution_t       = quarterly_dist_rate · N_after_call
N_after_dist         = N_after_call - distribution_t
nav_mark_t           = N_after_dist · (growth_pct / 4)
NAV_end              = N_after_dist + nav_mark_t
```

Pinned defaults (SPEC §6 P1, recorded in `configs/pe_pacing.yaml`):
`lifetime_years=12`, `commitment_period_years=4`,
`rate_of_contribution=[0.25, 0.30, 0.25, 0.20]`, `bow=2.5`,
`yield_pct=0.0`, `growth_pct=0.13`. A golden CSV
(`tests/golden/ta_single_fund.csv`, sha256 `9fa7b316`) generated from these
defaults on a $100M 2024Q1 fund pins the math against drift.

`pe/pacing.py::project_horizon(pacing, start, n_q)` filters all configured
funds' projections to the run horizon and attaches a `sleeve` column from
each fund's config. The orchestrator iterates per-quarter and emits two
ledger rows per `pe_call` (sleeve `+call`, cash `-call`) and per
`pe_distribution` (sleeve `-dist`, cash `+dist`), plus one `pe_nav_mark`
row on the sleeve only. This guarantees invariants 7 and 8.

### Scenario System

A `Scenario` (in `assumptions/scenario_builder.py`) is a frozen dataclass
with `name`, `description`, and three optional override fields:
`fixture_scenario`, `pe_pacing`, `spending`. The orchestrator applies
overrides via `cfg.model_copy(update=...)` in `_apply_scenario`; **the
orchestrator never inspects `scenario.name`**. There is no
scenario-aware branching anywhere in the engine.

`make_scenarios(base_fixture, base_pe_pacing, base_spending)` returns the
five canonical Phase 2 perturbations:

| name | override | what changes |
|---|---|---|
| `base` | none | reference run |
| `public_drawdown` | `fixture_scenario.returns` | -25% public_equity at q8 with 4-quarter recovery |
| `delayed_pe_distributions` | `pe_pacing.ta_defaults.bow` | bow 2.5 → 4.0 |
| `clustered_calls` | `pe_pacing.ta_defaults.rate_of_contribution` | [0.50, 0.30, 0.15, 0.05] |
| `inflation_shock` | `spending.inflation_pct` | 2.5% → 6.0% per year |

`run_scenario_sweep(base_config_path, scenarios)` runs each scenario
sequentially through `run_orchestrator(..., scenario=...)`, derives liquidity
metrics from each ledger, and aggregates a `SweepResult`. Each scenario
lands in its own auditable run dir under `data/processed/runs/`; the sweep
itself writes to `data/processed/sweeps/<sweep_id>/`.
`write_comparison_report(sweep)` emits `comparison.md` + `comparison.html`
(jinja2 template) with per-scenario rows.

`correlation_shock` (also listed in SPEC §6) is intentionally omitted —
Phase 1 does not model bucket-level correlation, so a correlation override
has no place to land. It will return when the CMA gains a covariance matrix.

---

## Assumptions

What the model assumes (Phase 1–3a):

1. **Quarterly grain.** All flows, returns, NAV evolution, and rebalancing
   are quarterly. No intra-quarter dynamics.
2. **Deterministic returns.** Per-bucket quarterly return rates come from
   the fixture scenario (with optional per-quarter overrides). They are
   not stochastic. A scenario perturbing returns hardcodes the perturbed
   path.
3. **Deterministic PE growth.** `growth_pct` is a constant, not regime- or
   market-linked. PE distributions are scheduled by the bow curve, not
   triggered by market events.
4. **Identity correlation.** Buckets are statistically independent at the
   model level. The Phase 3a riskfolio adapter's default-CMA fallback
   uses identity correlation explicitly.
5. **Rebalancing cost model is engine-dependent.**
   `StubImplementation` is zero-cost: trades exactly the gap between
   current and target dollar allocations, trades sum to zero per
   quarter, no transaction costs, no slippage, no execution delay.
   `CvxportfolioImplementation` (Phase 3b) applies a linear bps cost
   on traded volume but produces the same trade vector as the stub
   (no optimization-driven trade reduction, no market impact, no
   slippage, no execution delay). See L13 / L14.
6. **No taxes.** No tax-lot accounting, after-tax returns, or charitable
   structures. (Out of v1 scope per SPEC §11.)
7. **Single currency.** USD only. (Out of v1 scope per SPEC §11.)
8. **Liquid bucket definition.** `LIQUID_BUCKETS_DEFAULT = ("cash",
   "public_bond")` for coverage / shortfall metrics. The parameter is
   overridable but no caller currently does.
9. **Allocator targets are ranged through the rebalancer.** Whatever
   weights the allocator returns become the target for each quarter's
   rebalance. PE is included in the rebalance — i.e., the model treats
   PE as if it can be rebalanced into and out of as freely as public
   buckets. (See *Known Limitations*.)
10. **Object-based hashing.** `config_hash` and `fixtures_hash` are SHA-256
    over canonical JSON of the resolved pydantic models, so an in-memory
    scenario override hashes the same as an equivalent YAML edit.

---

## Known Limitations

These are explicitly called out so future readers cannot misinterpret model
output. Each entry separates **model behavior** (what the code does) from
**real-world interpretation** (why the model output may not match reality).

### Limitation status summary (2026-05-02 consolidation)

This table is the **single source of truth for L-status**. Per-entry
detail follows below; this summary exists so future-phase planning
doesn't have to skim eighteen entries to know what's open.

| L | Topic | Status | Closed / classified by |
|---|---|---|---|
| L1  | PE timing scenarios mechanically affect returns | **PARTIALLY RESOLVED** | Phase 7 (STAIRS resolves; TA persists) |
| L2  | Returns are NAV-dependent, not regime-dependent | **OPEN — architecture** | Future stochastic CMA + Monte Carlo |
| L3  | Stub-vs-riskfolio weights are not numerically comparable | **ACCEPTED LIMITATION** | Phase 5 (empty-CMA path test-only); structural difference is by design |
| L4  | Riskfolio default CMA fallback is a placeholder | **RESOLVED** | Phase 5 |
| L5  | `source` as a PE-leg pairing key is fragile | **OPEN — schema** | Future recommitment / multi-call work; partially mitigated by Phase 9 globally-unique `name` rule |
| L6  | `correlation_shock` scenario is omitted | **RESOLVED** | Phase 6 |
| L7  | Smoothing rule with `weight=0` freezes spending | **ACCEPTED LIMITATION** | Phase 10 consolidation — behavior matches EWMA formula; documented |
| L8  | Rebalancer treats PE as a liquid sleeve | **RESOLVED** | Phase 8 |
| L9  | Heavy install footprint for `riskfolio` extra | **ACCEPTED LIMITATION** | Phase 10 consolidation — third-party dependency, no fix possible |
| L10 | `/mnt/c` filesystem is unsuitable for `.venv` | **ENVIRONMENT NOTE** | Phase 10 consolidation — not a model limitation, environment doc only |
| L11 | Synthetic 2-row dummy returns frame in Riskfolio adapter | **ACCEPTED LIMITATION** | Phase 10 consolidation — third-party API constraint |
| L12 | Non-fatal "convert self.cov to PSD" warning | **ACCEPTED LIMITATION** | Phase 10 consolidation — riskfolio internal, benign |
| L13 | Cvxportfolio adapter has no path dependence | **RESOLVED** | Phase 4b |
| L14 | Only linear transaction cost is modeled | **PARTIALLY RESOLVED** | Phase 10 (PE-secondary closed by L8; public-bps documented; richer regimes deferred) |
| L15 | Owl reacts to forecasted NAV, not realized NAV | **RESOLVED** | Phase 4a |
| L16 | Owl is scale-invariant in initial NAV | **OPEN — modeling** | Future Owl reformulation against absolute spend rate |
| L17 | Cross-engine metric comparability is not meaningful | **ACCEPTED LIMITATION** | Phase 10 consolidation — interpretation problem, not architecture |
| L18 | Owl misreads inflation shock as "headroom" and raises spending | **RESOLVED** | Phase 4a |

#### Status definitions

* **RESOLVED** — code change closed the concern; entry retained for
  audit trail with a `[RESOLVED YYYY-MM-DD, Phase N]` callout.
* **PARTIALLY RESOLVED** — concern is closed under one engine /
  scope but persists under another; engine-conditional resolution
  is documented in the entry. L1 is engine-conditional (STAIRS
  closes, TA persists). L14 is scope-conditional (PE-secondary
  closed by L8; public-bps via diagnostic; richer regimes deferred).
* **ACCEPTED LIMITATION** — the behavior is documented and
  expected; no code change is planned. Either the limitation is
  a third-party constraint (L9, L11, L12), an interpretation
  problem rather than an architecture problem (L3, L17), or
  documented behavior matching a formula (L7).
* **ENVIRONMENT NOTE** — not a model limitation; environment-
  specific guidance for users (L10).
* **OPEN — architecture** — substantial future work, gated on
  Monte Carlo / stochastic regime layer (L2).
* **OPEN — modeling** — real model weakness, no engine in the
  current stack addresses it (L16).
* **OPEN — schema** — future schema or data-model work (L5).

#### Roadmap implications (open entries by priority)

Three entries are genuinely open and worth scoping next:

1. **L16 — Owl scale-invariant in initial NAV.** Owl's trigger
   condition reduces algebraically to a NAV-cancelling form; a
   $100M household and a $100B household get identical Owl
   trajectories at the same spend / inflation / band parameters.
   This is a real model weakness, not a third-party quirk. Fix
   path: Owl reformulation against absolute spend rate (e.g.,
   trigger on absolute NAV crossing a fraction of initial, not
   a ratio that cancels). **No engine in the current stack
   addresses this.** Standalone phase, ~Phase 11 candidate.

2. **L2 — Returns are NAV-dependent, not regime-dependent.**
   Scenario perturbations change *levels* but not *dynamics* —
   no autocorrelation, no volatility clustering, no drawdown
   contagion. Resolving requires a Monte Carlo path generator
   plus a stochastic regime layer over CMA. Substantial work;
   biggest realism upgrade remaining; explicitly deferred until
   correlation / illiquidity / pacing layers (Phases 6–9) had
   landed deterministic-first. **Now unblocked structurally;
   when the project commits to Monte Carlo, L2 is the gate.**

3. **L5 — `source` as PE-leg pairing key is fragile.** Currently
   adequate (one row per leg per fund per quarter; Phase 9's
   globally-unique `name` rule lifts the implicit invariant).
   Becomes binding when recommitment logic, secondary-purchase
   flows, or multi-call-per-fund-per-quarter pacing land. Fix:
   add a `flow_id` field to the ledger schema. Schema change;
   should ride the next phase that needs it (probably Phase 9's
   manager work landing in production).

The seven `ACCEPTED LIMITATION` / `ENVIRONMENT NOTE` entries
(L3, L7, L9–L12, L17) are explicitly **not** future work — they
are documented status calls. Future readers should not interpret
them as backlog.

#### What this consolidation pass changed

* Six entries had their status formalised (L3, L7, L9, L10, L11,
  L12, L17 → ``ACCEPTED LIMITATION`` or ``ENVIRONMENT NOTE``).
  Per-entry detail in the entries below now carries the matching
  status callout.
* No code changes; doc-only.
* Sets up the next phase as a model-weakness fix (L16 candidate),
  not yet another structural realism layer.

---

### L1 — PE timing scenarios mechanically affect returns — [PARTIALLY RESOLVED 2026-05-02, Phase 7]

> **Status: PARTIALLY RESOLVED in Phase 7.** Resolution is
> **engine-conditional**:
>
> * Under ``base.pe.engine = "stairs"`` (the new default-eligible
>   PE adapter), the artifactual "free return lift" mechanism is
>   closed. PE NAV growth becomes a CMA-anchored coupling to
>   realized public_equity excess: a public_drawdown propagates
>   into PE NAV proportional to ``beta_to_public_equity`` per
>   sleeve; ``clustered_calls`` and ``delayed_pe_distributions``
>   no longer fabricate cumulative return because the deployed
>   capital is now exposed to the same scenario shock. Any
>   residual scenario-driven PE return effect under STAIRS is
>   the *real* timing-coupling channel — calls deployed during a
>   public drawdown buy at low NAV that subsequently recovers if
>   public_equity recovers — not an artifact.
> * Under ``base.pe.engine = "ta"`` (the default for
>   backwards-compatibility), the original artifact persists.
>   ``ta_defaults.growth_pct`` is a constant, scenario-blind, and
>   the timing-scenario lift documented in the Phase 1 text below
>   continues to appear. Users who care about realistic timing
>   stress should opt into STAIRS.
> * Phase 7 (commit landing this entry) introduces the PE adapter
>   pattern (``pe/base.py`` + ``pe/ta_adapter.py`` +
>   ``pe/stairs_adapter.py`` + ``pe/factory.py``); the STAIRS
>   recursion clips ``growth_pct_q ≥ -0.99`` so NAV stays
>   non-negative under extreme drawdowns. Six anchor tests in
>   ``tests/test_pe_adapter_stairs.py`` pin the adapter
>   semantics (parity at ``beta=0`` + drift = ``growth_pct``,
>   beta amplification, idiosyncratic monotonicity, public-equity
>   decoupling, linear commitment, growth-clip activation).
>
> The original Phase 1 text follows for audit-trail purposes.

* **Model behavior.** `clustered_calls` produces a higher cumulative return
  than `base`; `delayed_pe_distributions` also produces a slightly higher
  return. PE NAV grows at a constant `growth_pct`, distributions are
  recycled into the same target weights via a zero-cost rebalance, and
  there is no opportunity cost for capital deployed early.
* **Real-world interpretation.** This is a model artifact, **not alpha**.
  Real PE timing scenarios should at minimum stress liquidity (cash
  drawn / refilled by call / distribution timing) and pacing risk; they
  should not produce free return lift. Treat PE-timing scenarios as
  liquidity / pacing stress only until the model gains a stochastic CMA
  with a regime shared across public + private buckets. Documented
  explicitly in `assumptions/scenario_builder.py`.

### L2 — Returns are NAV-dependent, not regime-dependent

* **Model behavior.** Scenario perturbations affect *levels* (return on
  bucket B at quarter Q is `rate · NAV_start[B,Q]`); they do not change
  the *dynamic* of returns. There is no autocorrelation, no volatility
  clustering, no fat tails, no drawdown contagion across buckets.
* **Real-world interpretation.** This is fine for studying allocation
  policy and spending sustainability under a deterministic baseline, but
  the model cannot answer "what's our 95% VaR" or "how often do we
  breach a coverage floor under realistic dynamics." A stochastic CMA
  + Monte Carlo path generator is needed.

### L3 — Stub-vs-riskfolio weights are not numerically comparable — [ACCEPTED LIMITATION 2026-05-02]

> **Status: ACCEPTED LIMITATION** (Phase 10 consolidation). Phase 5
> made the empty-CMA fallback path test-only (production runs always
> consume an explicit CMA), so the specific "98% cash" failure mode
> below now requires a deliberate test-only path. The structural
> point — stub returns config weights, riskfolio solves a min-var
> optimization — remains true by design; the two adapters solve
> different problems and their outputs are **not intended** to be
> directly comparable. Documented for users who might assume
> numerical comparability across allocation engines.

* **Model behavior.** With the Phase 1 default empty CMA, the riskfolio
  MinRisk optimizer produces ~98% cash allocation against the
  hard-coded fallback vol vector (cash 0.5%, bond 4%, equity 16%, pe
  20%) + identity correlation. The stub produces the configured
  `stub_weights` (cash 5%, bond 20%, equity 50%, pe 25%).
* **Real-world interpretation.** The two adapters solve different
  problems. The riskfolio adapter is correct *for what it was asked to
  solve* (minimum variance against the fallback CMA). The fallback CMA
  is a placeholder, not an investment view. Real allocation work
  requires a populated CMA. Until then, riskfolio output is only
  meaningful for testing the wiring.

### L4 — Riskfolio default CMA fallback is a placeholder — [RESOLVED 2026-05-02, Phase 5]

> **Status: RESOLVED in Phase 5.** The CMA pipeline now loads explicit
> capital market assumptions from `configs/cma.yaml` (validated via
> `CMAConfig`); the orchestrator builds a populated `CMA` via
> `CMA.from_config(cfg.cma)` and hands it to every adapter's `fit()`.
> Production runs always consume the loaded CMA. The
> `_DEFAULT_VOL_ANNUAL` fallback in `RiskfolioAdapter` is retained as
> a **test-only** path (gated on the empty `CMA()` default-constructed
> sentinel) and is unreachable from any orchestrator-driven run. The
> shipped `configs/cma.yaml` replicates the prior fallback values
> (vols matching `_DEFAULT_VOL_ANNUAL`, identity correlations, zero
> expected returns) so the cutover is structural — assumption surface
> becomes config-explicit; reproducibility hashes and ledger contents
> stay byte-stable. Real-CMA calibration is a separate concern,
> deferred until empirical inputs are available.
>
> The original Phase 3a text follows for audit-trail purposes.

* **Model behavior.** When called with an empty CMA, `RiskfolioAdapter`
  synthesizes annualized vols from a hard-coded per-bucket table
  (`_DEFAULT_VOL_ANNUAL`) and an identity correlation matrix. Expected
  returns default to zero (irrelevant for MinRisk).
* **Real-world interpretation.** Real users must populate a CMA. The
  fallback exists only so the orchestrator can drive the adapter through
  the existing Phase 1 fixtures without first growing a real CMA pipeline.

### L5 — `source` as a PE-leg pairing key is fragile

* **Model behavior.** Per-source PE-leg pairing (test in
  `test_orchestrator.py`) uses `(quarter, source)` to match the
  non-cash leg with its cash offset. With the current pacing, each fund
  produces one row per leg per quarter, so the key is unique.
* **Real-world interpretation.** Multiple calls per fund per quarter,
  recommitment logic, or shared-source flows would break this pairing
  silently. A `flow_id` field is the right Phase 3+ fix; deferred per
  SPEC §10.7 (no speculative abstractions).

### L6 — `correlation_shock` scenario is omitted — [RESOLVED 2026-05-02, Phase 6]

> **Status: RESOLVED in Phase 6.** A scenario-driven correlation shock
> layer was added once Phase 5 landed an explicit CMA correlation
> matrix to perturb. The shock is a discriminated-union schema in
> `io/schemas.py` (`scale` for sign-preserving multiplicative
> amplification with clip-to-`[-1, 1]`; `override` for explicit
> pairwise replacement with auto-mirror) carried as an optional field
> on the `Scenario` dataclass. Application materialises the shocked
> correlations into a new `CMAConfig` substituted into `cfg.cma` —
> so the shock automatically propagates into `config_hash` and the
> run_id, and `_build_ledger` consumes the shocked CMA without any
> shock-aware branching. Validation re-uses the CMA loader's
> per-cell + symmetry + diagonal + PSD checks (fixed `-1e-9`
> tolerance) — failure raises with the smallest eigenvalue in the
> message; no PSD repair, no nearest-matrix projection, no blending.
> A new `crisis_correlation` scenario (the 6th canonical) ships an
> override pushing public_equity ↔ pe_buyout to 0.95 and
> public_bond ↔ public_equity to 0.30. The report gains a
> "Correlation shock (scenario)" section with type, pairwise
> replacement count (or scale magnitude + clipped count),
> max |Δρ| vs baseline, and PSD status.

The original Phase 1 text follows for audit-trail purposes.

* **Model behavior.** `make_scenarios` returns five scenarios, not the
  six suggested in SPEC §6. There is no covariance matrix to perturb.
* **Real-world interpretation.** Realistic stress testing requires
  modeling cross-bucket correlations, especially the equity-bond and
  equity-PE links that tighten in drawdowns. Not addressable until a
  stochastic CMA lands.

### L7 — Smoothing rule with `weight=0` freezes spending — [ACCEPTED LIMITATION 2026-05-02]

> **Status: ACCEPTED LIMITATION** (Phase 10 consolidation). The
> behavior matches the EWMA formula literally
> (``spend_t = w · target_t + (1-w) · spend_{t-1}``; at ``w=0``,
> ``spend_t = spend_{t-1}`` for all t). Documented in the rule's
> docstring and in this entry. Users wanting "flat real with
> inflation" should set ``rule = "flat_real"`` instead. No code
> change planned.

* **Model behavior.** With `smoothing.weight = 0`, `SmoothingRule`
  freezes spending at `target_0 = annual_spend_usd / 4` for the entire
  horizon — no inflation re-anchoring.
* **Real-world interpretation.** This matches the EWMA formula
  literally (`spend_t = 0 · target_t + 1 · spend_{t-1} = spend_0`), but
  it is unlikely to be what a user wants. Users wanting "flat real with
  inflation" should set `rule = "flat_real"`. Documented in the rule's
  docstring.

### L8 — Rebalancer treats PE as a liquid sleeve — [RESOLVED 2026-05-02, Phase 8]

> **Status: RESOLVED in Phase 8.** A new illiquidity overlay layer
> sits between the cost-aware allocator's policy target (Phase 4b
> step 6.5) and the implementation rebalance call (step 7). The
> overlay is **default-on as a correctness fix**, not opt-in. CMA
> liquidity tags (added in Phase 5 as diagnostic metadata) are now
> the source of truth: any bucket tagged ``illiquid`` is locked at
> its current dollars, and the liquid set's policy weights
> renormalise over the residual liquid NAV. **PE rebalance trades
> are exactly zero** in the validated ledger — the load-bearing
> structural invariant added by Phase 8.
>
> PE exposure can now only change through the real-world mechanism:
> commitments → calls → distributions → NAV marks. PE drift away
> from strategic policy is expected, tolerated, and surfaced in the
> new ``## Illiquidity overlay`` section of ``report.md`` with
> per-bucket worst-quarter drift, aggregate drift statistics, and a
> liquid-bucket clipped-to-zero count.
>
> Edge cases fail loudly: ``liquid_nav < 0`` (pathological
> leveraged-via-PE state) raises with a per-bucket breakdown;
> ``liquid_nav == 0`` is allowed only when every liquid bucket
> already has zero current dollars. Cross-config validation enforces
> CMA liquidity coverage of every allocation bucket, ``pe_*`` tagged
> ``illiquid``, non-empty liquid set, and aggregate liquid policy
> weight ``> 0`` — overlay preconditions that would otherwise turn
> into apply-time ``0/0``.
>
> **Pairing with L14 (linear transaction cost only) is intentionally
> not addressed in Phase 8.** The overlay is upstream of cost
> emission — illiquid trades are zero, so no cost is generated on
> them. A realistic PE secondary-market cost model is a separate
> phase. The pre-L8 PE-tradable behavior remains reachable only
> through an internal-only ``base.rebalance.illiquid_overlay: false``
> flag intended for regression-anchor tests.
>
> The original Phase 1 text follows for audit-trail purposes.

* **Model behavior.** The rebalancer trades into and out of PE freely
  to bring the bucket back to target weight every quarter. After a PE
  call drains cash, the rebalancer immediately tops cash up by selling
  some of the PE bucket — at NAV, with no gate.
* **Real-world interpretation.** PE is illiquid. Real rebalancing
  cannot sell a buyout fund position at its NAV mark. The toy fixture
  exercises this only because SPEC §6 P1 explicitly asks the
  configuration to "stress integration logic" by including PE in the
  target weights. A realistic Phase 3+ implementation would treat PE
  positions as fixed-weight or have a separate PE-rebalance gate.
* **Pairing.** This limitation is **tightly coupled to L14** — fixing
  PE liquidity without simultaneously upgrading the cost model
  (PE secondaries cost 5–25% in real markets, not bps) would replace
  one unrealistic regime with another. L8 and L14 must move together
  when this part of the system gets serious.

### L9 — Heavy install footprint for `riskfolio` extra — [ACCEPTED LIMITATION 2026-05-02]

> **Status: ACCEPTED LIMITATION** (Phase 10 consolidation).
> Out-of-our-control third-party dependency footprint. The adapter is
> opt-in via the ``[project.optional-dependencies] riskfolio`` extra
> group, so the core install set stays lean. No fix possible.

* **Model behavior.** `pip install -e ".[riskfolio]"` pulls 80+
  packages including `matplotlib`, `numba`, `vectorbt`, `ipywidgets`,
  `plotly`, `dateparser`, `regex`. Most are unused by `RiskfolioAdapter`
  itself.
* **Real-world interpretation.** Out of our control — these are
  declared in `riskfolio-lib`'s `setup.py`. The adapter is opt-in via
  `[project.optional-dependencies] riskfolio` so the core install set
  remains lean.

### L10 — `/mnt/c` filesystem is unsuitable for `.venv` — [ENVIRONMENT NOTE 2026-05-02]

> **Status: ENVIRONMENT NOTE** (Phase 10 consolidation). Not a model
> limitation; environment-specific guidance for users on WSL2
> machines. Documented to save the next user the diagnostic time.
> Project-local ``.venv`` is now a symlink to a Linux-fs venv at
> ``~/.venvs/aa-model``.

* **Model behavior.** Installing the riskfolio extra into a venv at
  `/mnt/c/Projects/asset allocation/asset-allocation/.venv` took 11+
  minutes in disk-wait state. The same install on `~/.venvs/aa-model`
  (a Linux filesystem) took 40 seconds.
* **Real-world interpretation.** WSL2 + NTFS translation makes
  many-small-file operations very slow. The project-local `.venv` is now
  a symlink to a Linux-fs venv. Documented to save the next user the
  same 10 minutes.

### L11 — Synthetic 2-row dummy returns frame in Riskfolio adapter — [ACCEPTED LIMITATION 2026-05-02]

> **Status: ACCEPTED LIMITATION** (Phase 10 consolidation).
> Third-party API constraint (``riskfolio.Portfolio`` requires a
> returns frame at construction time even when statistics are
> overridden via ``optimization(..., hist=False)``). The adapter's
> 2-row zero-filled workaround is well-documented in
> ``allocation/riskfolio_adapter.py``. Closes when riskfolio's API
> changes or when an alternative optimizer replaces it.

* **Model behavior.** `riskfolio.Portfolio(returns=df)` requires a
  returns frame at construction time for shape / index inference, even
  when the optimizer is later told to use externally-provided statistics
  via `optimization(..., hist=False)`. The adapter feeds it a 2-row
  zero-filled DataFrame (`_synthetic_returns()` in
  `allocation/riskfolio_adapter.py`) and then overwrites `port.mu` and
  `port.cov` with the analytic CMA before calling `optimization()`.
* **Real-world interpretation.** Acceptable for the wiring as Phase 3a
  ships, but it is a coupling point against riskfolio's internals: if a
  future riskfolio version reads the returns frame for anything beyond
  shape/index (e.g. shrinkage estimators, sample-based risk measures,
  whitening), the synthetic zeros could silently produce wrong answers.
  The adapter currently relies on the assumption "riskfolio ignores
  the returns frame after `hist=False`," which is an **assumption about
  internal behavior, not a contract**.

  #### Version-bump policy (mandatory)

  > Any change to `riskfolio-lib` version MUST:
  >
  > 1. pass the structural parity tests in
  >    `tests/test_riskfolio_adapter.py`, **AND**
  > 2. match a frozen-CMA numerical anchor test case within tolerance
  >    (per-bucket absolute weight difference `≤ ε = 1e-4`),
  >
  > **OR** explicitly document the deviation in the Change Log under
  > the version bump's entry, including: the bucket(s) whose weight
  > moved, the magnitude of the move, the upstream change that caused
  > it (riskfolio release notes link), and the decision (accept the
  > new behavior, pin to the old version, or block the bump).
  >
  > **ε rationale.** `1e-4` is one basis point of weight, two orders of
  > magnitude tighter than the smallest economically meaningful
  > allocation step (1 bp position size at $100M NAV ≈ $10k). Tighter
  > would catch solver-reproducibility noise as drift; looser would
  > admit silently-different optimization behavior. The same ε applies
  > by analogy to other adapters' numerical anchor tests unless their
  > Change Log entry documents a different value and why.

  Structural parity alone will not catch subtle numerical drift; a
  numerical anchor case is required for the bump to be safe.

  #### Long-term fix

  Feed riskfolio a properly-shaped synthetic returns frame derived
  from the CMA — e.g. by Cholesky-factoring `cov` and emitting
  deterministic samples whose sample mean and sample cov match the
  CMA exactly. This makes the adapter provably insensitive to the
  current hack. Deferred until a real CMA pipeline lands; until then,
  the version-bump policy above is the load-bearing safeguard.

  An adapter-insensitivity test (compare adapter output with the
  current 2-row zero frame vs. with Cholesky-derived samples) will
  ship alongside that fix.

### L12 — Non-fatal "convert self.cov to a positive definite matrix" warning — [ACCEPTED LIMITATION 2026-05-02]

> **Status: ACCEPTED LIMITATION** (Phase 10 consolidation).
> Riskfolio internal warning, fired before the adapter overwrites
> the covariance matrix; benign. Suppression would require monkey-
> patching riskfolio's logging, which adds fragility for a
> non-functional issue. Closes alongside L11 when the synthetic
> 2-row workaround is no longer needed.

* **Model behavior.** Every `RiskfolioAdapter.fit()` call prints
  `You must convert self.cov to a positive definite matrix` to stderr.
  The covariance matrix the adapter actually hands riskfolio (a diagonal
  matrix of `vol²` against an identity correlation matrix) is positive
  definite by construction; the warning fires from riskfolio's internal
  eigenvalue check after it has run `assets_stats(method_mu="hist",
  method_cov="hist")` against the synthetic 2-row frame from L11
  (sample covariance of two zero rows is the zero matrix, which is
  PSD but not PD). The optimization itself uses our overwritten
  `port.cov`, not the sample one, and converges correctly.
* **Real-world interpretation.** Cosmetic noise as long as L11 holds.
  Suppressing it now would mask a genuine cov problem later, so it is
  left visible. The integration test asserts `weights() != NaN` and
  `sum(weights) ≈ 1` after `fit()`, so a warning that ever became
  fatal would surface as a test failure rather than a silent
  miscalculation.

### L13 — Cvxportfolio adapter has no path dependence — [RESOLVED 2026-05-02, Phase 4b]

> **Status: RESOLVED in Phase 4b.** Cost-aware allocation is now wired
> via the new `CvxportfolioAllocator` engine. The cost-aware optimizer
> solves a single convex problem per quarter (dollar-quadratic policy
> deviation + linear trade cost) using only the explicit
> `(current_dollars, w_policy, cost_model, λ)` inputs — it does not
> read the ledger and never sees future quarters. The
> `ImplementationAdapter.rebalance` adapter remains a pure executor
> with the Phase 3b signature; cost-awareness moved up the stack to
> the allocator. Six anchor tests in `tests/test_cost_aware_allocator.py`
> pin the optimizer's correctness and architectural invariants
> (zero-cost parity, closed-form 2-bucket partial trade, bucket-order
> symmetry, monotonicity in bps, path-blindness, spending-untouched);
> a seventh end-to-end orchestrator test
> (`test_cvxportfolio_allocation_engine_preserves_invariants_end_to_end`)
> confirms ledger invariants hold under the new engine.
>
> The original Phase 3b text follows for audit-trail purposes — the
> "two open questions" cited as gates are answered as follows: (1) no
> multi-period lookahead is wired; the optimizer is single-period and
> reads only `q-1`-or-earlier closed state. (2) The new anchor set is
> the closed-form 2-bucket partial-trade tuple, replacing the
> zero-bps stub-parity anchor that no longer applies.

* **Model behavior.** `CvxportfolioImplementation.rebalance(current,
  target, costs)` is a pure function. Trades depend only on the inputs
  to *this* call; no state from prior calls is retained. The full
  cvxportfolio framework offers multi-period (`MultiPeriodOptimization`)
  and rolling-window estimators that *would* introduce path dependence
  — none of those are wired in.
* **Real-world interpretation.** The adapter does not exploit the
  cost-aware features that make cvxportfolio worth its dependency
  weight. We get linear-cost realism but not cost-aware *trading
  decisions* (e.g. "defer this rebalance because the cost exceeds the
  expected drift correction"). Determinism is therefore preserved
  across runs and across engines: switching from `stub` to
  `cvxportfolio` at zero bps produces byte-identical ledger content
  (same trades, same `cost_usd = 0`); switching at non-zero bps adds
  exactly the linear cost rows and nothing else.
* **Forward risk — cost / rebalance feedback loop.** The current
  pipeline computes trades first and applies cost after. A real
  cost-aware optimizer (Single- or Multi-Period Optimization with a
  cost penalty) would let cost *influence the trade itself* — produce
  a smaller, partial rebalance when cost exceeds the drift correction's
  marginal benefit. Wiring that in requires resolving two open
  questions before any code change:
  1. The Phase 2 "one forward pass per quarter, no backfills, no
     retroactive mutation" rule must be reconciled with the
     optimizer's lookahead horizon (multi-period sees future
     quarters; the orchestrator only writes the current one).
  2. The numerical anchor at zero bps stops being the right test —
     a cost-aware optimizer with bps=0 still reduces to "trade to
     target," but with bps>0 the trade vector itself diverges from
     the stub. New anchors will be needed (likely fixed
     `(current, target, bps, cost-penalty)` tuples with a known
     optimal partial-trade vector).
  Treat this as a Phase 4+ task and a hard prerequisite, not an
  optimization to add later.

### L15 — Owl reacts to forecasted NAV, not realized NAV — [RESOLVED 2026-05-01, Phase 4a]

> **Status: RESOLVED in Phase 4a** (commit landing this entry).
> `OwlRule` now reads ``ledger.end_nav_through(quarter - 1)`` for the
> realized prior-quarter total NAV at every year boundary. The
> ``forecast_quarterly_return_pct`` field has been removed from
> `GuardrailConfig`. The exit-gate test
> `test_owl_cuts_spending_under_realized_drawdown` pins Owl's response
> to a real shock; cumulative Owl spending under `public_drawdown` is
> now strictly ≤ cumulative Owl spending under `base`.

The original Phase 3c text follows for audit-trail purposes:


* **Model behavior.** `OwlRule` computes its guardrail trigger against a
  deterministic forward NAV forecast
  `initial_nav · (1 + forecast_quarterly_return_pct)^t`. It does **not**
  read realized end-of-quarter NAV from the ledger — the
  `SpendingRule.quarterly_outflows` API is called once at run start with
  an empty ledger and returns the full horizon's spending series in one
  shot.
* **`forecast_quarterly_return_pct` is exogenous.** The forecast rate
  is supplied by the user via `spending.guardrail.forecast_quarterly_return_pct`.
  It is **not** derived from fixture returns, the CMA, or scenario
  perturbations. Two runs with different realized return paths
  (e.g. `base` vs `public_drawdown`) but the same forecast assumption
  produce **identical** Owl spending series. Reading the parameter as
  "Owl's view of the future" rather than "the model's expected return"
  is the right mental model.
* **Real-world interpretation.** Real Guyton-Klinger guardrails react to
  the realized portfolio path: a drawdown that pushes the rate above the
  upper band triggers a cut **at that quarter**. Owl's forecasted-NAV
  approach gives the same answer ONLY when the forecast matches reality —
  fine for the deterministic base scenario where forecast and realized
  bucket-weighted return roughly coincide, materially wrong for shock
  scenarios (`public_drawdown` etc.) where Owl will keep ratcheting on
  its smooth forecast while the actual portfolio is in drawdown. To get
  realized-NAV feedback, the orchestrator's "one forward pass per
  quarter, no backfills" rule (Phase 2 close-out) would need a
  per-quarter spending callback added to the `SpendingRule` ABC and a
  matching iterative loop in the orchestrator. Deferred: this is the
  same architectural lift L13 names (cost-aware optimizer feedback) and
  should land together with it as a single Phase 4+ "iterative
  per-quarter rule" upgrade.

### Forward-risk note — two parallel approximations awaiting Phase 4

The system now carries two independent NAV/cost-blind approximations
that both defer to a future iterative-per-quarter rule pass:

* **Allocation side (L13).** `cvxportfolio` is wired as an executor
  only — trades = target − current; no cost-aware optimization. Cost
  does not influence the trade decision.
* **Spending side (L15).** `OwlRule` reads only forecast NAV; cannot
  see realized portfolio drawdowns or scenario-driven NAV deviations.

Either approximation is acceptable in isolation. The risk is that
they would need to be lifted **together**: a cost-aware allocator
that defers trades until cost falls below the marginal-drift benefit
implies a non-deterministic trade vector mid-run, which then changes
realized NAV, which a NAV-aware Owl would then react to — so a
half-fix (e.g., cost-aware allocator without realized-NAV Owl) would
introduce a feedback loop the spending side ignores. Plan to lift L13
and L15 in a single Phase 4 "iterative per-quarter rule" pass; do not
ship one without the other.

### L17 — Cross-engine metric comparability is not meaningful — [ACCEPTED LIMITATION 2026-05-02]

> **Status: ACCEPTED LIMITATION** (Phase 10 consolidation).
> Interpretation problem rather than an architecture problem. Stub
> takes config policy verbatim; riskfolio solves min-var against
> CMA; cvxportfolio allocator (Phase 4b) solves cost-aware against
> policy. The three engines solve genuinely different problems —
> their per-quarter NAV / drawdown / coverage trajectories are not
> intended to be directly compared as if they answered the same
> question. Documented for users running multi-engine probes.

* **Model behavior.** The Phase 3 consolidation probe (60 combinations,
  see `scripts/consolidation_probe_p3.py`) surfaced that the existing
  liquidity / drawdown metrics are not directly comparable across
  allocation engines:

  | engine | min_coverage_months (base) | max_drawdown (public_drawdown) |
  |---|---:|---:|
  | stub allocation | 74.1 | -12.78% |
  | riskfolio allocation | 285.2 | -0.27% |

  Mathematically consistent — riskfolio MinRisk against the placeholder
  vol vector concentrates ~98% in cash, so the household has 285 months
  of cash on hand and the -25% public_equity shock barely registers
  (only 0.1% of the portfolio is in equity). But the headline numbers
  cannot be read at face value across engines.

* **Real-world interpretation.** `coverage_months` and `max_drawdown`
  are informative **within** an allocation policy, not **across**.
  Reading "riskfolio has better drawdown protection" off the table is
  wrong — the protection comes from giving up almost all return
  upside (riskfolio final NAV $107.23M vs stub $114.78M on the base
  scenario). Cross-engine comparison requires either:
  1. constraint-equalized inputs (same target weights via tight box
     bounds, leaving the engines only marginally different),
  2. risk-adjusted measures (Sharpe-style, or a return-per-unit-of-
     coverage-cost composite), or
  3. comparing engines against a *fixed* allocation policy and
     scoring only their *implementation* differences (transaction
     cost, slippage, drift correction).

  This affects only cross-engine reporting; within an engine, the
  metrics remain meaningful. Documented here so the comparison
  report's pivot tables are not misread.

### L18 — Owl misreads inflation shock as "headroom" and raises spending — [RESOLVED 2026-05-01, Phase 4a]

> **Status: RESOLVED in Phase 4a** (commit landing this entry).
> The exit-gate test
> `test_owl_does_not_raise_under_inflation_shock_end_to_end` walks
> year-over-year quarterly spending under both `inflation_shock` and
> `base` and asserts the year-over-year ratio never exceeds the
> inflation factor — i.e. Owl never *raises* under either scenario.
> Empirical Phase 4a behavior under `inflation_shock`: spending
> tracks pure inflation step-up (factor 1.06 per year), no raise
> triggered, no cut triggered (rate stays inside the band given
> realized NAV growing faster than spending). Under a more severe
> inflation shock or against a stagnant portfolio, Owl now correctly
> CUTS once the rate breaches the upper band.

The original Phase 3c text follows for audit-trail purposes:


* **Model behavior.** The Phase 3 consolidation probe surfaced an
  empirical case where Owl's forecast-only NAV design produces
  **backward** spending decisions:

  | scenario | flat_real total spend (20q) | owl total spend (20q) |
  |---|---:|---:|
  | base | $21.03M | $23.81M |
  | inflation_shock | $22.55M (+7.2%) | $24.09M (+1.2%) |

  Flat_real responds to `inflation_shock` as expected: higher
  inflation (6% vs 2.5%) produces ~7% more cumulative spending. Owl's
  response is smaller in *aggregate* but mechanistically perverse:
  Owl's guardrail triggers a **raise** at year-3 under inflation_shock
  (and another at year-4), not a cut.

  Mechanism. Owl uses a fixed `forecast_quarterly_return_pct = 4%/q`
  (~17%/yr) regardless of scenario. Inflation_shock raises the
  inflation step on actual spending from 2.5%/yr to 6%/yr but leaves
  Owl's forecast NAV growth untouched. By year 3:

      rate_year_3   = ($4M · 1.06^3) / ($100M · 1.04^12)
                    = $4.764M / $160.103M = 2.976%
      threshold     = 4% · (1 - 0.20) = 3.20%
      rate < threshold → raise triggers (+10%)

  Owl reads "spending rate falling against forecast NAV" as
  "portfolio outpacing spending — ratchet up." In reality the
  portfolio is no different from base (only the spending path
  changed); the "low rate" is purely a creature of the forecast-vs-
  spending comparison.

* **Real-world interpretation.** A real Guyton-Klinger guardrail
  under an inflation shock would trigger **cuts**: higher spending
  pressure → portfolio at risk → defensive ratchet down. Our Owl
  produces the opposite. This is the L15 limitation made concrete —
  the consolidation probe is the empirical case that justifies why
  L15 binds as a Phase 4 hard prerequisite, not a "nice to have."

  **Mitigation — the only structurally correct fix.**
  Realized-NAV feedback (per L15): Owl reads the running ledger's
  NAV instead of forecasting. Under inflation_shock, realized NAV is
  unchanged from base but realized spending is up, so the realized
  rate rises and the *cut* guardrail fires correctly. Under a
  return-side shock (e.g. `public_drawdown`), realized NAV is down
  while spending is unchanged, so realized rate also rises — same
  cut response, again correct.

  **Trap to avoid — partial-fix path that looks like a fix.**
  Binding `forecast_quarterly_return_pct` to scenario inflation
  (e.g. enforcing
  `forecast_quarterly_return_pct = expected_real_return + scenario_inflation`)
  is **NOT** a real fix:
  * it addresses *inflation-driven* failure only;
  * it does **not** address *return-driven* failure — under a
    public-equity drawdown that leaves inflation unchanged, the
    forecast still tracks its baseline trajectory and Owl still
    misreads the resulting rate mismatch (Owl in fact still
    *raises* spending into a drawdown under this patch);
  * worse, it creates a false sense of correctness for the one
    scenario family it covers while leaving the others silently
    broken.

  Do not implement the trap path as an alternative to realized-NAV
  feedback. Owl's failure is architectural; the only acceptable fix
  is the iterative-per-quarter-rule pass that L13 and L15 also bind.

  The probe's tx-cost-by-engine row also confirms a smaller cross-
  engine subtlety: cvxportfolio under riskfolio allocation costs
  ~$117k cumulative vs ~$65k under stub allocation, even on the
  same fixture. Mechanism: riskfolio's drift-prone heavy-cash target
  forces a ~$83M Q1 turnover (selling equity, buying cash) that the
  stub's pre-aligned 50/20/5/25 target avoids. Documented as a
  cross-engine effect, not a separate limitation.

### L16 — Owl is scale-invariant in initial NAV

* **Model behavior.** Doubling `initial_nav_total` produces an
  **identical** Owl spending series. The trigger condition
  `current_rate ≷ initial_rate · (1 ± band)` reduces algebraically to
  `annual_spend(t) ≷ annual_spend_0 · (1 ± band) · (1+g)^t` — initial
  NAV cancels. Tested directly in
  `tests/test_owl_adapter.py::test_owl_path_is_scale_invariant_in_initial_nav`.
* **Real-world interpretation.** This is a real-world weakness: a $100M
  household and a $1B household with the same initial spending rate
  (4%), inflation, bands, and forecast assumptions get the **same
  spending decisions** through Owl. In reality the larger household has
  more room to absorb a band breach without triggering a cut. The
  invariance is a direct consequence of using rates rather than
  absolute dollar guardrails; switching to an absolute-dollar guardrail
  is a real-world refinement but doesn't qualify as Phase 3c minimum.

### L14 — Only linear transaction cost is modeled — [PARTIALLY RESOLVED 2026-05-02, Phase 10]

> **Status: PARTIALLY RESOLVED in Phase 10.** Resolution is
> engine-conditional and scope-conditional:
>
> * **PE-secondary mispricing concern**: closed by L8 (Phase 8). PE
>   buckets are non-tradable in rebalance under default config; no
>   ``transaction_cost`` row attributable to PE rebalance can exist.
>   PE secondaries, if and when modelled, will land under a new
>   flow type with their own cost regime.
> * **Public-market linear-bps approximation**: documented as
>   appropriate for the modelled scale (trades < ~5% of NAV per
>   quarter; market depth assumption that bps-linear covers slippage
>   + commission). New ``## Transaction cost summary`` report
>   section flags when run-time data crosses scale thresholds where
>   the linear approximation may underprice market impact.
>   **Thresholds are diagnostic heuristics, not validation
>   failures. Crossing them does not invalidate the run; it flags
>   interpretation risk.** A run that breaches a threshold still
>   validates, still passes invariants, still produces a usable
>   ledger.
> * **Asymmetric buy/sell, per-bucket bps, fee economics, liquidity
>   haircuts**: explicitly out of scope for L14 resolution. Each is
>   a separate future phase with its own design and tests.
>
> The original Phase 3b text follows for audit-trail purposes. The
> "Pairing with L8" note at the bottom is updated: L8 closure
> (Phase 8) resolved the specific concern about PE rebalancing
> producing fictional bps cost — that surface no longer exists.

* **Model behavior.** `cost_usd = (bps_per_trade / 1e4) · ∑ |trade|`.
  No quadratic term (market impact ∝ |trade|^1.5 or volume-relative),
  no per-share fixed cost, no asymmetric buy/sell costs, no
  bucket-specific bps (every trade pays the same rate regardless of
  asset class).
* **Real-world interpretation.** Real public-equity / public-bond
  costs are well-approximated by a single linear bps term at the
  position sizes our toy fixture exercises (~$2–25M per trade vs.
  market depth of $100M+). PE rebalances would be wildly mispriced —
  a $20M PE secondary trade has a discount in the 5–25% range, not
  bps. The exit-gate test
  (`test_cvxportfolio_engine_preserves_invariants_under_nonzero_bps`)
  asserts *invariant preservation*, not *cost realism*.
* **Pairing.** Originally tightly coupled to **L8** (rebalancer
  treats PE as a liquid sleeve). **L8 closure in Phase 8 resolved
  this pairing**: PE no longer trades through rebalance, so the
  "uniform bps prices PE secondaries as if they were public equity"
  fiction has no surface to manifest. The Phase 10 advisory section
  surfaces the residual public-market linear-bps approximation as a
  diagnostic, not a gate. Future per-bucket bps / asymmetric / quadratic
  cost work is a clean independent phase — no longer paired against
  L8.

---

## Validation & Testing

### Test surface

71 tests across 9 test modules:

| module | count | what it gates |
|---|---:|---|
| `test_schemas.py` | 9 | extra-keys rejection, sum-to-1, floor ≤ ceiling, TA rate length / sum, quarter regex |
| `test_ledger.py` | 11 | per-row + chain consistency, canonical ordering, rebalance / pe_call zero-sum, external tie-out, total NAV conservation, NaN rejection, finalize-locks-add, end_nav_by_quarter inactive-bucket carry |
| `test_allocation_stub.py` | 3 | stub returns config weights, diagnostics, weights() returns a copy |
| `test_riskfolio_adapter.py` | 10 | structural sum-to-1, no NaN/inf, range, determinism, stub parity contract, binding-equality pinning. Gated on `pytest.importorskip("riskfolio")` |
| `test_spending_rules.py` | 8 | flat-real first-year + boundary inflation, smoothing full / zero / intermediate weight, EWMA closed-form match, factory, floor clip |
| `test_pe_ta.py` | 3 | golden CSV byte equality, projection length, calls sum to commitment |
| `test_orchestrator.py` | 5 | base scenario E2E (<10s), drawdown spot check, dry-run writes nothing, deterministic input hashes / unique run_ids, PE call/distribution per-source cash offset symmetry |
| `test_manifest.py` | 4 | byte-identical content across distinct dirs, manifest schema, run_id construction, explicit invocation_id reproduces dir |
| `test_scenario_builder.py` | 6 | five canonical scenarios, expected overrides only, validates + runs, distinct hash signatures, per-scenario reproducibility, frozen dataclass |
| `test_liquidity.py` | 7 | hand-worked coverage, PE bucket excluded, zero-spend coverage = ∞, shortfall threshold, drawdown on simple path, monotone path returns (0, 0), aggregator end-to-end |
| `test_sweep.py` | 5 | exit gate (5 scenarios <60s), comparison.html + comparison.md written, distinct run dirs, finite metrics, drawdown is the only scenario with `max_dd < -1%` |

Coverage on `integration/` + `pe/ta_model` + `spending/` + `assumptions/`:
**95.89%** (gate ≥ 80% per SPEC §7).

### Invariant strategy

* **Construction-level invariants** (per-row consistency, chain consistency)
  are guaranteed by the ledger's `finalize()` implementation: rows are
  sorted then chained via vectorized cumsum per bucket.
* **Validation-level invariants** (rebalance / pe_call / pe_distribution
  zero-sum, total NAV conservation, no NaN, single run_id) are enforced
  in `QuarterlyLedger.validate()`. Negative-case tests inject violating
  rows directly and assert `validate()` raises.
* **Cross-scenario invariants** (no return-row leakage between scenarios)
  are validated by an ad-hoc inspection script — see Phase 2 close-out.
  The drift across scenarios in non-targeted buckets is uniform in sign
  and magnitude across all liquid buckets, which is the expected signature
  of rebalance-feedback (proportional NAV scaling), not contamination.

### Determinism check (run twice)

The CI `core` job runs `scripts/run_sfo_study.py --config configs/base.yaml`
twice, asserts the two run dirs differ, then loads both `ledger.parquet`
files, drops the `run_id` column, and calls
`pd.testing.assert_frame_equal`. Failure of this step indicates non-determinism
has crept in.

### Adapter parity

Per SPEC §6 P3 exit gate, every non-stub adapter ships with a parity test
gated on `pytest.importorskip(<backend>)`. Parity is structural (not
numerical) — see *Adapter parity contract* above.

### CI workflow

`.github/workflows/ci.yml` runs two jobs on `ubuntu-latest`:

| job | install set | runs |
|---|---|---|
| `core` | `pip install -r requirements-dev.txt && pip install -e .` (no optional deps) | ruff check + format-check, full pytest with coverage gate ≥ 80% on the high-leverage modules, end-to-end determinism check (run twice + compare parquet content modulo `run_id`) |
| `adapters` | `pip install -r requirements-dev.txt && pip install -e ".[riskfolio]"` | adapter parity test (`tests/test_riskfolio_adapter.py`), real `engine=riskfolio` end-to-end run against `configs/base.yaml` |

`adapters` is gated on `core` passing first (`needs: core`). Splitting the
install set keeps the fast lane fast: `core` installs ~20 packages, while
`adapters` pulls 80+ transitive deps (matplotlib, numba, statsmodels,
vectorbt, ipywidgets, …). When new optional adapters land in Phase 3+,
either the `adapters` job grows to install all extras together, or
additional jobs (`adapters-cvxportfolio`, `adapters-stairs`, …) are
added — the choice will be made when the second adapter ships, based on
whether the extras can coexist in one venv without resolution conflicts.

---

## Phase 4 design (pre-implementation)

This section is **binding before any Phase 4 code lands**. It freezes the
iteration model, state-flow contract, API migration plan, determinism
contract, and phase split. No Phase 4 commit may deviate from these rules
without a documented amendment here first. The motivating failures are
**L13** (cost-unaware allocator), **L15** (Owl forecast-only NAV), and
**L18** (Owl misreads inflation shock as headroom — empirically
demonstrated by the consolidation probe at commit `6b8d0fb`).

### Load-bearing rule

> **No rule may depend on the quarter it is currently writing.**
> **Rules may observe only the fully closed ledger through the prior
> quarter.**

This is the single rule everything else flows from. It rules out
fixed-point solving within a quarter, partial-quarter visibility,
and any speculative or pre-rebalance state.

### State-flow contract

A rule called for quarter `q` may observe `ledger[quarter <= q-1]`.
The closed prior quarter includes **all** flow types in canonical
order: `inflow`, `return`, `pe_call`, `pe_distribution`,
`pe_nav_mark`, `spend`, `rebalance`, `transaction_cost`. The rule
**must not** see:

* partial current-quarter return rows;
* pre-rebalance prior-quarter state;
* speculative current-quarter state from any other rule.

This keeps the snapshot every rule sees stable and auditable. Two
rules called for the same quarter `q` see the same ledger view.

### Iteration model

Single forward pass per quarter. No inner loop. No fixed point.

```
for quarter in horizon:
    observed = ledger.closed_through(quarter - 1)
    spend_q  = spending_rule.quarterly_outflow_at(observed, params, quarter)
    target_q = allocation_engine.target_at(observed, params, quarter)
    trades_q = implementation_engine.rebalance(current_q, target_q, costs)
    write all q rows in canonical order
    close q
```

The orchestrator never re-opens a closed quarter. Determinism follows
trivially.

### API migration

Add a per-quarter method to each rule's ABC. Keep the existing
horizon-level method as a default wrapper that loops the per-quarter
method. The orchestrator switches to the per-quarter method in
Phase 4. Rules are **not forked** into "static" and "iterative"
variants.

```python
class SpendingRule(ABC):
    @abstractmethod
    def quarterly_outflow_at(
        self,
        ledger: QuarterlyLedger,   # closed through quarter - 1
        params: SpendingParams,
        quarter: pd.Period,
    ) -> float: ...

    def quarterly_outflows(
        self, ledger: QuarterlyLedger, params: SpendingParams
    ) -> pd.Series:
        # Default wrapper used by Phase 1–3 callers; Phase 4 orchestrator
        # uses quarterly_outflow_at directly. Default loops the per-quarter
        # method; rules whose answer doesn't depend on ledger state can
        # override with a faster vector form if they wish.
        ...
```

> **`quarterly_outflows()` is compatibility-only for path-dependent rules.**
> It is **not** a correctness path. The wrapper iterates
> `quarterly_outflow_at` against a *synthetic* working ledger seeded only
> with the rule's own prior `spend` rows — it has no realized `return`,
> `pe_*`, `rebalance`, or `transaction_cost` flows, so a path-dependent
> rule called through it sees a degenerate trajectory (its own outflows
> against a frozen NAV) rather than the real one.
>
> The authoritative correctness path is the orchestrator-driven
> `quarterly_outflow_at(ledger, params, quarter)` against the live
> ledger closed through `quarter - 1` (`ledger.closed_through(q-1)` /
> `ledger.end_nav_through(q-1)`). All production runs go through this
> path. `quarterly_outflows()` exists for (a) Phase 1–3 callers that
> haven't migrated and (b) unit tests that exercise per-quarter recursion
> in isolation. It must not be used for any analysis that depends on
> realized NAV, costs, or PE flows — including any future cost-aware
> sizing in Phase 4b. Path-dependent rules (`SmoothingRule`, `OwlRule`)
> are guaranteed *correct* only on the orchestrator path.

Per-rule migration:

* `FlatRealRule` — no ledger reads required; the per-quarter method
  re-derives the inflated quarterly target from config alone.
* `SmoothingRule` — reads its own prior `spend` row from the closed
  ledger to recover `spend_{t-1}`; computes
  `w · target_t + (1-w) · spend_{t-1}`. No more shared state across
  calls.
* `OwlRule` — reads `ledger.end_nav_through(q-1)` for the realized
  prior-quarter total NAV; reads its own prior `spend` row to recover
  the year's `annual_spend`; applies the year-boundary inflation +
  guardrail check against **realized** prior NAV (not forecast). This
  is the structural fix for L15 and L18.

For allocation and implementation adapters, a parallel
`*.target_at(ledger, params, quarter)` method or equivalent is added
when Phase 4b lands; Phase 4a keeps allocation / implementation on
their existing per-call APIs (see split below).

### q0 initialization

> **q0 is initialization, not a guardrail decision.**

At `q == start_quarter` the closed ledger has no rows for the rule to
read. The rule must produce a baseline value with no guardrail check,
no inflation step, no special ledger event:

```python
if quarter == params.start_quarter:
    return cfg.annual_spend_usd / 4.0
```

This is the rule's responsibility, not the orchestrator's. The
orchestrator does **not** seed q0 from outside; the rule owns q0
initialization end-to-end. Architectural rationale:

* keeps the orchestrator's per-quarter loop uniform — every
  quarter calls the same `quarterly_outflow_at` method;
* preserves "no orchestrator-side prior_spend state" — the
  orchestrator never holds a baseline value across calls;
* lets each rule define its own q0 semantics if needed (e.g., a
  future rule might compute q0 from a different baseline).

`FlatRealRule`, `SmoothingRule`, and `OwlRule` all return
`annual_spend_usd / 4` at q0. From `q == start_quarter + 1` onward
they branch to their respective per-quarter logic.

### Prior-spend-row source filter

> **A path-dependent `SpendingRule` may only read prior `spend` rows
> where `source == its own rule source`.**

Path-dependent rules (`SmoothingRule`, `OwlRule`) read their own
prior outflows from the closed ledger to recover `spend_{t-1}` or
the prior year's `annual_spend`. The source filter prevents a rule
from reacting to spend history produced by a *different* rule —
e.g., if a user switches `spending.rule` mid-horizon (not currently
supported, but cheap to defend against here) the new rule will not
inadvertently treat the previous rule's outflows as its own
trajectory.

Phase 4a wires per-rule source identifiers on emission, mirroring the
existing `impl:<engine>` and `pacing:<fund>` conventions:

* `FlatRealRule` emits `source="spending:flat_real"`
* `SmoothingRule` emits `source="spending:smoothing"`
* `OwlRule` emits `source="spending:owl"`

Rules read prior rows by filtering `flow_type == "spend"` AND
`source == "spending:<self>"`. This is a **rule-side contract**, not
an orchestrator-enforced one — the orchestrator continues to emit
whatever the rule produces, and the path-dependent rule polices its
own reads.

### Storage rule (load-bearing for Phase 4a)

> **No orchestrator-side prior_spend state. No q0 special emission
> outside the rule. The rule owns q0 initialization.**

Combined with the closed-prior-quarter view, this keeps the ledger as
the only state spine — the same rule that has held since Phase 1.

### Ledger capability addition

The current `QuarterlyLedger.finalize()` is one-shot and locks
appends. Phase 4 needs a read-only intermediate view:

```python
class QuarterlyLedger:
    def closed_through(self, quarter: pd.Period) -> ClosedLedgerView: ...
```

Returns a snapshot of all rows with `quarter <= q-1`, sorted in
canonical order, with `nav_start` / `nav_end` chained — the same
shape `finalize()` produces today, but for a partial range. The
ledger continues to accept appends after `closed_through()` is
called; only `finalize()` locks. **No shadow state** is introduced —
the view is computed from the existing append buffer on demand. A
helper `ledger.end_nav_through(q-1) -> pd.Series` returns end-of-
quarter NAV per bucket at `q-1`, the most common rule consumption
pattern.

### Determinism contract — Phase 4 addition

Existing contract holds: same configs + same fixture data → same
ledger content modulo `run_id`.

Phase 4 addition:

> **Rules may use solvers only if outputs are rounded /
> canonicalized before ledger emission.** Phase 4a forbids
> solver-based feedback entirely.

Phase 4a does **not** wire cvxportfolio cost into the optimizer's
objective (i.e., L13's cost-aware optimizer is explicitly deferred).
Phase 4b is the earliest a solver may inform a trade decision, and
even then within the strict closed-prior-quarter model — the
optimizer sees prior closed state and the current-quarter target,
nothing forward.

### Phase 4 split

| sub-phase | scope | gates |
|---|---|---|
| **4a — Per-quarter observation API** | new `quarterly_outflow_at` ABC method; `closed_through` ledger view; OwlRule reads realized prior NAV; FlatRealRule + SmoothingRule migrate to per-quarter wrappers; orchestrator switches to per-quarter spending. **No allocation / implementation API changes.** | regression: 103 existing tests pass under the new API; new tests pin Owl's correct behavior under `public_drawdown` (must cut, not raise) and `inflation_shock` (must cut). L15 and L18 marked resolved. |
| **4b — Cost-aware allocation** | new `target_at` per-quarter ABC method on `AllocationAdapter`; new opt-in `cvxportfolio` allocator engine that solves a single convex problem per quarter (dollar-quadratic policy deviation + linear trade cost); orchestrator passes `current_dollars` explicitly. **Implementation API unchanged** — `rebalance` stays a pure executor, no `rebalance_at`. Spending byte-identical to 4a. Still no fixed-point. | new closed-form 2-bucket anchor for cost-aware partial-trade vector; zero-cost parity, monotonicity-in-bps, path-blindness, and spending-untouched anchors. L13 marked resolved. |
| **4c — Optional fixed-point research** | within-quarter fixed-point solving for joint allocator + spending decisions. **Not production default.** Behind a config flag if it lands at all. | research only; not gated for ship. |

4a is the only sub-phase that addresses an empirically demonstrated
failure (L18). It must ship first. 4b is dependent on 4a's
infrastructure. 4c is optional and not blocking.

### What 4a is **not**

Listed explicitly so a future contributor reads them as guardrails:

* Not a fixed-point or iterative solve within a quarter.
* Not a sidecar / shadow state object outside the ledger.
* Not a multi-pass orchestrator (one pass per quarter, full stop).
* Not an API fork between static and iterative spending rules.
* Not a change to the canonical flow order or `transaction_cost`
  classification (the §Core Invariants block remains binding).
* Not a cost-aware optimizer (deferred to 4b).
* Not a fix for L17 (cross-engine metric comparability is a
  reporting / interpretation problem, not an architecture problem).

### Phase 4b design (cost-aware allocation, pre-implementation)

> **One-line goal.** Cost enters the **allocation** decision so the
> allocator can produce a partial-rebalance target when the cost of
> trading toward policy exceeds the marginal benefit. **Spending logic
> is byte-identical to 4a.** No fixed-point. No within-quarter
> iteration. Optimizer never reads future state.

#### Where cost lives — load-bearing decision

Cost-awareness lives in the **allocator** (`target_at`), **not** the
implementation. The implementation API does not change in 4b —
`ImplementationAdapter.rebalance(current, target, costs)` keeps the
Phase 3b signature and remains a pure executor. The cost-aware
trade-off is fundamentally an allocation choice (deviate from policy to
save cost); the trade is a downstream consequence. Reports that read
"the allocator's target" then see the actual target. Putting cost in
the rebalancer would split the economics from where readers expect it
and break the implementation's zero-cost-parity anchor.

The 4b deliverable adds **only** `target_at` to `AllocationAdapter`. No
`rebalance_at` is added. (The earlier Phase 4 split-table entry that
listed `rebalance_at` has been corrected.)

#### Optimization (single convex problem, one solver call per quarter)

The cost-aware allocator solves, for each quarter `q`:

```
λ_eff         = policy_loss_lambda_norm / V_total²
trade_dollars = w · V_total - current_dollars

minimize  λ_eff · ‖ w · V_total - w_policy · V_total ‖²
        + cost_per_dollar · ‖ trade_dollars ‖₁
subject to
        Σ w = 1
        0 ≤ w ≤ 1
        box bounds (per-bucket min/max from constraints)
```

with:

* `w_policy` — static policy weights (today's `weights()` output, the
  destination the allocator would pick absent costs).
* `current_dollars` — pre-rebalance NAV per bucket at quarter `q`,
  passed explicitly by the orchestrator (see *State channel* below).
* `V_total = current_dollars.sum()` — pre-rebalance total NAV.
* `cost_per_dollar = bps_per_trade / 1e4` — same coefficient as
  cvxportfolio's `StocksTransactionCost(a)`.
* `λ` — policy-deviation weight, surfaced as
  `allocation.policy_loss_lambda_norm` config field, default `1.0`.
  Internally the allocator computes
  `λ_eff = policy_loss_lambda_norm / V_total²` per quarter; the
  ``V_total²`` factor in the dollar-quadratic policy term and the
  divisor cancel mathematically, so the user-facing ``λ_norm`` is
  scale-invariant in the policy term (at the same fractional weight
  deviation, the policy-loss intensity is identical regardless of
  NAV). The **threshold at which partial-trade behavior engages**
  still scales with ``V_total / λ_norm`` because the L1 cost term is
  linear in dollars; the normalization fixes the policy term's units,
  not the policy/cost balance.

  > **Calibration note (2026-05-02 sweep, see
  > `docs/sweep_lambda_calibration_2026_05_02.md`):** the default
  > ``λ_norm = 1.0`` is **corner-dominated** at institutional NAV
  > scales. At V_total = $100M with bps ≥ 5, the cost-aware optimum
  > is bit-identical across ``λ_norm ∈ [0.01, 1e3]`` — the policy
  > gradient is too weak relative to the L1 cost term for interior
  > partial-trade behavior to engage. Sensitivity becomes visible
  > only at ``λ_norm ≈ 1e6`` and above for $100M / 5 bps.
  >
  > Rough rule of thumb for engaging interior partial-trade behavior:
  >
  > ```
  > λ_norm ≈ bps_per_trade × V_total × 1e-3
  > ```
  >
  > (e.g. ``λ_norm ≈ 5e5`` at $100M / 5 bps; ``λ_norm ≈ 1e8`` at
  > $100M / 100 bps). The default ``1.0`` is intentionally
  > conservative — it produces effectively cost-aware-OFF behavior
  > at institutional scales (the optimizer suppresses over-trading
  > but does not weight policy deviation against cost in a tunable
  > way). This is documented behavior, not a bug; calibrate empirically
  > against the desired policy-track-vs-cost-suppress balance for
  > the target portfolio.
  >
  > **Advisory diagnostic, no auto-tuning.** The orchestrator's
  > ``report.md`` emits a "Cost-aware allocator calibration (advisory)"
  > section when ``allocation.engine=cvxportfolio``, showing the
  > configured ``λ_norm`` against the rule-of-thumb suggested value at
  > the run's median ``V_total`` and a regime classification
  > (corner-dominated / tunable / policy-dominated). The allocator
  > also exposes the per-quarter calibration history via
  > ``alloc.diagnostics()["calibration_history"]`` for deeper
  > inspection. **The advisory does not auto-tune ``λ_norm`` and does
  > not influence the optimization** — the user keeps explicit control
  > over the configured value; the diagnostic just makes the
  > policy/cost balance legible without re-running a calibration sweep
  > by hand.

  See also the §Phase 4b — normalized λ migration note in the change
  log.

**Why dollar-quadratic policy deviation, not weight-quadratic.** Both
terms are now in dollars (policy in dollars², cost in dollars). λ has
interpretable scale and behavior is stable across NAV sizes — a
weight-based formulation would mix a unitless penalty with a dollar
penalty, hiding implicit units inside λ.

**Why the cost term is named `trade_dollars`.** Transaction cost is
proportional to **trade size**, not to position deviation from policy.
The `trade_dollars = w · V_total - current_dollars` definition makes
the per-quarter trade vector explicit. Multi-period reasoning (Phase 5+)
will need this distinction.

This is **one solver call per quarter**. No back-edge to spending or
to the ledger; no iteration; no future quarters. Strict single forward
pass.

#### Allocator API change

```python
class AllocationAdapter(ABC):
    # Existing — preserved as the cost-blind policy reference.
    @abstractmethod
    def weights(self) -> pd.Series: ...

    # New in 4b. Default impl returns weights() (cost-blind passthrough)
    # so non-cost-aware adapters work unchanged.
    def target_at(
        self,
        ledger: QuarterlyLedger,        # closed through q-1; per 4a contract
        params: AllocationParams,
        quarter: pd.Period,
        current_dollars: pd.Series,     # bucket → pre-rebalance dollars at q
        cost_model: CostModel,
    ) -> pd.Series:                      # bucket → target weights, sums to 1.0
        return self.weights()
```

`AllocationParams` is a new dataclass mirroring `SpendingParams`:
`config: PublicAllocationConfig`, `start_quarter: pd.Period`,
`num_quarters: int`.

Per-engine semantics:

* `StubAllocator.target_at` — returns `weights()` (config-verbatim).
  Cost-blind. Behavior identical to today.
* `RiskfolioAdapter.target_at` — returns `weights()` (min-variance
  solution from `fit`). Cost-blind. Behavior identical to today.
* **`CvxportfolioAllocator` (new)** — solves the cost-aware problem
  above. **The only adapter that introduces cost-aware behavior.**
  Wired through `make_allocator(engine="cvxportfolio")`. Cost-aware
  allocation is **opt-in**; the default engine remains `stub`.

q0: at `quarter == params.start_quarter`, `current_dollars` is the
initial NAV vector. Cost-aware adapters return `weights()` at q0 (no
prior trade context to reason about). This mirrors the spending rule's
q0 contract.

#### State channel — how prior + current state reach the allocator

Two channels, both already present or trivial extensions of 4a:

1. **Retrospective state** (quarters ≤ `q-1`): `ledger.closed_through(q-1)`
   and `ledger.end_nav_through(q-1)` — same 4a contract. Available to
   the allocator if a future cost-aware variant wants it; **the
   cost-aware optimizer specified above does not read it.**
2. **Current-quarter pre-rebalance state**: orchestrator passes
   `current_dollars` as an explicit parameter, computed from the
   existing per-quarter `running_nav` dict after canonical-order
   steps 0–6 (inflow / return / pe_* / spend) and before steps 7–8
   (rebalance / transaction_cost).

**Why pass `current_dollars` explicitly rather than derive it from the
ledger.** Under 4a's strict closure rule, `closed_through(q-1)` does
*not* include current-quarter pre-rebalance state. The optimizer needs
that state. Passing it as a named function argument is the explicit,
auditable channel — keeps the rule "ledger closed-through-q-1 is the
only retrospective state" intact, while making the current-quarter
pre-rebalance input visible at the call site.

> **Optimizer input rule (load-bearing).** The cost-aware optimizer
> reads ONLY:
>   * `current_dollars` (parameter)
>   * `w_policy` (the adapter's `weights()` output)
>   * `cost_model` (parameter)
>   * `λ` (config)
>
> It does **not** read `ledger`, does not read past quarters, does not
> read future quarters. The path-blindness anchor test (below) pins
> this: two runs that arrive at the same `current_dollars` from
> different histories produce the same `target_at` output.

#### Determinism

Risks: solver nondeterminism (cvxpy / clarabel / scs / osqp can produce
slightly different bytes across versions or platforms), and
floating-point reduction order in cost-penalty assembly.

Mitigations:

* **Solver outputs are canonicalized before ledger emission.** Round
  target weights to 12 decimal places; renormalize sum-to-1 by
  deterministic correction on the largest-weight bucket. The same
  rounding happens inside the adapter; the ledger sees only the
  rounded values.
* **Pinned solver versions** in the optional-dependencies group:
  `cvxportfolio==1.5.1`, `cvxpy==1.8.2`, plus pinned `clarabel` /
  `scs` / `osqp`.
* **Fixed solver tolerance** (`solver_tol = 1e-9`); not exposed as a
  user knob without dedicated test coverage.
* **Same-environment determinism is the contract**, matching the
  existing repo guarantee. Cross-environment determinism is not
  promised.

This extends — not replaces — the Phase 4 determinism contract: rules
may use solvers only if outputs are rounded / canonicalized before
ledger emission.

#### Numerical anchor tests (new file: `tests/test_cost_aware_allocator.py`)

L13's "at zero cost, cvx == stub bit-for-bit" anchor breaks once an
optimizer is involved (zero-cost cost-aware solution still equals
"trade to policy" only when policy-loss dominates). New anchors:

1. **Zero-cost parity.** With `cost_per_dollar == 0`, `target_at`
   MUST equal the policy weights for any `current_dollars` (all three
   adapters: stub, riskfolio, cvxportfolio).
2. **Closed-form partial-trade.** Two-bucket case (cash, equity), known
   `current`, policy 50/50, cost `c` bps, `λ` known. The optimum is
   computable analytically (1D quadratic + L1 problem). Pin to
   `1e-9` USD.
3. **Symmetry.** Swapping bucket order (cash, equity) ↔ (equity, cash)
   in inputs swaps the outputs. Guards against bucket-order-dependent
   solver behavior.
4. **Monotonicity in bps.** Increasing `bps_per_trade` produces trade
   magnitudes element-wise ≤ those at lower bps, holding policy +
   current fixed. Catches optimizer regressions.
5. **Path-blindness.** Two runs that arrive at the same
   `current_dollars` from different histories produce the same
   `target_at` output. Pins the optimizer-input rule above.
6. **Spending-untouched.** Owl + flat_real + smoothing produce
   identical spending series under a 4b run vs a 4a run holding
   `(current, target)` paths matched. Pins the directive that
   spending is byte-identical to 4a.

The existing `tests/test_riskfolio_adapter.py` stays green —
riskfolio is unchanged. The existing
`tests/test_cvxportfolio_adapter.py` (implementation, not allocation)
stays green — implementation is unchanged.

#### Invariants that must still hold (every 4a invariant, byte-for-byte)

* All §5.1 ledger invariants — including the **4a spend-uniqueness**
  hardening just landed (one row per `(run_id, quarter, source)` for
  `spend`).
* `transaction_cost` remains an external-on-cash outflow with no
  offset elsewhere; included in NAV conservation set.
* Canonical flow order unchanged.
* Spending source filter unchanged; `quarterly_outflow_at` unchanged;
  spending continues to see only `q-1` closed state.
* "No orchestrator-side state across calls" preserved — `running_nav`
  was already carried in 4a; 4b extends the per-quarter rebalance
  step, not the state model.

**New invariant introduced by 4b.** Cost-aware target is a function of
`(w_policy, current_dollars, cost_model, λ)` only. No call to
`ledger.closed_through(q)` (current quarter); no peek beyond `q-1`.
Tested by anchor #5 (path-blindness).

#### Orchestrator change (minimal — between current canonical steps 6 and 7)

```python
# 6.5 — Phase 4b: cost-aware target. Optimizer sees prior closed
# ledger and pre-rebalance current dollars; never future state.
current_dollars = pd.Series(running_nav, dtype=float)
target_weights = alloc.target_at(
    ledger, alloc_params, q, current_dollars, cost_model
)
target_nav = (target_weights * total_nav).reindex(running_nav.keys()).fillna(0.0)
```

Steps 7 (rebalance) and 8 (transaction_cost) are unchanged.

The pre-existing `alloc.fit(...)` + `alloc.weights()` calls at run
start become "set the policy reference"; the per-quarter target is
now `target_at`'s output. For `engine in {stub, riskfolio}` the
default-wrapper `target_at` returns `weights()` and the orchestrator
behavior reduces to today's behavior bit-for-bit.

#### Spending-side untouched (re-asserting forbidden list)

Spending logic is byte-identical to 4a:

* `quarterly_outflow_at` API is unchanged.
* `OwlRule` continues to read `ledger.end_nav_through(q-1)` for the
  realized prior-quarter total NAV at every year boundary.
* `SmoothingRule` continues to read its own prior `spend` row from
  the closed ledger.
* `spend` flow position in canonical order is unchanged.
* The spending decision uses the **closed-prior-quarter ledger** —
  it does NOT see this quarter's cost-aware target or pre-rebalance
  NAV. Exposing the cost-aware target to spending would re-introduce
  the cycle 4a closed.

#### What 4b is **not**

Listed explicitly so a future contributor reads them as guardrails:

* Not a fixed-point or iterative solve within a quarter.
* Not within-quarter iteration. One `target_at` call per quarter.
  One `rebalance` call per quarter. Same as 4a.
* Not a multi-period optimization. Allocator never reads quarter `q`
  or beyond. cvxportfolio's `MultiPeriodOptimization` is **not**
  wired in.
* Not a spending modification. `quarterly_outflow_at`, spending source
  filter, and `spend` flow position are byte-identical to 4a.
* Not a default-flip. `engine=stub` remains the default; cost-aware
  is opt-in via `engine=cvxportfolio` until promotion evidence
  lands.
* Not a cost-flow change. `transaction_cost` stays an external-on-cash
  outflow with the existing `impl:<engine>` source.
* Not an `ImplementationAdapter` API change. `rebalance(current,
  target, costs)` keeps the Phase 3b signature.

---

## Phase 5 design (pre-implementation)

> **One-line goal.** Replace placeholder allocation assumptions
> (riskfolio's hard-coded `_DEFAULT_VOL_ANNUAL` table, identity
> correlation, zero expected returns) with a validated, explicit
> capital market assumptions layer loaded from a config YAML. **L4
> closes** when this lands. Strictly assumption-layer work — no
> objective reformulation, no STAIRS, no PE pacing changes.

### Three load-bearing decisions

#### A. Bucket universe = allocation bucket universe

CMA covers **every** allocation bucket — public sleeves *and* PE
sleeves. Reason: the allocator solves a constrained MV problem over
the whole bucket index; if PE has no vol/corr entry the optimizer
either has to fabricate one (today's fallback) or refuse to run. The
PE pacing model still drives **realized** PE flows; the CMA gives the
allocator's *prior view* of PE risk for the optimization. Two
distinct concerns, both legitimate.

#### B. CMA is a config artifact, not a code constant

Today: `CMA()` instantiated empty in the orchestrator, riskfolio
adapter falls back to hardcoded numbers. After this design: CMA is
loaded from a YAML pointed at by `base.cma.config` (parallel to
`allocation.config`, `spending.config`). Hardcoded fallback is kept
**for test paths only** (callers passing `CMA()` literally get the
fallback; runs going through the orchestrator with a real
`base.yaml` must point at a real CMA YAML).

#### C. CMA is **not** consumed by the cost-aware allocator in Phase 5

> **Important callout (do not assume otherwise).** Phase 5 introduces
> validated CMA inputs but does **not** wire them into any allocator
> objective. `CvxportfolioAllocator.target_at` is unchanged — its
> objective remains
> ``λ_norm · ||(w − w_policy) · V_total||² + cost_per_dollar · ||trade_dollars||₁``
> with no return / vol / correlation inputs. CMA is consumed by
> ``RiskfolioAdapter`` (replaces the placeholder fallback in the
> MinRisk solve) and by ``report.md`` (diagnostics: portfolio
> expected return, expected vol). Surfacing CMA inside the cost-aware
> objective is a Phase 6+ task that requires explicit objective
> reformulation, design review, and new anchor tests. **Having a
> populated `expected_returns_annual` field does not mean the
> optimizer is using it.** This rule preserves the Phase 4b ship's
> integrity.

### Schema

New `configs/cma.yaml`, validated by a new `CMAConfig` pydantic model
(`io/schemas.py`):

```yaml
# configs/cma.yaml
expected_returns_annual:
  cash:           0.040
  public_bond:    0.045
  public_equity:  0.075
  pe_buyout:      0.105
  # ... entry per allocation bucket

vol_annual:
  cash:           0.005
  public_bond:    0.040
  public_equity:  0.160
  pe_buyout:      0.200

correlations:
  # full symmetric matrix; every bucket × bucket entry required.
  # diagonal == 1.0 enforced by the validator.
  cash:           { cash: 1.0, public_bond: 0.10, public_equity: -0.05, pe_buyout: 0.0 }
  public_bond:    { cash: 0.10, public_bond: 1.0, public_equity: 0.20, pe_buyout: 0.30 }
  public_equity:  { cash: -0.05, public_bond: 0.20, public_equity: 1.0, pe_buyout: 0.65 }
  pe_buyout:      { cash: 0.0, public_bond: 0.30, public_equity: 0.65, pe_buyout: 1.0 }

liquidity:
  cash:           liquid
  public_bond:    liquid
  public_equity:  liquid
  pe_buyout:      illiquid
  # optional. Used for liquidity-coverage diagnostics, not the optimizer.
```

Schema model:

```python
class CMAConfig(BaseModel):
    model_config = _STRICT
    expected_returns_annual: dict[str, float]
    vol_annual: dict[str, float]
    correlations: dict[str, dict[str, float]]
    liquidity: dict[str, Literal["liquid", "semi_liquid", "illiquid"]] | None = None
```

`base.yaml` gains `cma: { config: configs/cma.yaml }`. `StudyConfig`
gets a `cma: CMAConfig` field. CMA covers **every** allocation
bucket; the cross-config validator enforces this.

**Why explicit full matrix, not upper-triangle:** keeps the YAML
readable as a matrix (every cell visible), eliminates "did I forget
to list (i,j)?" ambiguity, and the symmetry check becomes a real
validation rather than an unwritten convention.

### Validation rules

**Pydantic-level (per-file):**

1. ``vol_annual[b] ≥ 0`` for every bucket.
2. ``expected_returns_annual[b]`` finite (no NaN / inf).
3. ``|expected_returns_annual[b]| < 1.0`` for every bucket — guards
   the percent-vs-decimal mistake (a config that sets
   ``public_equity: 5`` instead of ``0.05`` fails loudly with a
   bucket-named error). The bound is intentionally far above any
   realistic annualized expected return (~0.15 max for risk assets);
   it's a sanity check, not a modeling restriction.
4. ``correlations[i][j] ∈ [-1.0, 1.0]`` for every cell.
5. ``correlations[i][i] == 1.0`` within ``1e-9``.
6. ``correlations[i][j] == correlations[j][i]`` within ``1e-9``
   (symmetry).
7. ``expected_returns_annual.keys() == vol_annual.keys() ==
   correlations.keys()`` (bucket sets agree internally).
8. Each ``correlations[i].keys()`` matches the global bucket set
   (no missing entries, no extras).
9. ``liquidity`` (if present) covers the same bucket set; values
   restricted to ``{liquid, semi_liquid, illiquid}``.

**Cross-config (`io/validation.py`):**

10. CMA bucket set ``==`` ``allocation.stub_weights.keys()``.
    Missing or extra buckets fail with a precise diff in the error
    message.
11. Computed covariance matrix ``Σ = diag(vol) · corr · diag(vol)``
    is **PSD** (smallest eigenvalue ≥ ``-1e-9``). The fixed
    tolerance is intentional — a user-tunable PSD threshold is a
    validation concern, not a modeling parameter, and exposing it
    invites silent acceptance of bad inputs.

**Failure mode:** loud and immediate at config load (per SPEC §2.2).
No silent regularization, no nearest-PSD repair, no automatic
clipping. PSD failure surfaces as
``"CMA covariance matrix is not positive semi-definite; smallest
eigenvalue = X"``.

### Loader and types

``io/loaders.py`` gets ``load_cma_config(path) -> CMAConfig``.
``assumptions/cma.py``'s ``CMA`` dataclass keeps its current shape
(``expected_returns_annual: pd.Series``, ``vol_annual: pd.Series``,
``corr: pd.DataFrame``); a thin adapter
``CMA.from_config(cfg: CMAConfig) -> CMA`` constructs the dataclass —
sorted bucket index, explicit ``float`` dtype.

**Reproducibility hashing:** the CMA file content is folded into the
existing ``fixtures_hash``, so a CMA edit invalidates run
reproducibility the same way fixture edits do. The manifest gains
nothing new — ``fixtures_hash`` already covers it.

### Consumers

| consumer | before | after |
|---|---|---|
| `RiskfolioAdapter.fit` | empty CMA → falls back to `_DEFAULT_VOL_ANNUAL` + identity corr + zero ER | reads explicit CMA. `_DEFAULT_VOL_ANNUAL` retained but **only** used when `cma == CMA()` (empty default — test-only path). Production callers always pass loaded CMA. |
| `StubAllocator.fit` | ignores CMA | unchanged |
| `CvxportfolioAllocator.fit` | accepts CMA, doesn't use in `target_at` | unchanged. CMA is **available** but not consumed; documented as "future cost-aware-with-return-foregone variant." |
| `report.md` | no CMA section | new "Capital market assumptions" section: per-bucket expected return / vol; portfolio-level expected return (`w_policy · expected_returns`); portfolio expected vol (`sqrt(w_policy.T · Σ · w_policy)`); liquidity bucket counts. |

### Fallback policy

* ``CMA()`` empty default → riskfolio uses hardcoded
  ``_DEFAULT_VOL_ANNUAL`` + identity corr. **Test-only.** A test
  marker (e.g., ``_using_test_fallback = True`` in diagnostics) makes
  this visible.
* Orchestrator-loaded CMA (default behavior) → adapters use the
  loaded values; the fallback path is unreachable from production.
* Removing the fallback entirely is **deferred** to keep the
  existing test surface green without rewriting fixtures. Once test
  fixtures migrate to explicit CMA YAMLs the fallback can be deleted
  in a follow-up.
* L4 entry flips to ``[RESOLVED]`` when default config + adapters
  use the loaded CMA in production paths.

### Migration path

The initial shipped ``configs/cma.yaml`` replicates today's defaults
(values from ``_DEFAULT_VOL_ANNUAL`` + identity correlations + zero
expected returns) so that:

* existing reports, ledger contents, and reproducibility hashes are
  byte-stable across the cutover (modulo the new "Capital market
  assumptions" section in ``report.md``);
* "real CMA" calibration becomes a separate concern, deferred to
  whenever empirical inputs are available;
* the cutover is purely *structural* — the assumption surface
  becomes config-explicit, but the values being optimized over
  don't change.

The "real CMA" — non-trivial correlations, calibrated expected
returns, vol cone — is a deliberate non-goal of Phase 5.

### Tests

New test file ``tests/test_cma_loader.py``:

1. **Round-trip.** Valid YAML → ``CMAConfig`` → ``CMA`` dataclass;
   index, columns, dtypes, and values match.
2. **Negative vol fails.** ``vol_annual.public_equity = -0.1``
   raises with bucket name in message.
3. **NaN expected return fails.**
4. **Out-of-bounds expected return fails.**
   ``expected_returns_annual.public_equity = 5.0`` fails (the
   percent-vs-decimal guard).
5. **Out-of-range correlation fails.** ``corr[a][b] = 1.05``.
6. **Asymmetric correlation fails.**
   ``corr[a][b] = 0.5; corr[b][a] = 0.4``.
7. **Diagonal != 1 fails.**
8. **Bucket-set mismatch fails.** CMA has ``pe_growth`` but
   allocation has ``pe_buyout`` only; cross-config validator names
   both missing/extras.
9. **Non-PSD matrix fails.** Constructed example: pairwise
   correlations all in ``[-1, 1]``, full matrix has a negative
   eigenvalue. Error message includes the smallest eigenvalue.
10. **Liquidity tags optional + validated.** Wrong tag string
    fails; absent block is fine.

New test in ``tests/test_riskfolio_adapter.py``:

11. **Adapter parity with explicit CMA.** Same numerical anchor as
    today's binding-equality test, but with the explicit CMA loaded
    — proves the adapter is reading the loaded values (not the
    hardcoded fallback).

End-to-end:

12. **Orchestrator picks up CMA.** ``report.md`` contains the new
    "Capital market assumptions" section with values matching the
    loaded YAML.

### What Phase 5 is **not**

Listed explicitly so a future contributor reads them as guardrails:

* **Not a STAIRS layer.** Static CMA only; no S-curve, no regime
  switching, no time-varying inputs.
* **Not a PE realism change.** PE pacing model untouched; CMA
  carries PE vol/corr/ER as flat priors.
* **Not an objective reformulation.** Cost-aware allocator's
  objective unchanged; CMA available but not consumed there.
* **Not a return-shock generator.** CMA is a static prior;
  ``fixture_scenario.returns`` continues to perturb realized returns
  through separate plumbing.
* **Not a stress-test layer.** Phase 6+ may add ``correlation_shock``
  (L6); not in scope here.
* **Not a Bayesian / Black-Litterman views layer.** Phase 7+ if ever.
* **Not a calibration helper for ``λ_norm``.** The 4b advisory
  diagnostic stays separate.

---

## Phase 6 design (pre-implementation) — correlation_shock (L6)

> **One-line goal.** A scenario-driven perturbation layer that
> modifies the **CMA correlation matrix only**, preserving the
> invariant ``CMA = baseline prior; scenario = perturbation layer``.
> No vol changes, no return changes, no allocator-objective changes,
> no time variation. **L6 closes** when this lands.

### Load-bearing rules

1. **CMA immutability.** The loaded ``CMA`` is never mutated. The
   shock operates on a copy and produces a new ``CMA`` instance.
2. **Perturbation-only.** Only ``corr`` may change. ``vol_annual`` and
   ``expected_returns_annual`` pass through unchanged.
3. **Static per run.** The shock is applied once, before the
   per-quarter loop starts. No quarter-dependent or time-varying
   correlation.
4. **Validity is hard.** The post-shock matrix must satisfy:
   symmetry, diagonal == 1, entries in ``[-1, 1]``, and the
   assembled covariance ``Σ = diag(vol) · corr · diag(vol)`` must
   pass the same PSD check the CMA loader uses
   (``λ_min ≥ -1e-9``). Any violation **fails loudly** at run
   start. **No silent repair**, no nearest-PSD projection, no
   blending.
5. **No optimizer awareness change.** ``RiskfolioAdapter`` consumes
   the shocked CMA exactly as it consumes the baseline.
   ``CvxportfolioAllocator`` continues to ignore CMA entirely. No
   change to either objective.

### Scenario schema (discriminated union)

```python
class _ScaleCorrelationShock(BaseModel):
    type: Literal["scale"]
    magnitude: float  # positive, finite

class _OverrideCorrelationShock(BaseModel):
    type: Literal["override"]
    matrix: dict[str, dict[str, float]]  # partial; auto-mirrored

CorrelationShock = Annotated[
    _ScaleCorrelationShock | _OverrideCorrelationShock,
    Field(discriminator="type"),
]
```

The shock attaches to a ``Scenario`` (carried alongside the existing
optional ``fixture_scenario`` / ``pe_pacing`` / ``spending`` fields).
A new optional ``correlation_shock: CorrelationShock | None`` field
is added to the ``Scenario`` dataclass. Existing scenarios that
don't set it pass through with no CMA modification.

### Variant semantics

#### `scale` — sign-preserving amplification

```
ρ_new[i,j] = clip(ρ_baseline[i,j] · magnitude, -1, 1)   for i ≠ j
ρ_new[i,i] = 1                                            (diagonal preserved)
```

* Positive correlations move **further from 0** (more positive).
* Negative correlations also move **further from 0** (more
  negative — the operation is sign-preserving multiplication, not
  shrink-toward-1).
* Use `scale` to amplify existing co-movement direction. To force
  a "crisis-style all-risky-toward-+1" regime, use `override`
  instead — `scale` is not the right tool for that.
* `magnitude` must be positive and finite. Negative magnitudes
  would flip every off-diagonal sign — almost certainly a user
  error; failed at schema time.
* The clip-to-``[-1, 1]`` step is the only deviation from pure
  multiplication. The report emits ``max |Δρ|`` and a count of
  clipped entries so silent saturation is visible to the reader.
* `scale` always applies to **every off-diagonal entry**. It does
  not take a `target` field — for targeted stress, use `override`.
  Avoids overlapping capability between the two variants.

#### `override` — explicit pairwise replacement

```python
matrix: dict[str, dict[str, float]]
```

* User specifies one direction (``matrix["a"]["b"] = 0.95``);
  validator auto-mirrors to ``corr_new[a,b] = corr_new[b,a] = 0.95``.
* If both directions are supplied and they **disagree** within
  ``1e-9``, **fail loudly** with both values in the error. Do not
  silently average.
* Unspecified entries pass through from the baseline ``corr``.
* Unknown bucket names fail at apply time with a precise
  bucket-name error.
* Diagonal entries (``matrix["a"]["a"]``) must equal ``1.0``
  within ``1e-9`` if specified; otherwise omitted.
* Per-cell values must be in ``[-1, 1]``.

### Application point

Inside the orchestrator's ``_build_ledger``:

```
baseline_cma = CMA.from_config(cfg.cma)
cma          = (apply_correlation_shock(baseline_cma, scenario.correlation_shock)
                if scenario and scenario.correlation_shock else baseline_cma)
alloc.fit(returns=..., cma=cma, constraints=...)
```

Both the baseline and the shocked CMA are surfaced to the report so
the "max |Δρ|" diagnostic can be computed without re-running the
shock logic.

### Validation order (apply-time)

1. ``apply_correlation_shock`` constructs a new correlation
   ``DataFrame`` from the baseline copy + the shock.
2. Per-cell bounds re-checked (clip for ``scale``; range check for
   ``override``).
3. Symmetry holds by construction (``scale`` is symmetric in the
   multiplicand; ``override`` writes both ``[i,j]`` and ``[j,i]``
   with the same value). No symmetrization step needed.
4. PSD check: assemble ``Σ = diag(vol) · corr_new · diag(vol)``;
   compute ``λ_min``; raise ``ValueError`` if
   ``λ_min < -1e-9`` with ``λ_min`` in the error message.
5. Construct and return a new ``CMA`` dataclass; the baseline is
   left intact (immutability).

### Report additions

A new top-level ``## Correlation shock (scenario)`` section appears
in ``report.md`` only when a shock is active. Includes:

* shock ``type``
* ``scale``: ``magnitude``; count of entries clipped to ``[-1, 1]``
* ``override``: count of cell pairs replaced
* ``max |Δρ|`` against the baseline ``corr`` (off-diagonal only)
* PSD status (``pass`` if we reached this section — failure is loud
  at apply time)
* a one-line note that the CMA baseline was preserved and this is
  a perturbation layer

The existing ``## Capital market assumptions`` section continues to
display the **shocked** values (since that is what the allocator
actually saw), with no change to its layout.

### What Phase 6 is **not**

Listed explicitly so a future contributor reads them as guardrails:

* **Not a vol or return shock.** ``vol_annual`` and
  ``expected_returns_annual`` pass through unchanged. Magnitude /
  override fields cannot touch either.
* **Not time-varying.** A single shock is applied once at run
  start; no per-quarter trajectory, no regime switching.
* **Not a CMA replacement.** The baseline ``cfg.cma`` is the
  prior; the shock perturbs a copy. Replacing the CMA would
  collapse the "baseline vs perturbation" separation.
* **Not a STAIRS layer.** Phase 7+. Not unblocked until L6 is in
  observation.
* **Not a PSD repair.** Failure is the verdict; the user fixes the
  shock spec, not the validator.
* **Not an optimizer-objective change.** Both allocator engines
  consume the shocked CMA exactly as they consume the baseline.

### Tests planned

Schema (pydantic-level):

* ``scale`` rejects non-finite or non-positive magnitude.
* ``override`` rejects per-cell values outside ``[-1, 1]``,
  diagonal != 1, conflicting symmetric pairs.
* Discriminated union routes by ``type``.

Apply-time:

* ``scale`` is sign-preserving (positive and negative correlations
  both grow in magnitude).
* ``scale`` clips beyond ``[-1, 1]`` and exposes the clipped count.
* ``override`` partial merge + auto-mirror.
* ``override`` rejects unknown bucket names with the bucket name
  in the error.
* ``override`` rejects asymmetric supply with both values in the
  error.
* PSD failure raises with ``λ_min`` in the error message.
* Baseline ``CMA`` is unchanged after ``apply_correlation_shock``
  (immutability).

End-to-end:

* New ``crisis_correlation`` scenario applies an ``override`` shock
  (the shipped CMA has identity off-diagonals, so ``scale`` is a
  no-op against it; ``override`` is the right vehicle for
  end-to-end visibility).
* Riskfolio sees the shocked correlations.
* Report includes the new ``## Correlation shock (scenario)``
  section with the right diagnostics.

### Locked design choices

* Full matrix throughout (no upper-triangle form).
* PSD tolerance fixed at ``-1e-9`` (re-using the existing CMA
  validator's threshold).
* No exposure of the tolerance as a config field.
* ``liquidity`` field is unaffected — shocks are correlation-only.

---

## Phase 7 design (pre-implementation) — STAIRS PE adapter

> **One-line goal.** Replace the TA model's constant ``growth_pct``
> with a CMA-driven, public-equity-coupled growth term so PE NAV
> responds to public-side scenario moves. Introduce a PE adapter
> pattern (mirroring the allocation / implementation layers) so TA
> stays the default and STAIRS is opt-in. **Resolves L1 partially**:
> the "free return lift" mechanism that ``clustered_calls`` and
> ``delayed_pe_distributions`` produced under TA is closed; any
> residual scenario-driven PE return effect under STAIRS is the
> *real* timing-coupling term, not an artifact.
>
> Strictly assumption-layer work — no objective reformulation, no
> Monte Carlo, no recommitment optimizer, no ledger schema change.

### Scope reset

In PE-modeling literature STAIRS often connotes **stochastic / Monte
Carlo** cash-flow paths with regime switching. That is an
architectural change (the engine is single-path by construction) and
explicitly out of scope here. Phase 7 ships a **deterministic,
single-path** STAIRS variant: same call schedule, same distribution
curve, same NAV chain — only the per-quarter NAV-mark term changes.
Stochastic / Monte Carlo extensions are deferred to a future phase.

### What STAIRS replaces vs the current TA model

| dimension | TA today | STAIRS v1 |
|---|---|---|
| NAV growth driver | constant ``growth_pct`` | ``idiosyncratic_drift_pct + beta · realized_public_equity_excess_return`` |
| Coupling to public-side scenarios | none (drives L1) | proportional to ``beta`` per sleeve |
| Cash-call / distribution mechanics | ``rate_of_contribution``, ``bow``, ``yield_pct``, commitment-period schedule | unchanged |
| Per-fund projection schema | ``PROJECTION_COLUMNS`` (10 cols) | unchanged |
| Deterministic | yes | yes (single-path, deterministic in inputs) |
| Multi-path / Monte Carlo | no | **no** — explicitly deferred |
| Recommitment optimizer | no | **no** — out of scope |

Net change: one term in the TA recursion is replaced. Everything
else (call schedule, distribution curve, per-row schema) is
byte-compatible. This is the smallest structural change that
resolves L1.

### Adapter pattern

New module layout (mirrors the ``allocation/`` and
``implementation/`` adapter layers):

```
pe/
  base.py             # PEAdapter ABC
  ta_adapter.py       # TAAdapter — wraps the existing TA model
  stairs_adapter.py   # STAIRSAdapter — new
  factory.py          # make_pe_adapter(engine)
  ta_model.py         # unchanged
  pacing.py           # unchanged (used by TAAdapter)
```

```python
class PEAdapter(ABC):
    @abstractmethod
    def project_horizon(
        self,
        pacing: PEPacingConfig,
        horizon_start: pd.Period,
        num_quarters: int,
        *,
        cma: CMA,
        public_equity_path: pd.Series,
    ) -> pd.DataFrame:
        """Return PE projections (PROJECTION_COLUMNS schema) for the
        configured funds, filtered to the horizon. Both ``cma`` and
        ``public_equity_path`` are required arguments — the TA adapter
        ignores them, which is fine.
        """
```

Engine selector: a new ``pe.engine: Literal["ta", "stairs"] = "ta"``
field on ``BaseConfig.pe``. Default is ``ta``; every existing config
keeps its behavior bit-stable.

### Required PE input schema

``PEPacingConfig`` extends with optional STAIRS fields. Required
when ``pe.engine == "stairs"``; absent fails loudly at config
validation time:

```yaml
ta_defaults:
  lifetime_years: 12
  commitment_period_years: 4
  rate_of_contribution: [0.25, 0.30, 0.25, 0.20]
  bow: 2.5
  yield_pct: 0.0
  growth_pct: 0.13                  # TA only; STAIRS ignores

stairs_defaults:                    # NEW. Required when pe.engine == "stairs".
  per_sleeve:
    pe_buyout:
      idiosyncratic_drift_pct: 0.05  # annual; replaces TA growth_pct
      beta_to_public_equity:    1.20
    pe_venture:
      idiosyncratic_drift_pct: 0.06
      beta_to_public_equity:    1.50
    # ... one entry per pe_* sleeve in allocation.stub_weights

funds:
  - name: BuyoutFund_2026Q1
    commitment_usd: 25000000
    vintage: "2026Q1"
    sleeve: pe_buyout
```

Schema rules:

* ``idiosyncratic_drift_pct`` finite, ``|x| < 1.0`` (same
  percent-vs-decimal guard as Phase 5 ER).
* ``beta_to_public_equity`` finite. No bounds (values > 2 are
  documented unusual but allowed).
* Cross-config validator: when ``pe.engine == "stairs"``,
  ``stairs_defaults.per_sleeve`` keys must equal the ``pe_*``
  subset of ``allocation.stub_weights``. Missing or extra sleeves
  fail with a precise diff (mirrors Phase 5 CMA bucket-set check).
* When ``pe.engine == "ta"``, ``stairs_defaults`` is ignored
  (allowed but not required).

### STAIRS recursion

Per quarter ``t`` for a fund with ``sleeve = s``:

```
expected_quarterly_pu = cma.expected_returns_annual["public_equity"] / 4
realized_quarterly_pu = public_equity_path.get(quarter_t, expected_quarterly_pu)

excess  = realized_quarterly_pu - expected_quarterly_pu
drift   = stairs_defaults.per_sleeve[s].idiosyncratic_drift_pct / 4
beta    = stairs_defaults.per_sleeve[s].beta_to_public_equity

growth_pct_q = drift + beta * excess
growth_pct_q = max(growth_pct_q, -0.99)         # required clipping
nav_mark_t   = nav_after_dist * growth_pct_q
```

Quarters outside the public-equity path (pre-horizon for funds with
vintages before ``horizon_start``, or post-horizon) default to
``excess = 0`` — i.e., the path-uninformed quarters use ``drift``
alone. Documented as "we don't know what happened; assume CMA
expectation." This keeps determinism: same fund + same horizon +
same ``public_equity_path`` → same projection bytes.

#### Required tightening: growth-term clipping

> **``growth_pct_q ≥ -0.99``**, enforced at the per-quarter recursion
> step. NAV cannot drop below zero (-100%); upside is unbounded
> (consistent with the rest of the model). Without this, a deep
> public drawdown × high beta could push ``growth_pct_q`` below -1
> and produce a negative NAV chain — breaking implicit economic
> constraints and corrupting downstream distribution / IRR
> diagnostics.
>
> The clip is a **domain constraint**, not a silent repair. The
> count of quarters where the clip activated is surfaced in the
> diagnostics so the user sees when it's biting.

### Cash-call / distribution / NAV contract

**Unchanged.** STAIRS uses the TA call schedule and distribution
curve verbatim. Only the NAV-mark term changes. ``PROJECTION_COLUMNS``
is byte-compatible. The orchestrator's PE-flow emission code is
unchanged — it consumes the same frame regardless of engine.

This is load-bearing: the ledger invariants (per-source pairing
test L5; per-quarter zero-sum tests for ``pe_call`` and
``pe_distribution``; end-of-quarter NAV checks) all remain valid
without engine-specific branches.

### How outputs enter the ledger

No change. The orchestrator's per-quarter loop (``_build_ledger``
steps 3–5: ``pe_call``, ``pe_distribution``, ``pe_nav_mark``) emits
the same rows. STAIRS just produces a frame with the same columns;
the only difference is the *values* in ``nav_mark_usd`` and
``nav_end_usd``.

### Determinism contract

* ``STAIRSAdapter.project_horizon`` is a **pure function** of
  ``(pacing, horizon, cma, public_equity_path)``. No module-level
  state, no clocks, no randomness.
* Same inputs → byte-identical outputs.
* ``public_equity_path`` is computed deterministically from
  ``fixture_scenario.returns`` (already deterministic).
* CMA dump is in ``config_hash`` (Phase 5); fixture scenario in
  ``fixtures_hash``; both already invalidate ``run_id`` correctly.
  ``stairs_defaults`` is in ``cfg.pe_pacing.model_dump`` and
  flows through ``config_hash`` automatically.

### Parity tests vs TA

The structural anchor: **STAIRS at ``beta = 0`` and
``idiosyncratic_drift_pct = TA.growth_pct`` for every sleeve must
produce byte-identical output to TA**. Same pattern as riskfolio's
binding-equality structural parity.

1. ``test_stairs_at_zero_beta_matches_ta_per_fund`` — for the
   shipped fixture, set ``beta=0,
   idiosyncratic_drift_pct=ta_defaults.growth_pct``;
   ``STAIRSAdapter.project_horizon(...)`` must equal
   ``TAAdapter.project_horizon(...)`` to ``1e-9`` USD per cell.
2. ``test_stairs_engine_at_parity_yields_byte_stable_orchestrator_run``
   — at ``pe.engine=stairs`` with the parity settings, the full
   orchestrator run produces byte-identical ledger rows to the
   ``pe.engine=ta`` run.

These two pin that STAIRS is a strict generalization, not a
re-implementation that drifts.

### Numerical anchor tests (beyond parity)

3. **Beta amplification under drawdown.** Under ``public_drawdown``,
   STAIRS at ``beta=1.5`` produces a strictly lower terminal PE
   NAV than at ``beta=0`` for the affected quarters. Closed-form
   per-quarter delta = ``beta · excess · pre_distribution_NAV``.
4. **Idiosyncratic-only path.** At ``beta=0``, varying
   ``idiosyncratic_drift_pct`` from 0.05 to 0.13 changes terminal
   PE NAV monotonically — proves the drift term is wired.
5. **Public-equity decoupling.** At ``beta=0``, two scenarios that
   differ only in ``fixture_scenario.returns.public_equity`` produce
   identical PE projections — proves no leakage.
6. **Linear commitment property.** Two funds in the same sleeve
   with same vintage and split commitment (``$X + $Y`` vs
   ``$X+Y``) produce summed-equal ``pe_*`` flows under STAIRS — pins
   linearity in commitment size, a TA property that must survive.
7. **Growth-clip activation under extreme drawdown.** A fixture
   with public_equity at ``-50%`` for one quarter × ``beta=2.0``
   would push ``growth_pct_q`` below ``-1.0``; the clip must
   activate, the NAV chain must stay non-negative, and the
   diagnostic ``clipped_quarters`` count must be ``> 0``.

### Fallback behavior if STAIRS is unavailable

* Default config ships ``pe.engine: ta``. Existing runs unchanged.
* If a config sets ``pe.engine: stairs`` but ``stairs_defaults``
  is missing or the sleeve set doesn't match
  ``allocation.stub_weights``'s ``pe_*`` subset, **validation
  fails loudly at config load** (same pattern as Phase 5 CMA
  bucket-set check; same pattern as Phase 4a removed-field
  check). **No silent fallback to TA.**
* A future stochastic STAIRS variant gets a different engine name
  (``stairs_mc``) — ``stairs`` v1 is reserved for the
  deterministic single-path adapter.

### Reports / diagnostics

Optional, nice-to-have, not load-bearing:

* In the PE summary section of ``report.md``, show the engine name
  (``ta`` or ``stairs``).
* Under STAIRS, show per-sleeve ``(beta, idiosyncratic_drift_pct)``
  and the count of quarters where the growth clip activated.

### What Phase 7 is **not**

Listed explicitly so a future contributor reads them as guardrails:

* **Not stochastic.** Single-path, deterministic.
* **Not Monte Carlo / multi-path / regime-switching / GBM /
  jump-diffusion.** Deferred to a future phase.
* **Not a recommitment optimizer.** Configured fund schedule
  remains the full plan.
* **Not a new ledger schema.** ``PROJECTION_COLUMNS`` is unchanged.
* **Not an L8 fix.** Rebalancer's perception of PE doesn't change.
* **Not an L2 fix.** Dynamics remain deterministic.
* **Not a public-side return generator.** CMA's role is
  ``expected_returns_annual`` only; public return paths still
  come from ``fixture_scenario.returns``.
* **Not a per-sleeve coupling generalization.** Phase 7 couples
  every PE sleeve to ``public_equity`` only. A future phase can
  introduce per-sleeve coupling-source mappings (e.g., ``pe_infra
  → public_bond + public_equity`` blends); not in scope here.

### L1 status under STAIRS

L1 flips to ``[PARTIALLY RESOLVED 2026-05-02, Phase 7]`` on
implementation. Resolution wording:

* **Free return lift removed.** Under STAIRS, scenarios that move
  public_equity move PE proportional to ``beta`` per sleeve. The
  TA-era artifact where ``clustered_calls`` and
  ``delayed_pe_distributions`` produced unmotivated cumulative
  return lift is closed.
* **Residual timing effect remains, but is now economic, not
  artifactual.** Calls deployed during a public drawdown buy at
  a NAV that subsequently recovers if public_equity recovers —
  that's the actual timing-coupling channel, not a bug.
* **L1 stays open under TA.** When a user runs ``pe.engine=ta``
  the original artifact persists. The doc note records this
  engine-conditional resolution.

### Locked design choices

* Single-path, deterministic.
* Coupling reference: ``public_equity`` only (no per-sleeve
  mapping yet).
* Excess baseline: ``cma.expected_returns_annual["public_equity"] / 4``
  (CMA-anchored; no separate parameter).
* Adapter module layout: ``pe/base.py`` + ``pe/ta_adapter.py`` +
  ``pe/stairs_adapter.py`` + ``pe/factory.py``.
* Required tightening: ``growth_pct_q ≥ -0.99`` clip at the
  per-quarter recursion step, with the clip count surfaced in
  diagnostics.
* L1 marked ``[PARTIALLY RESOLVED]`` under STAIRS; stays open under TA.

---

## Phase 8 design (pre-implementation) — PE illiquidity in rebalancing (L8)

> **One-line goal.** Resolve L8: the rebalancer treats PE buckets as
> liquid. Phase 8 inserts an **illiquidity overlay** between the
> allocator's policy target and the implementation's rebalance call,
> using CMA liquidity tags as the source of truth. PE exposure can
> only change through ``pe_call`` / ``pe_distribution`` /
> ``pe_nav_mark`` flows — the rebalancer no longer trades PE.
> **Default-on as a correctness fix**, not opt-in; out-of-tree
> regression comparisons keep an internal-only opt-out flag.

### Core principle

```
PE (any bucket tagged illiquid) is non-tradable in rebalance.
```

Rebalance trades for illiquid buckets are forced to zero. PE calls,
distributions, and NAV marks are **unchanged** — those remain the
only legitimate channels for PE exposure changes. Liquid sleeves
absorb the entire rebalancing burden over the residual liquid NAV.
PE drift away from policy is **expected**, **tolerated**, and
surfaced as diagnostics in the report.

### Load-bearing rules

1. **PE flows remain distinct from rebalance.**
   ``pe_call`` / ``pe_distribution`` / ``pe_nav_mark`` rows are
   unchanged. ``rebalance`` rows for illiquid buckets are zero by
   construction.
2. **Rebalance only liquid buckets.** Illiquid buckets are locked at
   their post-pe-flow current dollars. Liquid sleeves rebalance
   within the residual ``V - sum(C_illiquid)``.
3. **Two target concepts.**
   ``policy_target = allocator.target_at(...)`` is the strategic
   intent.
   ``execution_target = apply_liquidity_overlay(policy_target,
   current_dollars, liquidity)`` is what the implementation engine
   actually consumes. The allocator stays unaware of liquidity in
   Phase 8.
4. **No optimizer-objective change.** Both ``StubAllocator`` and
   ``CvxportfolioAllocator`` continue to produce policy targets
   without illiquidity awareness. The cost-aware allocator's
   per-quarter cost calculation may be slightly less coherent under
   the overlay (some cost it accounted for in PE moves never
   materialises), but the Phase 4b objective and anchor tests are
   preserved verbatim.

### Liquidity source of truth — promoted from diagnostic to execution input

Phase 5 introduced ``cma.liquidity`` as optional diagnostic
metadata. Phase 8 promotes it:

* When the overlay is on (default), ``cma.liquidity`` **must** cover
  every allocation bucket.
* All ``pe_*`` buckets **must** be tagged ``illiquid``.
* The liquid set (``liquid`` ∪ ``semi_liquid``) **must** contain at
  least one bucket.
* The aggregate policy weight across the liquid set **must** be
  ``> 0``. Otherwise the renormalisation ``w_j / Σ w_L`` is ``0/0``.

All four checks live in the cross-config validator (mirroring the
Phase 5 / 6 / 7 patterns). Loud failure at config load.

### Execution target formula

Let ``I`` = illiquid buckets, ``L`` = liquid buckets,
``V = total current NAV``, ``C_b`` = current dollars in bucket
``b``, ``w_b`` = policy weight for bucket ``b``.

```
execution_dollars[i] = C_i              for i ∈ I    (locked)
liquid_nav           = V - Σ_{i ∈ I} C_i
liquid_policy_w[j]   = w_j / Σ_{k ∈ L} w_k          (renormalise)
execution_dollars[j] = liquid_nav · liquid_policy_w[j]   for j ∈ L
execution_weight[b]  = execution_dollars[b] / V
```

Result: PE rebalance trade = 0; liquid trades sum to zero;
``Σ execution_weight = 1``; portfolio drifts away from strategic
PE target as PE NAV evolves through calls / distributions / marks.

### Edge cases — fail loudly

* **`liquid_nav < 0`** (illiquid current dollars exceed total NAV —
  pathological leveraged-via-PE state): **fail loudly** with
  ``liquid_nav`` value and the per-bucket breakdown in the error.
  No silent repair.
* **`liquid_nav == 0`** is allowed **only** when every liquid
  bucket's current dollars are already zero (genuine no-op). Any
  other case where ``liquid_nav == 0`` and at least one liquid
  bucket has nonzero current dollars **fails loudly** — that would
  imply selling those liquid positions to zero, which is almost
  certainly wrong.
* **Empty liquid set** or **zero aggregate liquid policy weight**:
  fails at cross-config validation (above), not at apply time.

### Module location

```
src/aa_model/allocation/liquidity_overlay.py
```

Generic over CMA liquidity tags — not PE-specific. PE happens to be
the only illiquid bucket today; a future credit / real-estate / LP
bucket marked ``illiquid`` would be locked the same way.

### Application point in the orchestrator

Inserted between Phase 4b's step 6.5 (cost-aware target) and the
existing step 7 (rebalance):

```python
# 6.5  cost-aware target (Phase 4b)
target_weights = alloc.target_at(ledger, alloc_params, q,
                                 current_dollars, cost_model)

# 6.6  illiquidity overlay (Phase 8)  —  default-on
if cfg.base.rebalance.illiquid_overlay:
    target_weights, liquidity_diag = apply_liquidity_overlay(
        policy_weights=target_weights,
        current_dollars=current_dollars,
        liquidity=cma.liquidity,
    )

# 7.  rebalance to (possibly overlay-adjusted) target weights
target_nav = (target_weights * total_nav).reindex(...).fillna(0.0)
result = impl.rebalance(current_nav, target_nav, cost_model)
```

The implementation adapter is **unchanged**.

### Internal-only opt-out — `rebalance.illiquid_overlay`

A new ``base.rebalance.illiquid_overlay: bool = True`` field on
``RebalanceConfig``. **Production default is ``True``.** The
``False`` case exists **only** to preserve the pre-L8 PE-tradable
behavior under a regression-anchor test fixture so future bug
investigations can compare. It is **not advertised in user docs**
and **not a recommended user-facing mode**. The field is in the
config schema (visible in ``config_hash``) so a regression run is
loud about its non-default state.

### Transaction-cost treatment

Costs apply only to **executed liquid trades**. Because illiquid
rebalance trades are zero, illiquid buckets contribute no
``transaction_cost``. PE calls and distributions remain separate
flows and are not transaction-cost-generating; the Phase 3b
accounting rule (``transaction_cost`` is an external cash outflow
on the household) is preserved.

### Per-quarter diagnostics surfaced in `report.md`

Per illiquid bucket:

* policy weight
* current weight
* drift = current − policy

Aggregates:

* ``max_abs_illiquid_drift_pct``
* ``sum_abs_illiquid_drift_pct``
* ``clipped_to_zero_liquid_count`` — count of (quarter, liquid
  bucket) pairs where the post-overlay execution dollar amount
  rounds to ≤ \$1 (analog to STAIRS's ``clipped_quarters`` and the
  cost-aware allocator's advisory diagnostics)

The existing report sections are unchanged; Phase 8 adds a new
``## Illiquidity overlay`` block when the overlay is active.

### Invariants

Every existing §5.1 ledger invariant remains unchanged:

* per-row consistency, per-bucket chain consistency, per-quarter
  per-bucket flow tie-out, total NAV conservation, external cash-
  flow tie-out, rebalance per-quarter zero-sum,
  pe_call / pe_distribution per-quarter zero-sum, spend uniqueness.

**New invariant introduced by Phase 8:**

> For any bucket tagged ``illiquid`` in ``cma.liquidity``:
> **no `rebalance` rows exist** in the validated ledger.

Equivalent test:

```python
df[(df.flow_type == "rebalance") & df.bucket.isin(illiquid)].empty
```

This becomes the L8 load-bearing structural invariant.

### Tests planned

Unit (overlay function):

1. PE / illiquid rebalance trades are exactly zero across multiple
   pre-rebalance dollar mixes.
2. Liquid weights renormalise to the hand-worked example (cash
   4.33%, bond 17.33%, equity 43.33%, PE 35.00% from the design
   example).
3. ``Σ execution_weight == 1`` to ``1e-12`` across a parameter
   sweep.
4. Multi-sleeve illiquid fixture (e.g., ``pe_buyout`` + ``pe_venture``)
   — both buckets locked; liquid sleeves renormalise across the
   remaining liquid policy weight.
5. ``liquid_nav < 0`` raises ``ValueError`` with a precise per-bucket
   breakdown.
6. ``liquid_nav == 0`` allowed only when every liquid bucket's
   current dollars are zero; raises otherwise.

Schema / cross-config:

7. CMA missing ``liquidity`` field while overlay is on fails at
   cross-config validation.
8. CMA marks all PE sleeves ``liquid`` while overlay is on fails.
9. Empty liquid set or zero aggregate liquid policy weight fails.

End-to-end orchestrator:

10. Default-on shipped fixture: full run, ledger validates, **no
    `rebalance` rows on `pe_buyout`**, illiquidity-overlay report
    section present with per-bucket drift.
11. Internal opt-out (``rebalance.illiquid_overlay: false``):
    pre-L8 PE-tradable behavior reproduced; this is the
    regression-anchor fixture only.
12. PE call / distribution mechanics unaffected — paired
    cash offsets per quarter still zero-sum (existing test continues
    to pass).
13. STAIRS engine + overlay default-on: full run validates; PE
    drift reflects STAIRS-coupled growth + no rebalance.

### What Phase 8 is **not**

Listed explicitly so a future contributor reads them as guardrails:

* **Not a secondary-market PE sale path.** No way to reduce PE
  except via distributions.
* **Not a PE purchase path.** No way to increase PE except via
  committed-fund calls.
* **Not a commitment optimiser** or pacing-recommitment model.
* **Not a STAIRS change.** STAIRS continues to drive NAV marks; L8
  is upstream of that on the rebalance side.
* **Not a transaction-cost model for PE secondaries** — secondaries
  aren't modelled at all.
* **Not a liquidity-stress liquidation path.** A drawdown that
  drives ``liquid_nav < 0`` fails loudly; it does not auto-liquidate
  PE.
* **Not an allocator-objective reformulation.** The cost-aware
  allocator stays unchanged; teaching it illiquidity is a future
  phase.

### L8 status under Phase 8

Will flip to ``[RESOLVED 2026-xx-xx, Phase 8]`` on implementation.
Resolution wording:

* PE is no longer tradable through rebalance under default config.
* PE exposure changes only through commitments → calls →
  distributions → NAV marks (the real-world mechanism).
* PE drift away from strategic policy is expected, tolerated, and
  surfaced in the report.
* The pre-L8 PE-tradable behavior remains reachable only through an
  internal-only ``rebalance.illiquid_overlay: false`` flag intended
  for regression comparisons.

### Locked design choices

* Default-on as **correctness fix** — Phase 8 may intentionally
  change default ledger outputs. The implementation Change Log
  must explicitly account for any numeric-anchor changes (which
  tests get re-anchored, why, and the new pass count).
* ``liquid_nav < 0`` fails loudly; ``liquid_nav == 0`` allowed only
  when current liquid positions are all zero.
* Empty liquid set fails at cross-config validation; aggregate
  liquid policy weight must be ``> 0``.
* Module location: ``allocation/liquidity_overlay.py`` — generic
  over liquidity tags, not PE-specific.
* Diagnostics: per-illiquid-bucket policy weight / current weight /
  drift; aggregate ``max_abs_illiquid_drift_pct`` and
  ``sum_abs_illiquid_drift_pct``; ``clipped_to_zero_liquid_count``.
* Internal-only opt-out: ``base.rebalance.illiquid_overlay: bool =
  True``. Default-on production behavior; ``False`` reserved for
  regression-anchor tests.
* Pre-L8 calibration / probe artifacts that aren't regenerated
  should carry a "pre-L8" tag in their header so future readers
  don't compare values across the L8 cutover unawares.

---

## Phase 9 design (pre-implementation) — manager / fund metadata enrichment

> **One-line goal.** Enrich PE pacing inputs with manager and fund
> metadata so client-realistic questions become answerable in the
> report (commitments / unfunded / calls / distributions / NAV by
> manager; vintage and manager concentration). **No PE math change.**
> **No ledger schema change.** Allocator never sees managers; the
> rebalancer never sees managers; the ledger continues to use
> ``source = "pacing:<fund_name>"`` exactly as today. Manager
> identity is a labeling layer on top of the existing per-fund
> projection.

### Architectural rule (now load-bearing)

```
managers / funds   → PE pacing model (FundConfig)
PE sleeves         → allocation model (stub_weights[pe_*])
PE illiquidity     → rebalancing overlay (Phase 8)
```

Phase 9 stays inside the first lane. The other two are untouched.

### Three load-bearing decisions

#### A. Additive schema, not replacement

``FundConfig`` gains optional fields. Every existing config (the
shipped fixture; any out-of-tree config) continues to validate
without modification. The orchestrator's per-fund projection loop
reads only the fields the math needs (``commitment_usd``,
``vintage``, ``sleeve``, ``name``); the new fields flow into
reporting only.

#### B. PE math unchanged, ledger schema unchanged

* TA model untouched.
* STAIRS adapter untouched.
* ``PROJECTION_COLUMNS`` unchanged.
* ``pe_call`` / ``pe_distribution`` / ``pe_nav_mark`` ledger rows:
  same shape, **same ``source = "pacing:<fund_name>"`` value as
  today**. Manager identity does **not** enter the ledger.
* §5.1 invariants untouched.

The only place the new metadata appears is in **report-side
aggregation** plus **diagnostics surfacing**.

#### C. Loud failure for inconsistent metadata, no silent inference

When the new fields are present, validation enforces internal
consistency. When they're absent, no fallback / synthesis — just
absent in reports. ``(unknown)`` aggregation when ``manager`` is
partial; no all-or-none requirement.

### Schema additions

``FundConfig`` extends with all-optional fields:

```python
class FundConfig(BaseModel):
    model_config = _STRICT
    name: str
    commitment_usd: float = Field(gt=0.0)
    vintage: str
    sleeve: str
    # ---- Phase 9 additions, all optional ----
    manager: str | None = None
    fund_id: str | None = None
    strategy: Literal["buyout", "venture", "growth", "credit",
                      "real_estate", "infra", "secondary"] | None = None
    fee_model: _FeeModelConfig | None = None
    status: Literal["active", "committed", "exited", "planned"] = "active"
```

``_FeeModelConfig``:

```python
class _FeeModelConfig(BaseModel):
    model_config = _STRICT
    management_fee_pct:    float = Field(default=0.0, ge=0.0, le=0.05)
    carried_interest_pct:  float = Field(default=0.0, ge=0.0, le=0.30)
    preferred_return_pct:  float = Field(default=0.0, ge=0.0, le=0.20)
```

> **`fee_model` is metadata-only.** Phase 9 does **not** consume any
> of these in the projection math. They're stored as documentation
> of fund-level economics for future phases. The schema may evolve
> when fee economics are actually designed (Phase 10+); breaking
> changes will be loud-failure-friendly.

### Required tightening 1 — `FundConfig.name` must be **globally unique**

Because the ledger source remains ``source = "pacing:<fund_name>"``
and the report joins per-fund projection rows back to fund metadata
by ``fund_name``, two funds with the same ``name`` would create
ambiguous ledger sources and ambiguous metadata joins.

```
Locked rule:
  FundConfig.name must be unique globally across pacing.funds.
  fund_id, when present on any fund, must also be unique globally.
  (manager, name) uniqueness may remain as an additional check,
   but it is NOT sufficient — name alone must be globally unique.
```

### Required tightening 2 — `fund_id` is **not** hash-stable across rename

Earlier rough framing suggested ``fund_id`` could give "stable
hashing across a rename." That is **not** true: ``name`` remains in
``cfg.pe_pacing.funds`` (which is dumped into ``config_hash``) and
in the ledger ``source`` field. Renaming a fund changes both.

```
Locked semantics:
  fund_id is a stable EXTERNAL identifier — useful for client
  systems, accounting, manager portals, etc. mapping. It does NOT
  preserve run hash stability if FundConfig.name changes, because
  name remains part of config and ledger source identity.
```

### Required tightening 3 — `status` semantics table

| status      | Projection behavior                        | Forward-flow diagnostics         |
|---|---|---|
| ``active``    | included                                   | included                         |
| ``committed`` | included                                   | included                         |
| ``planned``   | included if vintage falls within horizon   | included in commitment / vintage diagnostics |
| ``exited``    | **excluded** from forward projections      | **excluded** from forward-flow totals (calls / distributions / unfunded / NAV); may appear in a metadata/status summary |

Phase 9 is not a historical-reporting layer — `exited` funds are
omitted from forward-flow diagnostics entirely. A future "historical
fund window" report could re-include them under explicit labeling.

### Cross-config validation rules (when fields are present)

1. ``FundConfig.name`` is **globally unique** across
   ``pacing.funds``. (New rule, lifts an unstated convention into
   an enforced invariant.)
2. ``fund_id``, when set on any fund, is **globally unique** across
   ``pacing.funds``.
3. ``strategy`` (when set) must be consistent with ``sleeve``:

   | strategy        | required sleeve       |
   |---|---|
   | ``buyout``      | ``pe_buyout``         |
   | ``venture``     | ``pe_venture``        |
   | ``growth``      | ``pe_growth``         |
   | ``credit``      | ``pe_credit``         |
   | ``real_estate`` | ``pe_re``             |
   | ``infra``       | ``pe_infra``          |
   | ``secondary``   | any ``pe_*`` sleeve   |

   Mismatch fails at config validation with both values in the
   error.
4. ``(manager, name)`` uniqueness when ``manager`` is set —
   redundant with rule 1 but kept as a defence-in-depth check.

Rules 1–4 are pydantic-level (model validators on
``PEPacingConfig``).

### Carrier through TA / STAIRS adapters

``PROJECTION_COLUMNS`` stays exactly as today (Phase 7
byte-stability preserved). The orchestrator (or the report
renderer) joins the per-fund projection frame against
``cfg.pe_pacing.funds`` at report time to attach metadata. The
adapter layer is **not** a metadata pipe — it's a math pipe.
Cleaner separation; keeps the Phase 7 STAIRS parity contract
intact.

### Reporting / diagnostics — where the new fields surface

A new ``## PE program structure`` section in ``report.md``,
rendered when at least one fund carries any new metadata field.
Six diagnostics, all pure aggregations of the existing per-fund
projection frame plus the metadata join:

1. **Commitment summary.** Total commitment per (manager, sleeve).
2. **Unfunded by manager.** Sum of
   ``max(0, commitment_usd - cumulative_calls_through_horizon_end)``
   per manager. Reported in dollars and as % of total commitment.
3. **Per-quarter call / distribution attribution.** Per-manager
   aggregate ``pe_call`` and ``pe_distribution`` totals over the
   horizon.
4. **Vintage concentration.** Total commitment grouped by vintage
   year.
5. **Manager concentration.** Top-3 managers by commitment, with
   share of total PE commitment.
6. **NAV by manager (end of horizon).** Aggregate end-of-horizon
   ``nav_end_usd`` per manager.

When ``manager`` is set on some funds and not others, unset funds
aggregate under a literal ``"(unknown)"`` row — explicit, visible,
not synthesized.

When **no** fund has any Phase 9 fields set, the section is
**omitted entirely**. The default-config run is byte-stable.

### Determinism contract

* Schema additions are pure data; no randomness.
* Reporting aggregations are pure groupbys over the existing
  per-fund projection frame; deterministic in inputs.
* The new metadata is folded into ``cfg.pe_pacing.model_dump``,
  which is already in ``config_hash`` — so a manager-name change
  invalidates ``run_id`` correctly.

### Tests planned (15)

Schema (8):

1. ``manager`` accepted as optional string; absent fund → no error.
2. ``fund_id`` global uniqueness enforced when set.
3. ``strategy`` ↔ ``sleeve`` consistency: matching pair passes;
   mismatch fails with both values in error.
4. ``status`` enum: ``"active"`` / ``"committed"`` / ``"exited"``
   / ``"planned"`` accepted; ``"frozen"`` (typo) fails.
5. ``_FeeModelConfig`` per-cell bounds.
6. ``(manager, name)`` uniqueness when manager set.
7. ``FundConfig.name`` globally-unique enforced (this is the
   tightening).
8. ``secondary`` strategy compatible with any ``pe_*`` sleeve.

Behavior (3):

9. ``status: "exited"`` fund: not present in the projection or in
   any ledger row.
10. ``status: "planned"`` fund with vintage outside horizon: not
    in projection. With vintage inside horizon: present (same as
    active).
11. ``_FeeModelConfig`` set: stored on the fund object but does
    not change projection numbers — anchor against a TA-equivalent
    fund without ``fee_model``.

Report (3):

12. New ``## PE program structure`` section rendered when
    ``manager`` is set on any fund.
13. Section **omitted** when no Phase 9 fields are set on any fund.
14. ``(unknown)`` aggregation when ``manager`` partial — set funds
    aggregate under their manager; unset under ``"(unknown)"``.

End-to-end (1):

15. Default shipped fixture (no Phase 9 fields) produces
    byte-identical ``ledger.parquet``, ``manifest.json``, and
    pre-Phase-9 report sections. The new section is omitted.

### What Phase 9 is **not**

Listed explicitly so a future contributor reads them as guardrails:

* **Not a fee economics change.** ``fee_model`` fields are stored,
  not consumed by the projection. Charging management fees on
  unfunded commitment, reducing distributions for carried interest,
  and preferred-return waterfalls are all Phase 10+.
* **Not a recommitment optimizer.** ``funds`` list remains the
  full plan.
* **Not a manager-level coupling override.** All PE sleeves still
  share the single ``public_equity`` coupling reference under
  STAIRS. Per-manager beta is a future phase.
* **Not a secondary-market sale path.** L8 still says PE doesn't
  trade in rebalance.
* **Not a STAIRS_MC / stochastic upgrade.**
* **Not an L14 fix.** Linear transaction cost only; ``fee_model``
  fields are not transaction-cost terms.
* **Not a new ledger schema.** ``pe_*`` flow rows still use
  ``source = "pacing:<fund_name>"``.
* **Not a historical-reporting layer.** Exited funds are omitted
  from forward-flow diagnostics.

### L-status under Phase 9

* **L1** — unchanged. STAIRS still resolves the artifact under
  ``pe.engine="stairs"``; TA still has it. No new math.
* **L8** — unchanged. RESOLVED in Phase 8.
* **L14** — unchanged. Still open. ``fee_model`` is metadata, not
  cost-model fix.

### Locked design choices

* All new fields **optional**; existing configs validate unchanged.
* ``FundConfig.name`` **globally unique** (load-bearing rule).
* ``fund_id`` optional + globally unique when set; **not** a hash-
  stability mechanism.
* ``status`` semantics per the table above; ``exited`` excluded
  from forward projection and forward-flow diagnostics.
* ``fee_model`` stored but **not consumed** by projection math
  (Phase 10+ scope); schema may evolve when fee economics land.
* ``PROJECTION_COLUMNS`` byte-stable — metadata joined at report
  time, not embedded in adapter output.
* New ``## PE program structure`` report section omitted when no
  Phase 9 fields are set.
* ``(unknown)`` aggregation when ``manager`` is partial; no
  all-or-none requirement.
* Ledger ``source`` unchanged (``pacing:<fund_name>``). Manager /
  fund_id do **not** enter the ledger.

---

## Phase 10 design (pre-implementation) — L14 transaction cost diagnostics

> **One-line goal.** Resolve **L14: only linear transaction cost is
> modeled** by clarifying scope, adding diagnostic visibility for
> when the linear approximation may strain, and explicitly deferring
> richer cost regimes (PE secondaries, fee economics, market impact,
> asymmetric flow, per-bucket bps) to future phases. **No PE math
> change. No ledger schema change. No allocator / rebalancer change.
> No config knobs.**

### Why this isn't a math change

Pre-Phase-8, L14 had two binding concerns:

1. **Public-market cost approximation** — linear ``bps · |trade|`` is
   appropriate at the modelled scale (trades ~$2–25M against
   ~$100M+ market depth). At larger sizes / thinner markets / strong-
   direction flow, market impact (∝ ``|trade|^1.5`` or volume-
   relative) and asymmetric buy/sell would matter.
2. **PE secondary cost fiction** — a $20M PE secondary trade has a
   5–25% discount, not a few bps. Pre-Phase-8 the rebalancer happily
   sold PE at NAV under linear bps, silently pricing PE secondaries
   as if they were public equity.

**Phase 8 closed concern (2) by removing the artifact entirely.** PE
no longer trades through rebalance — ``pe_*`` rebalance rows are
zero by construction (load-bearing invariant from Phase 8). The L14
"PE secondary mispricing" risk has no surface to manifest under the
default config. PE secondaries, if and when modelled, would land
under a separate flow type with its own cost regime.

That leaves only concern (1) — public-market cost realism — which
the original L14 text already acknowledged is "well-approximated"
at our scale. The remaining risk is users running the engine at
larger / thinner / more concentrated flow patterns and not
realising the linear approximation is straining. Phase 10 makes
that **visible**, not magically fixed.

### Resolution shape

**Documentation + light diagnostics**, not a richer cost model:

* L14 status flips to ``[PARTIALLY RESOLVED 2026-xx-xx, Phase 10]``
  with engine-conditional / scope-conditional wording.
* New ``## Transaction cost summary`` section in ``report.md``,
  rendered after the Cost-aware allocator calibration section and
  gated on the existence of ``transaction_cost`` rows in the ledger.
* Threshold-based advisory text flags interpretation risk when run-
  time data crosses scale thresholds; **the thresholds are
  diagnostic heuristics, not validation gates**.

### Required tightening — diagnostic vs. validation

> **The advisory thresholds are diagnostic heuristics, not
> validation failures. Crossing them does not invalidate the run; it
> flags interpretation risk.**

This rule is **load-bearing** for Phase 10 and must appear verbatim
in both this section and the report's advisory text. The project
has many hard validation gates (per-cell bounds, PSD checks, sum-
to-one, symmetry, etc.); L14 thresholds are explicitly **not** in
that category. A run that breaches a threshold still validates,
still passes invariants, still produces a usable ledger — the
report just notes that the linear-bps approximation may underprice
market impact in this regime.

### Report section — `## Transaction cost summary`

Renders only when ``transaction_cost`` rows exist in the ledger
(i.e., a non-stub implementation engine with ``bps_per_trade > 0``).
Under stub or zero-bps the section is omitted entirely.

Structure:

```markdown
## Transaction cost summary

- engine: <implementation.engine> @ <bps_per_trade> bps
- cumulative transaction_cost: $XX,XXX
- as % of initial NAV: 0.YY%
- liquid rebalance turnover (sum |trade|, liquid buckets): $X.XM total, $YYK / quarter mean
- max single-quarter liquid turnover as % of NAV: Z.ZZ%
- advisory: <one of three messages — see below>

_These thresholds are diagnostic heuristics, not validation failures.
Crossing them does not invalidate the run; it flags interpretation risk.
PE-secondary / asymmetric / quadratic-impact / fee-economics costs are
out of scope for the linear bps model. See MODEL_DOCUMENTATION.md
§Phase 10 / L14._
```

The advisory line picks one of three messages, in priority order:

1. **`max quarterly liquid turnover > 25% of NAV`** →
   *"⚠️ max quarterly liquid turnover > 25% of NAV — linear bps
   approximation may underprice market impact at this trade size."*
2. **`cumulative transaction_cost > 1% of initial NAV`** →
   *"⚠️ cumulative cost > 1% of initial NAV — cost is material;
   consider per-bucket bps or a richer cost model for stress runs."*
3. **Otherwise** →
   *"linear-bps approximation covers this regime (turnover and
   cost both within typical scale)."*

Priority order: max-quarterly-turnover trumps cumulative-cost
because the former is a more acute single-event signal. Both
breached → max-turnover message wins.

### Liquid-only turnover

The "liquid rebalance turnover" computation **excludes illiquid
buckets explicitly**, even though under the L8 default-on overlay
their rebalance trades are zero by construction. The rationale is
to make the L14 / L8 boundary visible in the report: liquid
buckets are the only ones where a transaction-cost approximation
is even relevant, so the turnover diagnostic should be liquid-only
by definition. The implementation reads ``cma.liquidity`` to
identify the liquid set.

### Implementation surface

* New computation in ``report.py``: aggregates over the ledger's
  existing ``transaction_cost`` and ``rebalance`` rows. **No new
  data sources.** The ``cma`` object is already passed to
  ``write_markdown_report`` (Phase 5); used here to filter
  rebalance rows to liquid buckets.
* Threshold values (``1.0%`` cumulative, ``25.0%`` quarterly) are
  module-level constants in ``report.py`` with comments documenting
  them as advisory heuristics, not gates. **No config field; no
  user-tunable knob.**
* No code change in ``ledger.py``, ``orchestrator.py``,
  ``allocation/`` adapters, ``implementation/`` adapters, or
  ``pe/`` adapters.

### What Phase 10 is **not**

Listed explicitly so a future contributor reads them as guardrails:

* **Not a quadratic / market-impact cost term.** Linear bps stays.
* **Not a per-bucket bps model.** Single global rate stays.
* **Not asymmetric buy/sell.** Single rate per side stays.
* **Not a PE secondary cost regime.** PE doesn't trade in rebalance
  under L8.
* **Not a fee-economics implementation.** ``fee_model`` (Phase 9)
  stays metadata-only.
* **Not a liquidity haircut model.** Stress-period bid-ask widening
  stays unmodelled.
* **Not a Monte Carlo / stochastic upgrade.**
* **Not a config knob.** Thresholds are renderer constants.
* **Not a validation gate.** Threshold breach is informational only.

### L14 status under Phase 10

Will flip to ``[PARTIALLY RESOLVED 2026-xx-xx, Phase 10]`` on
implementation. Resolution wording (engine-conditional and scope-
conditional):

* **PE-secondary mispricing**: closed by L8 (Phase 8). PE buckets
  are non-tradable in rebalance under default config; no
  ``transaction_cost`` row attributable to PE rebalance can exist.
  PE secondaries, if and when modelled, will land under a new flow
  type with their own cost regime.
* **Public-market linear-bps approximation**: documented as
  appropriate for the modelled scale. New "Transaction cost
  summary" report section flags when run-time data crosses scale
  thresholds where the linear approximation may underprice market
  impact. **Thresholds are diagnostic heuristics, not validation
  failures.**
* **Asymmetric buy/sell, per-bucket bps, fee economics, liquidity
  haircuts**: explicitly out of scope for L14 resolution. Each is
  a separate future phase with its own design and tests.

### Tests planned (5)

1. **Section omitted under stub engine.** Default base run
   (``implementation.engine="stub"``) produces a report with no
   ``## Transaction cost summary`` section.
2. **Section renders under cvxportfolio engine.** A run with
   ``implementation.engine="cvxportfolio"`` and
   ``bps_per_trade > 0`` produces the section with all four
   metric lines plus an advisory line plus the
   "diagnostic heuristics, not validation failures" note.
3. **All-clear advisory at low turnover.** Default fixture +
   cvxportfolio + 5 bps stays under both thresholds → advisory
   says "covers this regime."
4. **Threshold-trigger anchor.** Constructed scenario with high
   single-quarter liquid turnover (>25% of NAV) → advisory text
   contains the "may underprice market impact" warning.
5. **No ledger schema change** (regression). Existing ledger
   validation tests continue to pass; the ``transaction_cost``
   row format is byte-identical to today.

### Locked design choices

* Resolution shape: **documentation + diagnostics**, not full math
  change.
* L14 flips to ``[PARTIALLY RESOLVED 2026-xx-xx, Phase 10]`` with
  engine-conditional + scope-conditional wording.
* New ``## Transaction cost summary`` section in ``report.md``,
  positioned after the Cost-aware allocator calibration section,
  gated on ``transaction_cost`` rows existing.
* Four metrics + one advisory line + the
  "diagnostic heuristics, not validation failures" note (the
  required tightening).
* Thresholds: ``1.0%`` cumulative cost / initial NAV;
  ``25.0%`` max single-quarter liquid turnover / NAV. Module-level
  constants, no config knob.
* Liquid-only turnover (uses ``cma.liquidity`` to filter).
* No ledger schema change; no allocator / rebalancer change; no
  PE math change.
* L14's tightly-coupled-to-L8 paragraph in the original entry is
  updated to note the L8 closure resolved its specific concern.

---

## Change Log

Entries are appended in chronological order. Each entry: date, commit hash,
what changed, why, impact on outputs, backward-compatibility flag.

### 2026-05-01 — Phase 1 build (commits `26e3efe` → `f327b66`)

* **What.** Initial system: schemas + loaders + cross-config validation
  (`ed7259b`); QuarterlyLedger spine with §5.1 invariants (`61a3783`);
  TA model + golden CSV (`d02002b`); StubAllocator + AllocationAdapter ABC
  (`a6d2a2e`); FlatRealRule + SmoothingRule + ImplementationAdapter ABC +
  StubImplementation (`94e892a`); orchestrator + manifest + report +
  pe/pacing + scripts/run_sfo_study (`cca3655`); `aa-model` CLI
  (`a226289`); 39 tests (`ff5cd22`); CI workflow (`f327b66`).
* **Why.** Phase 1 spec brief — build the spine, schema-validate every
  input, project PE deterministically, run end-to-end against fixtures.
* **Impact on outputs.** Establishes the `ledger.parquet` schema, the
  `manifest.json` format, and the `report.md` layout. Default scenario
  produces `final_nav = $114.78M`, cumulative return `+14.78%` over 20q
  on a $100M starting NAV.
* **Backward-compatible.** Yes (initial build).

### 2026-05-01 — Smoothing rule implements EWMA formula (`9d0fe2f`)

* **What.** Replaced the placeholder `NotImplementedError` for nonzero
  smoothing weights with the proper EWMA recursion
  `spend_t = w · target_t + (1-w) · spend_{t-1}`.
* **Why.** Audit feedback — Phase 1 §6 lists smoothing as a *required*
  rule; the placeholder didn't qualify as implemented.
* **Impact on outputs.** When `spending.rule = "smoothing"`:
  * `weight = 1.0` produces the same series as `flat_real`.
  * `weight = 0.0` freezes spending at the initial target (no inflation
    re-anchoring) — see L7.
  * Intermediate weights produce a lag toward target.
  No effect when `spending.rule = "flat_real"` (which the toy fixture
  uses).
* **Backward-compatible.** Yes for the default config; behavior change
  for any caller previously relying on `SmoothingRule` raising.

### 2026-05-01 — `run_id` includes per-invocation nonce (`a6349ca`)

* **What.** `run_id` format changed from `aa-<cfg[:12]>-<fix[:12]>` to
  `aa-<cfg[:12]>-<fix[:12]>-<UTC_ts>-<nonce>`. Reruns now produce
  distinct run dirs. Hashing migrated to object-based
  (`hash_study_config`).
* **Why.** Audit found the prior deterministic `run_id` violated SPEC §8
  ("Every output directory is named ... and is never overwritten").
* **Impact on outputs.** Each invocation lands in a new dir under
  `data/processed/runs/`. Determinism semantics are preserved at the
  *content* level: two consecutive runs of the same config produce
  byte-identical `ledger.parquet` once the `run_id` column is dropped.
  All hash values changed (object-based vs file-based hash → different
  bytes; not just length).
* **Backward-compatible.** No for the on-disk run-dir naming or the
  manifest's hash values; yes for the public API.

### 2026-05-01 — PE call/distribution per-source cash offset symmetry test (`067b873`)

* **What.** Added a test asserting that for every `pe_call` /
  `pe_distribution` row on a non-cash bucket, there is exactly one paired
  row on `cash` with the same `source` and the negated amount.
* **Why.** Audit flagged that the existing aggregate zero-sum check would
  miss a per-source pairing bug that happens to net to zero across funds.
* **Impact on outputs.** None — defensive test only.
* **Backward-compatible.** Yes.

### 2026-05-01 — `ruff` pinned in `requirements.txt` (`27a16bb`)

* **What.** Moved `ruff==0.4.4` from `requirements-dev.txt` only to
  `requirements.txt` as well.
* **Why.** Audit recommendation to prevent CI / local drift if a
  contributor only installs runtime deps.
* **Impact on outputs.** None.
* **Backward-compatible.** Yes.

### 2026-05-01 — Phase 2 build (commits `c6d4567` → `2b779d1`)

* **What.** `Scenario` dataclass + `make_scenarios` + orchestrator
  `scenario=...` override (`c6d4567`); `spending/liquidity.py` with
  coverage / shortfall / drawdown metrics (`679241e`); `integration/sweep.py`
  + `comparison_report.py` + `aa-model sweep` CLI + `run_sfo_sweep.py`
  script (`8adafb9`); 18 new tests (`2b779d1`).
* **Why.** Phase 2 spec brief — scenario library + batch runner + comparison
  report. Discipline guardrails: scenarios as inputs not branches; ledger
  remains sole state spine; no allocator / PE / spending changes beyond
  consuming overrides.
* **Impact on outputs.** New artifacts:
  `data/processed/sweeps/<sweep_id>/comparison.html` and
  `comparison.md`. Sweep over the 5 canonical scenarios completes in
  ~5s on the toy fixtures (gate: <60s). Five scenarios produce final
  NAVs ranging from $113.16M (`inflation_shock`) to $118.19M
  (`clustered_calls`).
* **Backward-compatible.** Yes — `run_orchestrator` API extended with a
  default-`None` `scenario` parameter; no behavior change when omitted.

### 2026-05-01 — PE-timing scenario limitation documented (`9b2fb3a`)

* **What.** Docstring-only addition to `assumptions/scenario_builder.py`
  flagging that `clustered_calls` and `delayed_pe_distributions` shift
  PE timing but do not model the public-vs-private opportunity cost.
* **Why.** Phase 2 audit observation that `clustered_calls`'s `+18.19%`
  return vs base `+14.78%` could mislead future readers as alpha when
  it is in fact a deterministic-PE-growth artifact.
* **Impact on outputs.** None (docs-only).
* **Backward-compatible.** Yes.

### 2026-05-01 — Phase 3a / Riskfolio adapter (`9b35051`)

* **What.**
  * First non-stub `AllocationAdapter` implementation:
    `aa_model.allocation.riskfolio_adapter.RiskfolioAdapter`
    (riskfolio-lib 7.2.1 + cvxpy 1.8.2). Solves
    `model="Classic", rm="MV", obj="MinRisk", hist=False`
    against a CMA + long-only box bounds.
  * **Engine flag.** `configs/base.yaml::allocation.engine` widened
    from `Literal["stub"]` to `Literal["stub", "riskfolio"]`. Adapter
    selection runs through `aa_model.allocation.factory.make_allocator`
    (no orchestrator-level branching on engine identity).
  * **Optional dependency behavior.**
    * Backend `import riskfolio as rp` is lazy — performed inside
      `RiskfolioAdapter._solve()`, not at module top level. The
      package therefore imports cleanly without `riskfolio-lib`
      installed; the only failure mode is calling `fit()` with
      `engine=riskfolio` when the backend is missing.
    * Install set: `pip install -e ".[riskfolio]"` from the new
      `[project.optional-dependencies] riskfolio` group, pinning
      `riskfolio-lib==7.2.1`.
    * Tests are gated on `pytest.importorskip("riskfolio")` so the
      core test suite still passes on a core-only install.
  * **Fallback CMA.** When called with an empty `CMA` (Phase 1's
    orchestrator default), the adapter synthesizes per-bucket
    annualized vols from a hard-coded table
    (`_DEFAULT_VOL_ANNUAL`: cash 0.5%, public_bond 4%,
    public_equity 16%, pe_buyout 20%, pe_venture 30%,
    pe_growth 22%, pe_infra 12%, pe_re 14%, pe_pc 10%,
    fallback 15%) and an identity correlation matrix. Expected
    returns default to a zero vector (irrelevant for `MinRisk`).
    These values are placeholders to make the wiring testable, not
    investment views — see L3 / L4.
  * **Synthetic 2-row dummy returns frame** at `Portfolio(returns=...)`
    construction (overridden by `port.mu`/`port.cov` before
    `optimization`) — see L11.
  * **Known riskfolio warning** `You must convert self.cov to a
    positive definite matrix` — non-fatal; see L12.
  * **Stub parity contract** is structural (sums-to-1, range, no NaN,
    binding-equality pinning) — see *Core Invariants / Adapter parity
    contract*. Tested in `tests/test_riskfolio_adapter.py` (10 tests,
    all gated on `pytest.importorskip("riskfolio")`).
  * **Numpy / pyarrow bumps.** `numpy>=2,<3` and `pyarrow>=17,<24`
    (forced by riskfolio's transitive deps; pyarrow 15 was numpy-1-only
    ABI). Existing 61 tests still green under numpy 2.4.4 + pyarrow
    23.0.1, including the TA golden-CSV byte-equality regression.
  * **CI** split into `core` (no optional deps) and `adapters`
    (`pip install -e ".[riskfolio]"` + parity test + real
    `engine=riskfolio` end-to-end run). `adapters` is gated on `core`.
* **Why.** Phase 3a spec brief — first external optimizer adapter behind
  the ABC. Discipline guardrails: pure adapter, no shared state, no
  caches, no ledger access; lazy backend import; structural-not-numerical
  parity contract with the stub.
* **Impact on outputs.**
  * Default config (`engine: stub`) is unaffected. Existing tests still
    pass under the bumped numpy / pyarrow.
  * Setting `allocation.engine: riskfolio` against the base fixture
    produces this *observed difference* vs stub:
    * end-of-horizon allocation: **cash 98.31%, public_bond 1.54%,
      public_equity 0.10%, pe_buyout 0.06%** (vs stub config's
      5/20/50/25)
    * final NAV: **$107.4M** (vs stub's $114.8M)
    * cumulative return: **+7.36%** (vs stub's +14.78%)

    This is the expected MinRisk solution against the placeholder vol
    vector + identity correlation; it is **not** investment guidance.
    See L3 / L4.
  * 10 new parity tests in `test_riskfolio_adapter.py`, all gated on
    `pytest.importorskip("riskfolio")`.
* **Backward-compatible.** Yes for the public API. The schema widening
  is forward-compatible (existing configs with `engine: stub` validate
  unchanged). Runtime breakage is possible only for callers who pin
  `numpy<2` or `pyarrow<17` outside this repo.

### 2026-05-01 — `MODEL_DOCUMENTATION.md` introduced (`c724e12`)

* **What.** Created this document at the repo root as the authoritative
  record of model design, assumptions, limitations, and changes.
* **Why.** User directive — every commit that changes model behavior
  updates this file from now on; entries are appended, never rewritten.
* **Impact on outputs.** None.
* **Backward-compatible.** Yes.

### 2026-05-01 — Phase 4a / Per-quarter spending API + realized-NAV Owl

* **What.** Implementation of the Phase 4a design locked in the prior
  commits. Resolves L15 and L18.
  * **Ledger primitives.** `QuarterlyLedger.closed_through(quarter)`
    returns a chained read-only view of rows with
    `quarter <= the given quarter`; the ledger remains appendable.
    `QuarterlyLedger.end_nav_through(quarter)` returns end-of-quarter
    NAV per bucket (initial NAV for buckets with no rows). Both
    factor through a shared `_compute_view` helper; `finalize()` is
    refactored to use it. No behavioral change to the existing
    `finalize()` semantics.
  * **SpendingRule API.** New abstract method
    `quarterly_outflow_at(ledger, params, quarter) -> float`.
    `quarterly_outflows(ledger, params) -> pd.Series` is now a default
    wrapper that constructs a synthetic working ledger and iterates
    the per-quarter method, threading each result back as a `spend`
    row so subsequent iterations observe the prior quarter as closed.
    Phase 1–3 callers (and tests that haven't migrated) continue to
    work; the orchestrator switches to the per-quarter method
    directly.
  * **SOURCE_ID per rule.** Each `SpendingRule` declares its
    canonical `SOURCE_ID` class attribute (`spending:flat_real`,
    `spending:smoothing`, `spending:owl`); the orchestrator emits
    `spend` rows with that source id (replacing the previous
    hardcoded `"spending"`). The convention mirrors `impl:<engine>`
    and `pacing:<fund>`.
  * **Per-rule prior-row source filter.** Path-dependent rules read
    only their own prior `spend` rows from the closed ledger
    (`source == self.SOURCE_ID`); a different source raises
    `RuntimeError`. Prevents Owl from reacting to flat_real history
    across a config switch (Phase 4 design / prior-spend-row source
    filter; tested in
    `test_owl_does_not_react_to_other_rule_spend_rows`).
  * **Per-rule reimplementations.**
    - `FlatRealRule` — config-only formula; ignores ledger.
    - `SmoothingRule` — reads its own prior `spend` row to recover
      `spend_{t-1}`; computes `w · target_t + (1-w) · spend_{t-1}`.
    - `OwlRule` — reads `ledger.end_nav_through(prior_q)` for
      realized end-of-prior-quarter NAV; reads its own prior `spend`
      row to recover the year's `annual_spend`; applies the
      year-boundary inflation + guardrail check against **realized**
      NAV (not forecast). q0 returns `annual_spend / 4` with no
      guardrail check, no inflation step, no special ledger event
      (the rule owns q0 initialization end-to-end).
  * **GuardrailConfig change.** `forecast_quarterly_return_pct`
    removed. Existing configs that set it now fail schema validation
    — the right loud failure since the parameter became inert.
  * **Orchestrator switch.** The per-quarter loop now calls
    `rule.quarterly_outflow_at(ledger, spend_params, q)` at the start
    of each quarter (before any q rows are written) and emits the
    resulting `spend` row at canonical-order position 6 with
    `source=rule.SOURCE_ID`. The rule observes `ledger[quarter <= q-1]`
    by construction.
* **Why.** Phase 4 design / load-bearing rule:
  *no rule may depend on the quarter it is currently writing*.
  Resolves L15 (forecast-only NAV) and L18 (Owl misreads inflation
  shock as headroom). Holds every Phase 4 design rule verbatim — no
  fixed-point, no inner loop, no sidecar, no API fork, no canonical-
  order change, no cost-aware optimizer, no fix for L17.
* **Impact on outputs.**
  * Default config (rule=flat_real, no guardrail) — **same numerical
    output** as Phase 3c. flat_real is config-only, ledger-blind; the
    only on-disk diff is the `source` column on spend rows changed
    from `"spending"` to `"spending:flat_real"`. Reproducibility
    test still passes within a single environment.
  * `rule=owl` — now responds to realized NAV. Under
    `public_drawdown`, Owl produces strictly lower cumulative
    spending than under `base`. Under `inflation_shock`, Owl tracks
    pure inflation step-up (no raise), unlike Phase 3c which
    incorrectly raised.
  * 19 Owl tests rewritten for realized-NAV semantics; 4 ledger
    primitive tests added; 2 exit-gate orchestrator tests added.
    Total: **106 passed** (was 103; the net is +3 after replacing
    Phase 3c forecast-based tests with Phase 4a realized-NAV tests).
* **Backward-compatible.** Public-facing API: yes. Existing
  `quarterly_outflows(ledger, params)` calls on FlatRealRule continue
  unchanged. Owl users with configs that set
  `forecast_quarterly_return_pct` must remove the field. The `source`
  string on `spend` rows changed; any downstream tool that filters
  on `source == "spending"` literally will need to filter on
  `source.startswith("spending:")` instead.

### 2026-05-01 — Phase 4 design: q0 + prior-spend recovery rules

* **What.** Three additions to the §Phase 4 design (pre-implementation)
  section, all per user directive:
  1. **q0 initialization rule.** `q0 is initialization, not a
     guardrail decision.` At the first quarter, every rule returns
     `annual_spend_usd / 4` with no guardrail check, no inflation
     step, no special ledger event. The rule owns q0 — the
     orchestrator never seeds it from outside.
  2. **Prior-spending recovery via Option A.** Path-dependent rules
     recover `spend_{t-1}` (or prior-year `annual_spend`) from their
     own closed ledger rows; no orchestrator-threaded state. Adds the
     binding rule-side contract: *"A path-dependent `SpendingRule`
     may only read prior `spend` rows where `source == its own rule
     source`."* Phase 4a wires per-rule source identifiers
     (`spending:flat_real`, `spending:smoothing`, `spending:owl`)
     mirroring the existing `impl:<engine>` and `pacing:<fund>`
     conventions.
  3. **Storage rule (load-bearing).** *"No orchestrator-side
     prior_spend state. No q0 special emission outside the rule. The
     rule owns q0 initialization."* — promoted to its own callout
     so the design can't drift toward an orchestrator-side
     baseline-tracker in implementation.
* **Why.** Closes the two open boundary questions before any 4a
  code lands. Both directly preserve the ledger-as-spine rule that
  has held since Phase 1.
* **Impact on outputs.** None today.
* **Backward-compatible.** Yes (docs only).

### 2026-05-01 — Phase 4 design locked (pre-implementation)

* **What.** New §Phase 4 design (pre-implementation) section between
  §Validation & Testing and §Change Log freezes the architectural
  rules every Phase 4 commit must respect. Captures the user-supplied
  design verbatim:
  - **Load-bearing rule**: no rule may depend on the quarter it is
    currently writing; rules may observe only the fully closed ledger
    through the prior quarter.
  - **Iteration model**: single forward pass per quarter; no
    fixed-point; no inner loop.
  - **State-flow contract**: rules see `ledger[quarter <= q-1]` in
    canonical order with all flow types; no partial-current-quarter
    state, no pre-rebalance prior-quarter state, no speculative state.
  - **API migration**: new abstract `quarterly_outflow_at(ledger,
    params, quarter)` on `SpendingRule`; existing
    `quarterly_outflows` becomes a default wrapper; rules are not
    forked into static / iterative variants.
  - **Ledger addition**: read-only `closed_through(quarter)` view
    callable on a still-appendable ledger; no shadow state.
  - **Determinism addition**: solvers may be used only if outputs
    are rounded / canonicalized before ledger emission; Phase 4a
    forbids solver-based feedback entirely.
  - **Phase split**: 4a (per-quarter observation API + Owl
    realized-NAV) ships first and resolves L15 + L18; 4b (cost-aware
    implementation) follows and resolves L13; 4c (within-quarter
    fixed-point) is research-only and never gates ship.
  - **What 4a is not** — explicit list of guardrails (no fixed-point,
    no sidecar, no multi-pass orchestrator, no API fork, no canonical
    order change, no cost-aware optimizer, no fix for L17).
* **Why.** Phase 3 closed with three coupled limitations (L13, L15,
  L18) all pointing at the same architectural gap. Phase 4 is no
  longer "improvement" — it's the correction of empirically
  demonstrated failures, and the choice between fixed-point and
  strict-sequential models is the load-bearing decision that
  determines everything that follows. Locking the design before
  any code lands prevents the "two paths, both deferred" ambiguity
  that L18's first version had.
* **Impact on outputs.** None today. Binds every future Phase 4
  commit; deviations require an amendment to this section first.
* **Backward-compatible.** Yes (docs only).

### 2026-05-01 — L18 tightened: partial-fix path is a trap, not a mitigation

* **What.** Reframed L18's mitigation section. The previous text
  presented "bind forecast to scenario inflation" as one of two
  mitigation paths. The audit observation — *"this only fixes
  inflation-driven failure, not return-driven failure"* — promotes
  it from a partial fix to an explicit anti-pattern. New framing:
  - **Mitigation (only)**: realized-NAV feedback per L15. Works under
    both inflation shocks and return shocks; the only structurally
    correct fix.
  - **Trap to avoid**: forecast-binding patch. Addresses inflation
    failure, leaves return-driven failure broken, and creates false
    confidence by partially fixing one scenario family.
* **Why.** Phase 4 design is now imminent. The risk the audit named
  is that a future contributor reads L18's "two mitigation paths"
  language and tries the cheap one. The trap framing makes it
  unambiguous that only the architectural fix is acceptable.
* **Impact on outputs.** None (docs only).
* **Backward-compatible.** Yes.

### 2026-05-01 — Phase 3 consolidation probe + L17 + L18

* **What.**
  * New research probe: `scripts/consolidation_probe_p3.py` runs the
    full cross product of `{stub, riskfolio} × {stub, cvxportfolio@5bp}
    × {flat_real, smoothing, owl} × {base, public_drawdown,
    delayed_pe_distributions, clustered_calls, inflation_shock}` —
    60 combinations through the existing orchestrator. **60/60 ok**;
    no invariant failures; no schema rejections.
  * Two material limitations surfaced by the probe and now documented:
    - **L17 — Cross-engine metric comparability** is not meaningful
      when adapters produce wildly different sleeves. Concrete
      example: stub vs riskfolio min_coverage on the base scenario
      is `74 mo` vs `285 mo`; max_drawdown on public_drawdown is
      `-12.78%` vs `-0.27%`. Both internally consistent; their
      cross-engine comparison cannot be read at face value because
      riskfolio's MinRisk concentrates 98% in cash, trading return
      upside for "coverage" and "drawdown protection" the framework's
      headline metrics do not penalize.
    - **L18 — Owl misreads inflation shock as headroom** and raises
      spending. Empirical case: under inflation_shock, Owl spends
      $24.09M cum vs $23.81M under base (+1.2%) — but the mechanism
      is a year-3 *raise* trigger, not a defensive cut. Real GK
      guardrails would cut. This makes the L15 limitation concrete
      with a worked example and binds it as a Phase 4 hard
      prerequisite.
  * Sub-finding noted in L18 (no separate limitation): cvxportfolio
    transaction cost is ~$117k cumulative under riskfolio allocation
    vs ~$65k under stub allocation on the same fixture — the
    riskfolio target forces a ~$83M Q1 turnover that the stub's
    pre-aligned target avoids.
* **Why.** User-directed pause-and-consolidate before P3d. The
  surface area (60 combos) had outgrown what individual phase tests
  exercised; surfacing cross-component interactions empirically
  before adding STAIRS reduces the chance of P3d-era bugs masking as
  cross-component artifacts.
* **Impact on outputs.** None directly. The probe is reproducible
  via `python scripts/consolidation_probe_p3.py --out
  data/processed/probes/<name>.md` and writes its report into a
  gitignored directory; it does not run in CI and does not gate any
  build. L17 / L18 change the *interpretation* of existing reports;
  they do not change any output numerics.
* **Backward-compatible.** Yes (script + docs only).

### 2026-05-01 — P3c post-audit doc clarifications

* **What.** Two tightenings landed together per the Phase 3c audit:
  1. `forecast_quarterly_return_pct` is now explicitly documented as
     an **exogenous user assumption** that is *not* derived from
     fixture returns, the CMA, or scenario perturbations. Two runs
     with different realized return paths but the same forecast
     assumption produce identical Owl spending series. The note lives
     in three places: the `GuardrailConfig` docstring + Field
     description, the `OwlRule` module docstring, and L15.
  2. New §Known Limitations *Forward-risk note* between L15 and L16
     formalizes the two parallel approximations now in the system —
     allocation side (L13) is cost-unaware, spending side (L15) is
     NAV-unaware — and binds them as a single Phase 4 "iterative
     per-quarter rule" lift. Shipping a half-fix (one without the
     other) would introduce a feedback loop the unfixed side ignores.
* **Why.** Audit observation that the forecast parameter would be
  read as "scenario-aware" without an explicit exogeneity note, and
  that the L13 / L15 deferral pair is now a cross-component constraint
  that needs to be visible at the limitation level, not just buried
  in individual phase change-log entries.
* **Impact on outputs.** None (docs only).
* **Backward-compatible.** Yes.

### 2026-05-01 — Adapter discipline contract codified

* **What.** Promoted the previously per-adapter "Phase 3 guardrails"
  prose into a centralized §Core Invariants / *Adapter discipline
  contract (Phase 3+)* subsection. The contract is split by ABC
  because each ABC hands the adapter different inputs:
  - `AllocationAdapter` and `ImplementationAdapter` get no ledger via
    their ABC, so "no ledger access" remains correct for both.
  - `SpendingRule` ABC explicitly takes a `ledger` argument; the
    correct discipline for spending rules is therefore "**No ledger
    mutation. May read the ledger passed into the `SpendingRule`
    interface, but may not retain it, mutate it, or access
    global/shared state.**" — the audit-supplied phrasing, now
    canonical.
* **Why.** Phase 3c audit follow-up. The earlier "no ledger access"
  shorthand worked for the allocation and implementation adapters
  but was wrong for spending rules, where ledger reads are part of
  the ABC contract. Codifying the corrected wording per ABC removes
  the ambiguity for future adapters (notably P3d STAIRS and any
  later spending-rule additions).
* **Impact on outputs.** None (docs only).
* **Backward-compatible.** Yes.

### 2026-05-01 — Phase 3c / Owl guardrail spending rule

* **What.**
  * **Disambiguation.** "Owl" is not an external library — no PyPI
    package matches the SFO spending domain (the lone `owl` package is
    a Falcon API monitoring library). Per user direction, Owl is the
    project codename for the **Guyton-Klinger guardrail rule** missing
    from `spending/rules.py`'s type comment. The implementation lives
    at `spending/owl_adapter.py` for §4-layout consistency.
  * New `OwlRule` (subclass of `SpendingRule`):
    inflation-adjust at year boundaries, then check rate vs initial
    rate against `lower_band_pct` (raise trigger) and `upper_band_pct`
    (cut trigger); ratchet ±`raise_pct` / `cut_pct` on trigger; within-
    year constancy. NAV used in the rate check is forecasted from
    `forecast_quarterly_return_pct`; Owl does not read realized NAV
    (see L15).
  * **Schema.** New `GuardrailConfig` (5 fields:
    `upper_band_pct`, `lower_band_pct`, `raise_pct`, `cut_pct`,
    `forecast_quarterly_return_pct`); added to `SpendingConfig` as
    optional `guardrail`. Cross-config validation rejects
    `rule = owl` without `guardrail`.
  * **Factory.** `make_rule("owl") → OwlRule` with a deferred import
    inside the factory to avoid a circular import between
    `spending/rules.py` and `spending/owl_adapter.py`.
  * **Numerical anchor (hand-worked Guyton-Klinger trip).** Initial
    $4M annual spend, $100M NAV (rate 4%), 4%/q forecast growth, 20%
    bands, 10% raise. At q8: forecast NAV = $100M·(1.04)^8 =
    $136,856,905; annual spend after two inflation steps =
    $4M·(1.025)^2 = $4,202,500; rate = 3.0707% < 4%·(1−0.20) = 3.20%
    → raise triggers; new annual = $4,202,500·1.10 = $4,622,750;
    quarterly = **$1,155,687.50**. Tested to `1e-9` USD.
  * **Comparability tests.** Owl with bands so wide they never
    trigger reduces exactly to `FlatRealRule` for the same horizon
    (degenerate parity); Owl with active bands diverges from
    `SmoothingRule(weight=1)` (which is itself flat-real-equivalent).
  * **Boundary tests.** Cut trigger fires under negative forecast
    (verified: forecast = −5%/q → rate breaches upper band at q4 → cut
    to $922,500/q); within-year-constant; deterministic across runs;
    no NaN / no negative spending; floor/ceiling clip applied.
  * **End-to-end.** New orchestrator-level test
    (`test_owl_spending_rule_preserves_invariants_end_to_end`) runs
    the full base scenario with `rule=owl` + a guardrail block;
    asserts spend rows still emit on cash, all non-positive, one per
    quarter — i.e. Owl is invisible to the rest of the system as a
    spending source.
* **Why.** Phase 3c spec brief — first non-stub `SpendingRule` behind
  the ABC, intentionally path-dependent (per the user's prompt). Same
  discipline guardrails as P3a/P3b: pure rule, no shared state, no
  ledger mutation, no lookahead, structural parity + numerical anchor,
  documented path dependence, MODEL_DOCUMENTATION update gating
  completion.
* **Impact on outputs.**
  * Default config (`spending.rule: flat_real`) is unaffected. All 83
    prior tests still pass.
  * Setting `spending.rule: owl` with a guardrail block produces a
    spending trajectory that matches `flat_real` until a guardrail
    band is breached (against forecast NAV), then ratchets up or down
    by `raise_pct` / `cut_pct` and stays there until the next year
    boundary.
  * 19 new tests in `tests/test_owl_adapter.py` + 1 orchestrator-level
    test. Total suite: **103 passed**.
* **Backward-compatible.** Yes for the public API. The schema gained
  `guardrail` as optional on `SpendingConfig`; existing configs without
  it validate unchanged. `Literal["flat_real", "smoothing"]` widened to
  include `"owl"` (forward-compatible).

### 2026-05-01 — Phase 3b post-audit doc clarifications

* **What.** Three documentation tightenings landed together per the
  Phase 3b audit:
  1. **transaction_cost classification** — the implementation
     subsection now carries an explicit *Rationale / Implication /
     Stability commitment* block stating that
     `transaction_cost` is modeled as an external cash outflow, why
     (NAV-conservation invariant preservation; deterministic
     reconciliation; net-of-cost reported returns), and the binding
     instruction not to reclassify it without a global redesign of
     §Core Invariants and all external-tie-out / NAV-conservation
     tests.
  2. **L8 + L14 explicitly paired.** Both limitations now name each
     other and call out that they must move together when PE
     liquidity / cost realism is upgraded. Fixing L14 alone (e.g.
     per-bucket bps with PE at 1500 bps) would surface L8's
     fiction as a 15%/quarter drag and is therefore worse than the
     status quo.
  3. **L13 forward risk — cost/rebalance feedback loop.** L13 now
     names the two open questions that any cost-aware optimizer
     wiring (Single- or Multi-Period Optimization with a cost
     penalty) must resolve before code changes:
     - reconcile the Phase 2 "one forward pass per quarter, no
       backfills, no retroactive mutation" rule with the
       optimizer's lookahead horizon;
     - replace the bps=0 stub-parity anchor with anchors valid
       under non-trivial trade-vs-target divergence (likely fixed
       `(current, target, bps, cost-penalty)` tuples with known
       optimal partial-trade vectors).
* **Why.** Phase 3b audit observation that "defensible interpretation"
  was too soft a framing for a load-bearing accounting decision, and
  that the future risks (cost feedback loop; PE liquidity/cost
  mismatch) needed to be encoded next to the limitations they govern,
  not lost in commit text.
* **Impact on outputs.** None (docs-only).
* **Backward-compatible.** Yes.

### 2026-05-01 — Phase 3b / Cvxportfolio implementation adapter

* **What.**
  * First non-stub `ImplementationAdapter`:
    `aa_model.implementation.cvxportfolio_adapter.CvxportfolioImplementation`
    (cvxportfolio 1.5.1, pure-Python, builds on cvxpy 1.8.2 already
    installed by the riskfolio extra).
  * **Engine flag.** New `base.implementation` block in
    `configs/base.yaml`:
    ```yaml
    implementation:
      engine: stub | cvxportfolio
      bps_per_trade: 0.0  # linear cost coefficient in basis points
    ```
    `aa_model.implementation.factory.make_implementation(engine=...)`
    dispatches by engine.
  * **Optional dependency behavior.** Lazy `import cvxportfolio` in
    the adapter constructor. Tests gated on
    `pytest.importorskip("cvxportfolio")`. Optional-deps group:
    `[project.optional-dependencies] cvxportfolio = ["cvxportfolio==1.5.1"]`.
  * **Cost model.** Linear, all-bucket:
    `cost_usd = (bps_per_trade / 1e4) · ∑ |trade|`. Matches the linear
    term of `cvxportfolio.costs.StocksTransactionCost(a=bps/1e4)`.
    Quadratic / market-impact / per-share terms intentionally NOT
    modeled — see L14.
  * **Path dependence.** None. Adapter is pure: trades depend only on
    the current and target vectors handed in for *this* call. See
    L13.
  * **Ledger extension.** New canonical flow_type
    `transaction_cost`, ordered after `rebalance` in
    `FLOW_ORDER`. Treated as an external outflow on the `cash`
    bucket (no offset elsewhere). Two §Core Invariants updated:
    - **External cash flow tie-out** now sums
      `inflow + spend + transaction_cost`.
    - **Total NAV conservation** now includes `transaction_cost` in
      the contributing-flows set.
    The orchestrator emits a single `transaction_cost` row per
    quarter (only when `cost_usd > 0`) on `cash` with source
    `impl:<engine>`.
  * **Cross-config validation.** Rejects `engine=stub` paired with
    `bps_per_trade != 0.0` (would silently drop the requested cost).
  * **Numerical anchor test.** Hand-worked closed-form check at 5 bps
    on a fixed (current, target) pair: 4M total trade volume × 5/10000
    = $2,000.00 cost; per-bucket trades match `[0, -2M, +2M, 0]`
    within 1e-4 USD. This is the L11 ε convention applied to the
    cvxportfolio adapter — first non-stub adapter shipping with a
    binding numerical anchor.
  * **Stub parity at zero cost.** With `bps_per_trade == 0` the
    cvxportfolio adapter produces trades and cost bit-equal to the
    stub. Tested directly.
  * **End-to-end.** New orchestrator-level test
    (`test_cvxportfolio_engine_preserves_invariants_under_nonzero_bps`)
    runs the full base scenario with `engine=cvxportfolio` + 5 bps
    and asserts (a) `transaction_cost` rows appear, (b) every cost
    row lands on cash with non-positive amount, (c) at most one cost
    row per quarter, (d) cumulative cost is positive but small
    (`< $1M` against a $100M portfolio over 20 quarters).
* **Why.** Phase 3b spec brief — first non-stub `ImplementationAdapter`
  behind the ABC. Discipline guardrails: pure adapter, no shared state,
  no caches, no ledger access; lazy backend import; structural parity
  + numerical anchor at non-zero bps; explicit no-path-dependence
  statement; ledger-invariant preservation under transaction costs.
* **Impact on outputs.**
  * Default config (`implementation.engine: stub`,
    `bps_per_trade: 0.0`) is unaffected. Existing 71 tests still pass.
  * Setting `implementation.engine: cvxportfolio,
    bps_per_trade: 5.0` against the base fixture produces:
    * 272-row ledger (vs 252 for stub) — extra 20 `transaction_cost`
      rows, one per quarter.
    * Cumulative transaction cost: $62,556.50 over 20 quarters
      (~0.063% of $100M starting NAV).
    * Final NAV: $114,705,602 (vs stub's $114,778,335) — exactly
      the $72,733 difference is end-of-horizon-NAV scaling of the
      $62,557 paid out (small compounding offset).
    * Q1 carries the largest cost ($23,510) because the rebalance
      from initial NAV to target weights is largest in Q1; Q2+ costs
      drop to ~$2,500/quarter (small drift correction).
  * 9 new tests in `tests/test_cvxportfolio_adapter.py` (structural
    parity + numerical anchor + determinism + scaling + edge-case
    bucket alignment + diagnostics) plus 1 new orchestrator-level
    test, all gated on `pytest.importorskip("cvxportfolio")`.
* **Backward-compatible.** Yes for the public API. Schema gained an
  `implementation:` block with `stub` defaults so existing configs
  without that block load unchanged. Existing tests pass under the
  bumped `FLOW_ORDER` (the canonical ordering test was updated to
  include `transaction_cost`).

### 2026-05-01 — L11 ε defined as `1e-4`

* **What.** Made the previously-symbolic `< ε` in the L11 version-bump
  policy a concrete value: per-bucket absolute weight difference
  `≤ 1e-4` (one basis point of weight). Same ε applies by analogy to
  other adapters' numerical anchor tests unless their Change Log entry
  documents a different value and why.
* **Why.** Phase 3a audit follow-up. An unspecified ε is unenforceable
  (a future test could pass with implicitly-large tolerance and still
  miss real drift).
* **Impact on outputs.** None today. Will bind on the next riskfolio
  bump and on the first numerical anchor test (P3b will produce one
  for cvxportfolio).
* **Backward-compatible.** Yes (docs only).

### 2026-05-01 — L11 tightened to mandatory version-bump policy

* **What.** Strengthened L11 from "any bump *must include* a
  parity-vs-known-good-CMA test" to a normative version-bump policy:
  any `riskfolio-lib` version change MUST pass structural parity AND
  match a frozen-CMA numerical anchor case within tolerance, OR
  explicitly document the deviation in the Change Log (with which
  bucket moved, magnitude, upstream cause, and the decision).
  Added the long-term fix path (Cholesky-derived synthetic samples)
  with an explicit adapter-insensitivity test scheduled to ship
  alongside it. Made the underlying assumption — "riskfolio ignores
  the returns frame after `hist=False`" — visible as an assumption
  rather than a contract.
* **Why.** Phase 3a audit observation that structural parity alone
  cannot catch subtle numerical drift across riskfolio versions, and
  that the synthetic-returns-frame coupling is currently load-bearing
  on an unverified internal assumption. Encoding the policy in this
  document promotes the requirement from "good practice" to "blocked
  unless explicitly waived in the Change Log."
* **Impact on outputs.** None today. The first practical effect lands
  on the next riskfolio version bump or on the Cholesky-fix commit,
  whichever comes first. The numerical anchor test itself is not yet
  written (deferred to "soon, before the first version bump") — the
  long-term fix and the anchor test will land together.
* **Backward-compatible.** Yes (docs only).

### 2026-05-01 — Phase 3a documentation expansion (gating P3b)

* **What.** Documentation-only update flagged by the Phase 3a audit
  before P3b can begin:
  * New limitation **L11 — Synthetic 2-row dummy returns frame in
    Riskfolio adapter** documenting the coupling against riskfolio's
    `Portfolio(returns=...)` constructor and the path forward (proper
    Cholesky-derived synthetic samples once a real CMA pipeline lands).
  * New limitation **L12 — Non-fatal "convert self.cov to a positive
    definite matrix" warning**, including the eigenvalue-check
    mechanism that triggers it and why suppressing it would mask
    future genuine cov problems.
  * New §Validation & Testing / *CI workflow* subsection making the
    `core` vs `adapters` split explicit, including the rule for
    extending it as more adapters land.
  * The Phase 3a change-log entry above (`9b35051`) was expanded
    to enumerate adapter purpose, engine flag, optional-dependency
    behavior, fallback CMA assumptions, the synthetic returns frame,
    the riskfolio warning, the structural parity contract, the
    observed output difference vs stub, the numpy/pyarrow bumps, and
    the CI split — point-by-point against the Phase 3a audit
    checklist.
* **Why.** Audit verdict required this update before P3b
  (cvxportfolio) starts. The synthetic-returns-frame coupling was
  also called out specifically as a future numerical risk and now has
  an explicit mitigation path.
* **Impact on outputs.** None (docs-only).
* **Backward-compatible.** Yes.

### 2026-05-01 — Phase 4a hardening: spend uniqueness + wrapper compat-only (`724b1a5`)

* **What.** Two post-audit tightenings on Phase 4a, no behavior
  change. (1) `QuarterlyLedger.validate()` now asserts a uniqueness
  invariant: for each `(run_id, quarter, source)` where
  `flow_type == "spend"`, exactly one row exists. Two new tests in
  `tests/test_ledger.py` — pass case (multiple distinct sources in one
  quarter still legal) and duplicate-detection. (2)
  `MODEL_DOCUMENTATION.md` now states explicitly that
  `quarterly_outflows()` is **compatibility-only** for path-dependent
  rules — the wrapper iterates against a synthetic ledger that has no
  realized return / pe_* / rebalance / transaction_cost flows, so it
  is **not** a correctness path. The authoritative correctness path
  is the orchestrator-driven `quarterly_outflow_at()` against the
  live ledger closed through `q-1`.
* **Why.** Path-dependent rules (`SmoothingRule`, `OwlRule`) recover
  prior outflow by filtering spend rows on their own `SOURCE_ID`; a
  duplicate row at the same `(run_id, quarter, source)` would silently
  double-count and corrupt the recovery. The wrapper-vs-orchestrator
  distinction matters most before Phase 4b, where cost-aware sizing
  must not be validated against the wrapper's degenerate trajectory.
  Both items lock the 4a interface before 4b touches allocation.
* **Impact on outputs.** None — invariant holds trivially under the
  current orchestrator (one spend row per quarter at
  `source=rule.SOURCE_ID`); doc clarification only.
* **Backward-compatible.** Yes. 108 tests pass.

### 2026-05-01 — Phase 4b design (pre-implementation)

* **What.** Lock the Phase 4b design ahead of implementation. Three
  load-bearing decisions:
  1. **Cost-awareness lives in the allocator (`target_at`), not in
     the implementation.** `ImplementationAdapter.rebalance(current,
     target, costs)` keeps its Phase 3b signature unchanged.
     No `rebalance_at` is added. (The earlier Phase 4 split-table
     entry that listed `rebalance_at` is corrected.)
  2. **The cost-aware optimization is a single convex problem solved
     once per quarter:** dollar-quadratic policy deviation
     (`λ · ‖w·V_total − w_policy·V_total‖²`) plus linear trade cost
     (`cost_per_dollar · ‖trade_dollars‖₁`), with
     `trade_dollars = w·V_total − current_dollars`. Both terms are
     in dollars — λ has interpretable scale and behavior is stable
     across NAV sizes. The `trade_dollars` framing makes per-quarter
     turnover explicit (cost is proportional to trade size, not to
     position deviation from policy). λ is surfaced as
     `allocation.policy_loss_lambda` config field, default `1.0`.
  3. **The cost-aware optimizer reads ONLY `current_dollars`,
     `w_policy`, `cost_model`, and `λ`.** It does not read the
     ledger; it does not read past quarters; it does not read future
     quarters. Pinned by the path-blindness anchor test.
* **What 4b is not.** Not a fixed-point. Not within-quarter
  iteration. Not a multi-period optimization (cvxportfolio's
  `MultiPeriodOptimization` is **not** wired). Not a spending
  modification — `quarterly_outflow_at`, the source filter, and the
  `spend` flow position are byte-identical to 4a. Not a default-flip
  — `engine=stub` remains default; cost-aware allocation is opt-in
  via a new `engine="cvxportfolio"` allocator. Not a
  `transaction_cost` flow change.
* **State channel.** Orchestrator passes `current_dollars` (the
  pre-rebalance NAV per bucket at quarter `q`, after canonical-order
  steps 0–6) as an explicit function argument to `target_at`. This
  is the explicit, auditable channel — it sidesteps the question
  "is current-quarter pre-rebalance state visible through
  `closed_through(q-1)`?" by not deriving it from the ledger at all.
  The 4a closure rule ("ledger closed through `q-1` is the only
  retrospective state") remains intact.
* **New numerical anchors** (six tests in a new file
  `tests/test_cost_aware_allocator.py`): zero-cost parity, closed-form
  2-bucket partial trade, bucket-order symmetry, monotonicity in bps,
  path-blindness, spending-untouched. Replaces the Phase 3b zero-bps
  cvx-vs-stub anchor (which is no longer the right test once the
  optimizer is involved).
* **Why.** L13 has been carrying the explicit forward risk that the
  rebalancer can't see cost in the trade decision. 4a's
  per-quarter state-flow contract makes 4b possible without
  reintroducing iteration. Locking the design with the dollar-quadratic
  policy term + explicit `trade_dollars` cost term prevents two
  failure modes a future contributor would otherwise re-introduce:
  (i) mixing unitless and dollar penalties under an implicit-units
  λ, and (ii) treating the L1 cost term as a position-deviation
  penalty rather than a turnover penalty.
* **Impact on outputs.** None — design only, no code changes in this
  commit. When 4b ships, output impact is **zero for `engine=stub`
  and `engine=riskfolio`** (default `target_at` returns `weights()`
  unchanged); output divergence is gated entirely on opt-in to
  `engine=cvxportfolio` allocator.
* **Backward-compatible.** Yes. Allocator API extension is additive
  (`weights()` preserved as the cost-blind policy reference).

### 2026-05-02 — Phase 4b implementation: cost-aware allocator

* **What.** Implements the design locked one day prior. Five surfaces
  touched, no behavior change for any existing config:
  1. **Schema.** `PublicAllocationConfig.policy_loss_lambda: float =
     1.0` (gt=0). `AllocationRefConfig.engine` Literal extended with
     `"cvxportfolio"`. `validate_study_config` accepts the new engine.
  2. **ABC.** `AllocationAdapter.target_at(ledger, params, quarter,
     current_dollars, cost_model)` added as a non-abstract method
     with default body `return self.weights()` — stub and riskfolio
     adapters inherit unchanged. `AllocationParams` dataclass added
     mirroring `SpendingParams`.
  3. **CvxportfolioAllocator.** New
     `aa_model.allocation.cvxportfolio_adapter.CvxportfolioAllocator`.
     `weights()` returns the policy reference (config `stub_weights`,
     same source as the stub adapter). `target_at` solves the single
     convex problem from the design doc using `cvxpy` + CLARABEL and
     canonicalizes the output (clip to ≥ 0, round to 12 decimals,
     correct sum-to-1 by adjustment on the largest-weight bucket).
     Path-blindness enforced — the method does not read `ledger`.
     Wired through `make_allocator(engine="cvxportfolio")`.
  4. **Orchestrator.** Step 6.5 inserted between spend and rebalance:
     `current_dollars = pd.Series(running_nav)`; `target_weights =
     alloc.target_at(ledger, alloc_params, q, current_dollars,
     cost_model)`. The static `target_weights = alloc.weights()` call
     at run start is removed; the per-quarter target now drives the
     rebalance step. For `engine=stub` and `engine=riskfolio` the
     default `target_at` returns `weights()` and the per-quarter
     target collapses to today's static target.
  5. **Tests.** Six anchors plus three smoke tests in new
     `tests/test_cost_aware_allocator.py` (12 tests). One end-to-end
     orchestrator integration test `test_cvxportfolio_allocation_
     engine_preserves_invariants_end_to_end` (1 test). Total: **121
     passed** (was 108 after the 4a hardening).
* **Why.** L13's "no cost-aware trading decisions" forward-risk
  closes here. The 4a per-quarter state-flow contract made this
  possible without reintroducing iteration; the design doc reasoning
  (cost-in-allocator, dollar-quadratic policy term, explicit
  `current_dollars` channel, path-blindness) is preserved verbatim.
* **Impact on outputs.** Zero for every shipped config. All shipped
  configs use `allocation.engine=stub`; the default `target_at`
  passthrough produces the same `target_weights` every quarter, so
  the orchestrator behavior is bit-identical to pre-4b. Output
  divergence is gated entirely on opting into the new
  `allocation.engine=cvxportfolio` engine.
* **Backward-compatible.** Yes. The new schema field has a default
  (`policy_loss_lambda=1.0`); existing public allocation YAMLs load
  unchanged. The new engine name is opt-in. Existing test surface
  green; reproducibility test (same configs + same fixtures →
  byte-identical ledger) holds.

### 2026-05-02 — Phase 4b — normalized λ (`policy_loss_lambda_norm`)

> **Migration.** The `allocation.policy_loss_lambda` field added in
> the 4b implementation entry above is **renamed** to
> `allocation.policy_loss_lambda_norm`. To preserve identical
> numerical behavior under the rename, set
> `policy_loss_lambda_norm = (old policy_loss_lambda) · V_total²`,
> where `V_total` is the portfolio NAV the old value was tuned for.
> No public configs ship with the old field set, so this rename
> affects only out-of-tree consumers that opted into the
> `engine=cvxportfolio` allocator between 2026-05-02 and now.

* **What.** User-facing calibration improvement, no objective-form
  change. The cvxportfolio allocator now stores
  `policy_loss_lambda_norm` (default `1.0`) and computes the
  effective coefficient inside `target_at` as
  `λ_eff = policy_loss_lambda_norm / V_total²`. The cvxpy expression
  retains the locked Phase 4b form
  (`λ_eff · ‖(w − w_policy) · V_total‖²`) so the code text matches
  the design doc verbatim. Mathematically the `V_total²` factor and
  the `V_total²` divisor cancel — the policy term simplifies to
  `λ_norm · ‖w − w_policy‖²`. Files touched: `io/schemas.py` (field
  rename), `allocation/cvxportfolio_adapter.py` (storage + scaling),
  `tests/test_cost_aware_allocator.py` (migration of test λ values),
  `tests/conftest.py` (integration fixture).
* **Why.** Audit observation from the 4b ship: at the dollar-units
  default `policy_loss_lambda = 1.0`, the policy term scales as
  `V_total²` and dominates the cost term by `~V_total` orders of
  magnitude — cost-aware behavior is effectively disabled at any
  realistic NAV. The user-facing field becomes much more
  interpretable when the `V_total²` dependency is moved inside the
  adapter: the default `λ_norm = 1.0` now represents a unitless
  weight-quadratic intensity, comparable across portfolios. **Caveat
  the design doc retains:** the L1 cost term is linear in dollars,
  so the partial-trade *threshold* still scales with
  `V_total / λ_norm`. Normalization fixes the policy term's units,
  not the policy/cost balance — calibrate `λ_norm` empirically for
  the desired partial-trade-vs-policy-track behavior at the
  portfolio's representative scale.
* **Scope.** Affects `engine=cvxportfolio` allocator only.
  `engine=stub` and `engine=riskfolio` ignore the field; their
  default `target_at` returns `weights()` regardless of `λ_norm`.
* **Impact on outputs.** Zero for shipped configs (all use
  `engine=stub`). For out-of-tree `engine=cvxportfolio` consumers,
  see the migration note above.
* **Backward-compatible.** Field renamed loudly — pydantic's
  `extra="forbid"` will fail validation on the old field name. This
  is the same loud-failure pattern Phase 4a used when removing
  `forecast_quarterly_return_pct`. 121 tests pass.

### 2026-05-02 — Phase 4b — λ calibration sweep + zero-cost-parity fix

* **What.** Research probe (`scripts/sweep_lambda_calibration.py`) +
  generated report (`docs/sweep_lambda_calibration_2026_05_02.md`)
  iterating the cross-product
  ``λ_norm ∈ {0.01, 0.1, 1.0, 10.0, 1e3, 1e6}`` × ``bps ∈ {0, 5, 25,
  100}`` × ``scenario ∈ {base, public_drawdown, inflation_shock}`` =
  72 cells. Two material outputs:
  1. **Bug fix in `CvxportfolioAllocator.target_at`.** The sweep's
     bps=0 column surfaced 3–5pp policy deviation at small
     ``λ_norm`` (0.01, 0.1) at V_total = $100M — CLARABEL's default
     tolerance stops short of tight policy convergence on the
     weakly-conditioned policy quadratic (``λ_eff = λ_norm /
     V_total² = 1e-18`` for ``λ_norm = 0.01`` at $100M). Fix:
     short-circuit ``cost_per_dollar == 0`` to return policy
     directly; mathematically equivalent (the L1 term vanishes and
     the strictly-convex policy quadratic has its unique global
     minimum at policy). Regression test added in
     `tests/test_cost_aware_allocator.py`
     (`test_zero_cost_parity_holds_at_realistic_nav_and_low_lambda`)
     covering all four ``λ_norm`` values from the sweep at V = $100M.
     **122 tests pass** (was 121).
  2. **Calibration interpretation note in MODEL_DOCUMENTATION.md
     §Phase 4b design.** At V_total = $100M with realistic
     transaction costs (bps ≥ 5), the cost-aware optimum is
     **corner-dominated** across ``λ_norm ∈ [0.01, 1e3]`` — total
     turnover and cumulative tx cost are bit-identical across this
     entire range at any given bps>0. Sensitivity becomes visible
     only at ``λ_norm ≈ 1e6`` for $100M / 5 bps — six orders of
     magnitude above the schema default ``λ_norm = 1.0``.
     Rule-of-thumb scaling for engaging interior partial-trade
     behavior: ``λ_norm ≈ bps × V_total × 1e-3``. This is
     documented behavior of the dollar-quadratic + linear-cost
     formulation, not a bug; the default is intentionally
     conservative and produces effectively cost-aware-OFF behavior
     at institutional scales.
* **Why.** Per audit verdict on the 4b ship, calibrate before adding
  realism layers (CMA / STAIRS / PE). The sweep both validated that
  the engine is structurally correct (no failures, no invariant
  violations across 72 cells) and surfaced an interpretation risk
  worth pinning in the doc — the user-facing default does not engage
  cost/policy trade-off reasoning at typical institutional NAV
  scales. The bps=0 numerical bug was a side-effect of the same
  underlying fact: at small ``λ_eff``, the solver's optimality gap
  no longer constrains the decision space tightly.
* **Impact on outputs.** Zero for shipped configs (all
  ``engine=stub``). For ``engine=cvxportfolio`` users with
  ``bps_per_trade=0``, output is **changed**: now exactly equals
  policy. Previously could deviate up to ~5pp at small ``λ_norm`` at
  $100M-scale portfolios (the bug). For ``engine=cvxportfolio``
  users with ``bps_per_trade>0``, no behavior change — the
  short-circuit is gated on ``cost_per_dollar == 0``.
* **Backward-compatible.** Yes for non-buggy paths; the bps=0
  output change at small ``λ_norm`` is the bug fix itself (zero-cost
  parity now holds where it didn't).

### 2026-05-02 — Phase 4b — λ calibration advisory diagnostic

* **What.** Diagnostic-only surfacing of the calibration formula
  derived in the 2026-05-02 sweep. Three additions, no objective
  change:
  1. ``CvxportfolioAllocator`` now records a per-quarter calibration
     row inside ``target_at`` containing ``v_total_usd``,
     ``bps_per_trade``, ``policy_loss_lambda_norm_used``,
     ``suggested_policy_loss_lambda_norm`` (formula
     ``bps_per_trade × V_total × 1e-3``), and
     ``ratio_used_over_suggested``. The record is taken **before**
     the cost-branch / zero-cost short-circuit so every non-q0 call
     contributes one row regardless of cost.
  2. ``alloc.diagnostics()`` returns ``calibration_history`` (list)
     and ``calibration_summary`` (medians of V_total, suggested
     λ_norm, and used/suggested ratio).
  3. ``write_markdown_report`` accepts an optional
     ``allocator_diagnostics`` keyword and renders a
     "Cost-aware allocator calibration (advisory)" section when
     ``engine=cvxportfolio`` and the calibration history is
     non-empty. The orchestrator passes ``alloc.diagnostics()``
     through automatically. The section reports the formula, the
     median V_total, configured vs suggested λ_norm, the median
     ratio, and a regime classification (corner-dominated /
     tunable / policy-dominated) at thresholds 1e-2 and 1e2.
* **Why.** The 2026-05-02 sweep showed default
  ``λ_norm = 1.0`` is corner-dominated at institutional NAV scales
  (six orders of magnitude below the engagement threshold). Without
  per-run feedback, users have no in-line signal that their
  configured value is mathematically inert. Surfacing the
  rule-of-thumb suggestion alongside the configured value makes the
  policy/cost balance legible at run time without changing the
  model. **Strictly advisory** — no auto-tuning, no fallback, no
  influence on the optimization.
* **Impact on outputs.** None for shipped configs (all
  ``engine=stub``; the renderer skips the new section when no
  cost-aware diagnostics are present). For ``engine=cvxportfolio``
  runs, ``report.md`` gains a new section but ``ledger.parquet`` is
  byte-identical and ``manifest.json`` is unchanged — diagnostic
  separation matches §Reproducibility / §Determinism contracts.
  Trade weights are unchanged (regression test
  ``test_calibration_diagnostics_do_not_alter_target_weights``
  pins this). 126 tests pass (was 122).
* **Backward-compatible.** Yes. ``write_markdown_report``'s new
  parameter is keyword-only with a ``None`` default; existing
  callers (none in tree) continue to work. ``alloc.diagnostics()``
  output is additive.

### 2026-05-02 — Phase 5 design locked (CMA pipeline, pre-implementation)

* **What.** Lock the §Phase 5 design ahead of implementation.
  Replaces the placeholder allocation assumption surface
  (``_DEFAULT_VOL_ANNUAL`` table, identity correlation, zero
  expected returns) with a validated, explicit CMA layer loaded
  from ``configs/cma.yaml``. Key locked decisions:
  1. **CMA is config-side, not code-side.** ``base.yaml`` gains a
     ``cma: { config: configs/cma.yaml }`` reference; loaded by the
     orchestrator alongside other sub-configs; folded into the
     ``fixtures_hash`` so a CMA edit invalidates run reproducibility
     correctly.
  2. **Bucket universe = full allocation bucket index** (public +
     PE sleeves). Cross-config validator enforces
     ``CMA.buckets == allocation.stub_weights.keys()``.
  3. **Validation is loud and immediate.** Per-cell bounds
     (``vol ≥ 0``; ``|expected_return| < 1.0`` to catch
     percent-vs-decimal mistakes; correlations in ``[-1, 1]``);
     diagonal-1 + symmetry checks; full PSD check on the assembled
     covariance matrix at fixed tolerance (``-1e-9``). No
     regularization, no nearest-PSD repair, no clipping.
  4. **CMA is NOT consumed by the cost-aware allocator in
     Phase 5.** ``CvxportfolioAllocator.target_at`` is unchanged.
     CMA is consumed by ``RiskfolioAdapter`` (replaces the
     placeholder fallback in the MinRisk solve) and by
     ``report.md`` (new "Capital market assumptions" section with
     portfolio-level expected return + expected vol). Surfacing
     CMA inside the cost-aware objective is a Phase 6+ task.
  5. **Initial shipped values replicate today's defaults**
     (``_DEFAULT_VOL_ANNUAL`` + identity correlations + zero
     expected returns), so the cutover is structural — assumption
     surface becomes config-explicit while reproducibility hashes
     and ledger contents stay byte-stable.
  6. **L4 closes** when the implementation lands (placeholder CMA
     becomes test-only).
* **Why.** Per audit verdict on the 4b calibration ship —
  allocation engines are now structurally correct but still fed by
  placeholder assumptions; CMA is the next-highest-value realism
  layer. Locking the design ahead of implementation prevents
  scope creep (no STAIRS, no PE pacing changes, no objective
  reformulation, no Bayesian views) and pins the
  CMA-not-used-in-cost-aware-allocator rule before someone
  reasonably assumes the optimizer must be reading expected returns.
* **Impact on outputs.** None — design only, no code changes in
  this commit. When Phase 5 ships, output impact is zero for
  shipped configs (initial CMA values replicate defaults exactly).
* **Backward-compatible.** Yes (design only).

### 2026-05-02 — Phase 5 implementation: CMA pipeline (resolves L4)

* **What.** Implements the §Phase 5 design locked one commit prior.
  Eight surfaces touched:
  1. **Schema** (`io/schemas.py`). New `CMAConfig` pydantic model
     with per-cell validators (vol ≥ 0, |ER| < 1.0
     percent-vs-decimal guard, correlations in [-1, 1], diagonal
     == 1, symmetry within 1e-9), bucket-set agreement across the
     three dicts, optional liquidity tags restricted to
     `{liquid, semi_liquid, illiquid}`, and a model-level PSD check
     on the assembled `Σ = diag(vol) · corr · diag(vol)` at fixed
     `-1e-9` tolerance. `BaseConfig` gains `cma: _SubConfigRef` and
     `StudyConfig` gains `cma: CMAConfig`.
  2. **Loader** (`io/loaders.py`). New `load_cma_config(path)`;
     `load_study_config` resolves the new sub-config and includes
     it in the `StudyConfig`. The CMA dump goes into the
     `config_hash` (joining base / allocation / spending / pe_pacing
     / scenarios) so a CMA edit invalidates run reproducibility the
     same way edits to other config-side files do.
  3. **Cross-config validation** (`io/validation.py`). Enforces
     `CMA.buckets == allocation.stub_weights.keys()` with a
     missing/extra diff in the error message.
  4. **Dataclass adapter** (`assumptions/cma.py`). New
     `CMA.from_config(cfg)` classmethod constructs the populated
     dataclass with sorted bucket index. Empty `CMA()` is reserved
     as a test-only sentinel.
  5. **`configs/cma.yaml`**. Replicates the prior
     `_DEFAULT_VOL_ANNUAL` table (cash 0.5%, public_bond 4%,
     public_equity 16%, pe_buyout 20%) + identity correlations +
     zero expected returns + liquidity tags. Cutover is structural,
     not numerical.
  6. **`configs/base.yaml`**. Adds
     `cma: { config: configs/cma.yaml }`.
  7. **Orchestrator** (`integration/orchestrator.py`). Builds
     `cma = CMA.from_config(cfg.cma)` and passes to `alloc.fit(...)`
     and `write_markdown_report(...)`.
  8. **Report** (`integration/report.py`). New
     "Capital market assumptions" section with per-bucket ER / vol
     / liquidity table; "Portfolio priors at policy weights"
     subsection (`w_policy · expected_returns` and
     `sqrt(w_policy.T · Σ · w_policy)`); liquidity bucket counts.
* **Why.** Per audit verdict on the 4b calibration ship —
  allocation engines were structurally correct but still fed by
  placeholder code-side assumptions. CMA is the next-highest-value
  realism layer: validated, explicit, reproducible. Resolving L4
  closes the placeholder lineage and unblocks future calibration
  work (real correlations, calibrated expected returns, vol cones)
  as a separate concern.
* **CMA is NOT consumed by the cost-aware allocator.**
  `CvxportfolioAllocator.target_at` is unchanged; its objective
  remains `λ_norm · ||(w − w_policy) · V_total||² + cost · ||trade||₁`
  with no CMA inputs. Surfacing CMA in a cost-aware-with-Sharpe
  variant is a Phase 6+ task that requires explicit objective
  reformulation, design review, and new anchor tests.
* **Tests added (15).** `tests/test_cma_loader.py` covers all
  validation rules — round-trip, negative vol, NaN expected
  return, percent-vs-decimal guard, out-of-range correlation,
  asymmetry, diagonal != 1, cross-config bucket mismatch, non-PSD
  matrix (constructed counter-example with pairwise valid
  correlations), liquidity optional + invalid-tag + bucket-mismatch,
  shipped `configs/cma.yaml` round-trip + alignment.
  `test_riskfolio_consumes_explicit_cma_not_fallback` pins that
  the adapter actually uses the loaded vols (not the placeholder
  fallback) by varying public-equity vol and observing the MinRisk
  weight respond.
  `test_report_contains_capital_market_assumptions_section`
  exercises the end-to-end report rendering.
* **Impact on outputs.** Zero numerical change on shipped configs
  — the new CMA YAML matches the prior fallback values exactly
  (tested by `test_riskfolio_*` adapter tests still passing
  bit-for-bit). `report.md` gains the "Capital market assumptions"
  section. `ledger.parquet` and `manifest.json` are byte-stable.
  **141 tests pass** (was 126). 15 new tests; zero regressions.
* **Backward-compatible.** Yes for adapter behavior; **breaking**
  for any out-of-tree `base.yaml` that omits the new `cma` key
  (pydantic `extra=forbid` will fail validation loudly — same
  pattern Phase 4a used when removing
  `forecast_quarterly_return_pct`). All in-tree configs and
  fixtures are updated.

### 2026-05-02 — Phase 6 design locked (correlation_shock / L6, pre-implementation)

* **What.** Lock the §Phase 6 design ahead of implementation. A
  scenario-driven perturbation layer for the CMA correlation matrix
  only. Six locked decisions:
  1. **Discriminated-union schema.** Two variants:
     ``scale {type, magnitude}`` and
     ``override {type, matrix}``. No shared fields beyond
     ``type``.
  2. **`scale` is sign-preserving amplification** with
     ``ρ_new = clip(ρ × magnitude, -1, 1)`` for off-diagonals.
     Positive correlations move further positive; negative move
     further negative. To force a "tighten everything toward +1"
     regime, use `override`. ``scale`` always applies to **every**
     off-diagonal entry — no `target` field; targeted stress is
     `override`'s job.
  3. **`override` partial merge with auto-mirror** for the user-
     supplied direction. If both directions are supplied and
     **disagree**, fail loudly with both values. **No silent
     averaging.** Diagonal entries (if specified) must equal 1.0
     within 1e-9; per-cell values in ``[-1, 1]``; unknown bucket
     names fail at apply time with the bucket name in the error.
  4. **CMA immutability + perturbation-only.** Baseline ``CMA`` is
     never mutated; shock operates on a copy and produces a new
     instance. Only ``corr`` may change; ``vol_annual`` and
     ``expected_returns_annual`` pass through unchanged.
  5. **Validation is hard.** Post-shock matrix must satisfy
     symmetry (by construction in both variants), diagonal == 1,
     entries in ``[-1, 1]``, and PSD on the assembled covariance
     at fixed ``-1e-9`` tolerance. Failure raises ``ValueError``
     with ``λ_min`` in the message. **No PSD repair, no
     nearest-matrix projection, no blending.**
  6. **No optimizer-objective change.** ``RiskfolioAdapter``
     consumes the shocked CMA as if it were the baseline.
     ``CvxportfolioAllocator`` continues to ignore CMA. No
     allocator API change.
* **What this is not.** Not a vol/return shock; not time-varying;
  not a CMA replacement; not a STAIRS layer (Phase 7+, blocked
  on L6 observation); not a PSD repair; not an optimizer change.
* **Why.** Phase 5 landed an explicit CMA correlation matrix —
  L6 is the natural unblock. STAIRS would stack regime/time-varying
  complexity on top of an unshocked CMA prior; correlation shocks
  give the static stress-test layer first, validate it, then layer
  regime dynamics later.
* **Impact on outputs.** None — design only.
* **Backward-compatible.** Yes (design only).

### 2026-05-02 — Phase 6 implementation: correlation_shock (resolves L6)

* **What.** Implements the §Phase 6 design locked one commit prior.
  Six surfaces touched, one cohesive commit:
  1. **Schema** (`io/schemas.py`). New ``CorrelationShock``
     discriminated-union: ``_ScaleCorrelationShock`` (positive,
     finite ``magnitude``) and ``_OverrideCorrelationShock``
     (per-cell ``[-1, 1]`` bounds, diagonal == 1, asymmetric supply
     fails loudly with both values in the error). Pydantic
     ``Field(discriminator="type")`` routes by variant.
  2. **Apply function** (`assumptions/correlation_shock.py`).
     ``apply_correlation_shock(cma_cfg, shock) -> (CMAConfig,
     diagnostics)``. Operates on ``CMAConfig`` (dict form), not on
     the ``CMA`` dataclass — that lets the shocked correlations
     substitute into ``cfg.cma`` and flow through the existing
     ``hash_study_config`` + ``_build_ledger`` pipeline with no
     extra wiring. The shocked dict is fed back through
     ``CMAConfig.model_validate`` so per-cell + PSD validation
     re-runs on the post-shock state. Variant semantics:
     - ``scale``: ``ρ_new = clip(ρ × magnitude, -1, 1)`` for every
       off-diagonal. Diagonal preserved. Sign-preserving — positive
       and negative correlations both grow in absolute magnitude.
       Clipped pairs counted and surfaced.
     - ``override``: partial merge over the baseline, auto-mirrored
       (one-direction supply is fine; symmetric supply with equal
       values is fine; asymmetric supply raises at schema time).
       Unknown bucket names raise at apply time with bucket name
       in the message. Diagonal entries (if specified) must be 1.0.
     - Diagnostics dataclass (``CorrelationShockDiagnostics``) carries
       ``shock_type``, ``magnitude`` / ``override_pairs`` /
       ``clipped_pairs``, ``max_abs_delta`` for the report.
  3. **Scenario carrier** (`assumptions/scenario_builder.py`).
     ``Scenario`` gains optional ``correlation_shock``. New 6th
     canonical scenario ``crisis_correlation`` ships an override
     pushing ``public_equity ↔ pe_buyout`` to 0.95 and
     ``public_bond ↔ public_equity`` to 0.30 (``scale`` would be a
     no-op against the shipped identity-correlation CMA).
  4. **Orchestrator** (`integration/orchestrator.py`).
     ``_apply_scenario`` materialises the shock into a new
     ``CMAConfig`` substituted into ``cfg.cma`` and returns the
     diagnostics alongside the new ``cfg``. ``cfg.cma`` is in
     ``config_hash``, so the shock automatically produces a distinct
     ``run_id`` — no special handling required. ``_build_ledger``
     unchanged.
  5. **Report** (`integration/report.py`). New optional
     "Correlation shock (scenario)" section showing type,
     pairwise replacements (override) or magnitude + clipped count
     (scale), max |Δρ|, PSD status, baseline-preserved note.
  6. **Validation re-use.** No new PSD code path: the shocked
     ``CMAConfig`` round-trips through the existing CMA validator,
     which raises with ``smallest eigenvalue = X.XXXe-N`` if the
     shock breaks PSD.
* **Why.** Phase 5 landed an explicit CMA correlation matrix; L6
  was the natural unblock. Doing correlation shocks before STAIRS
  gives a static stress-test layer first, validates it, then layers
  regime dynamics (Phase 7+).
* **Tests added (18).**
  - ``tests/test_correlation_shock.py`` (16): discriminated-union
    routing (3); scale schema (positive + finite magnitude, 2);
    override schema (out-of-range value, diagonal != 1, asymmetric
    supply, symmetric-equal-supply allowed, 4); apply semantics
    (sign-preserving amplification, clip-and-count, partial-merge
    + auto-mirror, unknown-bucket failure, baseline immutability,
    PSD failure on override, PSD failure on scale-clip, 7).
  - ``tests/test_orchestrator.py`` (2): ``crisis_correlation``
    end-to-end (report section + diagnostics); shock changes
    ``config_hash`` (architectural contract that scenario
    substitution into ``cfg.cma`` is the right design).
  - ``tests/test_scenario_builder.py`` updated:
    ``test_canonical_scenarios`` now expects 6 scenarios (was 5);
    existing hash-uniqueness test passes with the new scenario
    because the shock propagates into ``config_hash``.
* **Impact on outputs.** Zero numerical change on shipped configs.
  Existing 5 canonical scenarios run identically (none of them
  set ``correlation_shock``). The new ``crisis_correlation``
  scenario produces a distinct run_id and a distinct
  end-of-horizon allocation under the riskfolio engine (the only
  CMA-consuming engine). ``ledger.parquet`` and ``manifest.json``
  byte-stable on the unshocked scenarios. **160 tests pass** (was
  142).
* **Backward-compatible.** Yes. ``Scenario`` gains a new optional
  field with ``None`` default; existing scenarios pass through
  unchanged. The new schema and the new scenario are additive.

### 2026-05-02 — Phase 7 design locked (STAIRS PE adapter, pre-implementation)

* **What.** Lock the §Phase 7 design ahead of implementation.
  Replaces the TA model's constant ``growth_pct`` with a CMA-driven,
  public-equity-coupled growth term, behind a new PE adapter
  pattern. Six locked decisions:
  1. **Scope is deterministic single-path.** Not Monte Carlo, not
     multi-path, not regime-switching. Stochastic STAIRS deferred
     to a future phase.
  2. **Adapter pattern.** New ``pe/base.py`` (``PEAdapter`` ABC) +
     ``pe/ta_adapter.py`` + ``pe/stairs_adapter.py`` +
     ``pe/factory.py``. Engine selector ``BaseConfig.pe.engine ∈
     {"ta", "stairs"}``, default ``"ta"`` so existing configs are
     bit-stable.
  3. **STAIRS recursion.** Per quarter:
     ``growth_pct_q = (drift / 4) + beta · (realized_pu - expected_pu)``
     where ``expected_pu = cma.expected_returns_annual["public_equity"] / 4``.
     Coupling reference is ``public_equity`` only (no per-sleeve
     mapping yet). Excess baseline is CMA-anchored (no separate
     parameter).
  4. **Required clipping.** ``growth_pct_q ≥ -0.99`` enforced at
     the per-quarter step. NAV cannot drop below zero; upside
     unbounded. The clip count is surfaced in diagnostics so
     activation is visible. This is a **domain constraint**, not
     silent repair.
  5. **Cash-call / distribution / NAV schema unchanged.** Only the
     NAV-mark term differs; ``PROJECTION_COLUMNS`` stays
     byte-compatible. The orchestrator's PE-flow emission code
     does not branch on engine.
  6. **Loud failure on misconfiguration.** When
     ``pe.engine=stairs`` but ``stairs_defaults`` is missing or
     its sleeve set doesn't equal the ``pe_*`` subset of
     ``allocation.stub_weights``, validation fails at config load
     with a precise diff. **No silent fallback to TA.**
* **Parity contract.** STAIRS at ``beta=0`` and
  ``idiosyncratic_drift_pct = ta_defaults.growth_pct`` for every
  sleeve must produce **byte-identical** projections to TA. Two
  parity tests pin this — same pattern as riskfolio's
  binding-equality structural anchor.
* **Numerical anchors planned (5 beyond parity).** Beta
  amplification under drawdown, idiosyncratic-only path
  monotonicity, public-equity decoupling at ``beta=0``, linear
  commitment property, growth-clip activation under extreme
  drawdown.
* **L1 status.** Will flip to
  ``[PARTIALLY RESOLVED 2026-05-02, Phase 7]`` on implementation:
  the artifact-driven "free return lift" mechanism is closed under
  STAIRS; under TA it persists and is documented as
  engine-conditional. Residual scenario-driven PE return effect
  under STAIRS is the *real* timing-coupling channel, not a bug.
* **What this is not.** Not stochastic, not Monte Carlo, not
  regime-switching, not GBM, not jump-diffusion, not a
  recommitment optimizer, not a new ledger schema, not an L8 fix,
  not an L2 fix, not a public-side return generator, not a
  per-sleeve coupling generalization.
* **Why.** Phase 6 closed L6 (correlation shocks); next
  highest-value structural realism is the public→PE transmission
  channel. This is what makes timing-scenario differences
  *economic* rather than *artifactual*. STAIRS first, stochastic
  PE later.
* **Impact on outputs.** None — design only. When Phase 7 ships,
  default-config output is byte-stable (``pe.engine=ta`` is
  default and is bit-equivalent to today).
* **Backward-compatible.** Yes (design only).

### 2026-05-02 — Phase 7 implementation: STAIRS PE adapter (partially resolves L1)

* **What.** Implements the §Phase 7 design locked one commit prior.
  Single cohesive commit because the adapter pattern only makes
  sense when schema, ABC, both engines, factory, orchestrator
  wiring, and tests land together. Five surfaces:
  1. **Schema** (`io/schemas.py`). New
     ``base.pe.engine: Literal["ta", "stairs"] = "ta"``. New
     ``StairsDefaultsConfig`` with ``per_sleeve: dict[str,
     _StairsSleeveParams]``; per-sleeve params validate
     ``|idiosyncratic_drift_pct| < 1.0`` (percent-vs-decimal
     guard) and ``finite(beta_to_public_equity)``.
     ``PEPacingConfig`` gains optional ``stairs_defaults``.
  2. **Cross-config validation** (`io/validation.py`). When
     ``pe.engine == "stairs"``, ``stairs_defaults`` must be present
     and its ``per_sleeve`` keys must equal the ``pe_*`` subset of
     ``allocation.stub_weights``. Missing or extra sleeves raise
     with a precise ``missing: [...] extra: [...]`` diff. **No
     silent fallback to TA.**
  3. **Adapter pattern** (new `pe/base.py`, `pe/ta_adapter.py`,
     `pe/stairs_adapter.py`, `pe/factory.py`). ``PEAdapter`` ABC
     mirrors ``AllocationAdapter`` / ``ImplementationAdapter``.
     ``TAAdapter`` is a thin pass-through to the existing
     ``pacing.project_horizon`` (zero behavior change at
     ``pe.engine="ta"``). ``STAIRSAdapter`` implements the
     CMA-coupled recursion:
       ``growth_pct_q = drift/4 + beta · (realized_pu - expected_pu)``
       ``growth_pct_q = max(growth_pct_q, -0.99)``  # required clip
     with ``expected_pu = cma.expected_returns_annual["public_equity"] / 4``.
     Quarters outside the supplied path default to ``excess = 0``
     (CMA-expectation default). The clip count is surfaced via
     ``adapter.diagnostics()["clipped_quarters"]``.
  4. **Orchestrator wiring** (`integration/orchestrator.py`).
     ``_build_ledger`` pre-computes a deterministic
     ``public_equity_path: pd.Series`` from ``rate_table`` (which
     already incorporates fixture overrides) and dispatches the
     PE projection through ``make_pe_adapter(engine=cfg.base.pe.engine)``.
     ``PROJECTION_COLUMNS`` is unchanged; the per-quarter ledger
     emission code does not branch on engine.
  5. **Tests** (`tests/test_pe_adapter_stairs.py`, 16 cases).
     Schema-level: drift bounds (out-of-range, non-finite),
     beta finite, per_sleeve non-empty. Cross-config:
     stairs-engine-without-stairs_defaults, sleeve-set mismatch.
     Adapter-level: parity at ``beta=0 + drift=growth_pct``
     (per-fund + orchestrator byte-stable), beta amplification
     under drawdown, idiosyncratic-drift monotonicity at ``beta=0``,
     public_equity decoupling at ``beta=0``, linear commitment
     property (``$X + $Y == $X+Y`` aggregate), growth-clip
     activation under extreme drawdown. Plus factory routing tests.
* **L1 status.** Flipped to
  ``[PARTIALLY RESOLVED 2026-05-02, Phase 7]``: free return lift
  closed under STAIRS; persists under TA (engine-conditional).
* **Why.** Phase 6 closed L6 (correlation shocks) but the
  public→PE transmission channel was still missing — scenarios
  that move public markets had no path into PE. STAIRS supplies
  that channel deterministically, without expanding the engine to
  multi-path / Monte Carlo. The growth clip is a domain
  constraint preventing NAV from going negative under extreme
  shocks × high beta.
* **Impact on outputs.** Zero on default-config runs
  (``pe.engine="ta"`` is the default; the TA adapter is
  byte-stable with the pre-Phase-7 single-function path).
  ``ledger.parquet`` and ``manifest.json`` byte-stable on every
  shipped scenario. End-to-end byte-stability also verified
  under ``pe.engine="stairs"`` at parity settings
  (``beta=0, drift=growth_pct``) by
  ``test_stairs_engine_at_parity_yields_byte_stable_orchestrator_run``.
  Output divergence is gated entirely on opting into
  ``pe.engine="stairs"`` with non-parity settings.
* **Tests.** **176 pass** (was 160). 16 new; zero regressions.
* **Backward-compatible.** Yes for adapter behavior; **breaking**
  for any out-of-tree config that explicitly sets
  ``pe.engine="stairs"`` without supplying ``stairs_defaults`` —
  pydantic + cross-config validation will fail loudly.

### 2026-05-02 — Phase 8 design locked (PE illiquidity in rebalancing, pre-implementation)

* **What.** Lock the §Phase 8 design ahead of implementation.
  Resolves L8 (rebalancer treats PE as liquid). An illiquidity
  overlay is inserted between the cost-aware target (Phase 4b
  step 6.5) and the implementation rebalance (step 7). Eight
  locked refinements:
  1. **Default-on as correctness fix.** Phase 8 may intentionally
     change default ledger outputs; the implementation Change Log
     must explicitly account for which tests get re-anchored.
  2. ``liquid_nav < 0`` fails loudly with a per-bucket breakdown.
     ``liquid_nav == 0`` allowed only when every liquid bucket
     already has zero current dollars (genuine no-op); otherwise
     fails loudly.
  3. **Empty liquid set fails at config validation**, not apply
     time. Aggregate policy weight across the liquid set must be
     ``> 0`` — otherwise the renormalisation
     ``w_j / Σ w_L`` is ``0/0``.
  4. **Module location: ``allocation/liquidity_overlay.py``**
     (generic over liquidity tags; not PE-specific).
  5. **Diagnostics**: per-illiquid-bucket policy weight / current
     weight / drift; aggregate ``max_abs_illiquid_drift_pct`` and
     ``sum_abs_illiquid_drift_pct``;
     ``clipped_to_zero_liquid_count`` (analog to STAIRS's
     ``clipped_quarters``).
  6. **Internal-only opt-out: ``base.rebalance.illiquid_overlay:
     bool = True``.** Default-on production behavior; ``False``
     reserved for regression-anchor tests capturing pre-L8
     PE-tradable behavior. Not advertised in user docs.
  7. **Comprehensive test list**: PE/illiquid trades zero,
     liquid renormalisation hand-worked, multi-sleeve illiquid
     fixture, ``liquid_nav<0`` failure, ``liquid_nav==0``
     behavior, empty-liquid-set validation, internal opt-out
     reproduces pre-L8 ledger, all §5.1 invariants preserved.
  8. **Pre-L8 calibration / probe artifacts** that aren't
     regenerated carry a "pre-L8" header tag so future readers
     don't compare values across the L8 cutover unawares.
* **What this is not.** No secondary-market PE sales, no PE
  purchase path outside committed-fund calls, no commitment
  optimiser, no STAIRS changes, no transaction-cost model for PE
  secondaries, no liquidity-stress auto-liquidation, no
  allocator-objective reformulation. Phase 8 only changes the
  rebalance execution target — every other surface is untouched.
* **New invariant under Phase 8.** For any bucket tagged
  ``illiquid`` in ``cma.liquidity``, no ``rebalance`` rows may
  exist in the validated ledger. This becomes the L8 load-bearing
  structural invariant.
* **L8 status.** Will flip to
  ``[RESOLVED 2026-05-02, Phase 8]`` on implementation. PE
  exposure changes only through commitments → calls →
  distributions → NAV marks (the real-world mechanism). PE drift
  away from strategic policy is expected, tolerated, and surfaced
  in the report.
* **Why default-on, not opt-in.** This is a correctness fix, not
  an experimental feature. The pre-L8 default behavior produces
  unrealistic PE rebalancing every quarter; keeping it as the
  default would mean the production model continues to lie about
  PE liquidity by default. Future PE-realism layers (manager
  enrichment, recommitment optimiser, commitment-stress, eventual
  Monte Carlo STAIRS) build on the correct illiquidity foundation.
  The internal-only opt-out preserves a regression anchor without
  giving users an "easy" way to revert to the wrong behavior.
* **Impact on outputs.** Design only — no code changes in this
  commit. When Phase 8 ships, default-config ledger output
  **changes** (rebalance rows for ``pe_*`` go to zero; liquid
  rebalance rows shift to renormalised execution targets). Every
  §5.1 invariant continues to hold; no ledger schema change.
* **Backward-compatible.** Yes (design only).

### 2026-05-02 — Phase 8 implementation: illiquidity overlay (resolves L8)

* **What.** Implements the §Phase 8 design locked one commit prior.
  Inserts an illiquidity overlay between the cost-aware allocator
  target (Phase 4b step 6.5) and the implementation rebalance call
  (step 7). One cohesive commit because schema, function,
  orchestrator wiring, report rendering, and tests only make sense
  together. Five surfaces:
  1. **Schema** (`io/schemas.py`).
     ``base.rebalance.illiquid_overlay: bool = True`` — default-on
     as correctness fix; ``False`` reserved for internal regression-
     anchor tests, not advertised in user docs.
  2. **Cross-config validation** (`io/validation.py`). When the
     overlay is on (default), four checks: ``cma.liquidity`` covers
     every allocation bucket; every ``pe_*`` bucket is tagged
     ``illiquid``; the liquid set (``liquid`` ∪ ``semi_liquid``)
     is non-empty; aggregate liquid policy weight ``> 0``. All
     four fail loudly with precise diagnostics.
  3. **Overlay function** (new
     `allocation/liquidity_overlay.py`). Pure function of
     ``(policy_weights, current_dollars, liquidity)`` returning
     ``(execution_weights, LiquidityOverlayDiagnostics)``. Locks
     illiquid buckets at current; renormalises liquid policy
     weights over the residual liquid NAV. Edge cases:
     ``liquid_nav < 0`` raises with per-bucket breakdown;
     ``liquid_nav == 0`` raises unless every liquid bucket already
     has zero current dollars. **No PSD repair, no clipping, no
     silent recovery.** Generic over CMA liquidity tags — not PE-
     specific.
  4. **Orchestrator wiring** (`integration/orchestrator.py`).
     Step 6.6 inserted between target_at and rebalance:
     ``target_weights, overlay_diag =
       apply_liquidity_overlay(target_weights, current_dollars,
       cma.liquidity)``. **Bit-exact illiquid pin in step 7**:
     ``target_nav[b] = running_nav[b]`` for every illiquid bucket,
     so the load-bearing invariant ("no rebalance rows on illiquid
     buckets") holds bit-perfectly without FP-reconstruction noise.
     Per-quarter diagnostics threaded back to the report.
  5. **Report rendering** (`integration/report.py`). New
     ``## Illiquidity overlay`` section: per-bucket worst-quarter
     drift table (policy / worst current / drift / quarter); max
     |drift| across all illiquid buckets × quarters; mean
     Σ|drift| per quarter; total liquid-bucket clipped-to-zero
     count; explanatory note that PE exposure changes only through
     calls / distributions / marks.
* **L8 invariant.** New: for any bucket tagged ``illiquid`` in
  ``cma.liquidity``, no ``rebalance`` rows exist in the validated
  ledger. Pinned bit-perfect by ``test_default_on_run_has_zero_pe_rebalance_rows``.
* **L8 status.** Flipped to
  ``[RESOLVED 2026-05-02, Phase 8]``. Original Phase 1 text
  retained for audit trail.
* **Tests added (15).** ``tests/test_liquidity_overlay.py``:
  hand-worked anchor (cash 4.33% / bond 17.33% / equity 43.33% /
  PE 35.00% from the design example); PE pinned at current when
  below target; sum-to-1 across parameter sweep; multi-sleeve
  illiquid (pe_buyout + pe_venture) renormalisation; ``liquid_nav
  < 0`` failure; ``liquid_nav == 0`` zero-current allowed; nonzero
  fails; cross-config missing-liquidity / pe-tagged-liquid /
  overlay-off-skips checks (3); end-to-end default-on zero PE
  rebalance rows; PE call/distribution pairing intact under
  overlay; internal opt-out reproduces pre-L8 PE-tradable
  behavior; report section renders; diagnostics dataclass shape.
* **Why.** Phase 5 introduced CMA liquidity tags as diagnostic
  metadata; the rebalancer continued to trade PE freely because
  the tag wasn't an execution input. Phase 8 promotes the tag to
  execution authority — the load-bearing correctness fix. PE
  exposure now changes only through the real-world mechanism.
* **Numerical-anchor accounting.** No existing test required
  re-anchoring. Every test from Phase 4 / 5 / 6 / 7 continued to
  pass under L8 default-on:
  - Invariant tests (`test_base_scenario_e2e`,
    `test_drawdown_scenario_passes_invariants`, OWL tests, the
    cvxportfolio integration test, the crisis_correlation test,
    the STAIRS adapter tests): all check structural / inequality
    properties, not specific NAV values; PE drift doesn't violate
    any §5.1 invariant.
  - Hash-determinism tests
    (`test_input_hashes_are_deterministic_run_ids_are_unique`,
    `test_scenarios_produce_distinct_hash_signatures`,
    `test_scenario_reproducibility`): unaffected; the
    ``illiquid_overlay`` field is in ``config_hash`` but its
    default value is constant across all in-tree configs.
  - PE call/distribution pairing test continues to pass: the
    overlay is upstream of cost emission, not of PE-flow
    emission.
  - STAIRS engine parity at zero coupling
    (`test_stairs_engine_at_parity_yields_byte_stable_orchestrator_run`):
    the test compares pre-rebalance PE-flow rows
    (`pe_call`, `pe_distribution`, `pe_nav_mark`) only, which the
    overlay never touches.
* **Impact on outputs.** Default-config ledger output **changes**:
  ``pe_*`` rebalance rows are now zero (was: non-zero rebalance
  rows quarterly to bring PE back to 25% policy). Liquid bucket
  rebalance rows shift accordingly (cash / public_bond /
  public_equity now renormalise over the post-overlay residual).
  **Every §5.1 ledger invariant continues to hold**; no ledger
  schema change. The pre-L8 PE-tradable behavior remains
  reachable only through an internal-only
  ``base.rebalance.illiquid_overlay: false`` flag.
* **Pre-L8 calibration / probe artifacts.** The 2026-05-02 λ
  calibration sweep (`docs/sweep_lambda_calibration_2026_05_02.md`)
  was run pre-L8 and is now stale. Future readers comparing values
  across the L8 cutover should regenerate the sweep — values shift
  because the underlying rebalance dynamics changed (PE drifts
  rather than tracking 25% target). Marked as a follow-up.
* **191 tests pass** (was 176). 15 new; zero regressions; zero
  re-anchored.
* **Backward-compatible.** Default behavior **changes** as the
  correctness fix; the schema field is additive (default ``True``
  preserves spirit of "production behavior is correct"). Any
  out-of-tree config that was relying on PE rebalancing under the
  default needs to either accept the corrected behavior or set
  ``base.rebalance.illiquid_overlay: false`` — which is loud about
  its non-default state via ``config_hash``.

### 2026-05-02 — Phase 9 design locked (manager/fund metadata enrichment, pre-implementation)

* **What.** Lock the §Phase 9 design ahead of implementation.
  Enriches PE pacing inputs with manager and fund metadata so
  client-realistic questions become answerable in the report
  (commitments / unfunded / calls / distributions / NAV by manager;
  vintage and manager concentration). Strictly a labeling +
  diagnostics layer; **no PE math change**, **no ledger schema
  change**, **no allocator change**.
* **Six confirmed decisions.**
  1. ``FundConfig`` gains optional ``manager``, ``fund_id``,
     ``strategy``, ``fee_model``, ``status``. All-or-nothing
     adoption is **not** required — partial use aggregates unset
     under ``"(unknown)"``.
  2. ``fee_model`` stored as metadata; **not consumed by
     projection math**. Future fee-economics phase (Phase 10+) may
     evolve the schema and consume it; metadata-only in v1.
  3. ``status: "exited"`` is **excluded** from forward projections
     and forward-flow diagnostics.
  4. ``PROJECTION_COLUMNS`` byte-stable — metadata joined at report
     time, not embedded in adapter output. Phase 7 STAIRS parity
     contract preserved.
  5. Ledger ``source`` unchanged (``pacing:<fund_name>``). Manager
     identity does **not** enter the ledger.
  6. ``(unknown)`` aggregation when ``manager`` partial.
* **Three required tightenings (per audit).**
  1. **``FundConfig.name`` must be globally unique.** Because the
     ledger source remains ``pacing:<fund_name>``, two funds with
     the same ``name`` would create ambiguous ledger sources and
     ambiguous metadata joins. ``(manager, name)`` uniqueness is
     not sufficient; ``name`` alone must be globally unique.
  2. **``fund_id`` is NOT hash-stable across fund renames.**
     ``name`` remains in ``config_hash`` and the ledger ``source``
     field; renaming changes both. ``fund_id`` is a stable
     **external** identifier (client systems, accounting,
     reporting) — not a hash-stability mechanism.
  3. **``status`` semantics table is locked.** ``active`` /
     ``committed`` → included. ``planned`` → projected only if
     vintage falls within horizon. ``exited`` → excluded from
     forward projections and forward-flow diagnostics.
* **Strategy↔sleeve consistency.** When ``strategy`` is set, the
  ``sleeve`` field must match the documented mapping
  (``buyout``→``pe_buyout`` etc.); ``secondary`` is the one
  flexible case (compatible with any ``pe_*`` sleeve). Mismatch
  fails at config validation with both values in the error.
* **What Phase 9 is not.** Not a fee economics change, not a
  recommitment optimizer, not a per-manager STAIRS coupling, not a
  secondary-market sale path, not a STAIRS_MC upgrade, not an L14
  fix, not a new ledger schema, not a historical-reporting layer.
* **L-status.** L1 / L8 / L14 unchanged.
* **Tests planned (15).** Schema (8): each new field's validation
  rules, including the globally-unique ``name`` and ``fund_id``
  rules and the ``strategy↔sleeve`` mapping. Behavior (3):
  ``exited`` skipped, ``planned`` projected per horizon,
  ``fee_model`` stored-not-consumed. Report (3): new section
  rendered only when metadata present, omitted when not, partial
  ``(unknown)`` aggregation. End-to-end (1): default fixture
  byte-identical to pre-Phase-9.
* **Impact on outputs.** Design only — none. When Phase 9 ships,
  shipped-fixture runs (no Phase 9 metadata in the fixture) are
  byte-identical to pre-Phase-9. The new ``## PE program structure``
  section appears only when at least one fund carries a Phase 9
  field.
* **Backward-compatible.** Yes (design only).

### 2026-05-02 — Phase 9 implementation: manager / fund metadata enrichment

* **What.** Implements the §Phase 9 design locked one commit prior.
  Five surfaces:
  1. **Schema** (`io/schemas.py`). New ``_FeeModelConfig`` (3
     bounded fields). ``FundConfig`` extended with optional
     ``manager`` / ``fund_id`` / ``strategy`` (Literal) /
     ``fee_model`` / ``status`` (default ``"active"``). New
     ``_STRATEGY_TO_SLEEVE`` mapping. Per-fund ``model_validator``
     enforces ``strategy ↔ sleeve`` consistency (with
     ``"secondary"`` flexible across pe_* sleeves).
  2. **Cross-fund validation** (PEPacingConfig.model_validator).
     Globally-unique ``FundConfig.name`` (load-bearing — ledger
     source remains ``pacing:<fund_name>``); globally-unique
     ``fund_id`` when set on any fund; ``(manager, name)``
     uniqueness when manager set (defence-in-depth).
  3. **Orchestrator** (`integration/orchestrator.py`). Filters
     ``status="exited"`` funds out of the pacing config before
     adapter dispatch via
     ``pacing.model_copy(update={"funds": [...]})``. TA and STAIRS
     adapters are unchanged. ``PROJECTION_COLUMNS`` byte-stable
     (Phase 7 contract preserved).
  4. **Report** (`integration/report.py`). New
     ``## PE program structure`` section gated on
     ``has_phase9_metadata`` (any fund with manager / fund_id /
     strategy / fee_model / non-default status). Six aggregations:
     commitment summary (manager × sleeve), unfunded by manager,
     cumulative calls / distributions by manager, vintage
     concentration, manager concentration (top 3), NAV by manager
     (end of horizon). Manager attribution computed by joining
     ledger PE-flow rows (``source = "pacing:<fund_name>"``) back
     to ``cfg.pe_pacing.funds`` — manager identity does not enter
     the ledger.
  5. **Tests** (`tests/test_pe_metadata.py`, 20 cases).
* **Locked semantics enforced.**
  - ``FundConfig.name`` globally unique (test: duplicate name
    fails loudly).
  - ``fund_id`` globally unique when set; partial population (some
    funds set, others not) is allowed.
  - ``status="exited"`` skipped in projection (test: zero ledger
    rows for the exited fund's source).
  - ``status="planned"`` projected when vintage is in horizon
    (test: planned fund produces flows).
  - ``fee_model`` stored but **not consumed** by projection math
    (test: TA projections byte-identical with vs without
    ``fee_model`` set).
  - Default fixture (no Phase 9 metadata) produces byte-identical
    ``ledger.parquet`` and ``manifest.json`` (test:
    ``test_default_fixture_run_id_unchanged_under_phase9``).
* **L-status.** L1 / L8 / L14 unchanged. Phase 9 adds no math.
* **Impact on outputs.** **Zero on shipped configs.** The new
  section is gated on Phase 9 metadata being present somewhere; the
  shipped fixture has no Phase 9 fields, so ``report.md`` is
  byte-identical to pre-Phase-9. Out-of-tree configs that add
  manager / fund_id / etc. start seeing the new section
  automatically.
* **211 tests pass** (was 191). 20 new; zero regressions; zero
  re-anchored.
* **Backward-compatible.** Yes. All Phase 9 fields are optional
  (or default to ``"active"`` for ``status``); existing
  ``pe_pacing.yaml`` files continue to validate. The globally-unique
  ``name`` rule is a new constraint but the shipped fixture (one
  fund) trivially satisfies it; out-of-tree configs with duplicate
  fund names will fail loudly — that is the intended catch
  (duplicate names create ambiguous ledger sources).

### 2026-05-02 — Phase 10 design locked (L14 transaction cost diagnostics, pre-implementation)

* **What.** Lock the §Phase 10 design ahead of implementation.
  Resolves L14 (only linear transaction cost is modeled) by
  clarifying scope and adding diagnostic visibility — not by
  introducing a richer cost model. Documentation + diagnostics
  only.
* **Locked decisions.**
  - Resolution shape: documentation + light diagnostics (not a
    math change; not a schema change; not a config knob).
  - L14 flips to ``[PARTIALLY RESOLVED 2026-xx-xx, Phase 10]``
    with engine-conditional + scope-conditional wording (PE-
    secondary closed by L8; public-market linear-bps documented
    as scale-appropriate; richer cost regimes explicitly
    deferred).
  - New ``## Transaction cost summary`` report section, rendered
    only when ``transaction_cost`` rows exist (i.e., non-stub
    implementation engine with ``bps_per_trade > 0``). Gated on
    that condition; under stub the section is omitted.
  - Section position: after the existing "Cost-aware allocator
    calibration (advisory)" section so cost-related diagnostics
    cluster.
  - Four metrics: cumulative ``transaction_cost``;
    cumulative-as-%-of-initial-NAV; liquid rebalance turnover
    total + per-quarter mean; max single-quarter liquid turnover
    as % of NAV.
  - Liquid-only turnover — uses ``cma.liquidity`` to filter the
    rebalance rows. Makes the L14 / L8 boundary explicit in the
    report.
  - Three-message advisory line, priority order:
      1. ``> 25%`` max quarterly liquid turnover → "may underprice
         market impact"
      2. ``> 1%`` cumulative cost / initial NAV → "cost is
         material; consider richer model"
      3. otherwise → "covers this regime."
  - Thresholds (``1.0%``, ``25.0%``) are module-level constants
    in the renderer with documenting comments. **No config knob,
    no user-tunable parameter.**
* **Required tightening (per audit, locked verbatim).**
  > **The advisory thresholds are diagnostic heuristics, not
  > validation failures. Crossing them does not invalidate the
  > run; it flags interpretation risk.**
  This rule is load-bearing: it must appear in both
  MODEL_DOCUMENTATION.md and the report's advisory section. The
  project has many hard validation gates (per-cell bounds, PSD,
  sum-to-one, symmetry); Phase 10 thresholds are explicitly
  **not** in that category.
* **What this is not.** No quadratic / market-impact term; no
  per-bucket bps; no asymmetric buy/sell; no PE secondary cost
  regime; no fee-economics implementation; no liquidity haircut
  model; no Monte Carlo upgrade; no config knob; no validation
  gate.
* **Tests planned (5).** Section omitted under stub; section
  renders under cvxportfolio + non-zero bps with all metrics +
  advisory + the "diagnostic heuristics" note; all-clear advisory
  at default-fixture-low-turnover; threshold-trigger anchor at
  constructed high-turnover scenario; no ledger schema change.
* **Why now.** Phase 8 closed L14's PE-secondary concern; Phase 9
  established the manager / fund attribution layer. With those in
  place, transaction-cost interpretation is ready for the next
  cleanup. Doing it as documentation + diagnostics first (rather
  than a cost-math overhaul) respects the "no math change without
  evidence" discipline that has run through 4b / 5 / 6 / 7 / 8 /
  9.
* **Impact on outputs.** Design only — none. When Phase 10 ships,
  shipped-fixture runs (``implementation.engine="stub"``,
  zero-bps) are byte-identical: the section is gated off. Runs
  with ``cvxportfolio`` + non-zero bps gain the new section; the
  ledger / manifest are byte-identical.
* **Backward-compatible.** Yes (design only).

### 2026-05-02 — Phase 10 implementation: L14 transaction cost diagnostics

* **What.** Implements the §Phase 10 design locked one commit prior.
  Documentation + light diagnostics; no math change, no schema
  change, no config knob. Two surfaces:
  1. **Report** (`integration/report.py`). New ``## Transaction
     cost summary`` section, rendered after the existing "Cost-aware
     allocator calibration (advisory)" section, gated on
     ``transaction_cost`` rows existing in the ledger. Four metrics
     (cumulative ``transaction_cost``, cumulative as % of initial
     NAV, liquid rebalance turnover total + per-quarter mean, max
     single-quarter liquid turnover as % of NAV) plus a 3-message
     priority advisory plus the load-bearing tightening note
     (``"diagnostic heuristics, not validation failures"``).
     Liquid set is determined from ``cma.liquidity``; under L8
     overlay-on the illiquid buckets contribute zero turnover by
     construction, but the diagnostic excludes them by definition
     to make the L14 / L8 boundary visible. Threshold constants
     (``_TX_COST_HEURISTIC_PCT_OF_INITIAL_NAV = 0.01``;
     ``_TX_QUARTERLY_LIQUID_TURNOVER_HEURISTIC_PCT = 0.25``) live
     at module level with documenting comments.
  2. **L14 entry flip** (`MODEL_DOCUMENTATION.md`). Marked
     ``[PARTIALLY RESOLVED 2026-05-02, Phase 10]`` with engine-
     conditional + scope-conditional wording. PE-secondary concern
     closed by L8 (Phase 8); public-market linear-bps documented
     as scale-appropriate; richer cost regimes explicitly deferred.
     Original Phase 3b text retained for audit trail.
* **Required tightening** (per audit, locked verbatim into both
  doc and report):
  > The advisory thresholds are diagnostic heuristics, not
  > validation failures. Crossing them does not invalidate the
  > run; it flags interpretation risk.
  Verified by ``test_section_renders_under_cvxportfolio_with_bps``
  which asserts the verbatim phrase is present in the rendered
  ``report.md``.
* **Tests added (5)** in ``tests/test_transaction_cost_summary.py``:
  - ``test_section_omitted_under_stub_engine`` — default fixture
    runs at ``implementation.engine="stub"``; no
    ``transaction_cost`` rows exist; section is omitted.
  - ``test_section_renders_under_cvxportfolio_with_bps`` —
    ``cvxportfolio`` + 5 bps produces the section with all four
    metrics + an advisory + the verbatim
    "diagnostic heuristics, not validation failures" tightening.
  - ``test_all_clear_advisory_at_low_turnover`` — default fixture
    + cvxportfolio + 5 bps stays under both thresholds → advisory
    says "covers this regime."
  - ``test_high_turnover_triggers_market_impact_advisory`` —
    constructed config with policy weights diverging sharply from
    fixture initial NAV (cash 0.40 / bond 0.10 / equity 0.25 /
    pe_buyout 0.25 vs. initial 15/20/65/0%) forces > 25% max
    single-quarter liquid turnover → advisory contains
    "may underprice market impact."
  - ``test_transaction_cost_row_schema_unchanged`` — Phase 10
    adds no columns to the ledger; ``transaction_cost`` rows still
    match the Phase 3b schema (``bucket=cash``, ``amount_usd ≤ 0``,
    ``source="impl:cvxportfolio"``).
* **L14 status.** Flipped to
  ``[PARTIALLY RESOLVED 2026-05-02, Phase 10]``. The original
  L8-pairing note is updated: L8 closure (Phase 8) resolved the
  specific concern about PE rebalancing producing fictional bps
  cost; future per-bucket / asymmetric / quadratic cost work is a
  clean independent phase, no longer paired against L8.
* **Impact on outputs.** **Zero on shipped configs.** Default
  fixture runs at ``implementation.engine="stub"`` with
  ``bps_per_trade=0``; ``transaction_cost`` rows do not exist;
  the new section is gated off; ``ledger.parquet`` and
  ``manifest.json`` byte-identical to pre-Phase-10. Runs with
  ``cvxportfolio`` + non-zero bps gain the new ``## Transaction
  cost summary`` section; the ledger and manifest are unchanged.
* **216 tests pass** (was 211). 5 new; zero regressions; zero
  re-anchored.
* **Backward-compatible.** Yes. The schema is unchanged; the
  threshold constants are renderer-internal (no config knob); the
  advisory text is informational. No breaking changes for any
  consumer.
