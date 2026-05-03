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

## Use-case context — standing modeling principle (Gen3–Gen5 SFO)

> **Authoritative scope:** `PROJECT_SCOPE.md` at the repo root is the
> authoritative scope statement for this project (codename:
> *Wake Robin Liquidity Architecture*). When this section and
> `PROJECT_SCOPE.md` disagree on what the project is *for* or what it
> must eventually cover, `PROJECT_SCOPE.md` wins; this file remains
> authoritative for *how the model is built and behaves*. Reference
> architecture diagram tracked at `docs/wake_robin_liquidity_architecture.png`
> (`.svg` for the vector source).
>
> This model is for a **Gen3–Gen5 single-family office** balance sheet,
> typically holding **large illiquid private real estate, operating-
> company interests, development assets, and land** alongside public
> portfolios. That use-case context is **load-bearing for every
> modeling decision** — assumptions appropriate for an endowment or a
> mass-affluent retirement plan often fail here.

**Standing principle — to be applied in every future phase of model
work:**

```
NAV is not liquidity.
Appraisal value is not spending capacity.
Development / land value is not distributable income.
Opco value is not automatically portfolio liquidity.
```

These four lines must be honored in every spending, liquidity, and
real-estate phase that follows. When a phase appears to treat total
NAV as a proxy for spendable resources (or appraisal value as cash, or
opco value as liquid), the design must explicitly call out that
assumption and either justify it for that phase's narrow scope or
queue a separate phase to address it.

**Concretely, future phases should distinguish — not conflate:**

* **Total NAV** — sum of every modeled asset, including illiquid
  appraisal carry. Currently the only NAV concept the model has.
* **Liquid NAV** — public sleeves + cash; immediately tradable.
  L8 (Phase 8) made this distinction load-bearing for the rebalancer
  via the illiquidity overlay.
* **Income-producing NAV** — assets generating distributable yield
  (dividends, rents, distributions). Real estate at appraisal but
  zero current income is *not* income-producing for spending purposes.
* **Distributable income** — actual cash flowing to the household
  this period. Distinct from NAV growth or appraisal step-ups.
* **Locked / illiquid appraisal NAV** — private real estate, opco
  equity, development assets, land. May appreciate but cannot fund
  spending without a liquidity event.
* **Spendable resources** — what the household can actually spend
  this period without forced sales of illiquid assets. Currently
  *not* a model concept; see L19.

**Where the model is today on this principle:**

* **L8 (Phase 8 / RESOLVED)** — rebalancer respects illiquidity:
  PE buckets are non-tradable in rebalance; only the liquid NAV
  residual rebalances. This is the first place the model honors
  "NAV is not liquidity" structurally.
* **L16 (Phase 11 / RESOLVED)** — Owl is scale-aware when the
  optional ``GuardrailConfig.absolute_min_annual_usd`` /
  ``absolute_max_annual_usd`` clamps are set. Trajectories
  diverge between same-rate-different-NAV households at the
  clamp boundary. Default-off behavior remains the original
  rate-based Guyton-Klinger semantics, which is scale-invariant
  by design. **L16 closure does NOT address spending-base
  realism — Owl still measures rate against total NAV.**
* **L19 (PARTIALLY RESOLVED, Phase 12 + 12.5 + 13 + 14)** —
  spending-rule denominator infrastructure complete (Phase 12 +
  12.5); config-driven producer (Phase 13) AND workbook-driven
  producer + ingestor (Phase 14) shipped.
  Phase 12 (commit `92c327d`) shipped the base-side: four
  configurable modes (`total_nav` default, `liquid_nav`,
  `liquid_plus_income_producing_nav`, `custom_policy`). Phase 12.5
  shipped the flow-side: new ``distribution_inflow`` ledger flow
  type + the ``distributable_income`` spending base reading the
  trailing realized sum with bootstrap fallback. Both initial-rate
  and current-rate denominators are replaced symmetrically;
  rate-band geometry preserved. **Production distributable-income
  realism remains producer-dependent (Phase 13 RE+OpCo pipeline +
  Phase 14 cash-flow / entity ingestion).** Phase 12.5 does NOT
  determine legal / tax / entity-governance distributability; it
  consumes rows already classified upstream as
  family-office-distributable. L19 flips to RESOLVED only after
  the producer layer exists.

A future contributor reading any spending- or liquidity-related
phase should treat this section as governing context — not the
"introduction" they can skim past.

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
| L16 | Owl is scale-invariant in initial NAV | **RESOLVED** | Phase 11 (optional absolute-dollar clamps in `GuardrailConfig`) |
| L17 | Cross-engine metric comparability is not meaningful | **ACCEPTED LIMITATION** | Phase 10 consolidation — interpretation problem, not architecture |
| L18 | Owl misreads inflation shock as "headroom" and raises spending | **RESOLVED** | Phase 4a |
| L19 | Spending base realism for illiquid SFO balance sheets | **PARTIALLY RESOLVED** | Phase 12 + 12.5 + 13 + 14 — full ingestion stack shipped (consumer + producer + workbook ingestor); RESOLVED only after a clean local validation pass against the real workbook + narrowed wording (legal/tax/governance distributability remains out of scope) |

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

> **Scope context.** The sequencing below is the
> implementation-level view. The project-level roadmap — including
> the unbuilt entity / cash-flow / RE+OpCo / liquidity-tier layers
> that surround these limitation entries — lives in
> `PROJECT_SCOPE.md` §6 and is authoritative when the two disagree.

After Phase 11 closed L16, three limitation entries are genuinely
open. Sequenced against the broader scope in `PROJECT_SCOPE.md`,
the order is:

1. **L19 — Spending base realism for illiquid SFO balance sheets.**
   The next-priority modeling fix and the entry point into the
   unbuilt SFO layers (`PROJECT_SCOPE.md` §3.1, §3.3, §3.5, §3.6).
   Owl currently measures withdrawal rate against **total NAV**.
   For a Gen3–Gen5 SFO with large private real estate, opco
   equity, development assets, and land, total NAV may materially
   overstate spendable capacity. **Phase 11 / L16 closure does NOT
   address this** — Phase 11 is scale-invariance only. A future
   phase should introduce a spendable-resource / liquidity-
   adjusted NAV base for spending rules. Honors the standing
   "NAV is not liquidity" principle (see top-of-doc §Use-case
   context). Most directly client-relevant of the open backlog.

2. **Cash-flow ingestion + entity schema (post-L19).** Validated
   pydantic v2 schemas for the entity chart and the per-entity
   cash-flow forecast; loaders for the external Cashflow Modeling
   workbook (read-only; see `PROJECT_SCOPE.md` §5.1); reconciliation
   tests against the workbook's family aggregate within a documented
   tolerance. **Not** a `Limitations` entry in this file because the
   gap is structural (whole layers not yet built), not a defect of an
   existing layer.

3. **Position ingestion + RE / OpCo pipeline (post cash-flow).**
   Loader for the Investment Summary workbook (read-only; see
   `PROJECT_SCOPE.md` §5.2), per-position metadata hydration,
   then RE capital-need schedules, NOI for stabilized RE, OpCo
   distribution policy. Unblocks an honest tier-by-tier liquidity
   layer (`PROJECT_SCOPE.md` §3.6 full build).

4. **L2 — Returns are NAV-dependent, not regime-dependent.**
   Scenario perturbations change *levels* but not *dynamics* —
   no autocorrelation, no volatility clustering, no drawdown
   contagion. Resolving requires a Monte Carlo path generator
   plus a stochastic regime layer over CMA. **Structurally
   unblocked post-Phase 11**, but explicitly deferred until the
   deterministic SFO layers above are honest. Sequencing Monte
   Carlo before those layers would dress up unrealistic
   deterministic assumptions in stochastic clothing.

5. **L5 — `source` as PE-leg pairing key is fragile.** Currently
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
* Set up the next phase as a model-weakness fix (L16 — now
  RESOLVED in Phase 11), not yet another structural realism
  layer. Post-Phase-11, the open backlog is L19 / L2 / L5.

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

### L16 — Owl is scale-invariant in initial NAV — [RESOLVED 2026-05-02, Phase 11]

> **Status: RESOLVED in Phase 11.** ``GuardrailConfig`` gained
> optional ``absolute_min_annual_usd`` / ``absolute_max_annual_usd``
> fields. Default ``None`` preserves byte-stable behavior (Owl
> remains scale-invariant under proportional setup, which is the
> correct default for users who explicitly want rate-based
> Guyton-Klinger semantics). When set, the absolute clamps break
> the rate-based scale-invariance by introducing dollar-denominated
> decisions in the trigger output. Trajectories diverge between
> same-rate-different-NAV households at the clamp boundary — pinned
> by ``test_owl_diverges_under_absolute_floor``.
>
> Phase 11 also added the regression test that the original L16
> entry referenced but didn't actually ship:
> ``test_owl_path_is_scale_invariant_in_initial_nav`` in
> ``tests/test_owl_scale_invariance.py``. The doc reference is now
> accurate.
>
> A new ``## Owl scale-sensitivity (advisory)`` report section
> classifies each Owl run as scale-aware (clamps configured) or
> scale-invariant (no clamps configured), and surfaces clamp
> activation counts.
>
> **Important: Phase 11 / L16 closure does NOT address L19.** Owl
> still measures rate against **total NAV**. For a Gen3-Gen5 SFO
> with large illiquid private real estate, opco equity, development
> assets, and land, total NAV may overstate spendable capacity.
> L19 (OPEN) tracks the spending-base realism concern; the
> top-of-doc §Use-case context section codifies the standing
> "NAV is not liquidity" principle that future spending- and
> liquidity-related phases must honor.
>
> The original Phase 3c text follows for audit-trail purposes.

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

### L19 — Spending base realism for illiquid SFO balance sheets — [PARTIALLY RESOLVED 2026-05-03, Phase 12 + Phase 12.5 + Phase 13 + Phase 14]

> **Status: PARTIALLY RESOLVED in Phase 12 + Phase 12.5 + Phase 13
> + Phase 14.** Full ingestion stack shipped: consumer-side base
> infrastructure (Phase 12 + 12.5) + producer-side seat (Phase 13
> config-driven, Phase 14 workbook-driven). The model can now
> ingest ``Cashflow Modeling v7.xlsx`` as a read-only integration
> target, normalize entity + cash-flow tables, and bridge
> qualifying lines into ``distribution_inflow`` rows that Owl's
> ``distributable_income`` spending base consumes. **L19 flips
> to RESOLVED only after a clean local validation pass against the
> real workbook**, with wording narrowed to "RESOLVED for modeled
> distributable-income ingestion; legal / tax / entity-governance
> distributability remains out of scope."
>
> **Producer-side (Phase 13):** new ``DistributionProducer`` ABC
> with ``ConfigDrivenProducer`` adapter; ``DistributionProducerConfig``
> Pydantic schema with per-domain hard validators (URL-safe ids,
> domain × recurrence sanity). The orchestrator wires the producer
> into the per-quarter loop; emissions land as ``distribution_inflow``
> rows on cash. Restricted entries filter at emit time. Recurrence
> and confidence captured as producer-side diagnostics; ledger row
> schema unchanged. New ``## Distribution producer (advisory)``
> report section composes with the Phase 12.5 spending-base
> advisory. **Phase 13 does NOT model inter-entity cash-movement
> mechanics** (declarations, approvals, withholding, distribution
> waterfalls, trust-payout calendars, banking settlements) —
> Phase 13 reviewer tightening 1; that work sits at Phase 14+.
>
> **Base-side (Phase 12, commit ``92c327d``):** Owl's withdrawal-
> rate denominator is configurable via ``GuardrailConfig.spending_base``
> across four NAV-side modes — `total_nav` (default, byte-stable
> with Phase 11), `liquid_nav`, `liquid_plus_income_producing_nav`,
> and `custom_policy` — with both the initial-rate and current-rate
> denominators replaced symmetrically.
>
> **Flow-side (Phase 12.5):** new ``distribution_inflow`` ledger
> flow type + ``distributable_income`` spending base. Sums realized
> distributable income over a trailing window
> (``distribution_window_quarters``, default 4q = TTM) with a
> static ``bootstrap_distributable_income_usd`` fallback for
> q0 / insufficient-history. The runtime guard fails loud when the
> realized window has elapsed but the trailing sum is zero. The
> mode is **infrastructure-only**: production runs require Phase
> 13 (RE+OpCo pipeline) and Phase 14 (cash-flow / entity
> ingestion) producers to emit ``distribution_inflow`` rows.
>
> **Phase 12.5 does NOT determine legal / tax / entity-governance
> distributability** — it consumes rows already classified upstream
> by a producer as family-office-distributable (CRUT mandates,
> gift-trust restrictions, OpCo retention policy, RE-LLC
> distribution waterfalls, federal / state tax withholding all
> sit at the producer layer, not here).
>
> **Recommended source convention** (documented but not enforced):
> ``source = "distribution:<domain>:<entity_or_asset_id>"`` with
> ``<domain> ∈ {real_estate, opco, portfolio, entity}``. Producers
> built in Phase 13/14 can emit conformant rows from day one.
>
> **Recurring vs one-time** classification is deferred to the
> producer layer. Phase 12.5 treats every ``distribution_inflow``
> row equally; the report renders a permanent CAVEAT line so a
> high trailing-income base is not silently mistaken for stable
> recurring yield.
>
> **L19 flips to RESOLVED** only after the producer layer exists
> and the SFO can run end-to-end on real household income data —
> not on the existence of a flow type and a helper.

* **Model behavior.** ``OwlRule.quarterly_outflow_at`` reads
  ``ledger.end_nav_through(prior_q).sum()`` — i.e., **total modeled
  NAV across every bucket**, including illiquid private real estate,
  PE / opco equity, and any future development / land assets. The
  withdrawal-rate trigger is computed against this total. There is
  currently no model concept of *spendable resources*,
  *income-producing NAV*, or *distributable income* — see top-of-doc
  §Use-case context for the standing distinction.
* **Real-world interpretation.** For a Gen3–Gen5 single-family
  office, a typical balance sheet might look like:
  * 25% public equity / fixed income / cash (liquid)
  * 35% private equity + private credit (mostly locked, distributions
    are episodic)
  * 30% private real estate (high appraisal carry, low current
    income unless rented)
  * 10% operating-company equity (worth $X on paper but not
    spendable until a sale or dividend)
  Total NAV may meaningfully overstate what the household can
  *actually spend* this period without forced sales. Owl computing
  ``annual_spend / total_nav`` as the withdrawal rate trigger
  understates the true rate against spendable resources, sometimes
  by a factor of 2–3×. A 4% rate against total NAV may be a
  10–12% rate against liquid + income-producing NAV — which is the
  rate the household actually faces.
* **What a fix would look like.** Define a spendable-resource base
  (e.g., ``liquid_nav + scaled_distributions_run_rate``) and
  optionally let ``GuardrailConfig`` use that base instead of total
  NAV. CMA already carries ``liquidity`` tags (Phase 5);
  ``income_producing`` would be a new tag. Distributable-income
  tracking would require a new ledger flow type or a new diagnostic.
  All non-trivial; scoped explicitly as future work.
* **Pairing.** Tightly coupled to the standing
  "NAV is not liquidity" principle (top-of-doc §Use-case context).
  L8 (Phase 8) honored the principle on the rebalancer side; L19 is
  the spending-side analog. **L19 should land before any work that
  treats Owl output as an authoritative spending policy** — the
  current Owl is scale-aware (post-Phase-11) but not yet
  spending-base-aware.

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

## Phase 11 design (pre-implementation) — L16 Owl scale-invariance

> **One-line goal.** Resolve **L16: Owl is scale-invariant in
> initial NAV** by adding optional **absolute-dollar guardrail
> bands** to ``GuardrailConfig``. Default-off; backward-compatible.
> When set, breaks the rate-based scale-invariance by introducing
> dollar-denominated decisions in the trigger output. **Strictly
> scale-invariance fix; does NOT resolve spending-base realism (see
> L19).** Preserves Phase 4a closed-prior-quarter state-flow
> contract. Preserves deterministic single-pass architecture. No
> Monte Carlo, no regime-dependent returns, no PE schema changes,
> no fee economics, no secondary-sale modelling.

### Required scope tightening — Phase 11 fixes scale-invariance ONLY

> **Phase 11 resolves Owl scale-invariance only. It does NOT
> resolve spending-base realism.**
>
> Owl still measures withdrawal rate against **total modeled NAV**
> (``ledger.end_nav_through(q-1).sum()``) unless a future phase
> introduces a spendable-resource / liquidity-adjusted base. For a
> Gen3–Gen5 SFO with large private real estate, operating-company
> interests, development assets, and land, total NAV may materially
> overstate spendable capacity. **Phase 11 makes Owl scale-aware,
> not spending-base-aware.** The spending-base concern is
> documented as **L19 (OPEN)** and is explicitly deferred.
>
> Future readers should not interpret the Phase 11 ship as "Owl is
> family-office-realistic." It isn't yet. It's scale-aware against
> total NAV; making it spendable-resource-aware is L19.

### Diagnosis

The L16 algebra:

```
initial_rate  = cfg.annual_spend_usd / initial_nav_total
current_rate  = annual_spend / nav_realized
trigger       = current_rate ≷ initial_rate · (1 ± band)
```

Under proportional setup
(``cfg.annual_spend_usd ∝ initial_nav_total``, e.g., always 4%),
both ``current_rate`` and ``initial_rate`` scale identically with
NAV; the band test reduces to a scale-invariant condition on the
*dynamics* — not specifically on the bands. **Any rate-based
trigger inherits scale-invariance from the underlying
proportional dynamics.** The fix must introduce a dollar-
denominated decision somewhere in the trigger path. The minimum-
risk place is to **clamp the trigger output to absolute-dollar
floor / ceiling**, leaving the rate-band logic untouched.

### Schema additions

Two optional fields on ``GuardrailConfig``:

```python
class GuardrailConfig(BaseModel):
    model_config = _STRICT
    upper_band_pct: float = Field(gt=0.0)
    lower_band_pct: float = Field(gt=0.0)
    raise_pct: float = Field(gt=0.0)
    cut_pct: float = Field(gt=0.0, lt=1.0)
    # Phase 11 / L16: optional absolute-dollar guardrail clamps.
    # Default None preserves the existing rate-band-only behavior
    # (which is scale-invariant under proportional setup). When set,
    # break scale-invariance by clamping the trigger output to a
    # dollar floor / ceiling that does NOT scale with initial NAV.
    # **Static**, not inflation-adjusted; users who want inflation-
    # indexed clamps set them externally as a policy choice.
    absolute_min_annual_usd: float | None = Field(default=None, ge=0.0)
    absolute_max_annual_usd: float | None = Field(default=None, gt=0.0)

    @model_validator(mode="after")
    def _absolute_band_bounds_well_formed(self) -> GuardrailConfig:
        if (
            self.absolute_min_annual_usd is not None
            and self.absolute_max_annual_usd is not None
            and self.absolute_min_annual_usd > self.absolute_max_annual_usd
        ):
            raise ValueError(
                f"absolute_min_annual_usd ({self.absolute_min_annual_usd}) > "
                f"absolute_max_annual_usd ({self.absolute_max_annual_usd})"
            )
        return self
```

### OwlRule trigger logic (year-boundary path)

```
1. Inflation-adjust prior annual.
   annual_spend = prior_annual * (1 + inflation_pct)

2. Existing rate-band trigger (UNCHANGED).
   nav_realized = end_nav_through(prior_q).sum()
   if nav_realized > 0:
       initial_rate = annual_spend_usd / initial_nav_total
       current_rate = annual_spend / nav_realized
       if current_rate < initial_rate * (1 - lower_band_pct):
           annual_spend *= 1 + raise_pct
       elif current_rate > initial_rate * (1 + upper_band_pct):
           annual_spend *= 1 - cut_pct

3. Phase 11 / L16: optional absolute-dollar clamps.
   if gr.absolute_min_annual_usd is not None:
       annual_spend = max(annual_spend, gr.absolute_min_annual_usd)
   if gr.absolute_max_annual_usd is not None:
       annual_spend = min(annual_spend, gr.absolute_max_annual_usd)

4. Convert to quarterly + apply existing safety clamp (UNCHANGED).
   quarterly = annual_spend / 4
   return max(cfg.floor_usd, min(cfg.ceiling_usd, quarterly))
```

The order is intentional: rate-band drives in-corridor dynamics
(unchanged); absolute clamp breaks scale-invariance at the
boundary; ``cfg.floor_usd`` / ``cfg.ceiling_usd`` provide the
generic safety backstop (also unchanged). When both
``cfg.floor_usd`` and ``gr.absolute_min_annual_usd`` are set, **the
more restrictive one binds** by composition of `max` / `min` — no
special handling needed.

### Why this is the minimum fix

* **One field pair**, both optional, both with safe defaults
  (``None``).
* **Default-off**: behavior byte-identical to today when fields
  absent.
* **Localized to ``GuardrailConfig`` and ``OwlRule``**: no
  allocator change, no rebalancer change, no ledger schema
  change, no PE math change.
* **Preserves the Phase 4a state-flow contract**: still a pure
  function of ``(closed_through(q-1), end_nav_through(q-1),
  params)``; no new ledger reads, no new state.
* **Preserves the Phase 4 design rules**: single-pass,
  deterministic, no fixed-point, no within-quarter iteration.
* **Engine-conditional resolution**: scale-invariance is closed
  under absolute clamps set; persists under bands-only (which is
  fine — that's the user's policy choice).

### Diagnostic — `## Owl scale-sensitivity (advisory)` in `report.md`

Gated on ``cfg.spending.rule == "owl"`` AND the rule fired during
the run. Section content:

```markdown
## Owl scale-sensitivity (advisory)

- absolute guardrail clamps:
  - absolute_min_annual_usd: $2,000,000   (or "not set")
  - absolute_max_annual_usd: not set       (or "$N")
- clamp activations during run:
  - min-clamp activated: 4 quarters
  - max-clamp activated: 0 quarters
- regime classification:
  - "scale-aware (clamps active)"     OR
  - "scale-invariant (no absolute clamps configured)"

_When neither absolute clamp is set, Owl is scale-invariant under
proportional setup (same initial spend rate × same dynamics → same
spending series at any NAV). Phase 11 introduced the optional
absolute-dollar clamps to break this when the user wants
scale-aware behavior. **Phase 11 fixes scale-invariance only — it
does NOT resolve spending-base realism (L19). Owl still measures
rate against total NAV.** See MODEL_DOCUMENTATION.md §Phase 11 /
L16 + §Use-case context._
```

### Tests planned (8)

Schema (3):

1. ``absolute_min_annual_usd`` non-negative; non-finite fails.
2. ``absolute_max_annual_usd > 0``; non-finite fails.
3. ``absolute_min > absolute_max`` fails at ``model_validator``
   time.

Behavior (4):

4. **Default-off byte-stability**: ``GuardrailConfig`` with no
   absolute fields → Owl trajectory byte-identical to pre-Phase-11.
5. **Scale-invariance regression test (NEW; the test L16 doc
   referenced but didn't actually ship)**: two ``OwlRule``
   instances with $100M and $1B initial NAV, same proportional
   setup, no absolute fields → identical quarterly trajectories.
6. **Scale-divergence under absolute floor**: same two instances
   with ``absolute_min_annual_usd`` set → trajectories diverge
   after the small household hits the floor and the large
   household keeps cutting.
7. **Cut-path floor binding**: cut sequence eventually pins
   ``annual_spend`` at ``absolute_min_annual_usd``; subsequent
   years see ``prior_annual = absolute_min_annual_usd`` (the
   clamped value rides through prior-spend feedback as designed).

End-to-end (1):

8. **Report diagnostic renders** for an Owl-rule run; classifies
   regime correctly; surfaces the L19 caveat verbatim
   (``"does NOT resolve spending-base realism"``).

### What Phase 11 is **not**

Listed explicitly as guardrails for future contributors:

* **Not a spending-base fix.** Owl still measures rate against
  total NAV. L19 is the open ticket for spendable-resource /
  liquidity-adjusted base; Phase 11 is scope-bounded against it.
* **Not a Monte Carlo / stochastic upgrade.**
* **Not a regime-dependent returns layer.**
* **Not a PE schema change.**
* **Not a fee economics change.**
* **Not a secondary-sale model.**
* **Not a power-law band scaling** (the speculative variant where
  bands depend on absolute NAV).
* **Not a reformulation of the rate-band trigger** — the existing
  rate logic is preserved verbatim.
* **Not an allocator / rebalancer / ledger change.**
* **Not an L2 or L5 fix.**

### L16 status under Phase 11

Will flip to ``[RESOLVED 2026-05-02, Phase 11]`` on
implementation, with the resolution wording explicitly noting:

* Optional ``absolute_min_annual_usd`` / ``absolute_max_annual_usd``
  fields break the rate-based scale-invariance when set.
* Default behavior (fields absent) preserves byte-stability.
* **L16 closure does not address L19** — the standing
  "NAV is not liquidity" principle (see top-of-doc) is honored on
  the rebalancer side (L8) and the scale-aware-Owl side (L16)
  but the spending-base side (L19) remains open.

The L16 entry's referenced-but-missing test
(``tests/test_owl_adapter.py::test_owl_path_is_scale_invariant_in_initial_nav``)
is **added** as part of this phase (test #5 above) — the doc
reference becomes accurate.

### Locked design choices

* Optional ``absolute_min_annual_usd`` / ``absolute_max_annual_usd``
  on ``GuardrailConfig``, default ``None``.
* Trigger logic order: rate-band → absolute clamp → existing
  quarterly floor/ceiling.
* Default-off byte-stability.
* Owl-only — flat_real / smoothing have no guardrail concept.
* Static clamps; not inflation-adjusted.
* New advisory section in ``report.md`` gated on Owl rule;
  surfaces the L19 caveat verbatim.
* L16 flips to ``[RESOLVED]`` on implementation.
* L19 stays open; documented as the next spending-side concern.

---

## Phase 12 design (pre-implementation) — L19 spending base realism

> **One-line goal.** Resolve **L19: Spending base realism for
> illiquid SFO balance sheets** by introducing a configurable
> **spending base** that Owl uses in place of total modeled NAV
> when the user opts in. Default-off; backward-compatible.
> Replaces "withdrawal rate against total NAV" with "withdrawal
> rate against spendable resources" for both the *initial* and
> *current* rate sides of the guardrail trigger, so the rate the
> household actually faces drives the band check. **Strictly a
> spending-base fix; does NOT change rate-band logic, absolute
> clamps (Phase 11), or any allocator / rebalancer / ledger
> behavior.** Preserves the Phase 4a closed-prior-quarter state-
> flow contract verbatim. No Monte Carlo, no regime-dependent
> returns, no PE schema changes, no fee economics, no secondary-
> sale modelling, no real-estate / OpCo pipeline.

### Required scope tightening — Phase 12 ships *three* modes only

> Phase 12 ships the spending-base modes that are pure functions
> of CMA tags + NAV-by-bucket on the closed ledger. Modes that
> require a new *flow type* on the ledger (i.e., realized
> distribution inflows, separate from `nav_change`) are explicitly
> **deferred to Phase 12.5** as a follow-on with the explicit
> ledger-schema change.

| Mode | Phase 12 ships? | Why |
| --- | --- | --- |
| `total_nav` | yes (default) | Pure NAV sum; backward-compatible |
| `liquid_nav` | yes | Pure function of CMA `liquidity` tag + NAV-by-bucket |
| `liquid_plus_income_producing_nav` | yes | Adds CMA `income_producing` tag (additive optional field) |
| `custom_policy` | yes | Per-bucket inclusion-weight vector; pure function of CMA tags + NAV |
| `distributable_income` | **no — Phase 12.5** | Requires new ledger flow type for realized distributions |

Phase 12 therefore lands the **base-side** of L19 — the question
of *what NAV are we measuring spending against* — and defers the
**flow-side** (*what cash actually distributed this period*) to a
follow-on phase that will introduce a `distribution_inflow` flow
type with its own state-flow contract review.

> **Naming discipline (reviewer tightening 1).** The mode is named
> ``liquid_plus_income_producing_nav`` — not ``liquid_plus_income``
> — because **it includes the NAV of buckets tagged
> ``income_producing``; it does not measure actual distributable
> income**. A stabilized real-estate bucket may be tagged
> income-producing because it generates rents, but its **appraised
> NAV is not automatically spendable**. Phase 12's mode admits the
> NAV as the closest available approximation; the diagnostic
> warning surfaces the resulting overstatement. True
> distributable-income measurement is Phase 12.5.

> **Bucket-level static metadata (reviewer tightening 2).** In
> Phase 12, ``income_producing`` is a **bucket-level static CMA
> tag**, not asset-, entity-, or property-level cash-flow
> classification. It is a pragmatic *bridge* until the entity /
> cash-flow / RE+OpCo layers (`PROJECT_SCOPE.md` §3.1, §3.3, §3.5)
> exist and produce per-asset realized-distribution evidence. A
> future reader must not interpret a True flag as "this bucket's
> dollars are spendable income"; it means only "this bucket
> contains assets that, on average, produce some distributable
> yield." Phase 12.5 (`distributable_income` mode + new
> `distribution_inflow` ledger flow) is the structurally correct
> replacement.

### Diagnosis

The L19 algebra (current Owl, post-Phase-11):

```python
# owl_adapter.py — the two load-bearing lines
initial_nav_total = float(sum(ledger.initial_nav.values()))
nav_realized      = float(ledger.end_nav_through(prior_q).sum())

initial_rate = cfg.annual_spend_usd / initial_nav_total
current_rate = annual_spend            / nav_realized
```

Both denominators are **total modeled NAV** across every bucket,
including illiquid private real estate, opco equity, development
land, and any future stabilized-RE bucket. For a typical Gen3-
Gen5 SFO balance sheet (25% liquid / 35% PE+credit / 30% private
RE / 10% OpCo), `total_nav` may be 2-3× the household's actual
spendable resources. Owl's "4% withdrawal rate" against total
NAV is a 10-12% withdrawal rate against spendable NAV — and the
12% is the rate the household actually faces. The guardrail-band
geometry is correct, but it's measuring against the wrong
denominator.

**The fix replaces the denominator on both rate sides
symmetrically** so the band test continues to fire on
*deviations from the household's true initial rate*, not on
deviations within an illiquid-padded measurement frame.

### Schema additions

Two new optional fields on ``GuardrailConfig`` and one new
optional field on ``CMAConfig``. All default to preserve Phase 11
behavior byte-identically.

```python
class GuardrailConfig(BaseModel):
    # ... existing fields preserved ...

    # Phase 12 / L19: optional spending-base selector. Default None
    # is semantically identical to "total_nav" — Owl measures rate
    # against ledger.end_nav_through(prior_q).sum() on both rate
    # sides, byte-identical to Phase 11. When set to a non-None
    # value, both initial_rate and current_rate denominators are
    # replaced by compute_spending_base(...) on the same NAV view.
    # **Owl-only** — flat_real / smoothing have no rate concept.
    spending_base: Literal[
        "total_nav",
        "liquid_nav",
        "liquid_plus_income_producing_nav",
        "custom_policy",
    ] | None = Field(default=None)

    # Phase 12 / L19: only meaningful when spending_base ==
    # "custom_policy". **Bucket-keyed** (NOT tier-keyed) — gives the
    # SFO user per-bucket control over inclusion fractions, e.g.
    # `private_real_estate_stabilized: 0.25` includes 25% of that
    # specific bucket's NAV in the spending base, independent of
    # how other illiquid buckets are weighted. A bucket missing from
    # the dict is treated as weight 0 (excluded). Weights are
    # **inclusion fractions, not allocation weights** and do NOT sum
    # to 1.
    #
    # Validation (StudyConfig cross-validator, reviewer tightening 3):
    #   - every key must be a valid CMA bucket
    #     (i.e., appear in cma.expected_returns_annual)
    #   - every value must be finite
    #   - every value must be >= 0
    #   - at least one value must be > 0
    #   - unspecified buckets default to weight 0
    #   - the resulting spending base must be > 0 at every quarter
    #     where Owl needs the rate denominator (runtime check in
    #     OwlRule; raises ValueError if violated)
    spending_base_weights: dict[str, float] | None = Field(default=None)


class CMAConfig(BaseModel):
    # ... existing fields preserved ...

    # Phase 12 / L19: extended liquidity tier set. Adds
    # "locked_strategic" for OpCo equity, development real estate,
    # development land, and any other bucket whose value is real
    # but never enters spending decisions short of an explicit
    # liquidity event. Existing configs (3-tier) load unchanged
    # because the new value is purely additive.
    liquidity: dict[
        str,
        Literal["liquid", "semi_liquid", "illiquid", "locked_strategic"],
    ] | None = None

    # Phase 12 / L19: optional per-bucket flag. Required when
    # GuardrailConfig.spending_base == "liquid_plus_income_producing_nav".
    # Validated at the StudyConfig level, not on CMAConfig alone,
    # because the requirement depends on the spending rule's
    # selected base.
    income_producing: dict[str, bool] | None = None
```

Cross-config validation lives on ``StudyConfig`` (the existing
home for cross-cutting consistency checks):

* If ``spending.guardrail.spending_base ==
  "liquid_plus_income_producing_nav"`` then ``cma.income_producing``
  must be present and cover **every** bucket in
  ``cma.expected_returns_annual`` (no silent default-False).
* If ``spending.guardrail.spending_base == "custom_policy"`` then
  ``spending_base_weights`` must be set and every key must be a
  valid CMA bucket (i.e., appear in
  ``cma.expected_returns_annual``). Weights must be finite and
  non-negative; at least one must be strictly positive. Unknown
  bucket keys are a hard config error — silent inclusion of a
  non-existent bucket is **not** a no-op. Unspecified buckets
  default to weight 0 (explicit exclusion).
* If any bucket has ``cma.liquidity[b] == "locked_strategic"``,
  the bucket's NAV is **never** included in any base except
  ``total_nav`` and ``custom_policy`` with explicit per-bucket
  weight — a soft default that codifies the standing principle.
* If ``spending.guardrail.spending_base in {"liquid_nav",
  "liquid_plus_income_producing_nav", "custom_policy"}`` then
  ``cma.liquidity`` must be present (covering all buckets); a
  config that selects a non-`total_nav` base without liquidity
  tags is a hard error rather than a degenerate fallback.

### Spending base computation (pure function)

```python
@dataclass(frozen=True)
class SpendingBaseBreakdown:
    """Pure data carrier for the diagnostic plumbing."""
    base_usd: float
    excluded_by_tier_usd: dict[str, float]
    excluded_by_income_flag_usd: dict[bool, float]


def compute_spending_base(
    nav_by_bucket: pd.Series,                    # index = bucket, value = USD
    cma_liquidity: pd.Series | None,             # index = bucket, value = liquidity tier
    cma_income_producing: pd.Series | None,      # index = bucket, value = bool (or None)
    spending_base: str | None,                   # GuardrailConfig.spending_base
    spending_base_weights: dict[str, float] | None,  # bucket-keyed
) -> SpendingBaseBreakdown:
    """Pure function. No ledger reads beyond the NAV series passed in.
    No CMA mutation. No state.

    Diagnostic outputs (NOT alpha signals):
      - excluded_by_tier_usd: dollars excluded broken out by
        liquidity tier (liquid / semi_liquid / illiquid /
        locked_strategic)
      - excluded_by_income_flag_usd: dollars excluded broken out
        by income_producing flag (True / False) — surfaces how
        much of the exclusion is structural-illiquidity vs. lack
        of distributable yield
    """

    if spending_base is None or spending_base == "total_nav":
        return SpendingBaseBreakdown(
            base_usd=float(nav_by_bucket.sum()),
            excluded_by_tier_usd={},
            excluded_by_income_flag_usd={},
        )

    if spending_base == "liquid_nav":
        included = nav_by_bucket[cma_liquidity == "liquid"]
        return SpendingBaseBreakdown(
            base_usd=float(included.sum()),
            excluded_by_tier_usd=_excluded_by_tier(
                nav_by_bucket, cma_liquidity, included.index
            ),
            excluded_by_income_flag_usd=_excluded_by_income_flag(
                nav_by_bucket, cma_income_producing, included.index
            ),
        )

    if spending_base == "liquid_plus_income_producing_nav":
        if cma_income_producing is None:
            raise ValueError(
                "spending_base='liquid_plus_income_producing_nav' "
                "requires cma.income_producing"
            )
        liquid_mask = cma_liquidity == "liquid"
        income_mask = cma_income_producing.fillna(False).astype(bool)
        included    = nav_by_bucket[liquid_mask | income_mask]
        return SpendingBaseBreakdown(
            base_usd=float(included.sum()),
            excluded_by_tier_usd=_excluded_by_tier(
                nav_by_bucket, cma_liquidity, included.index
            ),
            excluded_by_income_flag_usd=_excluded_by_income_flag(
                nav_by_bucket, cma_income_producing, included.index
            ),
        )

    if spending_base == "custom_policy":
        if spending_base_weights is None:
            raise ValueError("spending_base='custom_policy' requires weights")
        # Per-BUCKET weighted inclusion (reviewer tightening 3 — keys
        # are bucket names, not liquidity tiers). Unspecified bucket
        # → weight 0. Validation (valid bucket keys, finite, ≥0,
        # ≥1 positive) lives on StudyConfig; this function trusts
        # the validated input but defends against degenerate base
        # below.
        weights_per_bucket = pd.Series(
            {b: float(spending_base_weights.get(b, 0.0)) for b in nav_by_bucket.index},
            dtype=float,
        )
        included_usd = float((nav_by_bucket * weights_per_bucket).sum())
        # Diagnostic: a bucket with weight w contributes (1-w) of its
        # NAV to "excluded" for the by-tier and by-income-flag rollups.
        excluded_usd_per_bucket = nav_by_bucket * (1.0 - weights_per_bucket)
        return SpendingBaseBreakdown(
            base_usd=included_usd,
            excluded_by_tier_usd=_rollup_by_tier(
                excluded_usd_per_bucket, cma_liquidity
            ),
            excluded_by_income_flag_usd=_rollup_by_income_flag(
                excluded_usd_per_bucket, cma_income_producing
            ),
        )

    if spending_base == "distributable_income":
        raise NotImplementedError(
            "spending_base='distributable_income' is Phase 12.5 — requires "
            "the new `distribution_inflow` ledger flow type"
        )

    raise ValueError(f"unknown spending_base {spending_base!r}")
```

The OwlRule call site adds one runtime check beyond the
StudyConfig validators: if ``compute_spending_base`` returns
``base_usd <= 0`` at a quarter where Owl needs the rate
denominator, raise ``ValueError`` with the bucket weights and
NAV-by-bucket so the failure is debuggable. This is the
"selected base must be > 0 when Owl needs a rate denominator"
guard from reviewer tightening 3.

The function is **deterministic, single-pass, no fixed-point**.
It honors the Phase 4a state-flow contract because it reads only
inputs the orchestrator already had: the NAV-by-bucket series
from ``ledger.end_nav_through(prior_q)`` and the static CMA tags.
No new ledger reads, no new state, no within-quarter feedback.

### OwlRule integration

Two-line change inside ``quarterly_outflow_at`` at the year-
boundary path. Everything else preserved verbatim.

```python
# Year boundary: inflate, then guardrail-check vs realized base.
prior_annual = prior_quarterly * 4.0
annual_spend = prior_annual * (1.0 + cfg.inflation_pct)

# Phase 12 / L19: compute the spending base on the same NAV view
# used by Phase 11 (closed-through-prior-quarter). When
# gr.spending_base is None/total_nav, this is byte-identical to
# the prior code path. When non-None, both rate denominators are
# the spending base — preserving the rate-band geometry.
nav_realized_series = ledger.end_nav_through(prior_q)
realized = compute_spending_base(
    nav_realized_series,
    params.cma_liquidity,
    params.cma_income_producing,
    gr.spending_base,
    gr.spending_base_weights,
)

# Initial-rate denominator uses the SAME base on initial NAV so
# the rate-band test fires symmetrically. Initial NAV-by-bucket
# is read from ledger.initial_nav (already pure config).
initial_nav_series = pd.Series(ledger.initial_nav, dtype=float)
initial = compute_spending_base(
    initial_nav_series,
    params.cma_liquidity,
    params.cma_income_producing,
    gr.spending_base,
    gr.spending_base_weights,
)

# Reviewer tightening 3 runtime guard: the base must be > 0
# whenever Owl needs the rate denominator. Detects the
# pathological config where every weight excludes every bucket
# the household actually owns, or a quarter in which liquid
# buckets have drained to zero.
if initial.base_usd <= 0.0:
    raise ValueError(
        f"OwlRule: initial spending base is {initial.base_usd}; "
        f"selected mode={gr.spending_base!r}; "
        f"weights={gr.spending_base_weights!r}; "
        f"initial_nav_by_bucket={dict(initial_nav_series)}"
    )

if realized.base_usd > 0.0:
    initial_rate = cfg.annual_spend_usd / initial.base_usd
    current_rate = annual_spend           / realized.base_usd
    # ... rate-band trigger UNCHANGED ...

# Phase 11 / L16 absolute clamps UNCHANGED.
# Quarterly conversion + safety clamp UNCHANGED.
```

The two ``compute_spending_base`` calls are placed inside the
``OwlRule`` because the spending-base selector is on
``GuardrailConfig`` and Owl is the only rule with a guardrail.
They could be lifted to a higher abstraction in a future phase
if a non-Owl rule ever needs them; not in scope for Phase 12.

### SpendingParams wire-through

``SpendingParams`` is a frozen dataclass currently holding
``(config, start_quarter, num_quarters)``. Phase 12 adds two
optional fields:

```python
@dataclass(frozen=True)
class SpendingParams:
    config: SpendingConfig
    start_quarter: pd.Period
    num_quarters: int
    # Phase 12 / L19: CMA tags surfaced for the Owl spending base.
    # Optional so flat_real / smoothing call sites don't have to
    # construct them. OwlRule raises if spending_base != total_nav
    # and these are absent.
    cma_liquidity: pd.Series | None = None
    cma_income_producing: pd.Series | None = None
```

The orchestrator already constructs ``CMA`` from ``CMAConfig``
(see ``CMA.from_config``); it threads ``cma.liquidity`` and the
new ``cma.income_producing`` series into ``SpendingParams`` at
the same site. No allocator change. No PE change. No ledger
change. Test fixtures default to ``None`` — preserves the
default-off byte-stability for every existing test.

### Real-estate / OpCo / development categorization (mapping policy)

Phase 12 does **not** introduce per-bucket category metadata
(stabilized RE vs development RE vs land vs OpCo) — that lands
in Phase 13 with the §3.5 RE+OpCo pipeline. For Phase 12, the
SFO categorization is expressed entirely through the existing
2-axis CMA metadata: **liquidity tier × income-producing flag**.

Recommended SFO mapping (policy guidance, not enforced):

| Asset category | `liquidity` | `income_producing` | Default base inclusion |
| --- | --- | --- | --- |
| Public equity / FI / cash | `liquid` | true (yield) | all bases |
| Hedge funds (quarterly liq) | `semi_liquid` | varies | excluded except `total_nav` / `custom_policy` |
| Private equity / credit | `illiquid` | false (lumpy distros) | excluded except `total_nav` / `custom_policy` |
| Stabilized real estate | `illiquid` | **true** if rented and producing distributable yield | `liquid_plus_income_producing_nav`, `custom_policy`, `total_nav` |
| Development real estate | `locked_strategic` | false | only `total_nav` / `custom_policy` with explicit weight |
| Development land | `locked_strategic` | false | only `total_nav` / `custom_policy` with explicit weight |
| Operating-company equity | `locked_strategic` | false (until distribution policy modeled) | only `total_nav` / `custom_policy` with explicit weight |

Stabilized RE is the deliberate edge case (see open question 3
below). Tagging it `income_producing=true` admits its **NAV**
into the `liquid_plus_income_producing_nav` base — which still overstates
spending capacity vs. its actual distributable yield. Phase 12
treats this as a knowingly-loose approximation and surfaces the
overstatement in the diagnostic warning band. Phase 12.5
(`distributable_income` mode) is the proper fix.

### Open questions — answered

1. **Should default spending base remain total NAV for backward
   compatibility?** **Yes.** ``GuardrailConfig.spending_base``
   defaults to ``None``, which short-circuits to total NAV.
   Existing configs and tests are byte-identical post-Phase-12.
   Backward compatibility is non-negotiable per the established
   pattern (Phase 4a, Phase 8, Phase 11).

2. **Should liquid + semi_liquid be the first recommended SFO
   policy base?** **No — `liquid_plus_income_producing_nav` is.** "Liquid +
   semi_liquid" mixes a liquidity-tier criterion (semi_liquid)
   with no income criterion, which lets quarterly-liquidity
   hedge funds count even when they hold no distributable
   capacity. ``liquid_plus_income_producing_nav`` matches the standing
   principle ("Income-producing NAV — assets generating
   distributable yield") more cleanly. The ``liquid_nav`` mode
   remains available as the strictest base; `custom_policy` is
   available for households that want a semi_liquid weight.

3. **Should income-producing-but-illiquid stabilized real estate
   count in the base, or only its distributable income?** **NAV
   counts in Phase 12 (with explicit overstatement diagnostic);
   distributable-income counting is Phase 12.5.** This is the
   honest position: Phase 12 cannot track realized distributions
   without a new ledger flow type, so the closest available
   approximation is "include its NAV when tagged
   `income_producing=true`." The diagnostic surfaces
   `excluded_nav_by_tier` and a warning when stabilized-RE NAV is
   a large fraction of the base, so readers don't mistake the
   approximation for a tight spending-capacity figure. **The
   correct fix is Phase 12.5**, which adds a `distribution_inflow`
   flow type and switches the base to realized distributions over
   a trailing window. This is documented as a hard follow-on, not
   a "future work" handwave.

4. **Should development and land be excluded by default?**
   **Yes.** The recommended mapping tags them
   `liquidity=locked_strategic`, which excludes them from every
   base except `total_nav` and a `custom_policy` that explicitly
   names `locked_strategic` with a non-zero weight. This matches
   the standing principle line *"Development / land value is not
   distributable income."* `locked_strategic` is the new fourth
   tier introduced in Phase 12 specifically to make this mapping
   declarative.

5. **Should OpCo equity be excluded by default unless explicit
   distributions are modeled?** **Yes.** Same mechanism — tag
   OpCo as `locked_strategic`. Future Phase 12.5 enables the
   user to tag OpCo `income_producing=true` and route its
   modelled distribution policy through `distribution_inflow`
   flows, at which point `distributable_income` mode counts the
   *flow* not the *equity carry*. Phase 12 deliberately does
   **not** take a position on OpCo equity counting via
   `liquid_plus_income_producing_nav` — the design forces the user to either
   leave it locked or commit to a `custom_policy` weight, which
   keeps the standing-principle line *"OpCo value is not
   automatically portfolio liquidity"* declaratively visible in
   the config rather than buried in a default.

### Why this is the minimum fix

* **Two new fields on `GuardrailConfig`** (selector + custom
  weights) and **one new field on `CMAConfig`**
  (`income_producing`); the `liquidity` Literal grows by one
  value (`locked_strategic`). All optional, all default-off.
* **Default-off byte-stability**: behavior byte-identical to
  Phase 11 when `spending_base is None`. Same regression
  guarantee Phase 8 and Phase 11 carried.
* **Localized to ``OwlRule`` and a single pure helper**: no
  allocator change, no rebalancer change, no PE change, no
  ledger schema change, no new flow type, no new diagnostic
  pipeline.
* **Preserves the Phase 4a state-flow contract verbatim**:
  spending base is a pure function of `(closed-through(q-1)
  NAV-by-bucket, static CMA tags, config)`. No new ledger
  reads, no new state, no within-quarter feedback, no fixed
  point, no sidecar.
* **Scope-conditional resolution**: L19 is closed for the
  *base-side* of spending realism (which is the L19 ticket as
  written). The *flow-side* (realized distributions) is split
  off into Phase 12.5 with its own design block, not silently
  bundled.
* **Engine-conditional resolution**: applies to Owl only.
  flat_real and smoothing have no rate concept and are
  unaffected. This is consistent with how Phase 11 / L16 was
  scoped.

### Phase 4a state-flow contract — explicit preservation

The Phase 4a contract (see top of `spending/base.py`) requires
that a spending rule:

1. Read only `ledger.closed_through(quarter - 1)` /
   `ledger.end_nav_through(quarter - 1)`.
2. Not mutate or finalize the ledger.
3. Filter prior `spend` rows by `source == self.SOURCE_ID`.
4. Own q0 initialization end-to-end (no guardrail at q0).

Phase 12 honors all four:

1. ``compute_spending_base`` consumes the same NAV view Owl
   already reads. No additional ledger calls.
2. The function is pure; it returns a tuple, mutates nothing.
3. The source filter is unchanged — `_read_own_prior_spend`
   still keys on `"spending:owl"`.
4. q0 path is untouched. Spending base is consulted only at the
   year-boundary trigger, identical to where Owl currently
   consults `nav_realized`.

The CMA tags threaded through `SpendingParams` are **static
config**, not ledger state. They cannot violate the closed-
prior-quarter contract because they are not time-varying within
a run.

### Diagnostic — `## Owl spending base (advisory)` in `report.md`

New section (separate from the Phase 11 `## Owl scale-sensitivity`
section so the L16 and L19 narratives don't tangle in a single
block). The section renders under **two** gating conditions, each
producing a different framing:

* **Non-default base, Owl fired:** full diagnostic with both
  exclusion breakdowns + base/total ratio + dual withdrawal rates
  + warning bands.
* **Default base (`spending_base = total_nav`), Owl fired, AND
  ``illiquid`` or ``locked_strategic`` NAV is material** (≥30% of
  total NAV at run end): a *separate* short warning surfaces that
  the household has material non-spendable NAV but Owl is still
  measuring rate against total NAV — pointing the reader at the
  non-default modes. This is the reviewer-requested warning that
  catches the "default-on but the SFO needs a non-default base"
  failure mode.

```markdown
## Owl spending base (advisory)

- selected base: liquid_plus_income_producing_nav
- run-end totals:
  - total NAV:       $123,456,789
  - spending base:   $ 47,000,000   (38% of total NAV)
- excluded NAV by liquidity tier:
  - illiquid:         $ 41,200,000
  - locked_strategic: $ 35,256,789
- excluded NAV by income_producing flag:
  - income_producing=False: $ 76,456,789
  - income_producing=True:  $          0
- withdrawal-rate comparison (run end):
  - rate vs total NAV:      1.62%
  - rate vs spending base:  4.26%   ← rate the household actually faces
- regime:
  - "spending-base aware (selected base materially below total NAV)"
  - WARNING: spending base is 38% of total NAV; Owl trajectory
    reflects spending-capacity rate, not paper-NAV rate.

_Phase 12 / L19 closes the base-side of spending realism. The
flow-side (realized distributions) is Phase 12.5. The
``liquid_plus_income_producing_nav`` mode includes NAV of
buckets tagged ``income_producing``; it does not measure actual
distributable income. Stabilized real estate tagged
``income_producing=true`` contributes its appraised NAV to this
base — which still overstates spending capacity vs. its true
distributable yield. For a tight distributable-income figure
see Phase 12.5 (`distributable_income` mode + new
`distribution_inflow` ledger flow type)._
```

```markdown
## Owl spending base (advisory)

- selected base: total_nav (default)
- run-end totals:
  - total NAV:       $123,456,789
  - illiquid NAV:    $ 41,200,000   (33% of total NAV)
  - locked_strategic: $ 35,256,789  (29% of total NAV)
- WARNING: spending_base = total_nav, but ≥30% of total NAV is
  illiquid or locked_strategic. Owl is measuring withdrawal rate
  against paper NAV that is not spendable. Consider setting
  ``spending.guardrail.spending_base`` to one of:
  - liquid_nav (strictest)
  - liquid_plus_income_producing_nav (recommended SFO default;
    note this includes NAV of income-producing buckets, not
    distributable income)
  - custom_policy (per-bucket inclusion weights)
```

Warning thresholds:

| Trigger | Threshold | Severity |
| --- | --- | --- |
| `spending_base / total_nav` | `< 0.7` | WARNING |
| `spending_base / total_nav` | `< 0.4` | STRONG WARNING — "confirm CMA tagging policy reflects the actual balance sheet" |
| `spending_base = total_nav` AND `(illiquid + locked_strategic) / total_nav` | `>= 0.30` | WARNING — "material non-spendable NAV; consider a non-default base" |

All thresholds are advisory and configurable in Phase 12.5;
none gate or alter the Owl trajectory.

`OwlRule.diagnostics()` extends with:

```python
{
    "engine": "OwlRule",
    "min_clamp_activations": <int>,
    "max_clamp_activations": <int>,
    # Phase 12 / L19 additions:
    "spending_base_mode": <str | None>,
    "spending_base_run_end_usd": <float>,
    "spending_base_initial_usd": <float>,
    "total_nav_run_end_usd": <float>,
    "excluded_nav_by_tier_usd": <dict[str, float]>,
    "excluded_nav_by_income_flag_usd": <dict[bool, float]>,
    "withdrawal_rate_vs_total_nav": <float>,
    "withdrawal_rate_vs_spending_base": <float>,
    "material_illiquid_share": <float>,  # (illiquid + locked_strategic) / total_nav, run end
}
```

Existing `## Owl scale-sensitivity (advisory)` section is left
unchanged; its L19-caveat language stays in place but reads
differently when both Phase 11 clamps and Phase 12 base are
configured (the report renders both sections; readers see them
as orthogonal concerns, which they are).

### Tests planned (13)

Schema (4):

1. ``GuardrailConfig.spending_base`` defaults to ``None``;
   accepts the four documented Literals; rejects unknown values
   at validation time. ``"distributable_income"`` is accepted at
   schema time (parked in the Literal) but raises
   ``NotImplementedError("Phase 12.5")`` at runtime when Owl
   tries to use it.
2. ``CMAConfig.liquidity`` accepts ``"locked_strategic"`` for at
   least one bucket; old 3-tier configs still load unchanged.
3. ``StudyConfig`` cross-validation, positive paths:
   `liquid_plus_income_producing_nav` with full `cma.income_producing`
   coverage validates; `custom_policy` with valid bucket-keyed
   weights validates.
4. ``StudyConfig`` cross-validation, failure paths (reviewer
   tightening 3 bucket-keyed weights):
   - `liquid_plus_income_producing_nav` without
     `cma.income_producing` → fails loudly
   - `liquid_plus_income_producing_nav` with `cma.income_producing`
     missing a bucket → fails loudly (no silent default-False)
   - `custom_policy` without `spending_base_weights` → fails loudly
   - `custom_policy` with a key that is not a CMA bucket → fails
     loudly (silent inclusion of a non-existent bucket is NOT a
     no-op)
   - `custom_policy` with a non-finite weight → fails loudly
   - `custom_policy` with a negative weight → fails loudly
   - `custom_policy` with all-zero weights → fails loudly (≥1
     positive weight is required)
   - non-`total_nav` base without `cma.liquidity` → fails loudly

Behavior — base computation (3):

5. **Total-NAV byte-stability**: `spending_base=None` produces
   trajectories byte-identical to a `spending_base="total_nav"`
   run AND byte-identical to the pre-Phase-12 baseline (the
   default-config golden test).
6. **Liquid-NAV exclusion + dual breakdown**: a 4-bucket fixture
   (1 liquid + income, 1 semi_liquid + non-income, 1 illiquid +
   income (stabilized RE), 1 locked_strategic + non-income) with
   equal initial NAV. `spending_base="liquid_nav"` returns
   exactly the liquid bucket's NAV; `excluded_by_tier_usd`
   accounts for the other three; `excluded_by_income_flag_usd`
   shows the income/non-income split of the excluded dollars.
7. **Custom-policy bucket-weighted blend**: weights
   `{public_eq: 1.0, public_fi: 1.0, hf: 0.5,
   private_re_stabilized: 0.25, land: 0.0}` on a matching CMA
   fixture returns the exact dollar-weighted sum to FP
   tolerance; `excluded_by_tier_usd` and
   `excluded_by_income_flag_usd` reflect the (1−w)·NAV
   contributions.

Behavior — Owl integration (4):

8. **Owl trigger fires on spending-base rate, not total-NAV
   rate**: a fixture where `total_NAV` rate is comfortably in-
   band but `spending_base` rate is above the upper band → Owl
   *cuts* spending. The mirror case (in-band on base, above-band
   on total NAV) is *not* triggered.
9. **Initial-rate symmetry**: the initial-rate denominator uses
   the same base as the current-rate denominator. Test
   constructs a setup where the two would diverge if only one
   side were swapped, and asserts band geometry is preserved.
10. **State-flow contract preservation**: a test that calls
    ``OwlRule.quarterly_outflow_at(t)`` after asserting that the
    ledger view passed in only contains rows with
    `quarter <= t-1`; the rule must still produce the documented
    value (no read from the current quarter).
11. **Runtime guard — base must be > 0**: a fixture where every
    weight excludes every bucket the household actually owns at
    a given quarter → `OwlRule` raises `ValueError` with the
    bucket weights and NAV-by-bucket in the message (reviewer
    tightening 3 runtime check).

End-to-end (2):

12. **Non-default report diagnostic renders** for an Owl-rule
    run with `spending_base="liquid_plus_income_producing_nav"`;
    the new section is present, classifies the regime correctly,
    surfaces both `excluded_by_tier_usd` and
    `excluded_by_income_flag_usd`, surfaces the dual
    withdrawal-rate comparison, and emits the warning when
    `base/total < 0.7`.
13. **Default-base material-illiquid warning** renders when
    `spending_base=None` (or `total_nav`) AND
    `(illiquid + locked_strategic) / total_nav >= 0.30` at run
    end. The warning names all three non-default modes so the
    reader has the actionable redirect.

### What Phase 12 is **not**

Listed explicitly as guardrails for future contributors:

* **Not a `distributable_income` implementation.** That mode is
  named in the Literal but raises NotImplementedError in Phase
  12 if selected; Phase 12.5 lands the new ledger flow type and
  the realized-distribution accumulator.
* **Not a real-estate / OpCo pipeline.** Phase 13. Phase 12 does
  not introduce per-bucket category tags (stabilized vs
  development vs land vs OpCo); the SFO categorization is
  expressed via the existing `liquidity × income_producing`
  axes only.
* **Not a cash-flow workbook ingestion.** The next phase after
  L19 closes is `Cashflow Modeling v7.xlsx` ingestion + entity
  schema (per `PROJECT_SCOPE.md` §6).
* **Not a Monte Carlo / stochastic upgrade.** L2 remains
  deferred until the deterministic SFO layers are honest.
* **Not a regime-dependent returns layer.**
* **Not a PE schema change.** No `flow_id` upgrade (L5).
* **Not a fee economics change.**
* **Not a secondary-sale model.**
* **Not an allocator / rebalancer change.** The Phase 8
  illiquidity overlay handles the rebalance side of the standing
  principle; Phase 12 handles the spending-rule side.
* **Not a ledger schema change.** The optional fourth liquidity
  tier (`locked_strategic`) lives on `CMAConfig`, not on the
  ledger.
* **Not a non-Owl rule change.** flat_real and smoothing are
  rate-free; they ignore `spending_base`. The Literal is parked
  on `GuardrailConfig` (Owl-only) by design.

### L19 status under Phase 12

Will flip to ``[PARTIALLY RESOLVED 2026-05-02, Phase 12]`` —
**not** RESOLVED — on implementation. The resolution wording in
the limitations table reads exactly:

```
L19 — PARTIALLY RESOLVED, Phase 12.
Base-side spending denominator realism introduced.
Flow-side distributable-income realism remains open until
Phase 12.5.
```

With the explicit notes:

* **Base-side closed**: Owl's withdrawal-rate denominator is
  configurable across `total_nav`, `liquid_nav`,
  `liquid_plus_income_producing_nav`, and `custom_policy`. The
  standing principle's distinction between *total NAV* and
  *spendable resources* is now expressible in config and visible
  in the report. Both initial-rate and current-rate denominators
  are replaced symmetrically; rate-band geometry is preserved.
* **Flow-side open (Phase 12.5)**: realized distributions are
  not yet a ledger flow type. `distributable_income` mode is
  named in the Literal but raises `NotImplementedError` if
  selected. Stabilized RE counted via the bucket-level
  `income_producing` flag overstates spending capacity by
  whatever fraction of NAV is non-distributable carry — this is
  documented inline in the report diagnostic, not silently
  absorbed.
* **Default behavior preserved**: `spending_base=None` ⇒ total
  NAV, byte-identical to Phase 11. Existing fixtures and the
  225-test baseline pass unchanged.

L19's **full** resolution requires Phase 12.5 (flow-side). The
limitation entry is updated to track both halves separately so
no future reader interprets Phase 12 as closing it entirely.

### Locked design choices

* `GuardrailConfig.spending_base` Literal of four named modes
  (plus the parked `distributable_income`), default `None` ≡
  `"total_nav"`.
* `GuardrailConfig.spending_base_weights` is **bucket-keyed**
  (`dict[str, float]`), not tier-keyed (reviewer tightening 3).
  Default `None`, only used by `custom_policy`. Validation:
  every key is a valid CMA bucket; values are finite, ≥0; ≥1
  positive value required; unspecified buckets default to
  weight 0; runtime guard ensures resulting base > 0 when Owl
  uses it as the rate denominator.
* `CMAConfig.liquidity` extended to a 4-tier Literal with
  `"locked_strategic"` as the new value; existing 3-tier configs
  still load. Required (covering all buckets) when any
  non-`total_nav` base is selected.
* `CMAConfig.income_producing` optional `dict[str, bool]`,
  default `None`. Required by the cross-validator only when
  `liquid_plus_income_producing_nav` is selected, and must
  cover every bucket (no silent default-False).
* `income_producing` is **bucket-level static metadata** in
  Phase 12, not asset-/entity-/property-level cash-flow
  classification (reviewer tightening 2). It is a bridge until
  Phase 12.5's `distribution_inflow` ledger flow type lands.
* `liquid_plus_income_producing_nav` is named to make the
  semantics explicit: it includes the **NAV** of buckets tagged
  `income_producing`; it does **not** measure realized
  distributable income (reviewer tightening 1).
* `compute_spending_base` is a pure helper module-private to
  `aa_model.spending`; returns a frozen `SpendingBaseBreakdown`
  carrying `base_usd` plus dual exclusion breakdowns
  (`excluded_by_tier_usd`, `excluded_by_income_flag_usd`).
  No new public surface beyond the existing `SpendingRule` ABC.
* OwlRule replaces the `nav_realized` / `initial_nav_total`
  denominators on **both** rate sides symmetrically. No partial
  asymmetric variant is supported.
* Spending base computed against the same `closed-through(q-1)`
  NAV view Owl already reads. No new ledger access.
* `SpendingParams` gains two optional CMA-tag fields; non-Owl
  call sites pass `None`.
* New advisory section `## Owl spending base` in `report.md`,
  with two render modes: (a) non-default base full diagnostic,
  (b) default base + material-illiquid warning. Both gated on
  Owl + rule-fired.
* Warning thresholds: WARNING at `base/total < 0.7`; STRONG
  WARNING at `base/total < 0.4`; default-base material-illiquid
  WARNING at `(illiquid + locked_strategic) / total_nav >= 0.30`.
  All advisory, all tunable in Phase 12.5.
* `distributable_income` mode is **named but not implemented**
  in Phase 12; selecting it raises
  `NotImplementedError("Phase 12.5")`.
* L19 flips to `PARTIALLY RESOLVED` (base-side closed; flow-side
  open) on implementation. Phase 12.5 is the explicit follow-on.
* Default-off byte-stability for every existing test fixture and
  config.
* Owl-only — flat_real / smoothing have no rate concept.

---

## Phase 12.5 design (pre-implementation) — L19 flow-side: distributable-income spending base

> **One-line goal.** Resolve the **flow-side** of L19 by introducing
> a new ledger flow type ``distribution_inflow`` and the
> ``distributable_income`` spending base it feeds. Replaces "withdrawal
> rate against NAV of income-producing buckets" (Phase 12's
> ``liquid_plus_income_producing_nav`` approximation) with "withdrawal
> rate against trailing realized distributable income" — which is the
> rate the household *actually* faces. **Strictly a flow-side fix; does
> NOT change Phase 12's base-side modes, the rate-band logic, the
> absolute clamps, the allocator, or the rebalancer.** Preserves the
> Phase 4a closed-prior-quarter state-flow contract verbatim.
>
> **Phase 12.5 is infrastructure-only on the flow side.** It lands the
> flow type, the base computation, the schema validation, the report
> diagnostic, and synthetic-fixture tests — but produces zero
> ``distribution_inflow`` rows in production runs because **no
> producer exists yet** (cash-flow ingestion + RE+OpCo pipeline are
> Phase 13+, per `PROJECT_SCOPE.md` §6 roadmap). Until a producer
> lands, ``spending_base="distributable_income"`` is configurable and
> validated end-to-end against fixtures, and raises a runtime
> ``ValueError`` in any production run that selects it without a
> populated trailing window. This is intentional: the flow type and
> the base computation must exist before the producer can feed them.

### Required scope tightening — Phase 12.5 is the *flow type + base* only

| Concern | In scope? | Why |
| --- | --- | --- |
| New ``distribution_inflow`` ledger flow type | yes | The structural fix the parked Phase 12 mode needs |
| ``distributable_income`` base wired into ``compute_spending_base`` | yes | Removes the ``NotImplementedError("Phase 12.5")`` stub |
| Trailing-window rollup (default 4 quarters) | yes | Smooths lumpy quarterly distributions into an annualized rate |
| Bootstrap distributable-income value for q0 / insufficient history | yes | The initial-rate denominator must exist before any closed quarter does |
| Source-string convention for income origin | yes (policy guidance, not strict validation) | Lets diagnostics break out RE / OpCo / dividend / interest |
| Recurring vs one-time classification | **no — Phase 13** | All ``distribution_inflow`` rows count for now; subdivision is a downstream refinement |
| ``pe_distribution`` rows opting in | **no — Phase 13** | PE distributions are intrinsically lumpy; mixing them into a trailing-income smoother distorts the rate |
| Cash-flow workbook ingestion | **no — Phase 14** | Producers come later |
| RE + OpCo pipeline schema | **no — Phase 13** | Producers come later |
| Entity schema | **no — Phase 14** | Producers come later |

Phase 12.5 lands the **seat** for distributable income. Populating that
seat is downstream. This split is the same discipline Phase 12 used:
schema + base + tests now, producers later.

### Diagnosis

The parked code path in Phase 12:

```python
# spending_base.py — Phase 12 stub
if spending_base == "distributable_income":
    raise NotImplementedError(
        "spending_base='distributable_income' is Phase 12.5 — requires "
        "the new `distribution_inflow` ledger flow type"
    )
```

The standing-principle line *"Income-producing NAV — assets generating
distributable yield. Real estate at appraisal but zero current income
is **not** income-producing for spending purposes"* is honored
declaratively in Phase 12 (via the ``income_producing`` tag) but only
*directionally* — the tag still admits the bucket's full NAV, not its
realized yield. The flow-side fix replaces NAV with yield: sum
``distribution_inflow`` rows over the trailing window and use that
dollar figure as the rate denominator. **No more "appraisal-as-income"
approximation; no more silent overstatement.**

### New ledger flow type

```python
# integration/ledger.py — FLOW_ORDER additions (additive only; existing
# flow types preserved verbatim)
FLOW_ORDER: tuple[str, ...] = (
    "return",
    "external_inflow",
    "distribution_inflow",   # NEW — Phase 12.5 / L19
    "pe_call",
    "pe_distribution",
    "pe_nav_mark",
    "rebalance",
    "transaction_cost",
    "spend",
)
```

> **Reviewer tightening 1 — Phase 12.5 does NOT model
> distributability.** Phase 12.5 is the *consumer-side*
> infrastructure: it consumes ``distribution_inflow`` rows that an
> upstream producer has *already classified* as cash distributable
> to the family-office level. **Phase 12.5 does not determine
> whether a distribution is legally, tax-wise, or
> entity-governance-wise distributable to the family office.**
> Cash sitting at an OpCo, project LLC, restricted trust, or
> entity with a payout constraint may not be spendable at the
> household level even if it appears as USD in the operating
> ledger of that entity. The producer (Phase 13 RE+OpCo pipeline +
> Phase 14 cash-flow / entity ingestion) owns the upstream
> classification — applying CRUT mandates, gift-trust restrictions,
> OpCo retention policy, RE-LLC distribution waterfalls, federal /
> state tax withholding, etc. — and emits **only the family-office-
> distributable subset** as ``distribution_inflow`` rows. Phase
> 12.5 trusts that classification and consumes the rows; it has no
> opinion about legal / tax / governance distributability of its
> own.

Sleeve semantics — mirrored on ``external_inflow``:

* ``distribution_inflow`` is **always emitted on the cash bucket** with
  ``amount_usd > 0``. The dollars represent realized cash distributed
  by an income-producing source AND already classified upstream as
  family-office-distributable (rent collected and net of LLC retentions,
  OpCo distribution declared and after entity-tax / governance
  approval, dividend paid into a household-level account, interest
  received into household cash).
* The originating bucket's NAV is **unchanged**. A stabilized RE
  bucket carrying $30M of appraisal value distributes $1M of rent →
  RE bucket stays at $30M (appraisal carry unaffected); cash bucket
  gains $1M; total NAV grows by $1M (real wealth was generated).
* The ``source`` string identifies the originating asset / entity.
  **Reviewer tightening 2 — recommended canonical convention**:

  ```
  source = "distribution:<domain>:<entity_or_asset_id>"
  ```

  Examples:

  ```
  distribution:real_estate:westplan_woodlawn
  distribution:opco:liv_holding
  distribution:portfolio:public_dividends
  distribution:entity:bft
  ```

  ``<domain>`` ∈ {``real_estate``, ``opco``, ``portfolio``,
  ``entity``} for Phase 12.5; future domains may extend the
  vocabulary. ``<entity_or_asset_id>`` is the producer's stable
  identifier for the originating asset / entity (e.g., property
  short-name, OpCo holdco code, portfolio dividend bucket name,
  entity short-name from the §3.1 entity layer).

  **The convention is documented but NOT enforced in Phase 12.5.**
  The ledger accepts any non-empty source string. Future Phase
  13.x may tighten via a controlled vocabulary once the producer
  layer commits to a stable set of domains and IDs. Recording the
  convention now lets producers built in Phase 13/14 emit
  conformant rows from day one.

* ``amount_usd`` must be strictly positive. Negative or zero rows
  fail at ledger-add time (consistent with the existing ``add()``
  validation pattern).

Why a new flow type rather than reusing ``external_inflow``:

* ``external_inflow`` semantically represents capital injected from
  *outside* the modeled portfolio (gifts received, scheduled inflows,
  external salary). ``distribution_inflow`` represents cash *generated
  by the portfolio's income-producing assets*. Conflating the two
  would lose the diagnostic distinction between "household received
  $X external this period" and "household's income-producing assets
  yielded $X this period" — which is exactly what L19 needs to
  separate.
* The diagnostic in the report relies on this distinction to show
  trailing-distributable-income separately from any external inflows.

### Schema additions

Two new optional fields on ``GuardrailConfig``. Both default-off;
both required only when ``spending_base == "distributable_income"``.

```python
class GuardrailConfig(BaseModel):
    # ... existing fields preserved through Phase 12 ...

    # Phase 12.5 / L19 flow-side: trailing window for the
    # distributable_income base. Default 4 quarters (TTM). Smaller
    # windows are noisier; larger windows lag regime shifts. The
    # window is rolled forward each year-boundary call; only quarters
    # in [prior_q - window + 1, prior_q] count. **Required when
    # spending_base == "distributable_income".**
    distribution_window_quarters: int | None = Field(
        default=None, ge=1, le=20
    )

    # Phase 12.5 / L19 flow-side: bootstrap distributable-income value
    # used for (a) the initial-rate denominator at run start (no
    # closed quarters yet) and (b) any year-boundary call where the
    # closed-prior-quarter window is incomplete (run age < window).
    # Static USD value the user provides — typically the household's
    # most recent calendar-year realized distributable income.
    # **Required when spending_base == "distributable_income".**
    bootstrap_distributable_income_usd: float | None = Field(
        default=None, gt=0.0
    )
```

Cross-config validation extends ``StudyConfig._phase12_spending_base_cross_config``:

* ``spending_base == "distributable_income"`` requires both
  ``distribution_window_quarters`` and
  ``bootstrap_distributable_income_usd`` to be set.
* The two fields are meaningful **only** for
  ``distributable_income``; setting them with any other base fails
  loud (same discipline as Phase 12's
  ``spending_base_weights``-only-with-``custom_policy`` rule).
* No CMA-tag requirement: the new base reads ledger flows, not CMA
  tags. ``cma.liquidity`` and ``cma.income_producing`` are unused
  for this mode (and explicitly *not* required in the cross-validator
  for ``distributable_income``).

### Spending-base computation extension

The Phase 12 ``compute_spending_base`` helper is extended to consume
the closed ledger view (not just the NAV-by-bucket series). To
preserve the existing pure-function shape, the trailing-distributable-
income branch is implemented as a thin wrapper that takes both the
ledger view and the config window/bootstrap values:

```python
def compute_distributable_income_base(
    ledger: QuarterlyLedger,
    prior_q: pd.Period,
    *,
    window_quarters: int,
    bootstrap_usd: float,
) -> tuple[float, dict[str, float], bool]:
    """Returns (base_usd, by_source_usd, is_bootstrap).

    Pure function. Reads only ledger.closed_through(prior_q). No CMA
    access. No state.

    Window definition: sums ``distribution_inflow`` rows where
    ``prior_q - window_quarters < quarter <= prior_q``. The result
    is annualized as ``trailing_sum`` (NOT * 4 / window) — i.e., the
    sum is a literal dollar figure for the trailing N quarters; for
    a default window of 4, this IS the trailing-12-month figure
    directly comparable to ``annual_spend_usd``.

    is_bootstrap is True when the realized window does not cover the
    full ``window_quarters`` (run age too short) AND the trailing sum
    equals the bootstrap fallback. The diagnostic surfaces this so
    readers can see which years used realized vs. bootstrap data.
    """
    # ... pure-function implementation ...
```

Integration into the existing ``compute_spending_base``:

```python
def compute_spending_base(
    nav_by_bucket: pd.Series,
    cma_liquidity: pd.Series | None,
    cma_income_producing: pd.Series | None,
    spending_base: str | None,
    spending_base_weights: dict[str, float] | None,
    *,
    # Phase 12.5 / L19 flow-side additions — only consumed for the
    # distributable_income branch. All other branches ignore them.
    ledger: QuarterlyLedger | None = None,
    prior_quarter: pd.Period | None = None,
    distribution_window_quarters: int | None = None,
    bootstrap_distributable_income_usd: float | None = None,
) -> SpendingBaseBreakdown:
    # ... existing branches preserved verbatim ...

    if spending_base == "distributable_income":
        if ledger is None or prior_quarter is None:
            raise ValueError(
                "spending_base='distributable_income' requires ledger "
                "and prior_quarter at the OwlRule call site"
            )
        if distribution_window_quarters is None or bootstrap_distributable_income_usd is None:
            raise ValueError(
                "spending_base='distributable_income' requires "
                "distribution_window_quarters and "
                "bootstrap_distributable_income_usd on GuardrailConfig"
            )
        base, by_source, is_bootstrap = compute_distributable_income_base(
            ledger,
            prior_quarter,
            window_quarters=distribution_window_quarters,
            bootstrap_usd=bootstrap_distributable_income_usd,
        )
        return SpendingBaseBreakdown(
            base_usd=base,
            excluded_by_tier_usd={},          # tier rollup not meaningful here
            excluded_by_income_flag_usd={},   # income-flag rollup not meaningful here
            # NEW Phase 12.5 fields (additive on the dataclass):
            distributable_income_by_source_usd=by_source,
            is_bootstrap=is_bootstrap,
        )
```

Note the dataclass extension: ``SpendingBaseBreakdown`` gains two new
optional fields (``distributable_income_by_source_usd: dict[str, float]
= {}`` and ``is_bootstrap: bool = False``), defaulting to neutral
values so all existing call sites remain byte-identical.

### Trailing window — the minimal default

**Default: 4 quarters (trailing 12 months).** Reasons:

* Matches how households think about "annual income" — a TTM figure
  is the natural read.
* Smooths quarterly lumpiness without over-smoothing through
  long-cycle changes (a 20-quarter window would lag regime shifts by
  years).
* Aligns directly with ``cfg.annual_spend_usd`` units — the trailing
  4-quarter sum IS an annual figure, so the rate is
  ``annual_spend_usd / trailing_4q_sum`` without unit gymnastics.

Configurable via ``distribution_window_quarters`` (range 1-20). Annualized-
latest-quarter (window=1, multiplied by 4) is **not** introduced — too
noisy, too sensitive to a single quarterly distribution timing. If a
user wants it, they can set ``window_quarters=4`` and weight by their
own business policy outside the model. Phase 12.5 ships the simple
default-4-quarters policy; complexity adds in Phase 13 if a user
demonstrates a need.

### q0 / insufficient-history — the bootstrap

**Initial-rate denominator at q0**: there are no closed quarters, so
the realized trailing sum is zero. Use
``bootstrap_distributable_income_usd`` as the denominator. This is
the initial-rate analog of the "static initial spend rate" that the
existing Owl path already uses (``cfg.annual_spend_usd /
initial_nav_total`` in Phase 11; ``cfg.annual_spend_usd /
initial_base`` in Phase 12).

**Year-boundary calls when run age < window_quarters**: the realized
window does not yet cover the full lookback. Two consistent options:

* **(A) Use realized sum even if window incomplete.** Pro: monotone
  schedule. Con: a one-quarter run with $200K of distributions
  produces a $200K base — a 16× overstated rate vs. the eventual
  steady-state base.
* **(B) Use bootstrap until window is full, then switch to
  realized.** Pro: matches user intuition that "bootstrap is the
  household's real income figure until enough data accumulates."
  Con: a step function at the window-completion boundary.

**Phase 12.5 picks (B).** The discontinuity is deliberate — it
mirrors the analogous Phase 11 pattern of a clamp activation showing
up sharply at the trigger. The diagnostic surfaces the boundary
explicitly via ``is_bootstrap`` so readers can see which year first
used realized data.

The cross-validator does not enforce
``num_quarters >= distribution_window_quarters`` because short
research-style runs are valid use cases — the user just gets the
bootstrap rate throughout. The diagnostic warning surfaces "run
length insufficient for realized window" when applicable.

### Zero-income guard

If the realized trailing sum is exactly ``$0`` *after the bootstrap
window has elapsed*, the household genuinely has no spendable income
this period. Asserting a withdrawal rate against zero is meaningless
and degenerate. The runtime guard:

```python
if not is_bootstrap and base_usd <= 0.0:
    raise ValueError(
        f"OwlRule: realized trailing distributable income is "
        f"{base_usd}; window={window_quarters}q ending at {prior_q}; "
        f"by_source={by_source}. The household has no realized "
        f"distributable income in the closed window. Either wait "
        f"for a producer-feed quarter, configure a wider window, "
        f"or switch to a non-flow-side spending base."
    )
```

This reuses the Phase 12 ``initial_base <= 0`` guard pattern. Failing
loud is the right policy: any silent fallback (to bootstrap forever,
or to total_nav) would mask a real data gap that the household
operator needs to act on. The error message names the specific
remediation paths.

### OwlRule integration

Owl already (Phase 12) computes both initial and current rate
denominators via ``compute_spending_base``. Phase 12.5 threads the
ledger + prior_quarter + window/bootstrap values through to the
helper. The integration is:

```python
# Year boundary path — same shape as Phase 12; new kwargs threaded.
realized = compute_spending_base(
    nav_realized_series,
    params.cma_liquidity,
    params.cma_income_producing,
    gr.spending_base,
    gr.spending_base_weights,
    ledger=ledger,
    prior_quarter=prior_q,
    distribution_window_quarters=gr.distribution_window_quarters,
    bootstrap_distributable_income_usd=gr.bootstrap_distributable_income_usd,
)

# Initial-rate denominator: at q0 / first year-boundary, the
# realized window is empty. The helper short-circuits to the
# bootstrap value via the is_bootstrap flag — no special-case
# OwlRule code path needed. Initial NAV-by-bucket is still passed
# (other modes consume it); for distributable_income, the helper
# ignores the NAV series entirely.
initial = compute_spending_base(
    initial_nav_series,
    params.cma_liquidity,
    params.cma_income_producing,
    gr.spending_base,
    gr.spending_base_weights,
    ledger=ledger,
    prior_quarter=prior_q,
    distribution_window_quarters=gr.distribution_window_quarters,
    bootstrap_distributable_income_usd=gr.bootstrap_distributable_income_usd,
)
```

The initial-rate denominator is intentionally computed against the
**same** ledger view the realized denominator uses. At q0 this means
both initial and realized use the bootstrap value (rate = 1.0 ×
``annual_spend_usd / bootstrap`` — no drift signal). At later year
boundaries, both use the realized trailing sum. The symmetry is the
Phase 12 invariant — preserved verbatim.

### State-flow contract — explicit preservation

Phase 4a contract (read-only, closed-prior-quarter, no mutation, q0
owns init):

1. ``compute_distributable_income_base`` calls
   ``ledger.closed_through(prior_q)`` only. No reads of the current
   quarter; no reads of any future quarter.
2. The function is pure; mutates nothing.
3. No source filter is needed for the distribution-inflow rollup
   itself, but the ``source`` column is read and grouped for the
   ``by_source`` diagnostic.
4. q0 path is untouched. The bootstrap value is consulted only at
   the year-boundary trigger, identical to where Owl currently
   consults the rate denominator.

The new flow type does not change any existing invariant on the
ledger:

* ``return`` rows still fire first per quarter / bucket.
* ``spend`` rows still fire last.
* ``distribution_inflow`` lives between ``external_inflow`` and
  ``pe_call`` in ``FLOW_ORDER`` — a sleeve-side ordering choice
  that puts realized inflows before PE activity within a quarter.
* No new aggregation invariant; the row sums into cash NAV via the
  existing chain logic.

### PE handling — explicit exclusion

``pe_distribution`` rows are **NOT** included in the
``distribution_inflow`` rollup by default. Reasons:

* PE distributions are episodic / lumpy — a single $10M secondary
  exit in one quarter would dominate a 4-quarter trailing window
  and produce a 4× overstated rate.
* PE distributions are already captured separately in the existing
  PE program structure section of the report (cumulative calls,
  cumulative distributions, NAV by manager).
* The standing principle's distinction between "spendable yield"
  (recurring) and "capital event" (lumpy) supports treating PE as
  a separate category.

**Future Phase 13.x may add an opt-in flag** (e.g.,
``GuardrailConfig.include_pe_distributions: bool = False``) once a
realistic recurring-PE-distribution policy is modeled. Phase 12.5
deliberately does not introduce this — adding the toggle without
the supporting recurring/one-time classification would be a footgun.

### Default behavior + byte stability

* ``spending_base=None`` ⇒ ``"total_nav"`` (Phase 12 default
  unchanged).
* All Phase 12 modes byte-identical post-Phase-12.5.
* Adding the new flow type to ``FLOW_ORDER`` is **additive** — no
  existing fixture emits ``distribution_inflow`` rows, so every
  existing test trajectory is byte-identical.
* Adding the two new ``GuardrailConfig`` fields is additive with
  ``None`` defaults — no existing config rejected at load.
* Extending ``SpendingBaseBreakdown`` with two new fields with
  neutral defaults is non-breaking for every existing caller.

### Diagnostic — `## Owl spending base (advisory)` extension

The Phase 12 advisory section gets a third render mode for
``spending_base="distributable_income"``:

```markdown
## Owl spending base (advisory)

- selected base: distributable_income
- run-end totals:
  - total NAV:                           $123,456,789
  - trailing distributable income (4q):  $  4,200,000
  - bootstrap distributable income:      $  4,000,000
  - source of base this year:            realized   (or "bootstrap")
- distributable income by source (run end):
  - re_stabilized: $ 2,800,000
  - opco:          $   900,000
  - dividend:      $   300,000
  - interest:      $   200,000
- withdrawal-rate comparison (run end):
  - rate vs total NAV:                  3.24%
  - rate vs distributable-income base:  100.00%   ← rate the household actually faces
- regime:
  - "flow-side aware (selected base = trailing realized income)"
  - WARNING: rate vs distributable-income base ≥ 100% — household
    is spending more than it earns; trajectory will erode capital.
  - INFO: years 1-1 used bootstrap (insufficient closed window);
    years 2+ used realized trailing.
  - **CAVEAT: Phase 12.5 treats every ``distribution_inflow`` row
    equally. Recurring vs. one-time classification is deferred to
    the producer layer (Phase 13/14). A high distributable-income
    base may be overstated if the trailing window is dominated by
    asset sales, refinancings, special dividends, or one-time
    entity transfers. The household operator should review the
    by-source breakdown above against their knowledge of what is
    recurring before relying on the headline rate.**

_Phase 12.5 / L19 lands the **infrastructure** for flow-side
spending-base realism — the ledger flow type, base computation,
and rate-band integration. **Production-grade distributable-income
realism remains dependent on Phase 13 (RE+OpCo pipeline) and
Phase 14 (cash-flow / entity ingestion) producers.** Phase 12.5
does not determine legal / tax / entity-governance distributability;
it consumes rows already classified upstream as
family-office-distributable. Use the bootstrap-only path for short
research runs; otherwise wait for Phase 13/14._
```

Warning bands:

| Trigger | Threshold | Severity |
| --- | --- | --- |
| ``rate vs distributable_income`` | ``>= 1.00`` | STRONG WARNING — spending exceeds income; capital erosion |
| ``rate vs distributable_income`` | ``>= 0.80`` | WARNING — within 20% of income ceiling |
| ``is_bootstrap`` for any year ≥ ``window_quarters`` | true | STRONG WARNING — realized window expected but not populated; check producer feed |
| ``len(by_source_usd) == 1`` AND ``base > 0`` | (concentration) | INFO — single-source income concentration |
| Realized trailing sum drops > 30% YoY | (regime shift) | INFO — yield regime change; verify policy |
| Always (Phase 12.5 standing caveat) | always | CAVEAT — recurring vs one-time not modeled; trailing sum may be inflated by sales / refis / specials / one-time transfers |

All advisory; none gate Owl's trajectory.

``OwlRule.diagnostics()`` extension (additive on Phase 12 fields):

```python
{
    "engine": "OwlRule",
    # ... Phase 11 + 12 fields preserved verbatim ...
    # Phase 12.5 / L19 flow-side additions:
    "trailing_distributable_income_usd": <float>,
    "distributable_income_by_source_usd": <dict[str, float]>,
    "distribution_window_quarters": <int | None>,
    "bootstrap_distributable_income_usd": <float | None>,
    "used_bootstrap_at_run_end": <bool>,
}
```

### Tests planned (13)

Schema (3):

1. ``GuardrailConfig.distribution_window_quarters`` defaults to
   ``None``; accepts integers in [1, 20]; rejects 0 and 21+ at
   validation time.
2. ``GuardrailConfig.bootstrap_distributable_income_usd`` defaults
   to ``None``; accepts strictly positive finite values; rejects
   zero and negative.
3. ``StudyConfig`` cross-validation:
   - ``spending_base="distributable_income"`` without
     ``distribution_window_quarters`` → fails loudly
   - ``spending_base="distributable_income"`` without
     ``bootstrap_distributable_income_usd`` → fails loudly
   - ``distribution_window_quarters`` with any other ``spending_base``
     → fails loudly (analog of Phase 12's
     ``spending_base_weights`` discipline)
   - ``bootstrap_distributable_income_usd`` with any other
     ``spending_base`` → fails loudly
   - ``spending_base="distributable_income"`` with valid window +
     bootstrap → validates

Ledger flow type (2):

4. ``ledger.add(flow_type="distribution_inflow", amount_usd=1.0,
   bucket="cash", source="re_stabilized:building_a")`` succeeds;
   row appears in ``closed_through`` after finalize; chain extends
   cash NAV by +$1.
5. Negative or zero ``amount_usd`` for ``distribution_inflow``
   fails at ledger-add time.

Base computation (4):

6. **Default-off byte-stability**: ``spending_base=None`` produces
   trajectories byte-identical to a Phase-12 baseline (the existing
   239-test suite passes unchanged).
7. **Distribution rows summed; NAV-only buckets excluded**: a
   ledger seeded with $1M / $0.5M / $0.7M / $0.3M of
   ``distribution_inflow`` across q0..q3 PLUS arbitrary NAV across
   buckets → trailing-4q sum = $2.5M; NAV is irrelevant.
8. **PE distribution explicitly excluded**: the same ledger plus
   $5M of ``pe_distribution`` rows → trailing
   ``distributable_income`` base remains $2.5M (PE rows do NOT
   leak in).
9. **By-source rollup correct**: the same ledger with mixed sources
   → ``by_source_usd`` reports per-source totals matching the seed
   data.

Bootstrap path (2):

10. **Insufficient-history bootstrap**: a run age of 2 quarters
    with ``window_quarters=4`` returns
    ``base_usd == bootstrap_distributable_income_usd`` AND
    ``is_bootstrap == True``. The diagnostic surfaces "year 1 used
    bootstrap."
11. **Window-completion handoff**: a run age of exactly
    ``window_quarters`` switches to realized; ``is_bootstrap``
    flips False; the realized trailing sum is used.

Zero-income guard (1):

12. **Realized zero after bootstrap window** → ``OwlRule`` raises
    ``ValueError`` with the named remediation paths in the message.

End-to-end (1):

13. **Report renders the third advisory mode** for an
    ``spending_base="distributable_income"`` run; section is
    present, surfaces by-source rollup, dual rates, regime
    classification; STRONG WARNING fires when
    ``rate >= 1.00``.

### What Phase 12.5 is **not**

* **Not a producer for ``distribution_inflow`` rows.** Phase 13 +
  Phase 14 land producers (RE+OpCo pipeline; cash-flow workbook
  ingestion). Phase 12.5 is the seat, not the feeder.
* **Not a recurring/one-time classifier.** All
  ``distribution_inflow`` rows count toward the trailing window.
  Phase 13.x may subdivide.
* **Not a PE distribution opt-in.** Excluded by default; future
  phase may flag.
* **Not a cash-flow workbook ingestion.**
* **Not an entity schema.**
* **Not a real-estate / OpCo pipeline.**
* **Not a Monte Carlo / stochastic upgrade.**
* **Not a regime-dependent returns layer.**
* **Not a fee economics change.**
* **Not a secondary-sale model.**
* **Not an allocator / rebalancer change.**
* **Not a non-Owl rule change.** flat_real and smoothing remain
  rate-free; the new flow type does not interact with them.
* **Not a backwards-compatibility break.** Every Phase 12 fixture,
  config, and trajectory remains byte-identical.

### L19 status under Phase 12.5

> **Reviewer tightening 4 — do not overclaim.** Phase 12.5 lands
> *infrastructure*: a flow type, a base computation, schema and
> cross-validation, report diagnostics, and tests. It does **not**
> land a producer that emits realized ``distribution_inflow`` rows
> for the SFO. Until Phase 13 (RE+OpCo pipeline) and Phase 14
> (cash-flow / entity ingestion) commit producers, **the model
> still cannot actually know the household's distributable income**
> in a production run; it can only consume rows a future producer
> will emit. Marking L19 fully RESOLVED on Phase 12.5 alone would
> overclaim the SFO realism gain.

L19 stays at ``[PARTIALLY RESOLVED]`` after Phase 12.5
implementation. The limitations-table wording reads exactly:

```
L19 — PARTIALLY RESOLVED, Phase 12 + Phase 12.5.
Spending-rule denominator infrastructure complete:
  base-side (Phase 12, 92c327d) + flow-side (Phase 12.5).
Production distributable-income realism remains dependent on
Phase 13 (RE+OpCo pipeline) + Phase 14 (cash-flow / entity
ingestion) producers.
```

Equivalently for short-form references:
``L19 infrastructure resolved; production-input realism still
producer-dependent.``

With the explicit notes:

* **Base-side infrastructure** (Phase 12, shipped): four
  configurable modes against NAV views. Default-off byte-stable.
* **Flow-side infrastructure** (Phase 12.5): ``distributable_income``
  mode reads realized ``distribution_inflow`` rows over a trailing
  window. Default-off byte-stable. Pure function preserves Phase
  4a contract. **Phase 12.5 does not determine legal / tax /
  entity-governance distributability** (reviewer tightening 1) —
  it consumes upstream-classified rows.
* **Producer-deferred**: production runs require Phase 13 / 14 to
  emit ``distribution_inflow`` rows. Phase 12.5 ships the seat;
  the realized base is unusable in production until producers land
  and the runtime guard fails loudly when used without rows.
  Research / synthetic-fixture runs are fully usable in 12.5.
* **L19 flips to RESOLVED only after Phase 13 + Phase 14
  producers exist and the SFO can run end-to-end on real
  household income data**. That is the criterion for closing L19
  in full — not the existence of a flow type and a helper.
* The standing principle ("NAV is not liquidity / Appraisal value
  is not spending capacity / Development+land value is not
  distributable income / OpCo value is not automatically portfolio
  liquidity") is honored on every spending lane: rebalance side
  via L8 (Phase 8); spending-base infrastructure via L19 (Phase 12
  + 12.5); spending-base production-input realism remains the open
  Phase 13/14 work.

### Locked design choices

* New ledger flow type ``distribution_inflow`` added to
  ``FLOW_ORDER`` between ``external_inflow`` and ``pe_call``.
* ``distribution_inflow`` is always emitted on the cash bucket
  with ``amount_usd > 0``; ``source`` string identifies origin
  (RE / OpCo / dividend / interest).
* PE distributions (``pe_distribution`` rows) are **not** included
  in the rollup; future opt-in.
* ``GuardrailConfig.distribution_window_quarters`` Literal-style
  ``int | None`` in [1, 20]; required iff
  ``spending_base == "distributable_income"``.
* ``GuardrailConfig.bootstrap_distributable_income_usd`` strictly
  positive ``float | None``; required iff
  ``spending_base == "distributable_income"``.
* Default trailing window = **4 quarters (TTM)**. Annualized-
  latest-quarter explicitly rejected as too noisy.
* q0 / insufficient-history → use bootstrap value; both initial
  and current denominators consult the same path so the runtime
  guard sees consistent state.
* Window-completion is a **step function**: the year the realized
  window first covers ``window_quarters`` switches off bootstrap.
  ``is_bootstrap`` flag surfaces this in the diagnostic.
* Zero realized trailing sum after the bootstrap window has elapsed
  → ``OwlRule`` raises ``ValueError`` (no silent fallback).
* ``compute_spending_base`` extended with ``ledger`` /
  ``prior_quarter`` / window / bootstrap kwargs (additive,
  default-off).
* ``SpendingBaseBreakdown`` extended with
  ``distributable_income_by_source_usd`` (default ``{}``) and
  ``is_bootstrap`` (default ``False``).
* ``OwlRule.diagnostics()`` extended with five new fields
  (additive on Phase 12).
* New report-render mode for the ``distributable_income`` base
  surfaces by-source breakdown, dual withdrawal rates, regime,
  and the bootstrap-vs-realized history.
* Source-string format is **policy guidance, not strict
  validation**; controlled vocabulary deferred to Phase 13.
* Producer is out of scope: zero ``distribution_inflow`` rows in
  any existing fixture; production runs that select the mode
  without a populated window fail the runtime guard loudly.
* L19 stays at **PARTIALLY RESOLVED** after Phase 12.5
  implementation (reviewer tightening 4). Status text:
  "spending-rule denominator infrastructure complete; production
  distributable-income realism remains producer-dependent
  (Phase 13/14)." L19 flips to RESOLVED only after producers land.
* Phase 12.5 does NOT determine legal / tax / entity-governance
  distributability (reviewer tightening 1). The producer is
  responsible for upstream classification.
* Recommended source convention (reviewer tightening 2):
  ``distribution:<domain>:<entity_or_asset_id>`` — documented but
  not enforced. Examples: ``distribution:real_estate:<asset>``,
  ``distribution:opco:<entity>``, ``distribution:portfolio:<bucket>``,
  ``distribution:entity:<entity>``.
* Recurring-vs-one-time disclaimer (reviewer tightening 3) is
  rendered as a permanent CAVEAT line in the
  ``distributable_income`` advisory section so a high base is not
  silently mistaken for stable recurring yield.
* Default-off byte-stability for every existing fixture, config,
  and 239-test run.
* Owl-only — flat_real / smoothing have no rate concept.

---

## Phase 13 design (pre-implementation) — RE / OpCo distribution_inflow producer

> **One-line goal.** Implement the **producer-side** layer that
> emits ``distribution_inflow`` ledger rows from real estate, OpCo,
> land, development, portfolio income, and entity-level
> distributions. Closes the consumer-producer loop opened by
> Phase 12.5: Owl can now run end-to-end on
> ``spending_base="distributable_income"`` with hand-authored or
> config-driven family-office income data. **Strictly producer-side;
> does NOT change Owl math, the ledger schema (beyond adding rows),
> the rate-band logic, or any allocator / rebalancer behavior.**
> Preserves the Phase 4a closed-prior-quarter state-flow contract.
>
> **Phase 13 is config-driven, not workbook-driven.** It defines a
> Pydantic ``DistributionProducerConfig`` that the user (or a future
> ingestion phase) populates with classified distribution events.
> The producer reads the spec and emits ``distribution_inflow``
> rows during the orchestrator's per-quarter loop. **Workbook
> ingestion (``Cashflow Modeling v7.xlsx`` and entity schema) is
> Phase 14** and is explicitly out of scope here.

### Required scope tightening — Phase 13 is the *config-driven producer* only

| Concern | In scope? | Why |
| --- | --- | --- |
| ``DistributionProducerConfig`` Pydantic schema | yes | The structured input the producer consumes |
| ``DistributionProducer`` ABC + ``ConfigDrivenProducer`` adapter | yes | Matches the existing adapter governance contract |
| Per-domain emission rules (RE / OpCo / land / development / portfolio / entity) | yes | Codifies the standing-principle audit at the producer layer |
| Source convention enforcement at emit time | yes | Locks the Phase 12.5 reviewer-tightening-2 convention into machine-checkable code |
| ``restricted=True`` filtering | yes | Producer-side gate before rows hit the ledger |
| Recurrence + confidence as diagnostic metadata (no ledger schema change) | yes | Captured in producer-side diagnostics, surfaced in report |
| Orchestrator wiring of the producer into the per-quarter loop | yes | Producer must run before the spending rule reads `closed_through(q-1)` for q+1 |
| Workbook ingestion (``Cashflow Modeling v7.xlsx``) | **no — Phase 14** | Workbook → spec is downstream |
| Entity ownership waterfall | **no — Phase 14** | The spec author classifies upstream |
| Tax modeling / withholding | **no** | Producer trusts upstream classification per Phase 12.5 reviewer tightening 1 |
| Legal distributability engine | **no** | Same as above |
| PE distribution opt-in | **no — Phase 13.x or later** | PE distributions remain excluded by default per Phase 12.5 |
| Monte Carlo / scenario regimes | **no — L2** | Deferred per `PROJECT_SCOPE.md` §6 |
| New ledger schema | **no** | Producer emits rows; ledger row layout unchanged |

Phase 13 closes the **producer-side seat** for distribution income.
Phase 14 closes the **workbook-ingestion + entity-schema** seat that
populates the producer config from real SFO data. L19 stays at
PARTIALLY RESOLVED until Phase 14 — see "L19 status" below.

> **Reviewer tightening 1 — cash movement / source-of-cash
> boundary.** Phase 13 emits ``distribution_inflow`` rows that
> represent **income recognition into modeled cash**. It does
> NOT model the upstream account-/entity-level cash mechanics
> that move dollars from origin to the family-office liquidity
> pool. Phase 13 treats configured entries as **already
> approved, distributable, and payable** to the modeled
> liquidity pool. It does not determine whether the cash
> originated in a project LLC, OpCo, trust, or holding entity,
> nor does it model inter-entity transfer mechanics (declarations,
> approvals, withholding, distribution waterfalls,
> trust-payout calendars, governance approvals, banking
> settlements).
>
> That work — the *cash-movement engine* and the **entity /
> account ownership graph** — sits at Phase 14+ (cash-flow
> ingestion + entity schema). The Phase 12.5 reviewer-tightening-1
> posture stands at the producer side: Phase 13 trusts the
> upstream classification it receives. The standing-principle
> audit table (below) captures *what is asserted* by the producer;
> it does not assert *how the cash arrived in the household*.

### Diagnosis

The Phase 12.5 boundary (commit ``9e77fb1``):

```text
Phase 12.5 lands the consumer-side seat for distributable_income.
It does not land the producer.
```

Production runs that select ``spending_base="distributable_income"``
fail Phase 12.5's zero-income runtime guard because no
``distribution_inflow`` rows are emitted by anything in the
orchestrator. The bootstrap path covers q0 + insufficient-history
runs, but once the realized window has elapsed the household
appears to have zero distributable income — which is correct given
no producer, but blocks any non-bootstrap research run.

Phase 13 fills that gap by introducing a deterministic, config-driven
producer that emits classified ``distribution_inflow`` rows during
the per-quarter loop.

### Producer ABC + adapter governance

Matches the existing five-family adapter pattern (AllocationAdapter,
ImplementationAdapter, SpendingRule, PEAdapter — and now
DistributionProducer):

```python
# src/aa_model/producers/distribution.py

class DistributionProducer(ABC):
    """Emits distribution_inflow rows for a given quarter.

    Pure: emit_for_quarter(q) is a deterministic function of the
    producer's config + q. No ledger reads. No mutation. Idempotent.

    Phase 4a state-flow contract: emissions for quarter q are
    consumed by the spending rule at quarter q+1 via
    closed_through(q). The producer therefore must complete its
    quarter-q emissions BEFORE the orchestrator advances to q+1.
    The orchestrator wires this in the per-quarter loop.
    """

    @abstractmethod
    def emit_for_quarter(
        self, quarter: pd.Period
    ) -> tuple[
        list[DistributionEmission],
        DistributionProducerDiagnosticsDelta,
    ]:
        ...


@dataclass(frozen=True)
class DistributionEmission:
    amount_usd: float          # > 0; producer enforces
    source: str                # distribution:<domain>:<id>
    # Producer-side metadata kept here for the diagnostics path; the
    # ledger row written from this emission carries only
    # (amount_usd, source, bucket="cash") so the ledger schema is
    # unchanged.
    domain: str
    recurrence_type: str       # "recurring" | "one_time"
    confidence: str            # "contractual" | "forecast" | "scenario"
    producer_id: str           # spec entry id


@dataclass(frozen=True)
class DistributionProducerDiagnosticsDelta:
    """Per-quarter contributions; the orchestrator accumulates these
    into a run-level DistributionProducerDiagnostics."""
    emitted_by_domain_usd: dict[str, float]
    emitted_by_source_usd: dict[str, float]
    emitted_by_recurrence_usd: dict[str, float]   # "recurring" / "one_time"
    emitted_by_confidence_usd: dict[str, float]   # "contractual" / "forecast" / "scenario"
    excluded_restricted_count: int
    excluded_restricted_usd: float


class ConfigDrivenProducer(DistributionProducer):
    """Reads a DistributionProducerConfig and emits the entries
    matching the requested quarter. The user (or a future workbook
    ingestion in Phase 14) populates the config; no ledger reads
    here, no entity schema, no tax model.
    """
```

The orchestrator constructs the producer via a factory that mirrors
``make_allocator`` / ``make_pe_adapter``:

```python
producer = make_distribution_producer(
    cfg.distribution_producer,
    engine=cfg.base.distribution_producer.engine,
)
```

For Phase 13 ``engine`` is fixed at ``"config"`` (the only available
adapter). Phase 14 introduces ``"workbook"``.

### ProducerSpec schema

```python
class DistributionEntryConfig(BaseModel):
    """One classified distribution event."""

    model_config = _STRICT
    producer_id: str = Field(min_length=1)            # spec-row id; globally unique
    domain: Literal[
        "real_estate",
        "opco",
        "land",
        "development",
        "portfolio",
        "entity",
    ]
    entity_id: str = Field(min_length=1)
    asset_id: str | None = None                       # optional finer grain
    quarter: str                                       # "2026Q1" — Period-parseable
    amount_usd: float = Field(gt=0.0)
    recurrence_type: Literal["recurring", "one_time"]
    confidence: Literal["contractual", "forecast", "scenario"]
    restricted: bool = False
    source_reference: str | None = None                # human note; not consumed

    # Hard validators (per-entry):
    # - quarter parses cleanly via pd.Period(..., freq="Q-DEC")
    # - amount_usd > 0 + finite
    # - producer_id is non-empty + URL-safe (no colons — colons are
    #   reserved for the source convention separator)


class DistributionProducerConfig(BaseModel):
    """Producer-level config. Loaded as a sub-config under StudyConfig
    via the same _SubConfigRef pattern (e.g., configs/distribution_producer.yaml).
    """
    model_config = _STRICT
    entries: list[DistributionEntryConfig]

    # Cross-entry validators:
    # - producer_id is globally unique across entries
    # - domain-recurrence sanity:
    #   * domain="development" + recurrence_type="recurring" → ValidationError
    #     (development real estate by definition does not yield recurring
    #      operating distributions; if the user wants to claim it does,
    #      they should reclassify as domain="real_estate" with the
    #      stabilized building's entity/asset id)
    #   * domain="land" + recurrence_type="recurring" → ValidationError
    #     (land does not generate recurring distributable cash short of
    #      an explicit agricultural / extraction lease — Phase 13 treats
    #      land as one-time-monetization-only)
```

The schema is deliberately simple. The user (or future workbook
ingestion) is responsible for **upstream classification** —
reviewer tightening 1 of Phase 12.5 stands: Phase 13 trusts the
classification it receives. Hard validators catch only the
self-evidently nonsensical (development + recurring; land +
recurring); everything else is a config truthfulness problem the
spec author owns.

### Per-domain emission rules (HARD vs SOFT)

Each domain entry in the spec follows the same two-step gate:

1. **HARD gates** (validation-time, fails loud at config load):
   - ``producer_id`` globally unique
   - ``producer_id`` URL-safe (no colons; colons reserved for the source convention)
   - ``amount_usd`` strictly positive + finite
   - ``quarter`` parses as a valid Q-DEC Period
   - Domain × recurrence sanity (development/recurring; land/recurring → fail)

2. **EMISSION-TIME gates** (per-quarter, applied by the producer):
   - ``restricted=True`` → skip emission; log into
     ``excluded_restricted_count`` / ``excluded_restricted_usd``
   - All other entries matching ``quarter`` → emit ``DistributionEmission``
     with ``source = f"distribution:{domain}:{asset_id or entity_id}"``

| Domain | Recurring allowed? | One-time allowed? | Standing-principle alignment |
| --- | :---: | :---: | --- |
| `real_estate` | yes | yes | Stabilized RE producing recurring rent satisfies "income-producing NAV"; one-time = property-level distribution event (e.g., refi proceeds passed through) |
| `opco` | yes | yes | OpCo with explicit dividend / distribution schedule = recurring; OpCo with declared one-time return-of-capital = one_time. Both require upstream classification (the spec author says "this is distributable to the FO liquidity pool"). |
| `land` | **no** | yes | Land has no recurring yield (Phase 13 hard-rejects ``recurring`` for land); only one-time monetization (sale proceeds, agricultural lease bonus payment) qualifies. |
| `development` | **no** | yes | Development has no recurring yield until the project stabilizes; only one-time events (refi, sale, capital event) qualify. After stabilization, the asset graduates to ``real_estate`` in the spec. |
| `portfolio` | yes | yes | Recurring = dividends / interest; one-time = special dividend / capital gain distribution. |
| `entity` | yes | yes | Recurring = scheduled trust distribution; one-time = entity-level capital transfer to FO. The spec author classifies legally; Phase 13 trusts the classification. |

The validation table is reproduced in the producer module's
docstring so a future contributor can audit it without reading
the design block.

### Source convention enforcement at emit time

The Phase 12.5 reviewer-tightening-2 convention is:

```text
source = "distribution:<domain>:<entity_or_asset_id>"
```

Phase 13 enforces this at emit time. The producer constructs:

```python
ident = entry.asset_id if entry.asset_id is not None else entry.entity_id
source = f"distribution:{entry.domain}:{ident}"
```

If ``entry.entity_id`` or ``entry.asset_id`` contains a colon
(would break the parse), the schema-level URL-safety validator
already rejected it. The producer therefore emits a parseable
source string deterministically.

This locks the Phase 12.5 documented-but-unenforced convention
into machine-checkable code. Phase 12.5's ``compute_distributable_income_base``
groups by source for the by-source rollup; with Phase 13, every
rollup key now follows the canonical convention exactly.

> **Reviewer tightening 2 — duplicate (source, quarter) pairs are
> allowed.** Multiple ``DistributionEntryConfig`` entries may emit
> the same ``distribution:<domain>:<id>`` source string in the same
> quarter. This is **legitimate and expected** — the same building
> may declare a recurring quarterly rent distribution PLUS a
> one-time refi-proceeds distribution in the same quarter, or an
> OpCo may declare a regular operating distribution PLUS a special
> dividend, both routed to the same source key. Enforcing
> uniqueness on ``(source, quarter)`` would force such cases into
> awkward synthetic separations.
>
> The audit key is **``producer_id``**, not ``(source, quarter)``.
> ``producer_id`` is required to be globally unique across the
> entire spec (validated at schema-load time). Each emitted
> ``DistributionEmission`` carries its originating ``producer_id``
> on the producer-diagnostics side, so the audit trail from
> ledger-row → producer-config-entry remains intact even when
> multiple emissions share a source.
>
> Phase 12.5's ``compute_distributable_income_base`` sums by
> source, so multiple same-source-same-quarter rows contribute
> additively to the by-source rollup — semantically correct
> ("$X from this origin this quarter") without losing
> information. The ledger itself stores each emission as its own
> row; the per-row audit chain is preserved by the source-string +
> quarter + amount tuple, with ``producer_id`` available on the
> producer-diagnostics side for full traceability.

### Restricted handling

``restricted=True`` is the producer-side gate that captures
"this distribution exists in the underlying entity's books, but is
not distributable to the FO liquidity pool." Examples:

* RE-LLC retention (rent collected at the property, retained for
  capex reserve, not distributed to the FO)
* OpCo working-capital retention
* Trust distribution scheduled but legally restricted (CRUT
  payout calendar, gift-tax exclusion ceiling)
* Entity cash earmarked for an out-of-FO obligation

Restricted entries are **filtered at emit time** — never written
to the ledger. They appear only in
``excluded_restricted_count`` / ``excluded_restricted_usd`` for
the producer-diagnostics report.

This preserves Phase 12.5 reviewer tightening 1 (Phase 12.5 does
not determine distributability; Phase 13 also does not — the spec
author marks ``restricted`` upstream, and the producer obeys).

### Recurrence + confidence handling — no ledger schema change

The ledger row schema (`quarter, bucket, flow_type, amount_usd,
source, run_id`) is unchanged. Recurrence and confidence are
captured in the **producer-side diagnostics** dataclass and
surfaced via the report — not as ledger columns and not as
source-string suffixes (the convention stays single-id-slot per
Phase 12.5 tightening 2).

The diagnostic surface (``DistributionProducerDiagnostics`` —
accumulated by the orchestrator across all quarters):

```python
{
    "emitted_by_domain_usd": {"real_estate": ..., "opco": ..., ...},
    "emitted_by_source_usd": {"distribution:real_estate:bldg_a": ..., ...},
    "emitted_by_recurrence_usd": {"recurring": ..., "one_time": ...},
    "emitted_by_confidence_usd": {"contractual": ..., "forecast": ..., "scenario": ...},
    "excluded_restricted_count": <int>,
    "excluded_restricted_usd": <float>,
    "top_3_source_concentration_pct": <float>,
    "one_time_share_pct": <float>,
}
```

### Orchestrator integration

Per-quarter loop additions (additive only; existing flow
unchanged):

```text
for q in horizon:
    # ... existing inflow / return / pe / spending steps ...

    # Phase 13: distribution_inflow emissions for this quarter.
    # FLOW_ORDER places distribution_inflow between inflow and
    # return, so canonical sort handles intra-quarter ordering
    # regardless of when add() is actually called inside this
    # iteration.
    emissions, dprod_delta = producer.emit_for_quarter(q)
    for em in emissions:
        ledger.add(
            quarter=q,
            bucket="cash",
            flow_type="distribution_inflow",
            amount_usd=em.amount_usd,
            source=em.source,
        )
    distribution_producer_diagnostics.merge(dprod_delta)

    # ... existing rebalance / transaction_cost steps ...
```

The producer runs **once per quarter**, deterministically, with no
ledger reads. The Phase 4a state-flow contract is preserved: the
spending rule at quarter q+1 reads ``closed_through(q)``, which by
that point includes all of quarter-q's distribution_inflow rows.

The orchestrator's existing total-NAV-conservation check
(``validate()``) already includes ``distribution_inflow`` in the
contributing-flows set as of Phase 12.5 — no further ledger change.

### State-flow contract — explicit preservation

Phase 4a contract (read-only, closed-prior-quarter, no mutation,
q0 owns init):

1. ``ConfigDrivenProducer.emit_for_quarter`` reads only the static
   producer config — no ledger reads of any kind.
2. The function is pure; mutates nothing.
3. Producer-side state is held in a per-run
   ``DistributionProducerDiagnostics`` accumulator that the
   orchestrator owns; the producer itself returns a pure delta
   per quarter and holds no module-level state.
4. q0 path is untouched. Producer emits q0 rows during the q0
   iteration; they appear in ``closed_through(q0)`` for the q1
   iteration just like any other ledger row.

Producer is therefore safe to run inside the deterministic
single-pass loop; no within-quarter feedback, no fixed-point, no
sidecar.

### Default-off byte-stability

* ``cfg.distribution_producer is None`` (or
  ``DistributionProducerConfig(entries=[])``) ⇒ producer emits
  zero rows ⇒ Phase 12.5 trajectories byte-identical.
* All existing tests pass unchanged.
* The ``StudyConfig`` cross-validator gains nothing new for Phase
  13 — ``distribution_producer`` is optional, defaulting to None.
* Phase 12.5's ``spending_base="distributable_income"`` runs that
  previously hit the zero-income runtime guard now have a path
  forward: configure a producer, populate entries, run.
* Owl math is **completely unchanged** by Phase 13. The producer
  just feeds the same flow type Phase 12.5 already consumes.

### Diagnostics + report rendering

New report section ``## Distribution producer (advisory)``,
gated on ``cfg.distribution_producer is not None`` AND
producer emitted at least one row:

```markdown
## Distribution producer (advisory)

- emissions by domain (run total):
  - real_estate: $ 4,800,000
  - opco:        $ 1,200,000
  - portfolio:   $   400,000
- emissions by recurrence type:
  - recurring:   $ 5,800,000   (89%)
  - one_time:    $   600,000   (11%)
- emissions by confidence:
  - contractual: $ 5,200,000   (80%)
  - forecast:    $ 1,200,000   (18%)
  - scenario:    $   200,000   ( 2%)
- top-3 sources (by USD, run end):
  - distribution:real_estate:bldg_a:    $ 2,800,000
  - distribution:real_estate:bldg_b:    $ 1,800,000
  - distribution:opco:liv_holding:      $   800,000
- excluded (restricted=True):
  - count: 3 entries
  - dollars: $ 1,400,000
- regime:
  - "producer-feed active (config-driven)"
  - WARNING: top-3 sources account for 80%+ of emissions —
    high concentration in trailing-income base.
  - INFO: 18% of emissions are forecast confidence; 2% are
    scenario confidence — review producer config before
    relying on the trailing-income rate.

_Phase 13 implements the config-driven producer for
distribution_inflow rows. Workbook-driven ingestion of real SFO
income data lands in Phase 14. The producer trusts upstream
classification (legal/tax/entity-governance distributability,
recurring vs one-time, restricted flag) per Phase 12.5 reviewer
tightening 1; it does not determine distributability of its own._
```

Warning bands:

| Trigger | Threshold | Severity |
| --- | --- | --- |
| ``one_time_share_pct`` | ``>= 0.30`` | WARNING — trailing income materially dependent on one-time flows |
| ``top_3_source_concentration_pct`` | ``>= 0.80`` | WARNING — concentration risk; rate-band reads against a narrow base |
| ``forecast + scenario`` share | ``>= 0.20`` | WARNING — material non-contractual emissions; confirm spec |
| ``excluded_restricted_count`` | ``> 0`` | INFO — restricted entries surfaced for transparency |

Producer-side warnings are advisory only — they do not gate Owl's
trajectory. They DO compose with Phase 12.5's existing
``## Owl spending base (advisory)`` section: the report renders
both, and a reader gets the consumer-side view (rate vs base,
by-source rollup) AND the producer-side view (emissions by
domain / recurrence / confidence, restricted exclusions).

The Phase 12.5 standing CAVEAT line on recurring-vs-one-time
remains in the consumer-side section; the producer-side section
adds **quantification** of that caveat (the share is now visible).

### Tests planned (14)

Schema (4):

1. ``DistributionEntryConfig`` accepts a valid full-fields entry;
   rejects ``amount_usd <= 0``, non-finite, and unparseable
   ``quarter``.
2. ``DistributionEntryConfig.producer_id`` URL-safety: rejects
   strings containing colons (would break source-convention
   parsing).
3. ``DistributionProducerConfig`` rejects duplicate ``producer_id``
   across entries.
4. Domain × recurrence sanity:
   - ``domain="development"`` + ``recurrence_type="recurring"`` → fails
   - ``domain="land"`` + ``recurrence_type="recurring"`` → fails
   - All other domain × recurrence combinations validate.

Producer behavior (5):

5. ``ConfigDrivenProducer.emit_for_quarter`` returns the entries
   matching the requested quarter, with sources formatted exactly as
   ``"distribution:<domain>:<asset_id-or-entity_id>"``.
6. ``restricted=True`` entries are filtered at emit time:
   - Not in the returned emissions
   - Counted in ``excluded_restricted_count`` /
     ``excluded_restricted_usd``
7. ``asset_id``-vs-``entity_id`` precedence: when ``asset_id`` is set,
   the source uses asset_id; when it is None, falls back to
   entity_id.
8. Per-quarter purity: ``emit_for_quarter(q)`` returns the same
   emissions on repeated calls; no module-level state.
9. **Duplicate (source, quarter) allowed (reviewer tightening 2)**:
   two entries with the same ``(domain, entity_id, asset_id,
   quarter)`` but distinct ``producer_id`` (e.g., recurring rent +
   one-time refi proceeds from the same building, same quarter)
   both emit. The ledger receives two rows; the by-source rollup
   sums them; ``producer_id`` distinguishes them on the
   producer-diagnostics side.

Orchestrator integration (3):

10. ``make_distribution_producer`` returns a ``ConfigDrivenProducer``
    for ``engine="config"``; rejects unknown engines (Phase 14
    placeholder).
11. End-to-end run: a Phase 13 + Phase 12.5 fixture (4-quarter run,
    real_estate + opco + portfolio entries, no restricted) drives
    Owl with ``spending_base="distributable_income"``; the realized
    trailing income matches the sum of producer emissions; the
    runtime zero-income guard does NOT fire.
12. Default-off byte-stability: ``cfg.distribution_producer = None``
    ⇒ Phase 12.5 trajectories byte-identical; existing 251-test
    suite green.

End-to-end (2):

13. **Producer-diagnostic report renders** with by-domain / by-
    recurrence / by-confidence / top-3 / excluded-restricted
    sections; warning bands fire correctly for high concentration
    and forecast-heavy emissions.
14. **Composes with Phase 12.5 advisory**: the report renders BOTH
    ``## Distribution producer (advisory)`` AND ``## Owl spending
    base (advisory)`` for an Owl run with
    ``spending_base="distributable_income"`` and a populated
    producer config; the by-source rollups in the two sections
    cross-reference cleanly.

### What Phase 13 is **not**

* **Not a workbook ingester.** Phase 14 reads
  ``Cashflow Modeling v7.xlsx`` and builds a
  ``DistributionProducerConfig`` from it. Phase 13 stops at the
  config; whatever populates the config is upstream.
* **Not an entity ownership waterfall.** The spec author classifies
  upstream which distributions reach the FO liquidity pool.
* **Not a tax / withholding model.** Phase 13 trusts upstream
  net-of-tax classifications.
* **Not a legal distributability engine.** Phase 12.5 reviewer
  tightening 1 stands: Phase 13 trusts upstream classification
  too.
* **Not a PE distribution opt-in.** ``pe_distribution`` rows still
  do NOT contribute to the trailing-income rollup; Phase 13.x or
  later may add an opt-in.
* **Not a Monte Carlo / stochastic regime.**
* **Not a scenario-based distribution generator.** The producer is
  deterministic. Stress / scenario overlays may decorate the
  spec at higher-level config layers but the producer itself is
  pure config-driven.
* **Not a fee-economics or secondary-sale model.**
* **Not a ledger schema change.** Recurrence and confidence are
  diagnostic-only and live on the producer side.
* **Not an allocator / rebalancer / PE math change.**
* **Not a non-Owl rule change.** flat_real and smoothing remain
  unaffected.

### L19 status under Phase 13

L19 stays at ``[PARTIALLY RESOLVED]`` after Phase 13 implementation.

Updated wording in the limitations table on Phase 13 implementation:

```
L19 — PARTIALLY RESOLVED, Phase 12 + 12.5 + 13.
Spending-rule denominator infrastructure complete (Phase 12 + 12.5);
config-driven producer for distributable_income shipped (Phase 13).
Workbook-driven realism (Cashflow Modeling v7.xlsx + entity schema)
remains dependent on Phase 14.
```

Equivalently for short-form:
``L19 producer-side seat shipped (config-driven); workbook-driven
realism still Phase 14.``

Why not flip to RESOLVED:

* Phase 13 enables a research-grade or hand-authored production
  run. The model can run end-to-end on a manually populated
  ``DistributionProducerConfig``.
* It cannot yet run on the **real Wake Robin household income
  data** because that data lives in
  ``Cashflow Modeling v7.xlsx`` and the entity chart, neither of
  which is ingested. Phase 14 closes that loop.
* The standing principle ("NAV is not liquidity / Appraisal value
  is not spending capacity / OpCo value is not automatically
  distributable capital / Development+land require separate
  capital-need and monetization assumptions") is now expressible
  AND enforceable per-domain (Phase 13 hard validators), but not
  yet **populated** from real-world data.

L19 flips to RESOLVED only after Phase 14 (workbook + entity
ingestion) lands and the SFO can run end-to-end on real
household income data.

### Standing-principle audit at the producer layer

Each Phase 13 emission rule is auditable against the four-line
principle. Locking the audit table here so the design is not
re-litigated post-implementation:

| Principle | Enforcement at Phase 13 |
| --- | --- |
| NAV is not liquidity. | Producer emits **only** classified distributable cash; never reads NAV; never converts appraisal value to liquidity. |
| Appraisal value is not spending capacity. | No spec entry corresponds to an appraisal mark; appraisal flows are ``pe_nav_mark`` / public-equity ``return`` rows (separate flow types not consumed by ``compute_distributable_income_base``). |
| OpCo value is not automatically distributable capital. | ``opco`` domain entries require explicit upstream classification; restricted=True filters retained capital; forecast/scenario entries are surfaced separately so a paper OpCo dividend doesn't silently become trailing income. |
| Development+land require separate capital-need and monetization assumptions. | ``development`` and ``land`` domains hard-reject ``recurring`` at the schema level; only explicit one-time monetization events qualify. Capital calls / draws are out of scope (Phase 13 emits only inflows; outflows are handled elsewhere). |

### Locked design choices

* New module ``src/aa_model/producers/distribution.py`` with
  ``DistributionProducer`` ABC + ``ConfigDrivenProducer`` concrete.
* New ``make_distribution_producer(cfg, engine="config")``
  factory; ``engine`` Literal will extend in Phase 14
  (``"workbook"``).
* New Pydantic config ``DistributionProducerConfig`` with
  ``DistributionEntryConfig`` entries; loaded as a sub-config
  under ``StudyConfig`` via the ``_SubConfigRef`` pattern;
  defaults to ``None`` (default-off).
* Hard schema validators: ``producer_id`` unique + URL-safe
  (no colons); ``amount_usd`` > 0 + finite; ``quarter`` parses;
  domain × recurrence sanity (development/recurring; land/recurring
  → fail). **Uniqueness is on ``producer_id`` only**; multiple
  entries may share a ``(domain, entity_id, asset_id, quarter)``
  tuple and emit the same source string in the same quarter
  (reviewer tightening 2).
* Source convention enforced at emit time:
  ``f"distribution:{domain}:{asset_id or entity_id}"``. Duplicate
  ``(source, quarter)`` pairs are allowed and sum additively in the
  by-source rollup; ``producer_id`` remains the row-level audit key
  on the producer-diagnostics side.
* ``restricted=True`` filters at emit time; surfaced in
  diagnostics; never reaches the ledger.
* Recurrence and confidence captured **only** in producer
  diagnostics; ledger row schema unchanged.
* Producer is pure: no ledger reads, no module state; per-quarter
  diagnostics returned as a delta the orchestrator accumulates.
* Orchestrator wires the producer once per quarter via the
  per-quarter loop; canonical sort handles intra-quarter ordering.
* Phase 4a state-flow contract preserved verbatim.
* Default-off byte-stability for every existing fixture, config,
  and 251-test run.
* New advisory section ``## Distribution producer (advisory)``
  in ``report.md``, gated on producer configured + at least one
  emission.
* Composes with Phase 12.5's ``## Owl spending base (advisory)``
  rather than replacing it; readers get both consumer- and
  producer-side views.
* PE distributions remain excluded by default (Phase 12.5 stance
  preserved).
* L19 stays at ``[PARTIALLY RESOLVED]`` after implementation;
  Phase 14 (workbook + entity ingestion) is the explicit follow-on
  that flips L19 to RESOLVED.
* Phase 13 does NOT determine legal / tax / entity-governance
  distributability — same upstream-classification posture as
  Phase 12.5 reviewer tightening 1.
* Phase 13 does NOT model the cash-movement / source-of-cash
  mechanics that move dollars from origin (project LLC, OpCo,
  trust, holding entity) to the family-office liquidity pool
  (reviewer tightening 1). Configured entries are treated as
  already approved, distributable, and payable. Inter-entity
  transfer mechanics (declarations, approvals, withholding,
  distribution waterfalls, trust-payout calendars, banking
  settlements) sit at Phase 14+ entity / cash-flow ingestion work.

---

## Phase 14 design (pre-implementation) — cash-flow workbook and entity ingestion

> **One-line goal.** Ingest the operating cash-flow workbook
> (``Cashflow Modeling v7.xlsx``) as a **read-only integration
> target** and produce normalized entity + cash-flow tables that
> downstream layers (Phase 13's ``DistributionProducer``, future
> liquidity-coverage layer, board-snapshot reconciliation) consume.
> Closes the workbook-driven half of L19 — the model can finally
> run end-to-end on the household's actual operating forecast,
> not just on hand-authored producer specs. **Strictly an
> ingestion layer; does NOT change Owl math, the ledger schema,
> the spending-base helpers, the producer ABC, or any allocator /
> rebalancer behavior.** The workbook is **never mutated** —
> Phase 14 has no write paths to the Excel file.

### Required scope tightening — Phase 14 is the *workbook ingestor*, not a tax / legal / waterfall engine

> Phase 14 reads structured Excel and emits normalized model
> tables. It does **not** model:
>
> * Legal distributability of inter-entity cash
> * Tax treatment / withholding / character
> * Ownership waterfall mechanics
> * Whether OpCo cash is "available" to the family office
> * Whether appraisal NAV is spendable
> * Any business rule the workbook does not already classify
>
> Phase 14 is the **plumbing**. The human spec author (in
> ``Cashflow Modeling v7.xlsx``) owns the upstream classification
> — same posture as Phase 12.5 reviewer tightening 1 and Phase 13
> reviewer tightening 1. The workbook's columns mark each line as
> ``distributable_candidate``, ``restricted``, ``recurring`` /
> ``one_time``, etc.; Phase 14 reads the marks and writes the
> normalized rows. It never invents a classification.

| Concern | In scope? | Why |
| --- | --- | --- |
| ``WorkbookIngestor`` reads the Excel via ``openpyxl(read_only=True, data_only=True)`` | yes | Read-only + computed values; no formula evaluation; no mutation |
| Pydantic ``EntityRecord`` + ``CashFlowLineRecord`` schemas | yes | Normalized model input; downstream-stable interface |
| ``WorkbookManifestConfig`` (sheets → roles mapping) | yes | Lets the workbook structure drift without changing the ingestor |
| Period-header parser (multiple Excel formats → ``pd.Period``) | yes | Needed for any quarter-aligned downstream consumer |
| Subtotal / total / blank-row exclusion | yes | Excel realities — a structural ingestion concern |
| Sign-convention normalization | yes | Workbook signs (positive=in / negative=out) → model convention |
| Entity-id normalization | yes | Stable cross-row joining and producer-source bridging |
| Workbook hash + manifest version capture (provenance) | yes | Reproducibility + audit trail |
| Board-snapshot reconciliation diagnostic | yes | Validation against the human's running aggregate |
| Bridge: workbook lines → ``DistributionProducerConfig`` candidate entries | yes | Consumes the Phase 13 producer interface unchanged |
| ``WorkbookDrivenProducer`` adapter (engine="workbook") | yes | Phase 13 ABC commitment fulfilled |
| Legal / tax / entity-governance engine | **no** | Workbook is the upstream classifier |
| Full ownership waterfall | **no** | Out of scope unless design proves required (it does not) |
| Investment Summary workbook ingestion | **no — Phase 15** | Position / account layer is a separate workbook + concern |
| Live values / person-level data in repo docs | **no** | PROJECT_SCOPE.md §5.3 — not committed |
| Mutation of the workbook | **no** | Read-only invariant |
| Monte Carlo / stochastic regime | **no — L2** | Deferred per `PROJECT_SCOPE.md` §6 |
| New ledger flow type | **no** | Workbook lines flow through Phase 13's ``distribution_inflow`` |

### Diagnosis

The Phase 13 boundary (commit ``efbfcf1``):

```text
Phase 13 implements the consumer-side infrastructure plus a
config-driven producer. It does not implement the workbook-driven
producer that ingests the SFO operating forecast.
```

Production runs that select ``spending_base="distributable_income"``
require a populated ``DistributionProducerConfig``. Hand-authoring
the spec is fine for research / synthetic runs but does not match
how the household's forecast actually lives — in
``Cashflow Modeling v7.xlsx``, with quarterly rows per entity,
multi-year horizon, and board-snapshot tabs tracking forecast
revisions. Phase 14 reads that workbook and emits a
``DistributionProducerConfig`` (plus the broader normalized
entity / cash-flow tables that future phases consume).

### Module layout

```text
src/aa_model/ingestion/
    __init__.py            # package marker; minimal exports
    workbook.py            # WorkbookIngestor + the parser primitives
    workbook_producer.py   # WorkbookDrivenProducer (engine="workbook"
                           # adapter satisfying Phase 13's
                           # DistributionProducer ABC)
    schemas.py             # EntityRecord, CashFlowLineRecord,
                           # WorkbookManifestConfig,
                           # IngestionDiagnostics, IngestionResult
```

Adapter governance: Phase 13's existing ``DistributionProducer`` ABC
is the stable interface. ``WorkbookDrivenProducer`` is the second
concrete implementation under that ABC; the Phase 13 factory
(``make_distribution_producer``) gains an ``engine="workbook"``
branch. Consumer-side code (Owl, ``compute_spending_base``, the
report renderer) is **completely unchanged**.

### Read-only / determinism contract

```python
from openpyxl import load_workbook
wb = load_workbook(
    workbook_path,
    read_only=True,    # streaming reader; no in-memory write graph
    data_only=True,    # last-cached computed values; never formulas
    keep_links=False,  # do not follow external workbook links
)
```

* No ``wb.save()``, ``wb.write()``, ``wb.create_sheet()``,
  or any mutation API ever called.
* No formula evaluation; if a cell's cached value is stale (workbook
  not opened + saved by Excel since the last formula edit), the
  ingestor sees the stale value. The diagnostic surfaces a
  warning when key reconciliation cells appear stale (e.g., the
  family-aggregate roll-up mismatches the entity sum by more than
  the documented tolerance).
* No external link following; if a sheet contains ``=[other.xlsx]Sheet!A1``,
  the cached value is read but the external workbook is not opened.
* SHA256 hash of the raw ``.xlsx`` bytes captured before opening
  (``IngestionDiagnostics.workbook_hash``) — provenance + cache key
  for downstream reuse.

> **Reviewer tightening 1 — stale formula / cache risk.** Because
> openpyxl reads with ``data_only=True``, **the ingestor consumes
> the cell values that Excel cached the last time the workbook was
> opened, recalculated, and saved**. If the workbook was edited
> externally (a script, a programmatic edit, another tool) and the
> formulas were never recalculated by Excel, the ingestor will read
> stale cached values without warning. This is an unavoidable
> consequence of the read-only contract — Phase 14 does not
> evaluate formulas because doing so would require either an
> Excel runtime (Windows / COM only) or a third-party formula
> engine (added complexity + correctness risk).
>
> The mitigation is a **standing CAVEAT** in the report's
> ``## Workbook ingestion (advisory)`` section, rendered on every
> ingestion run regardless of whether staleness is detected:
>
> ```text
> CAVEAT: Workbook ingestion uses cached formula values
> (openpyxl data_only=True). If the workbook was edited but not
> recalculated and saved in Excel, ingested values may be stale.
> Open the workbook in Excel, allow it to recalculate, save,
> then re-run ingestion before relying on the output.
> ```
>
> ``IngestionDiagnostics.formula_cache_caveat`` carries the same
> text so a programmatic consumer can surface it equivalently.
> The board-snapshot reconciliation deltas (advisory only — see
> reviewer tightening 3) are the implicit detection signal: if
> aggregates disagree, suspect stale cache first.

### Workbook manifest

The workbook structure can drift (sheet renames, new entities,
period-header format changes). Phase 14 isolates that risk in a
single Pydantic config:

```python
class WorkbookManifestConfig(BaseModel):
    """Maps the workbook's structural layout to the ingestor's
    parser. The manifest is committed; the workbook is not.
    """

    model_config = _STRICT
    # Phase 14 reviewer tightening 2: workbook_version is REQUIRED,
    # URL-safe (no colons; reserved for the Phase 13 source-convention
    # separator), and human-controlled. It anchors deterministic
    # producer_ids (see workbook_lines_to_producer_config below).
    # workbook_hash is captured for provenance but is NOT included
    # in producer_id — hash-based ids would change every workbook
    # edit, breaking cross-run audit. Version-controlled ids are
    # better for board / forecast vintages.
    workbook_version: str = Field(min_length=1)
    expected_workbook_filename: str = Field(min_length=1)
                                         # "Cashflow Modeling v7.xlsx"
                                         # — matched against the supplied
                                         # path's basename for provenance

    @field_validator("workbook_version")
    @classmethod
    def _version_url_safe(cls, v: str) -> str:
        # Same URL-safety discipline as Phase 13 producer_ids /
        # entity_ids — colons are reserved for the Phase 13
        # source-convention separator (distribution:<domain>:<id>),
        # and producer_id concatenates workbook_version with
        # sheet/row/quarter via "__". A colon in workbook_version
        # would silently corrupt the source-convention parse on
        # downstream by-source rollups.
        if ":" in v:
            raise ValueError(
                f"workbook_version must be URL-safe (no colons); got {v!r}"
            )
        return v

    # Sheet roles. Each role maps to a list of sheet names. An
    # ingestion run reads ONLY the sheets named here; sheets not
    # mapped are skipped (and surfaced as
    # IngestionDiagnostics.unmapped_sheets).
    family_aggregate_sheets: list[str]   # validation targets — the
                                         # "Summary" / "Cash Flow" /
                                         # "Assumptions" tabs
    entity_sheets: list[EntitySheetSpec] # one entity per sheet
    re_partnership_sheets: list[REPartnershipSheetSpec]
    board_snapshot_sheets: list[str]     # period-by-period forecast
                                         # snapshots; reconciliation
                                         # targets

    # Parser overrides (defaults documented per sheet role).
    period_header_format: Literal[
        "yyyy_q",       # "2026Q1"
        "q_yy",         # "Q1'26"
        "q_yyyy",       # "Q1 2026"
        "calendar_qe",  # quarter-end date in the header cell
    ] = "yyyy_q"

    # Subtotal / total exclusion patterns. Matched case-insensitively
    # against the row label; rows hitting any pattern are excluded
    # from data emission but counted in
    # IngestionDiagnostics.excluded_subtotal_rows.
    subtotal_label_patterns: list[str] = [
        "total", "subtotal", "sum", "grand total", "net cash",
    ]


class EntitySheetSpec(BaseModel):
    sheet_name: str
    entity_id: str          # canonical id used across model layers
    entity_type: Literal[
        "operating_llc", "holding_llc",
        "trust_crut", "trust_family", "trust_gift", "trust_gst",
        "individual_account",
        "real_estate_partnership",
        "opco",
        "family_aggregate",
    ]
    display_name: str       # human-friendly label; never used as a key
    parent_entity_id: str | None = None
    cash_flow_role: Literal["operating", "investment", "distribution_only"]
    restricted_default: bool = False    # default for rows lacking an
                                         # explicit restricted column
    distributable_default: bool = False  # default for rows lacking an
                                         # explicit distributable column


class REPartnershipSheetSpec(EntitySheetSpec):
    """Partnership-level cash-flow sheets typically have project
    sub-ledgers (per-property rows). Inherits all EntitySheetSpec
    fields plus a per-asset map for per-row asset_id assignment.
    """
    asset_id_by_row_label: dict[str, str] = Field(default_factory=dict)
```

The default manifest committed to the repo is keyed by
``workbook_version="v7"`` and maps to the structural layout the
user described in `PROJECT_SCOPE.md` §5.1. Future workbook
versions get their own manifest; the workbook itself stays out
of git.

### Entity schema

```python
class EntityRecord(BaseModel):
    """One normalized entity row produced by the ingestor."""

    model_config = _STRICT
    entity_id: str = Field(min_length=1)
    display_name: str
    entity_type: Literal[
        "operating_llc", "holding_llc",
        "trust_crut", "trust_family", "trust_gift", "trust_gst",
        "individual_account",
        "real_estate_partnership",
        "opco",
        "family_aggregate",
    ]
    parent_entity_id: str | None = None
    cash_flow_role: Literal["operating", "investment", "distribution_only"]
    source_sheet: str               # workbook sheet name (provenance)
    source_workbook: str            # workbook filename (provenance)

    # Phase 14 reviewer-tightening posture: cash-flow data lives at
    # the cash-flow line layer (see CashFlowLineRecord). The entity
    # record carries STRUCTURAL metadata only — type, parent,
    # cash-flow role. It does NOT carry "distributable to FO" as an
    # entity attribute; distributability is per-line classification.

    @field_validator("entity_id")
    @classmethod
    def _no_colons(cls, v: str) -> str:
        # Same convention discipline as Phase 13 producer ids:
        # colons reserved for the source-string separator
        # (distribution:<domain>:<id>).
        if ":" in v:
            raise ValueError(
                f"entity_id may not contain colons; reserved for source "
                f"convention separator. got {v!r}"
            )
        return v
```

### Cash-flow line schema

```python
class CashFlowLineRecord(BaseModel):
    """One normalized cash-flow line — a single quarter's amount on
    a single row label of a single entity sheet."""

    model_config = _STRICT
    source_workbook: str
    sheet_name: str
    row_label: str
    entity_id: str = Field(min_length=1)
    quarter: str = Field(pattern=r"^\d{4}Q[1-4]$")
    amount_usd: float
    category: str                              # workbook-supplied or
                                                # manifest-derived
                                                # (e.g., "rent",
                                                # "distribution",
                                                # "tax_payment")
    direction: Literal["inflow", "outflow"]
    certainty: Literal["actual", "contractual", "forecast", "scenario"]
    recurrence_type: Literal["recurring", "one_time", "unknown"] = "unknown"
    distributable_candidate: bool = False      # explicit upstream flag
    restricted: bool = False                   # explicit upstream flag
    source_reference: str | None = None        # optional sheet-cell ref
                                                # (e.g., "Summary!B17")

    @field_validator("amount_usd")
    @classmethod
    def _amount_finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError(f"amount_usd must be finite; got {v!r}")
        return v

    @model_validator(mode="after")
    def _direction_sign_consistent(self) -> CashFlowLineRecord:
        # Sign convention check: outflows have amount_usd < 0,
        # inflows have amount_usd > 0. A workbook line with the
        # opposite sign is a config / classification error
        # surfaced during ingestion validation.
        if self.direction == "inflow" and self.amount_usd < 0:
            raise ValueError(
                f"direction='inflow' requires amount_usd >= 0; "
                f"got {self.amount_usd}"
            )
        if self.direction == "outflow" and self.amount_usd > 0:
            raise ValueError(
                f"direction='outflow' requires amount_usd <= 0; "
                f"got {self.amount_usd}"
            )
        return self
```

### Domain-to-model mapping

| Workbook structural domain | Phase 14 ingestion role | Downstream consumer |
| --- | --- | --- |
| Family aggregate roll-ups (Summary / Cash Flow / Assumptions) | parsed for **validation reconciliation only**; not emitted as data rows | Board-snapshot reconciliation (compares model aggregate to workbook aggregate) |
| Operating LLCs | `EntityRecord(entity_type="operating_llc")` + `CashFlowLineRecord` rows for each quarter | future liquidity-coverage layer; some rows become distribution_inflow candidates |
| Holding LLCs | `EntityRecord(entity_type="holding_llc")` + line rows | same |
| Trust vehicles (CRUT / family / gift / GST) | `EntityRecord(entity_type="trust_*")` + line rows; CRUT rows particularly important for payout-calendar diagnostics | distribution_inflow candidates marked at the line layer |
| Individual accounts | `EntityRecord(entity_type="individual_account")` + line rows | terminal beneficiary side; distributions IN to individuals are NOT producer candidates (those are post-FO consumption) |
| Real-estate development partnerships | `EntityRecord(entity_type="real_estate_partnership")` + per-project sub-ledger rows; uses `REPartnershipSheetSpec.asset_id_by_row_label` to assign asset-level identifiers | distribution_inflow candidates with `domain="real_estate"` (stabilized) or `domain="development"` (one-time monetization) |
| Multi-quarter actuals + forecast horizon | `CashFlowLineRecord.certainty` ∈ {actual, contractual, forecast, scenario} based on the quarter's position relative to the run's horizon and any explicit certainty column | producer entries inherit the certainty into the Phase 13 confidence diagnostic |
| Period-by-period board snapshots | parsed into `IngestionDiagnostics.board_snapshots` for reconciliation deltas | report renderer surfaces deltas |

### Workbook → distribution_inflow producer bridge

The ingestor produces ``CashFlowLineRecord`` rows for **every**
parsed line. A separate filter step converts the subset that
qualify into ``DistributionEntryConfig`` entries:

```python
def workbook_lines_to_producer_config(
    lines: list[CashFlowLineRecord],
    entities_by_id: dict[str, EntityRecord],
) -> DistributionProducerConfig:
    """Phase 14 → Phase 13 bridge.

    A line becomes a DistributionEntryConfig entry IFF:
      - line.distributable_candidate == True (explicit flag, NOT inferred)
      - line.restricted == False
      - line.direction == "inflow"
      - line.amount_usd > 0
      - line.recurrence_type ∈ {"recurring", "one_time"}  # not "unknown"
      - the resolved entity has cash_flow_role != "distribution_only"-target
        (i.e., we don't treat distributions INTO an individual as a producer
         entry — those are FO outflows from the family side, not FO inflows)

    Source convention preserved exactly:
      source = "distribution:<domain>:<asset_id-or-entity_id>"

    Domain comes from the entity's entity_type (operating_llc /
    holding_llc / trust_* → "entity" or "opco" depending on
    cash_flow_role); real_estate_partnership rows use the asset_id
    when available, fall back to the entity_id otherwise.

    producer_id is constructed deterministically from
    f"{workbook_version}__{sheet_name}__{row_label}__{quarter}" so
    repeated ingestions of the same workbook produce identical
    producer_ids — important for diff-based change tracking and
    audit trails.
    """
```

The bridge is **a separate function, not a hidden side-effect of
ingestion**. The orchestrator decides whether to consume the
candidate ``DistributionProducerConfig`` (default: use the bridge
output if a workbook is configured; otherwise fall back to the
existing config-driven producer or no producer at all).

### What must NOT be inferred

Codified per the user prompt and the standing principle:

* **Legal distributability.** The workbook's
  ``distributable_candidate`` column captures whether the human
  has classified a line as legally distributable; the ingestor
  reads the column. It does NOT decide.
* **Tax treatment.** No federal / state / withholding logic. The
  workbook's amounts are net-of-tax IF the human classified them
  that way; the ingestor does not adjust.
* **Ownership waterfall.** No multi-tier distribution math; no
  preferred returns, no carry, no waterfall splits. If the workbook
  declares a distribution to the FO of $X, the ingestor records $X.
* **Unrestricted access to entity cash.** Cash recorded at an
  operating LLC sheet is NOT inferred to be distributable. Only
  rows where the human marked ``distributable_candidate=True`` are
  bridged to the producer.
* **Whether appraisal NAV is spendable.** The ingestor reads
  cash-flow lines, not balance-sheet appraisals. NAV-side modeling
  remains where Phase 12 + 12.5 left it.
* **Whether OpCo cash is available to the FO.** OpCo cash flows
  recorded on an OpCo sheet are NOT producer candidates by default.
  Only rows the human classified as distributable to the FO qualify.

### Validation rules

| Rule | Behavior |
| --- | --- |
| Workbook path absent / not a file | ``FileNotFoundError`` with the resolved absolute path in the message |
| Manifest expected_version ≠ workbook actual version | Hard error at ingestion start; manifest must be updated to match |
| Manifest expected_workbook_filename ≠ supplied basename | Warning (filename drift); proceed with hash-based provenance |
| Required sheet missing | Hard error; no partial ingestion |
| Optional sheet missing | Warning; recorded in ``IngestionDiagnostics.missing_optional_sheets`` |
| Period header unparseable | Skip column; record in ``IngestionDiagnostics.unparseable_period_headers`` for review |
| Duplicate (entity_id, quarter, row_label) | Hard error; the ingestor refuses to choose between conflicting rows |
| Blank rows | Silently skipped; counted in ``IngestionDiagnostics.blank_rows_skipped`` |
| Subtotal / total rows | Excluded by ``WorkbookManifestConfig.subtotal_label_patterns`` matching; counted in ``IngestionDiagnostics.excluded_subtotal_rows`` |
| Sign convention violation | Hard error at ``CashFlowLineRecord`` construction time (per-row validator) |
| Entity ID has colons | Hard error (Phase 13 source-convention discipline) |
| Quarter outside model horizon | Recorded; downstream consumers may filter via the orchestrator's start-quarter / num-quarters config |
| Workbook hash | SHA256(raw .xlsx bytes) captured into ``IngestionDiagnostics.workbook_hash``; surfaced in the report |
| Manifest version | Captured into ``IngestionDiagnostics.manifest_version`` |
| Stale-formula heuristic | If family-aggregate roll-up cells differ from the entity-sum by more than the documented tolerance, flag in ``IngestionDiagnostics.stale_formula_warnings`` |

> **Reviewer tightening 3 — board-snapshot reconciliation is
> ADVISORY ONLY.** Phase 14 ingestion never fails ingestion or
> blocks a downstream run on a board-snapshot reconciliation
> delta. Reconciliation deltas surface as WARNING entries in the
> report's ``## Workbook ingestion (advisory)`` section and are
> recorded in
> ``IngestionDiagnostics.board_snapshot_reconciliations``. There
> is **no strict-mode flag** in Phase 14; a future phase may
> introduce one if the reviewer judges that hard validation is
> wanted, but it is explicitly out of scope here.
>
> The rationale: reconciliation deltas can stem from (a) stale
> formula cache (reviewer tightening 1), (b) the workbook's
> snapshot-tab maintenance lagging the entity sheets, (c) genuine
> arithmetic discrepancies the human author needs to investigate.
> All three are upstream-author concerns. Phase 14's job is to
> surface them with provenance, not to refuse to run.

### Outputs

```python
@dataclass(frozen=True)
class IngestionResult:
    entities: list[EntityRecord]
    cash_flow_lines: list[CashFlowLineRecord]
    candidate_producer_config: DistributionProducerConfig | None
    diagnostics: IngestionDiagnostics


@dataclass
class IngestionDiagnostics:
    workbook_hash: str                      # SHA256 of the raw .xlsx
    workbook_filename: str                   # supplied basename
    workbook_version: str                    # from manifest expected_version
    manifest_version: str                    # the manifest committed to the repo
    sheets_ingested: list[str]
    unmapped_sheets: list[str]
    missing_optional_sheets: list[str]
    blank_rows_skipped: int
    excluded_subtotal_rows: int
    unparseable_period_headers: list[str]
    stale_formula_warnings: list[str]

    # Reconciliation against the family-aggregate / board-snapshot tabs.
    # Each entry: (snapshot_label, snapshot_total_usd,
    # ingestor_recomputed_total_usd, abs_delta_usd, abs_delta_pct).
    board_snapshot_reconciliations: list[tuple[str, float, float, float, float]]

    # Per-entity totals for the run horizon — used by the report
    # diagnostic and by Phase 14 + Phase 13 cross-validation.
    total_inflows_usd_by_entity: dict[str, float]
    total_outflows_usd_by_entity: dict[str, float]

    # Distribution-candidate breakdown. Counts and dollar totals of
    # lines that qualified for the Phase 13 producer bridge,
    # broken out by domain.
    distribution_candidates_by_domain_usd: dict[str, float]
    distribution_candidates_count: int
    excluded_restricted_count: int
    excluded_restricted_usd: float

    # Lines that didn't fit any mapping rule — surfaced for human review.
    unmatched_lines_count: int
    unmatched_lines_sample: list[str]        # first N row labels for triage
```

### Report diagnostic

New section ``## Workbook ingestion (advisory)`` rendered when a
workbook ingestion ran. Composes with Phase 12.5's
``## Owl spending base (advisory)`` and Phase 13's
``## Distribution producer (advisory)`` so a reader sees the full
provenance chain: workbook → producer → spending base → trajectory.

```markdown
## Workbook ingestion (advisory)

- workbook:
  - filename: Cashflow Modeling v7.xlsx
  - hash: <sha256-prefix>
  - version (manifest): v7
  - manifest version: 1
- sheets:
  - ingested: 14
  - unmapped: 2 ('Notes (DJS)', 'Old')
  - missing optional: 0
- rows:
  - parsed: 1,247
  - blank skipped: 38
  - subtotal excluded: 96
  - unparseable period headers: 0
- per-entity totals (run horizon):
  - <entity-type-aggregate listing>
- board snapshot reconciliation:
  - 'Summary FY2026': model = $X,XXX,XXX  workbook = $X,XXX,XXX  Δ = 0.02% — within tolerance
  - <one row per snapshot tab>
- distribution candidates (bridge to Phase 13 producer):
  - by domain: real_estate / opco / portfolio / entity totals
  - count: N entries
  - excluded (restricted=True): K entries
- unmatched lines:
  - count: 0  (or "M lines — see ingestion log for triage")

_Phase 14 ingests the operating cash-flow workbook as a read-only
integration target. The workbook's classifications
(``distributable_candidate``, ``restricted``, ``recurrence_type``,
``certainty``) flow through to the Phase 13 producer unchanged.
Phase 14 does NOT determine legal / tax / entity-governance
distributability; it transcribes the human's classifications._
```

Warning bands:

| Trigger | Threshold | Severity |
| --- | --- | --- |
| Board-snapshot Δ% | ``> 0.5%`` on any reconciliation | WARNING — workbook aggregate disagrees with entity sum; investigate stale formulas |
| ``unmatched_lines_count`` | ``> 0`` | WARNING — lines need a manifest update |
| ``stale_formula_warnings`` | ``> 0`` | WARNING — open + save the workbook in Excel to refresh cached values |
| ``unparseable_period_headers`` | ``> 0`` | WARNING — manifest period_header_format mismatch |

### State-flow contract / Phase 4a preservation

* Ingestion runs **once at orchestrator construction**, before the
  per-quarter loop begins. It does not interact with the ledger
  directly; it produces a ``DistributionProducerConfig`` that the
  Phase 13 producer consumes inside the per-quarter loop exactly
  as it does for hand-authored configs.
* The closed-prior-quarter contract is therefore preserved without
  any new logic — Phase 14 is upstream of the loop, not inside it.
* The ingestor is pure: same workbook + same manifest → same
  ``IngestionResult`` byte-for-byte (modulo the workbook hash,
  which is intentionally a function of the input bytes).

### Default-off byte stability

* If ``cfg.workbook_ingestion is None`` (or the manifest path is
  unset), no ingestion runs. The orchestrator behaves exactly as
  Phase 13 does today: producer is built from
  ``cfg.distribution_producer`` if present, else None.
* Existing fixtures, configs, and 265-test trajectories remain
  byte-identical post-Phase-14.
* Workbook ingestion is opt-in via a top-level ``StudyConfig.workbook_ingestion``
  field (default ``None``). Tests construct synthetic
  ``CashFlowLineRecord`` lists directly without touching openpyxl.

### Tests planned (12)

> **Reviewer tightening 4 — tests use synthetic workbook fixtures
> ONLY.** Phase 14 tests construct their own minimal openpyxl
> workbooks programmatically at test time (in ``tmp_path``) using
> the openpyxl write API, then exercise the ingestor against
> those synthetic fixtures. **No real workbook rows, live values,
> sheet extracts, person names, entity names, or forecast tables
> are committed into ``tests/`` or anywhere else in the repo.**
> The real ``Cashflow Modeling v7.xlsx`` is used for local
> validation runs only — never for committed test data. This
> mirrors the PROJECT_SCOPE.md §5.3 discipline: live data stays
> outside the repo.
>
> The test file accordingly avoids any committed binary fixture
> files. Synthetic builders live in the test module itself
> (``tests/test_phase14_workbook_ingestion.py``); each test
> constructs the workbook it needs, exercises the ingestor, then
> cleans up via ``tmp_path``.

Schema (3):

1. ``EntityRecord`` rejects entity_id with colons; accepts the full
   ``entity_type`` Literal set.
2. ``CashFlowLineRecord`` sign-convention validator: inflow + negative
   amount → fail; outflow + positive amount → fail; finite/non-finite
   amount validation.
3. ``WorkbookManifestConfig`` cross-validates: required-sheet entries
   reference distinct sheet names; ``EntitySheetSpec.entity_id`` is
   globally unique across entity_sheets + re_partnership_sheets.

Workbook reading (3):

4. **Workbook absent fails clearly**: a missing path raises
   ``FileNotFoundError`` with the resolved absolute path in the
   message; no openpyxl-internal error leaks.
5. **Fixture workbook parses deterministically**: a synthetic
   ``tests/fixtures/cashflow_v7_synthetic.xlsx`` (committed; no live
   data) parses to the same ``IngestionResult`` across repeated
   reads (hash stable; counts stable; row-by-row equality).
6. **Period headers normalize correctly**: each supported format
   ("yyyy_q", "q_yy", "q_yyyy", "calendar_qe") parses to the
   expected ``pd.Period`` set; unparseable formats land in
   ``IngestionDiagnostics.unparseable_period_headers`` with no
   crash.

Validation rules (3):

7. **Subtotal rows excluded**: rows whose label matches any
   ``subtotal_label_patterns`` entry are not emitted as data rows;
   ``IngestionDiagnostics.excluded_subtotal_rows`` is incremented
   correctly.
8. **Restricted rows excluded from producer bridge**: a fixture row
   with ``restricted=True`` AND ``distributable_candidate=True``
   does NOT become a ``DistributionEntryConfig`` entry; it counts
   in ``excluded_restricted_count`` instead.
9. **Workbook hash + manifest version captured**: the diagnostics
   carry SHA256 of the fixture bytes + the manifest version string;
   re-running on the same bytes produces the same hash.

Bridge to Phase 13 (2):

10. **Explicit distributable rows become producer entries**:
    workbook_lines_to_producer_config converts the qualifying
    fixture lines into a ``DistributionProducerConfig`` whose
    ``producer_id`` values are deterministic and globally unique;
    source-convention strings are well-formed
    (``distribution:<domain>:<id>``).
11. **End-to-end ingestion → producer → Owl**: the synthetic fixture
    drives an Owl run with ``spending_base="distributable_income"``;
    realized trailing income matches the bridged producer emissions;
    no zero-income runtime guard fires.

End-to-end (1):

12. **Workbook ingestion advisory renders** with all sub-sections,
    workbook-hash provenance, board-snapshot reconciliation
    deltas, and warning bands when a synthesized fixture has a
    deliberate stale-formula scenario.

### What Phase 14 is **not**

* **Not a tax engine.** No withholding, no character logic, no
  jurisdictional rules.
* **Not a legal distributability engine.** Workbook-classified is
  the source of truth.
* **Not a full ownership waterfall.** No tier math; no preferred
  returns; no carry.
* **Not a workbook editor.** Read-only; no write paths anywhere.
* **Not an Investment Summary ingester.** That workbook is Phase
  15 (positions, asset-class taxonomy, time-horizon / liquidity
  bucket metadata).
* **Not a board-snapshot generator.** Phase 14 reconciles AGAINST
  board snapshots; it does not produce them.
* **Not a CMA / allocation / PE producer.** Workbook ingestion
  feeds the cash-flow + entity layers (PROJECT_SCOPE.md §3.1, §3.3)
  and bridges to the Phase 13 producer. It does not touch the CMA,
  the allocator, or the PE pacing layer.
* **Not a Monte Carlo / scenario generator.** Forecast / scenario
  classification is preserved as line-level metadata only.
* **Not a fee-economics or secondary-sale model.**
* **Not a backwards-compatibility break.** Default-off
  byte-stable; no existing fixture, config, or trajectory changes.
* **Not a live-data exposure.** Live values, person-level data,
  and forecast tables are NEVER copied into ``MODEL_DOCUMENTATION.md``
  or any committed artifact (PROJECT_SCOPE.md §5.3).

### L19 status under Phase 14

> **Reviewer guidance — do not overclaim.** L19 stays at
> ``[PARTIALLY RESOLVED]`` after Phase 14 implementation alone.
> The conditions for flipping to ``[RESOLVED]`` are conservative:
>
> 1. Workbook-driven ``distribution_inflow`` producer exists and
>    passes the Phase 14 test suite.
> 2. The real ``Cashflow Modeling v7.xlsx`` validates cleanly
>    locally (board-snapshot reconciliation deltas within tolerance,
>    no material unmatched lines, no stale-formula warnings, the
>    SFO end-to-end run succeeds).
> 3. The MODEL_DOCUMENTATION wording makes explicit that legal /
>    tax / entity-governance distributability remains outside model
>    scope.
>
> Even when all three conditions hold, the resolution wording must
> be narrow:
>
> ```
> L19 — RESOLVED for modeled distributable-income ingestion;
> legal / tax / entity-governance distributability remains
> out of scope.
> ```

L19 status text on Phase 14 *implementation alone* (no live-workbook
validation run yet):

```
L19 — PARTIALLY RESOLVED, Phase 12 + 12.5 + 13 + 14.
Spending-rule denominator infrastructure complete (Phase 12 + 12.5).
Producer-side seat shipped: config-driven (Phase 13) and workbook-
driven (Phase 14). Workbook ingestion is implemented and
synthetic-fixture tested; L19 flips to RESOLVED only after a clean
local validation pass against the real Cashflow Modeling v7.xlsx
(board-snapshot reconciliation, no material unmatched lines, no
stale-formula warnings). Legal / tax / entity-governance
distributability remains out of scope and will not be modeled.
```

The Phase 14 implementation commit therefore leaves L19 at
PARTIALLY RESOLVED. Promotion to RESOLVED is **operational** —
follows a successful local validation run, not the implementation
commit itself. A separate docs-only commit can flip the wording
once the reviewer confirms the validation pass.

### Locked design choices

* New package ``src/aa_model/ingestion/`` with ``workbook.py``,
  ``workbook_producer.py``, ``schemas.py``, ``__init__.py``.
* ``WorkbookIngestor`` opens the workbook with
  ``openpyxl(read_only=True, data_only=True, keep_links=False)``;
  no mutation API ever invoked.
* ``WorkbookDrivenProducer`` adapter under the existing Phase 13
  ``DistributionProducer`` ABC; ``make_distribution_producer``
  factory gains ``engine="workbook"`` branch.
* Pydantic ``WorkbookManifestConfig`` committed to the repo;
  workbook itself never committed (PROJECT_SCOPE.md §5.3).
* SHA256 of raw .xlsx bytes captured as
  ``IngestionDiagnostics.workbook_hash`` for provenance; manifest
  version separately tracked.
* ``EntityRecord`` carries structural metadata only (type, parent,
  cash-flow role, source provenance); distributability is
  per-line, not per-entity.
* ``CashFlowLineRecord`` carries explicit upstream classification
  fields (``distributable_candidate``, ``restricted``,
  ``recurrence_type``, ``certainty``); the ingestor never infers
  any of them.
* Period header parser supports four formats; default ``"yyyy_q"``;
  unparseable headers land in diagnostics, never crash.
* Subtotal / total / blank rows excluded by manifest patterns;
  counted in diagnostics; never emitted as data rows.
* Sign-convention enforced at ``CashFlowLineRecord`` construction;
  inflow + negative amount or outflow + positive amount → hard
  error.
* Entity ID URL-safety enforced (no colons; same Phase 13
  discipline).
* Workbook → producer bridge is a **separate, named function**
  (``workbook_lines_to_producer_config``); the ingestor returns
  the broader ``IngestionResult`` and the bridge is invoked
  explicitly by the orchestrator.
* ``producer_id`` for workbook-derived entries is deterministic
  and **uses ``workbook_version`` only**, not ``workbook_hash``
  (reviewer tightening 2): ``f"{workbook_version}__{sheet_name}__{row_label}__{quarter}"``.
  Hash is captured separately for provenance; using it in
  producer_id would break cross-run audit on every workbook edit.
* ``workbook_version`` is REQUIRED on ``WorkbookManifestConfig``,
  URL-safe (no colons), human-controlled (reviewer tightening 2).
* Board-snapshot reconciliation is **advisory only** (reviewer
  tightening 3): abs-Δ% > 0.5% surfaces a WARNING; ingestion never
  fails on a reconciliation delta. No strict-mode flag in Phase 14.
* New report section ``## Workbook ingestion (advisory)``;
  composes with the Phase 12.5 + Phase 13 advisory sections;
  carries a standing CAVEAT line about cached-formula stale-state
  risk (reviewer tightening 1) on every ingestion run.
* ``IngestionDiagnostics.formula_cache_caveat`` carries the same
  CAVEAT text for programmatic consumers (reviewer tightening 1).
* Default-off byte-stability: ``cfg.workbook_ingestion = None``
  ⇒ no ingestion ⇒ Phase 13 trajectories byte-identical.
* Phase 14 does NOT mutate the workbook; does NOT determine
  legal / tax / entity-governance distributability; does NOT
  copy live values into ``MODEL_DOCUMENTATION.md`` or any
  committed artifact.
* **Tests use synthetic workbook fixtures only** (reviewer
  tightening 4). Each test builds its own minimal openpyxl
  workbook in ``tmp_path`` via the openpyxl write API and
  exercises the ingestor against it. The real
  ``Cashflow Modeling v7.xlsx`` is used for local validation only;
  no real rows / values / sheet extracts / person or entity
  names are committed under ``tests/``.
* L19 stays at ``[PARTIALLY RESOLVED]`` after Phase 14
  implementation alone. Promotion to RESOLVED is **operational**:
  requires a clean local validation pass against the real workbook
  AND wording narrowed to "RESOLVED for modeled distributable-
  income ingestion; legal / tax / entity-governance
  distributability remains out of scope." A separate docs-only
  commit performs that flip after the reviewer confirms.

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

### 2026-05-02 — Phase 11 design locked + SFO use-case context + L19 (pre-implementation)

Three coupled doc-only changes that together set the framing for all
future spending- and liquidity-related modeling work.

* **(1) New top-of-doc §Use-case context — Gen3-Gen5 SFO standing
  principle.** Establishes the load-bearing modeling principle:

  > NAV is not liquidity.
  > Appraisal value is not spending capacity.
  > Development / land value is not distributable income.
  > Opco value is not automatically portfolio liquidity.

  Distinguishes total NAV / liquid NAV / income-producing NAV /
  distributable income / locked appraisal NAV / spendable
  resources as **non-interchangeable** concepts. Future phases
  must honor this principle and explicitly call out any place
  they treat total NAV as a proxy for spendable resources.
  Ties the existing L8 (Phase 8 illiquidity overlay) and the new
  L19 (open) to the same governing principle.

* **(2) New L19 (OPEN — modeling): Spending base realism for
  illiquid SFO balance sheets.** Owl currently computes
  withdrawal rate against **total modeled NAV**, including
  illiquid private real estate / opco / land / development carry.
  For a Gen3-Gen5 SFO this may overstate spending capacity by
  2–3×. Future work needed; explicitly out of scope for Phase 11.
  Added to the Limitation status summary table; entry text
  documents what a fix would look like (CMA-side
  ``income_producing`` tag, distributable-income tracking,
  optional ``GuardrailConfig`` field to use spendable-resource
  base instead of total NAV).

* **(3) Phase 11 design (pre-implementation) — L16 Owl
  scale-invariance.** Resolves L16 by adding optional
  ``GuardrailConfig.absolute_min_annual_usd`` and
  ``absolute_max_annual_usd`` fields, default ``None``. When set,
  break the rate-based scale-invariance by clamping the trigger
  output to dollar-denominated thresholds. Trigger logic order:
  rate-band → absolute clamp → existing quarterly floor/ceiling.
  Default-off byte-stability. Owl-only. Static (not inflation-
  adjusted). New ``## Owl scale-sensitivity (advisory)``
  diagnostic in ``report.md`` that classifies each Owl run as
  ``scale-aware`` (absolute clamps active) or ``scale-invariant``
  (no clamps configured), and surfaces the **L19 caveat
  verbatim**: "Phase 11 fixes scale-invariance only — it does
  NOT resolve spending-base realism."

* **Required tightening (per audit, locked verbatim).** Phase 11
  resolves Owl scale-invariance only. It does NOT resolve
  spending-base realism. Total NAV may not equal spendable
  resources for a Gen3-Gen5 SFO. L19 documents the open
  spending-base concern; Phase 11 is scope-bounded against it.
  Future readers should not interpret the Phase 11 ship as "Owl
  is family-office-realistic" — it isn't yet, and the standing
  §Use-case context principle states this explicitly.

* **Tests planned (8)** for Phase 11 implementation: schema
  validation per field (3); default-off byte-stability;
  scale-invariance regression test (the one L16 referenced but
  didn't ship — Phase 11 adds it); scale-divergence under
  absolute floor; cut-path floor binding via prior-spend
  feedback; report diagnostic renders + surfaces the L19 caveat.

* **What Phase 11 is NOT**: spending-base fix; Monte Carlo;
  regime-dependent returns; PE schema change; fee economics;
  secondary-sale model; power-law band scaling; rate-band
  reformulation; allocator / rebalancer / ledger change.

* **L-status implications.** L16 will flip to ``[RESOLVED]`` on
  implementation; L19 stays ``OPEN — modeling``; the "open"
  list in the L-status summary becomes L16 (becomes RESOLVED) /
  L19 (newly opened) / L2 / L5.

* **Impact on outputs.** Design only — none. When Phase 11
  ships, default-config runs are byte-identical (fields default
  to ``None``).

* **Backward-compatible.** Yes (design only).

### 2026-05-02 — Phase 11 implementation: L16 Owl scale-invariance fix

* **What.** Implements the §Phase 11 design locked one commit prior.
  Three surfaces:
  1. **Schema** (`io/schemas.py`). ``GuardrailConfig`` gains optional
     ``absolute_min_annual_usd`` (``ge=0``) and
     ``absolute_max_annual_usd`` (``gt=0``). New ``field_validator``
     rejects non-finite values explicitly (pydantic's ``ge`` / ``gt``
     admit ``inf``, which would silently disable the clamp). New
     ``model_validator`` enforces ``min ≤ max`` when both are set.
  2. **OwlRule** (`spending/owl_adapter.py`). Per-quarter recursion
     adds the absolute clamps after the rate-band trigger and before
     the existing quarterly floor/ceiling:
       1. inflation-adjust prior annual
       2. rate-band trigger (UNCHANGED)
       3. **NEW: absolute_min / absolute_max clamps**
       4. quarterly conversion + cfg.floor_usd / cfg.ceiling_usd
          (UNCHANGED)
     Rule retains per-instance ``_min_clamp_activations`` /
     ``_max_clamp_activations`` counters that reset on each new
     ``params.start_quarter`` (a fresh run); diagnostic only — does
     NOT influence the trigger output. New ``OwlRule.diagnostics()``
     surfaces these counts.
  3. **Report** (`integration/report.py`). New
     ``## Owl scale-sensitivity (advisory)`` section gated on
     ``cfg.spending.rule == "owl"``. Surfaces the absolute-clamp
     configuration, activation counts, and a regime classification
     (``scale-aware (clamps configured)`` vs.
     ``scale-invariant (no absolute clamps configured)``). Ends with
     the **L19 caveat verbatim**:
     "Phase 11 fixes scale-invariance only — it does NOT resolve
     spending-base realism (L19). Owl still measures rate against
     total NAV." Pinned by
     ``test_report_owl_advisory_section_renders_with_l19_caveat``.
* **Required tightening verified.** The phrase
  "Phase 11 fixes scale-invariance only — it does NOT resolve
  spending-base realism (L19)" appears verbatim in both
  ``MODEL_DOCUMENTATION.md`` (this file) and the report's advisory
  section, and is asserted by a regression test.
* **L-status.** L16 flipped to ``[RESOLVED 2026-05-02, Phase 11]``.
  L19 remains ``OPEN — modeling``. After Phase 11, the open
  backlog is **L19 / L2 / L5** in priority order.
* **Tests added (9).** ``tests/test_owl_scale_invariance.py``:
  - schema (4): negative min, zero max, non-finite max, min > max
  - default-off byte-stability (no absolute fields → trajectory
    byte-identical across two consecutive runs)
  - **scale-invariance regression test** — the one L16 doc
    referenced but didn't actually ship before Phase 11. Now
    pinned: $100M and $1B households with proportional setup
    produce trajectories that scale exactly 10× at every quarter.
  - scale-divergence under absolute floor: same two scaled
    instances with absolute floor set → small fund pins at floor;
    large fund's clamp never activates; trajectory ratios diverge
    from the constant-10× pre-clamp shape.
  - cut-path floor binding via prior-spend feedback: cut sequence
    drives below floor → clamp activates → next year's
    ``prior_annual`` reads the clamped value (via the orchestrator-
    emitted spend rows in the test ledger) → trajectory pins at
    floor for the rest of the horizon.
  - end-to-end report renders with the L19 caveat verbatim.
* **Standing principle (§Use-case context) updated.** L16 entry in
  the principle's status block flipped from "Phase 11 candidate"
  to "Phase 11 / RESOLVED" with the engine-conditional wording.
  The L19 entry in that block remains the spending-base-realism
  ticket; the principle states explicitly that Phase 11 closure
  does NOT address it.
* **Impact on outputs.** **Zero on shipped configs.** The default
  ``configs/spending.yaml`` uses ``rule: flat_real`` so the new
  Owl-only fields and report section never fire. For out-of-tree
  Owl configs without the absolute clamps set, behavior is
  byte-identical to pre-Phase-11. For Owl configs WITH the
  clamps set, trajectory diverges from pre-Phase-11 (the clamps
  bind) — this is the intended fix.
* **225 tests pass** (was 216). 9 new; zero regressions; zero
  re-anchored.
* **Backward-compatible.** Yes. New fields default to ``None`` so
  existing Owl configs validate and run unchanged. No allocator,
  rebalancer, ledger, PE, or fee-economics code touched.

### 2026-05-02 — docs(scope): scope-lock — `PROJECT_SCOPE.md` + Wake Robin reference architecture

* **What.** Docs-only scope-lock commit. Three surfaces:
  1. **New `PROJECT_SCOPE.md`** at the repo root. Authoritative scope
     statement reframing the project from "asset allocation framework"
     to a Gen3–Gen5 single-family-office (SFO) modeling stack covering
     seven layers in dependency order: Entity, Account/Position,
     Cash-flow, PE pacing, RE+OpCo, Liquidity, Allocation/Policy.
     Codifies the four-line principle ("NAV is not liquidity / appraisal
     value is not spending capacity / development+land value is not
     distributable income / OpCo value is not automatically portfolio
     liquidity"). Records two external read-only integration targets
     and what is committed vs. what is not. Lists out-of-scope items
     explicitly. Defines the authority + update protocol.
  2. **Reference architecture artifacts.** Tracked
     ``docs/wake_robin_liquidity_architecture.png`` (canonical render)
     and ``docs/wake_robin_liquidity_architecture.svg`` (vector
     source). Diagram lays out Inputs → Engines → Outputs for the full
     SFO stack.
  3. **`MODEL_DOCUMENTATION.md` reframings.** §Use-case context now
     opens with a pointer making `PROJECT_SCOPE.md` authoritative for
     project scope; this file remains authoritative for *how the model
     is built and behaves*. Roadmap implications section (under
     §Limitations) reordered: L19 → cash-flow ingestion + entity schema
     → position ingestion + RE/OpCo pipeline → L2 (Monte Carlo,
     deferred until deterministic SFO layers are honest) → L5 (rides
     a future phase). Cash-flow / entity / RE+OpCo are explicitly
     called out as **not** ``Limitations`` entries because the gap is
     structural (whole layers not yet built), not a defect of an
     existing layer.
* **External integration targets (read-only; not committed).**
  * `Cashflow Modeling v7.xlsx` —
    `C:\Users\DarrenSchulz\Brooks Capital Management\Accounting - Documents\Cashflow\Cashflow Modeling v7.xlsx`.
    Canonical reference for the cash-flow + entity layers. Structural
    domains only (sheet inventory, entity types, period structure)
    are referenced in `PROJECT_SCOPE.md` §5.1; live values, person
    names, and forecast tables are not.
  * `Investment Summary for Categorization March 2026.xlsx` —
    `C:\Users\DarrenSchulz\Brooks Capital Management\Investment - Documents\Investment Summary for Categorization March 2026.xlsx`.
    Canonical position universe for the account/position layer.
    Structural taxonomy (Asset Allocation Class set, five-tier
    Liquidity Bucket, liquidity granularity, time horizon,
    cash-flow-producing flag) referenced in `PROJECT_SCOPE.md` §5.2;
    live balances, manager identities, and per-position dollar
    columns are not.
* **Why.** Phases 1–11 closed under an "asset allocation framework"
  framing. Under the actual Gen3–Gen5 SFO use case the project
  must eventually cover entity, cash-flow, RE/OpCo, and liquidity-
  tier layers that the current code does not. Locking the scope
  now — before L19 and the unbuilt layers land — prevents the
  documentation from drifting further behind the real intent and
  gives future phases an authoritative reference for what is in
  scope vs. what is not.
* **Repo name.** Directory remains ``asset-allocation/`` for
  continuity. ``Wake Robin Liquidity Architecture`` is a
  documentation codename only; no code, package, or import path
  changes.
* **Tests.** Docs-only commit; 225 tests still pass. Zero new tests;
  zero re-anchored.
* **Backward-compatible.** Yes. Documentation and tracked diagram
  artifacts only; no code, schema, config, or output changes.
* **Authority.** `PROJECT_SCOPE.md` becomes the authoritative scope
  reference. Future scope changes follow the protocol in
  `PROJECT_SCOPE.md` §8 — scope-lock commits and implementation
  commits stay disjoint.

### 2026-05-02 — Phase 12 design-lock: L19 spending base realism (pre-implementation)

* **What.** Docs-only design-lock commit. Adds the
  ``## Phase 12 design (pre-implementation) — L19 spending base
  realism`` block to `MODEL_DOCUMENTATION.md`. Subsequently
  amended in a second docs-only commit to apply three reviewer
  tightenings: (1) rename ``liquid_plus_income`` to
  ``liquid_plus_income_producing_nav`` with explicit "NAV not
  income" callout; (2) document ``income_producing`` as bucket-
  level static metadata bridge, not asset/entity/property-level
  cash-flow modeling; (3) ``spending_base_weights`` becomes
  bucket-keyed with strict validation (valid CMA bucket keys,
  finite, ≥0, ≥1 positive, base must be > 0 when used).
* **Why.** Under the SFO use case Owl's withdrawal-rate trigger
  measures rate against total NAV, but for a Gen3–Gen5 SFO with
  large illiquid balance sheet the household's actual spendable
  rate is 2–3× higher. The base-side fix is the gating modeling
  improvement before any cash-flow ingestion / entity schema /
  RE+OpCo pipeline lands.
* **Tests.** Docs-only; zero code change.
* **Backward-compatible.** Yes — design only.

### 2026-05-02 — Phase 12 implementation: L19 base-side spending realism

* **What.** Implements the design-lock above as one cohesive
  commit. Owl's withdrawal-rate denominator is now configurable
  via ``GuardrailConfig.spending_base`` across four modes:
  ``total_nav`` (default, byte-stable with Phase 11),
  ``liquid_nav``, ``liquid_plus_income_producing_nav``, and
  ``custom_policy`` (bucket-keyed inclusion weights). Both
  initial-rate and current-rate denominators are replaced
  symmetrically; rate-band geometry preserved. ``CMAConfig``
  gains an optional fourth liquidity tier ``"locked_strategic"``
  and an optional ``income_producing: dict[str, bool]`` flag.
  ``StudyConfig`` cross-validates every required combination
  loudly. ``compute_spending_base`` is a new pure helper in
  ``src/aa_model/spending/spending_base.py``. ``OwlRule.diagnostics()``
  surfaces both exclusion breakdowns + dual withdrawal rates.
  ``report.md`` gains a new ``## Owl spending base (advisory)``
  section with two render modes (non-default-base full
  diagnostic + default-base material-illiquid warning).
* **Why.** Closes the base-side of L19 (flow-side
  ``distributable_income`` mode is parked in the Literal but
  raises ``NotImplementedError`` — Phase 12.5 lands the new
  ``distribution_inflow`` ledger flow type). Implements the
  reviewer tightenings without scope creep.
* **Files touched.** ``src/aa_model/io/schemas.py`` (Guardrail
  + CMA + StudyConfig validators); ``src/aa_model/assumptions/cma.py``
  (income_producing series); ``src/aa_model/spending/base.py``
  (SpendingParams CMA-tag fields); ``src/aa_model/spending/owl_adapter.py``
  (denominator swap + extended diagnostics);
  ``src/aa_model/spending/spending_base.py`` (NEW — pure helper
  + ``SpendingBaseBreakdown``); ``src/aa_model/integration/orchestrator.py``
  (CMA-tag wire-through); ``src/aa_model/integration/report.py``
  (new advisory section, additive — no changes to existing
  sections); ``tests/test_phase12_spending_base.py`` (NEW — 13
  tests).
* **Tests.** 13 new tests across schema (4), base computation
  (3 + 1 NotImplementedError stub), Owl integration (4), and
  end-to-end report rendering (2). Existing 225-test baseline
  passes unchanged via the default-off byte-stability of
  ``spending_base=None``.
* **Backward-compatible.** Yes. ``spending_base=None`` short-
  circuits to ``total_nav``; ``compute_spending_base`` returns
  ``nav.sum()`` exactly. Existing fixtures, configs, and
  trajectories byte-identical to Phase 11.
* **L19.** Flips to ``[PARTIALLY RESOLVED 2026-05-02, Phase 12]``.
  Base-side closed; flow-side open until Phase 12.5.

### 2026-05-02 — Phase 12.5 design-lock + reviewer tightenings

* **What.** Two docs-only commits (``63ec0ef``, ``7505770``) adding
  the ``## Phase 12.5 design (pre-implementation) — L19 flow-side``
  block to ``MODEL_DOCUMENTATION.md``, then applying four reviewer
  tightenings to it. No code changes.
* **Tightenings.** (1) Phase 12.5 does NOT determine legal /
  tax / entity-governance distributability — consumes upstream-
  classified rows. (2) Recommended source convention
  ``distribution:<domain>:<entity_or_asset_id>`` documented
  (not enforced). (3) Recurring-vs-one-time disclaimer added to
  the rendered advisory + standing warning-band entry. (4) L19
  status narrowed: stays at PARTIALLY RESOLVED after
  implementation; flips to RESOLVED only after Phase 13/14
  producers exist.
* **Tests.** Docs-only; zero code change.
* **Backward-compatible.** Yes — design only.

### 2026-05-02 — Phase 12.5 implementation: L19 flow-side infrastructure

* **What.** Implements the design-lock above as one cohesive
  commit. New ledger flow type ``distribution_inflow`` (additive to
  ``FLOW_ORDER`` between ``inflow`` and ``return``; cash-bucket
  positive rows; structural validation at add-time).
  ``GuardrailConfig`` gains ``distribution_window_quarters``
  (default 4q TTM, range [1,20]) and
  ``bootstrap_distributable_income_usd`` (strictly positive;
  finite). New pure helper
  ``compute_distributable_income_base`` reads
  ``ledger.closed_through(prior_q)`` and rolls up
  ``distribution_inflow`` rows over the trailing window with a
  bootstrap fallback for q0 / insufficient history. Extended
  ``compute_spending_base`` dispatches to the new helper for the
  Phase 12.5 mode.  ``SpendingBaseBreakdown`` gained two additive
  fields (``distributable_income_by_source_usd``, ``is_bootstrap``)
  with neutral defaults — Phase 12 callers byte-identical.
  ``OwlRule`` integrates the new mode symmetrically across initial
  and current rate denominators (initial forces bootstrap by passing
  ``prior_quarter < start_quarter``); zero-income runtime guard
  fails loud after the bootstrap window has elapsed; diagnostics
  surfaces five new fields. ``report.md`` renders a third advisory
  mode (``distributable_income``) with by-source breakdown, dual
  rates, regime classification, recurring-vs-one-time CAVEAT, and
  the producer-dependent framing. PE distributions explicitly
  excluded by default. ``test_canonical_intra_quarter_ordering``
  updated to seed a ``distribution_inflow`` row at the new slot in
  ``FLOW_ORDER``; the obsolete Phase 12
  ``NotImplementedError`` stub test was retired.
* **Why.** Closes the *infrastructure side* of L19 (flow side);
  the schema, helper, and report can now consume realized
  distributable-income rows when a producer feeds them. **The
  producer is out of scope** — Phase 13 (RE+OpCo pipeline) and
  Phase 14 (cash-flow / entity ingestion) own that.
* **Scope discipline.** Phase 12.5 is *consumer-side*
  infrastructure. It does not implement the producer of
  distributable income, does not determine legal / tax / entity-
  governance distributability, and does not classify rows as
  recurring vs one-time.
* **Files touched.** ``src/aa_model/integration/ledger.py``
  (FLOW_ORDER + add-time validation + NAV-conservation set);
  ``src/aa_model/io/schemas.py`` (GuardrailConfig + StudyConfig);
  ``src/aa_model/spending/spending_base.py``
  (``compute_distributable_income_base`` + extended
  ``compute_spending_base`` + ``SpendingBaseBreakdown``);
  ``src/aa_model/spending/owl_adapter.py`` (mode wiring + initial
  bootstrap forcing + zero-income guard + extended diagnostics);
  ``src/aa_model/integration/report.py`` (new render mode);
  ``MODEL_DOCUMENTATION.md`` (L19 status + this entry).
  ``tests/test_phase125_distributable_income.py`` (NEW — 13 tests).
  ``tests/test_ledger.py`` (canonical-ordering test seeds new flow
  type). ``tests/test_phase12_spending_base.py`` (Phase 12 stub
  test retired; Phase 12 test #1 now sets the Phase 12.5 schema
  fields when constructing ``distributable_income``).
* **Tests.** 13 new Phase 12.5 tests + 12 Phase 12 tests still
  green (one stub retired) + 225 existing tests still green =
  **252 passed**. Default-off byte-stability verified.
* **Backward-compatible.** Yes. Existing fixtures, configs, and
  trajectories byte-identical to Phase 12.
* **L19.** Stays at ``[PARTIALLY RESOLVED 2026-05-02, Phase 12 +
  Phase 12.5]`` (reviewer tightening 4). Wording: spending-rule
  denominator infrastructure complete; production distributable-
  income realism producer-dependent (Phase 13/14). L19 flips to
  RESOLVED only after producers exist.

### 2026-05-02 — Phase 13 design-lock + reviewer tightenings

* **What.** Two docs-only commits (``89ab712``, ``345f964``) adding
  the ``## Phase 13 design (pre-implementation) — RE / OpCo
  distribution_inflow producer`` block to ``MODEL_DOCUMENTATION.md``,
  then applying two reviewer tightenings.
* **Tightenings.** (1) Cash-movement / source-of-cash boundary —
  Phase 13 emits income recognition into modeled cash, treats
  configured entries as already approved/distributable/payable,
  does NOT model inter-entity transfer mechanics (Phase 14+ work).
  (2) Duplicate (source, quarter) pairs allowed when producer_id
  is unique; producer_id is the row-level audit key.
* **Tests.** Docs-only; zero code change.
* **Backward-compatible.** Yes — design only.

### 2026-05-02 — Phase 13 implementation: RE / OpCo distribution_inflow producer (config-driven)

* **What.** Implements the design-lock above as one cohesive
  commit. New module ``src/aa_model/producers/distribution.py``
  with ``DistributionProducer`` ABC, ``ConfigDrivenProducer``
  concrete, ``DistributionEmission`` /
  ``DistributionProducerDiagnosticsDelta`` /
  ``DistributionProducerDiagnostics`` dataclasses, and
  ``make_distribution_producer`` factory (engine="config" only;
  Phase 14 will add "workbook"). New schemas
  ``DistributionEntryConfig`` and ``DistributionProducerConfig`` in
  ``io/schemas.py`` with hard validators (URL-safe ids,
  amount > 0 + finite, quarter parses, domain × recurrence sanity,
  producer_id globally unique). ``StudyConfig.distribution_producer``
  optional with default None. Orchestrator wires the producer once
  per quarter inside the existing per-quarter loop; emissions land
  as ``distribution_inflow`` rows on cash via the existing
  ledger.add() path. Producer-diagnostics dataclass accumulates
  across the run; surfaced in the orchestrator return tuple and
  threaded into ``write_markdown_report``. Report gains a new
  ``## Distribution producer (advisory)`` section with by-domain /
  by-recurrence / by-confidence / top-3 / excluded-restricted
  breakdowns + warning bands (one-time share ≥ 30%, top-3
  concentration ≥ 80%, forecast/scenario share ≥ 20%, restricted
  entries surfaced). Composes with Phase 12.5's
  ``## Owl spending base (advisory)`` rather than replacing it.
* **Why.** Closes the consumer-producer loop opened by Phase 12.5.
  Owl can now run end-to-end on
  ``spending_base="distributable_income"`` with hand-authored or
  config-driven family-office income data. Production-grade
  workbook-driven realism remains Phase 14.
* **Reviewer tightenings.**
  - (1) Cash-movement boundary: producer treats entries as
    already-approved / distributable / payable; does NOT model
    inter-entity transfer mechanics. The standing-principle audit
    sits at the producer; cash mechanics are Phase 14+.
  - (2) Duplicate (source, quarter) allowed: uniqueness is on
    ``producer_id`` only. Multiple entries may share a source
    string in the same quarter (recurring rent + one-time refi
    proceeds from the same building); the by-source rollup sums
    additively, ``producer_id`` distinguishes the audit trail.
* **Files touched.** ``src/aa_model/producers/__init__.py`` (NEW);
  ``src/aa_model/producers/distribution.py`` (NEW);
  ``src/aa_model/io/schemas.py`` (DistributionEntryConfig,
  DistributionProducerConfig, StudyConfig field);
  ``src/aa_model/integration/orchestrator.py`` (producer wiring,
  per-quarter emit, return tuple extension);
  ``src/aa_model/integration/report.py`` (new advisory section);
  ``MODEL_DOCUMENTATION.md`` (L19 status + this entry).
  ``tests/test_phase13_distribution_producer.py`` (NEW — 14 tests).
* **Tests.** 14 new Phase 13 tests + 251 existing tests still
  green = **265 passed**. Default-off byte-stability verified
  (cfg.distribution_producer = None ⇒ Phase 12.5 trajectories
  byte-identical).
* **Backward-compatible.** Yes. Existing fixtures, configs, and
  trajectories byte-identical to Phase 12.5.
* **L19.** Stays at ``[PARTIALLY RESOLVED 2026-05-02, Phase 12 +
  Phase 12.5 + Phase 13]``. Wording: spending-rule denominator
  infrastructure complete; config-driven producer shipped;
  workbook-driven realism remains dependent on Phase 14
  (Cashflow Modeling v7.xlsx + entity schema). L19 flips to
  RESOLVED only after Phase 14 producers exist and the SFO can
  run end-to-end on real household income data.

### 2026-05-03 — Phase 14 design-lock + reviewer tightenings

* **What.** Two docs-only commits (``52721fb``, ``cdd73c4``) adding
  the ``## Phase 14 design (pre-implementation) — cash-flow workbook
  and entity ingestion`` block to ``MODEL_DOCUMENTATION.md``, then
  applying four reviewer tightenings.
* **Tightenings.**
  - (1) Stale formula / cache risk surfaced as standing CAVEAT in
    the report + ``IngestionDiagnostics.formula_cache_caveat``.
  - (2) ``workbook_version`` REQUIRED on
    ``WorkbookManifestConfig``; URL-safe; used in deterministic
    producer_id; workbook_hash captured for provenance but NOT in
    producer_id.
  - (3) Board-snapshot reconciliation is ADVISORY ONLY; no strict
    mode in Phase 14.
  - (4) Tests use synthetic workbook fixtures only; no real
    workbook rows / live values / person-level data committed.
* L19 status narrowed: stays at PARTIALLY RESOLVED on Phase 14
  implementation alone; promotion to RESOLVED is operational.
* **Tests.** Docs-only; zero code change.
* **Backward-compatible.** Yes — design only.

### 2026-05-03 — Phase 14 implementation: cash-flow workbook + entity ingestion

* **What.** Implements the design-lock above as one cohesive
  commit. New package ``src/aa_model/ingestion/`` with:
  - ``schemas.py``: ``EntityRecord``, ``CashFlowLineRecord``,
    ``RowClassificationRule``, ``EntitySheetSpec``,
    ``REPartnershipSheetSpec``, ``WorkbookManifestConfig``,
    ``IngestionDiagnostics``, ``IngestionResult``. Hard validators
    enforce URL-safe ids/version (no colons), finite amounts, sign
    convention, distributable_candidate-requires-domain.
  - ``workbook.py``: ``ingest_workbook`` opens via
    ``openpyxl(read_only=True, data_only=True, keep_links=False)``;
    SHA256 hashes the raw .xlsx bytes for provenance; parses period
    headers in four formats (yyyy_q / q_yy / q_yyyy / calendar_qe);
    excludes subtotal rows by manifest pattern; matches row labels
    to ``RowClassificationRule`` instances first-match-wins;
    enforces sign convention at ``CashFlowLineRecord`` construction;
    runs board-snapshot reconciliation as ADVISORY ONLY (Phase 14
    RT3). ``workbook_lines_to_producer_config`` is a SEPARATE
    named bridge function that converts qualifying lines into a
    ``DistributionProducerConfig`` with deterministic producer_id =
    ``f"{workbook_version}__{sheet}__{row_label}__{quarter}"``
    (workbook_version-anchored per RT2; hash NOT in producer_id).
  - ``workbook_producer.py``: ``WorkbookDrivenProducer`` adapter
    satisfies the Phase 13 ``DistributionProducer`` ABC by
    delegating to ``ConfigDrivenProducer`` on the workbook-derived
    config. ``make_distribution_producer`` factory extends with
    ``engine="workbook"``.
- ``StudyConfig.workbook_ingestion`` (optional, default None).
  Orchestrator runs ingestion → bridge → workbook producer when
  configured; otherwise behaves identically to Phase 13.
- New ``## Workbook ingestion (advisory)`` report section composes
  with Phase 12.5 + Phase 13 advisory sections; carries the
  standing CAVEAT for cached-formula stale-state risk on every
  ingestion run (Phase 14 RT1).
* **Why.** Closes the workbook-driven half of L19's producer seat.
  The model can now run end-to-end on the household's actual
  operating cash-flow forecast (Cashflow Modeling v7.xlsx) instead
  of only on hand-authored config. The workbook is treated as a
  read-only integration target — never mutated, never committed.
* **Reviewer tightenings.**
  - RT1: ``IngestionDiagnostics.formula_cache_caveat`` carries the
    standing advisory text; rendered on every ingestion run.
  - RT2: ``WorkbookManifestConfig.workbook_version`` is required +
    URL-safe; producer_id uses workbook_version, NOT workbook_hash.
  - RT3: Reconciliation deltas surface as advisory entries; never
    raise.
  - RT4: All Phase 14 tests build their own openpyxl workbooks at
    test time in tmp_path. NO real workbook rows / values / person
    or entity names committed under tests/. The real
    ``Cashflow Modeling v7.xlsx`` is for local validation only.
* **Files touched.** ``src/aa_model/ingestion/__init__.py`` (NEW);
  ``src/aa_model/ingestion/schemas.py`` (NEW);
  ``src/aa_model/ingestion/workbook.py`` (NEW);
  ``src/aa_model/ingestion/workbook_producer.py`` (NEW);
  ``src/aa_model/io/schemas.py`` (WorkbookIngestionConfig +
  StudyConfig field);
  ``src/aa_model/producers/distribution.py`` (engine="workbook"
  factory branch); ``src/aa_model/integration/orchestrator.py``
  (ingestion wiring + producer dispatch + return-tuple extension);
  ``src/aa_model/integration/report.py`` (new advisory section);
  ``tests/test_phase14_workbook_ingestion.py`` (NEW — 12 tests,
  synthetic fixtures only); ``tests/test_phase13_distribution_producer.py``
  (factory dispatch test updated — engine="workbook" now valid).
  ``MODEL_DOCUMENTATION.md`` (L19 status + this entry).
* **Tests.** 12 new Phase 14 tests + 265 baseline = **277 passed**.
  Default-off byte-stability verified (cfg.workbook_ingestion =
  None ⇒ Phase 13 trajectories byte-identical).
* **Backward-compatible.** Yes. Existing fixtures, configs, and
  trajectories byte-identical to Phase 13.
* **L19.** Stays at ``[PARTIALLY RESOLVED 2026-05-03, Phase 12 +
  12.5 + 13 + 14]``. Wording: full ingestion stack shipped; L19
  flips to RESOLVED only after a clean local validation pass
  against the real workbook AND wording narrowed to "RESOLVED for
  modeled distributable-income ingestion; legal / tax / entity-
  governance distributability remains out of scope." Promotion is
  operational, not implementational — a separate docs-only commit
  performs that flip after the reviewer confirms the validation
  pass.

### 2026-05-03 — Phase 14.1: workbook layout discovery support

* **What.** Targeted Phase 14 extension surfaced by the operational
  validation probe. The discovery probe found that v7's data-rich
  sheets (``Cash Flow``, ``PB Westplan``, ``PB Westplan v2``) use a
  multi-row header band with the canonical quarterly header on
  **row 4** in ``q_yyyy`` format ("Q1 2025"-style), while Phase 14
  hard-coded row 1. Without this fix, ~90% of the workbook would
  be unparseable.
* **Schema additions (additive, default-off byte-stable).**
  - ``WorkbookManifestConfig.default_header_row_index: int = 1``
    (1-indexed; manifest-level default; preserves Phase 14
    behavior).
  - ``EntitySheetSpec.header_row_index: int | None = None``
    (per-sheet override).
  - ``EntitySheetSpec.period_header_format: Literal[...] | None = None``
    (per-sheet override).
  - ``EntitySheetSpec.layout_type: Literal["horizontal_quarter",
    "display_only"] = "horizontal_quarter"``. ``"display_only"``
    declares the sheet but skips data extraction (no
    CashFlowLineRecord rows emitted) — the right disposition for
    aggregate / summary sheets that legitimately repeat row labels
    across sub-sections (Cash Flow, PB Westplan, ownership).
* **Parser behavior.** ``_ingest_entity_sheet`` now uses
  ``spec.header_row_index or manifest.default_header_row_index``
  and ``spec.period_header_format or manifest.period_header_format``;
  ``layout_type == "display_only"`` short-circuits before data
  extraction. ``_reconcile_aggregate_sheet`` reads from
  ``manifest.default_header_row_index`` (per-sheet override on
  aggregate / board-snapshot sheets out of scope for Phase 14.1 —
  would require promoting those declarations from ``list[str]`` to
  structured specs).
* **Privacy fixes (uncovered by the operational validation probe).**
  Two ingestor-level error / diagnostic strings exposed raw row
  labels — these labels can contain entity names, dollar amounts,
  and transaction-level detail. Fixed:
  - Duplicate-row ValueError now reports row position + entity_id +
    quarter + label *length* (content redacted) plus a remediation
    hint pointing the user at ``layout_type="display_only"`` for
    aggregate sheets.
  - ``unmatched_lines_sample`` (which renders into the report) now
    carries row position only, not the label content.
* **Tests.** 5 new Phase 14.1 tests in
  ``tests/test_phase14_1_layout_discovery.py``: row-4 ``q_yyyy``
  fixture parses; per-sheet ``header_row_index`` override beats
  manifest default; per-sheet ``period_header_format`` override
  beats manifest default; ``layout_type="display_only"`` declares
  but skips; default behavior byte-stable vs. Phase 14 (regression
  anchor). Total: 282 passed (277 + 5). All synthetic fixtures
  built programmatically in tmp_path (Phase 14 RT4 still in force).
* **Operational validation re-probe.** Live workbook re-probed via
  ``/tmp/phase14_1_validation_probe.py`` (not committed) with an
  in-memory probe manifest declaring 4 sheets as ``display_only``.
  Results: 4 entities declared, 0 cash-flow lines (correct), 4
  display_only diagnostics surfaced, 0 unparseable headers, 0
  unmatched lines, no leaks of values / labels / identifiers.
  Standing CAVEAT (Phase 14 RT1) populated.
* **Backward-compatible.** Yes. All existing fixtures, configs,
  and trajectories byte-identical to Phase 14.
* **L19.** Held at ``[PARTIALLY RESOLVED 2026-05-03, Phase 12 +
  12.5 + 13 + 14 + 14.1]``. The operational validation against
  the real workbook still requires manifest authoring (entity
  ``row_classification_rules``) plus a clean reconciliation pass.
  L19 flips to RESOLVED only after that operational milestone, with
  wording narrowed to "RESOLVED for modeled distributable-income
  ingestion; legal / tax / entity-governance distributability
  remains out of scope."

### 2026-05-03 — Phase 14.x: workbook_v7_manifest.yaml scaffold

* **What.** Committed ``configs/workbook_v7_manifest.yaml`` as a
  STRUCTURAL TEMPLATE — not a runnable manifest. All 43 sheets the
  v7 workbook contains are enumerated by role; family aggregates,
  board snapshots, and clearly-structural project / place names are
  committed with literal sheet_names. Sheet names that may decode to
  family-internal abbreviations (LLC codes, trust codes, person-shaped
  names) carry ``<TODO_*>`` placeholder slots that the user fills in
  locally before running ingestion.
* **Privacy posture.** PROJECT_SCOPE.md §5.3 + Phase 14 RT4 stand:
  no live values, no row labels, no per-line classification rules
  committed. ``row_classification_rules`` are left as TODO on every
  entity_sheet entry — they carry sensitive per-line content and
  must be authored locally.
* **Localize-then-use workflow.** Documented in the manifest's header
  comment:
  - copy ``configs/workbook_v7_manifest.yaml`` to
    ``configs/workbook_v7_manifest_local.yaml`` (gitignored — see
    ``.gitignore`` update);
  - replace each ``<TODO_*>`` ``sheet_name`` with the actual workbook
    sheet name; do not change committed ``entity_id`` values
    (they anchor deterministic Phase 14 → Phase 13 producer_ids);
  - author ``row_classification_rules`` per sheet locally;
  - point ``StudyConfig.workbook_ingestion`` at the local manifest;
  - run ingestion locally; do NOT commit the local manifest, raw
    ingestion output, or any rendered report content that contains
    live values.
* **Manifest defaults reflect v7's layout** (probed via
  ``/tmp/phase14_layout_probe.py`` — not committed):
  - ``default_header_row_index: 4`` (v7 entity sheets place the
    canonical quarter header on row 4)
  - ``period_header_format: "q_yyyy"`` (e.g., "Q1 2025")
  - ``subtotal_label_patterns`` carries the Phase 14 defaults
* **Sheet disposition (43 total):**
  - 2 family aggregates (literal: ``Summary``,
    ``Summary Dev and Op``) — reconciliation targets
  - 5 board snapshots (literal: 5 dated board sheets) —
    advisory reconciliation only (Phase 14 RT3)
  - 36 entity sheets:
    - 6 ``display_only`` (Cash Flow, Assumptions, Ownership,
      PB Westplan, PB Westplan v2, plus 1 person-shaped
      placeholder marked display_only) — declared but no rows
      extracted; right disposition for aggregate sheets that
      legitimately repeat row labels across sub-sections
    - 30 ``horizontal_quarter`` — 5 literal (project / place
      names: SE Holland, WR-Dev, LH, LH (2), LH - L and B Acq)
      + 25 placeholders (2 LLC, 14 trust, 5 individual, 5 capital)
* **Tests.** Schema-load only (``WorkbookManifestConfig.model_validate``);
  no ingestion run. The scaffold parses cleanly; sheet-name
  uniqueness + entity_id uniqueness validators pass; total
  declarations (50) tally to the workbook's 43 unique sheets via
  family_aggregate (2) + board_snapshot (5) + entity_sheets (36).
* **Backward-compatible.** Yes — adds a new file under ``configs/``;
  the orchestrator does not auto-load it. Existing trajectories,
  fixtures, and tests byte-identical.
* **L19.** Held at PARTIALLY RESOLVED. The scaffold is the
  prerequisite for the operational manifest-authoring milestone;
  the milestone itself (local manifest authored + clean
  reconciliation against the live workbook) is not yet complete.

### 2026-05-03 — Phase 14.2: workbook discovery + draft-manifest generator

* **What.** Reduces the burden of authoring the v7 manifest by
  hand. The model now scrapes workbook structure read-only and
  emits a draft :class:`WorkbookManifestConfig`. The scraper
  discovers STRUCTURE; the manifest governs MEANING.
  Discovery-assisted ingestion flow:

  ```
  workbook → discovery → draft manifest → human classification
       → approved local manifest → ingestion
  ```

* **New module ``src/aa_model/ingestion/discovery.py``.**
  - ``discover_workbook(path)`` opens via
    ``openpyxl(read_only=True, data_only=True, keep_links=False)``,
    walks every sheet, infers role + layout_type + header
    candidates, computes manifest-level majorities (header row +
    period format), and returns a
    :class:`WorkbookDiscoveryResult`.
  - ``build_draft_manifest(discovery, *, mode, workbook_version)``
    converts the discovery into a :class:`WorkbookManifestConfig`.
    ``row_classification_rules`` are deliberately empty on every
    entity sheet — those carry sensitive per-line content and
    must be authored locally.
  - Per-sheet classification: family_aggregate / board_snapshot /
    assumptions_metadata / ownership_structure / re_partnership /
    entity_trust / entity_llc / entity_sheet / unknown. Layout
    classification: horizontal_quarter (header + ≥3 label rows)
    or display_only (catch-all).

* **CLI ``src/aa_model/ingestion/discover_workbook.py``.**
  Invoked as ``python -m aa_model.ingestion.discover_workbook``.
  Flags: ``--workbook PATH`` (required), ``--mode``
  (``privacy_safe`` default; ``local_private``), ``--out PATH``
  (optional; stdout diagnostics if omitted), ``--workbook-version``,
  ``--dry-run``. ``local_private`` mode refuses to write to a path
  that doesn't end in ``_local.yaml`` or live under
  ``data/external/`` — both gitignored per the Phase 14.x
  conventions.

* **Privacy posture (Phase 14 RT4 + Phase 14.2 RT).**
  - **privacy_safe** (default): redacts any sheet name that
    doesn't match a known structural keyword set
    (``summary`` / ``aggregate`` / ``rollup`` / ``cash flow`` /
    ``board`` / ``snapshot`` / ``assumption`` / ``notes`` /
    ``ownership`` / ``structure``). Suitable for chat summaries,
    committed scaffold, public review.
  - **local_private**: preserves real sheet names; refuses to
    write to a non-gitignored path.
  - The scraper NEVER prints cell values, dollar amounts, or row
    contents. ``--dry-run`` surfaces aggregate structural
    diagnostics only.

* **Human-classification boundary (the scraper MUST NOT infer):**
  legal distributability; tax treatment / withholding;
  entity-governance availability; whether OpCo cash is
  family-office spendable; whether development / land value is
  spendable; final ``distributable_candidate`` /
  ``restricted`` / ``recurrence_type`` / ``certainty`` /
  ``domain`` per row. These remain TODO on the draft manifest's
  ``row_classification_rules`` for human authoring under
  ``configs/workbook_v7_manifest_local.yaml``.

* **Tests (``tests/test_phase14_2_discovery.py`` — 8 tests).**
  All synthetic-fixture-only:
  1. row-4 q_yyyy synthetic discovers correctly
  2. display_only detection (sparse / no-header sheets)
  3. draft manifest validates under
     ``WorkbookManifestConfig.model_validate``
  4. privacy_safe redacts non-structural sheet names; structural
     names preserved
  5. local_private path safety check refuses non-gitignored output
  6. majority format / header detection across multiple sheets
  7. CLI ``--dry-run`` prints diagnostics; no YAML; no cell values
  8. Phase 14 ingestion regression anchor — adding discovery does
     not change ``ingest_workbook`` behavior

* **Live-workbook discovery dry-run.** Aggregate structural counts
  only — see the post-implementation operational summary
  surfaced after the commit. No live values, no sensitive sheet
  names; report-grade structural digest only.

* **Backward-compatible.** Yes. Discovery is a separate read path
  added alongside ingestion; existing fixtures, configs, and
  trajectories byte-identical. ``ingest_workbook`` unchanged.

* **L19.** Held at PARTIALLY RESOLVED. Discovery + draft-manifest
  reduce the friction of moving toward the operational L19 gate
  (local manifest authored + clean reconciliation pass) but do
  not on their own complete it. ``row_classification_rules``
  remain a human-authored input.

---

## Phase 15 design (pre-implementation) — Investment Summary / Account-Position ingestion

### Motivation

Phase 14 gave the model cash-flow rows from the workbook. Phase 15 gives it
the **position universe** — what the family owns, where it is held, how liquid
it is, and what contractual terms attach. Without this layer, the
``liquid_nav`` spending base is a config assertion, not a derived fact.

**Standing principle addressed:**

```
NAV is not liquidity.
Appraisal value is not spending capacity.
OpCo value is not automatically distributable capital.
Development and land assets require separate capital-need
and monetization assumptions.
```

**L-status:**

* L2 (Monte Carlo): still deferred; Phase 15 is a direct prerequisite.
* L19: unchanged; workbook classification continues independently.
* L20 opened: liquidity coverage ratio (liquid NAV / spending base) cannot
  be computed until position-level liquidity tagging exists. Gate held open
  until Phase 15 ingestion + Phase 12 spending-base integration complete.

**Second canonical data source:**

```
Investment Summary for Categorization March 2026.xlsx
```

Never committed, never mutated. Same privacy discipline as Phase 14.

---

### Read-only ingestion contract

* ``openpyxl(read_only=True, data_only=True, keep_links=False)`` — same
  call contract as Phase 14.
* SHA-256 hash stored in ``PositionIngestionDiagnostics`` for provenance.
* No live values, manager names, fund names, or position names in committed
  artifacts or chat.
* Synthetic fixtures only in tests.
* Local-private manifest: ``configs/investment_summary_manifest_local.yaml``
  (gitignored).
* Local-private exports: ``data/external/`` (gitignored).

---

### New schema: record types

#### ``AccountRecord``

One row per custodial / institutional account.

```
account_id      str   URL-safe, required. Human-assigned in manifest.
                      Flat sheets synthesize: "synthetic:<sheet_id>"
entity_id       str   FK → EntitySheetSpec.entity_id (Phase 14)
custodian       str   Institution holding the account (display-only)
account_type    Literal["taxable","tax_deferred","tax_exempt",
                        "trust","partnership","direct"]
valuation_date  date  As-of date for positions in this account
source_sheet    str   Sheet name (local-private only)
```

**Tightening T1:** Normalized output always has ``AccountRecord``. When
the workbook is flat (no explicit account grouping), the ingestor
synthesizes ``account_id = "synthetic:<sheet_id>"`` and
``account_type = "direct"``. Consumer always sees a uniform
account → position hierarchy.

#### ``PositionRecord``

One row per position.

```
position_id               str          f"{account_id}__{row_index}"
account_id                str          FK → AccountRecord
manager_id                str | None   FK → ManagerTermsRecord
asset_class               _ASSET_CLASS_LITERAL
strategy                  str | None
market_value_usd          float        >= 0; manager-reported NAV
cost_basis_usd            float | None
unfunded_commitment_usd   float | None >= 0
income_cash_flow_flag     bool         human-authored; True links to
                                       distribution_inflow producer
liquidity_bucket          _LIQUIDITY_BUCKET_LITERAL
time_horizon_quarters     int | None
valuation_date            date         required after fallback (T2)
source_row                int          1-indexed; no label content
```

``market_value_usd >= 0`` always. Positions are stocks of value, not
flows; sign convention differs from ``CashFlowLineRecord``.

**Tightening T2 — valuation date fallback + stale diagnostics:**
``PositionRecord.valuation_date`` is required. Resolution order:

```
position row cell
→ AccountSheetSpec.valuation_date
→ PositionManifestConfig.as_of_date
```

Diagnostics added:

```
positions_with_fallback_valuation_date   int
stale_valuation_count                    int  (> 90 days before as_of_date)
max_valuation_age_days                   int
```

Private market NAVs are often one or two quarters stale; this surfaces
without hiding it.

#### ``ManagerTermsRecord``

Fund / manager contractual terms. Human-authored in manifest; never
auto-inferred by the scraper.

```
manager_id               str   URL-safe, required
redemption_frequency     Literal["daily","monthly","quarterly",
                                 "semi_annual","annual","none"]
notice_days              int | None
gate_pct                 float | None   0.0–1.0
side_pocket              bool           default False
lockup_end_date          date | None
capital_call_notice_days int | None
distribution_policy      Literal["discretionary","mandatory",
                                 "reinvest","unknown"]
management_fee_bps       int | None
carry_pct                float | None   0.0–1.0
hurdle_rate              float | None   0.0–1.0 (preferred return)
fee_basis                Literal["committed","invested","nav","unknown"]
source_document          str | None
confidence               Literal["actual","contractual",
                                 "estimated","unknown"]
```

**Tightening T5 — confidence/completeness validation:**
``confidence="unknown"`` allows all fields ``None`` (placeholder).

When ``confidence in {"actual","contractual","estimated"}``, validation
requires at minimum:

```
redemption_frequency           required
fee_basis or management_fee_bps   at least one required
source_document or source_reference   at least one required
```

When the linked position's ``liquidity_bucket`` is ``"semi_liquid"`` or
``"illiquid"`` and ``confidence != "unknown"``, also requires:

```
notice_days is not None
OR lockup_end_date is not None
OR redemption_frequency == "none"
```

Rationale: a fund with ``confidence="contractual"`` but no redemption
terms is either missing its source document or miscategorized. The
validation surfaces the gap rather than accepting silent incompleteness.

---

### Liquidity taxonomy

``liquidity_bucket`` is a **schema-level enum** — not config-configurable —
because the model's spending-base and coverage logic depends on stable
bucket semantics.

```python
_LIQUIDITY_BUCKET_LITERAL = Literal[
    "cash_equivalent",    # money market, T-bills, bank sweep — T+0/T+1
    "daily_liquid",       # public equity/ETF/IG bond, T+2 settlement
    "semi_liquid",        # quarterly/annual redemption with notice; HFs
    "illiquid",           # PE/PC/RE funds, lockup > 1yr, no redemption
    "locked_strategic",   # no near-term exit path; early-vintage PE
    "re_stabilized",      # income-producing RE, monetize ~12–24 mo
    "re_development",     # development stage, monetize 2–4 yr
    "re_land",            # raw land, monetize 3+ yr
    "opco_strategic",     # operating company strategic hold
]

_ASSET_CLASS_LITERAL = Literal[
    "public_equity", "fixed_income_public", "cash_equivalent",
    "hedge_fund", "private_equity", "private_credit",
    "real_estate_equity", "real_estate_debt",
    "infrastructure", "commodity", "direct_operating", "other",
]
```

**Tightening T3 — explicit Phase 15 → Phase 12 liquidity tier mapping:**
Phase 15 bucket taxonomy is preserved. A deterministic, configurable
mapping layer bridges to Phase 12 spending-base tiers.

| Phase 15 ``liquidity_bucket`` | Phase 12 tier (default) | Configurable? |
|---|---|---|
| ``cash_equivalent`` | ``liquid`` | no |
| ``daily_liquid`` | ``liquid`` | no |
| ``semi_liquid`` | ``semi_liquid`` | no |
| ``illiquid`` | ``illiquid`` | no |
| ``locked_strategic`` | ``locked_strategic`` | no |
| ``re_stabilized`` | ``illiquid`` | yes — can be ``locked_strategic`` |
| ``re_development`` | ``locked_strategic`` | no |
| ``re_land`` | ``locked_strategic`` | no |
| ``opco_strategic`` | ``locked_strategic`` | no |

Override via ``PositionManifestConfig.liquidity_tier_overrides:
dict[str,str] | None``. Defaults applied when field absent. Report
emits the effective mapping so it is always visible.
``re_stabilized`` income-producing status never silently upgrades to
``liquid``.

---

### ``income_cash_flow_flag`` authority (Tightening T4)

Human-authored only. Discovery may propose candidates in
``local_private`` mode (e.g., a column named "distribution" with
non-zero values is flagged as a candidate in the draft YAML with a
``# PROPOSED — confirm before use`` comment). The manifest field is
the authority. Ingestion reads only the manifest value.

Posture mirrors Phase 14's ``distributable_candidate``.

---

### ``position_terms_status`` diagnostics (reviewer addition)

Each ``ManagerTermsRecord`` is classified:

| Status | Condition |
|---|---|
| ``complete_terms`` | All required fields populated for given confidence level |
| ``partial_terms`` | Some required fields missing for given confidence level |
| ``missing_terms`` | Position has no ``manager_id`` linkage |
| ``unknown_confidence`` | ``confidence="unknown"`` regardless of field population |

Emitted as ``position_terms_status: dict[str,int]`` in
``PositionIngestionDiagnostics``. Positions with ``missing_terms`` or
``partial_terms`` listed by ``position_id`` in
``positions_with_incomplete_terms: list[str]``.

---

### Manifest schema: ``PositionManifestConfig``

```
PositionManifestConfig:
    manifest_version         str   URL-safe, required
    workbook_version         str   URL-safe, required
    expected_filename        str
    as_of_date               date  manifest-level valuation date fallback
    accounts                 list[AccountSheetSpec]
    manager_terms            list[ManagerTermsRecord]
    liquidity_tier_overrides dict[str,str] | None   Phase15→Phase12 mapping

AccountSheetSpec:
    account_id               str   URL-safe; or "synthetic:<sheet_id>"
    entity_id                str   FK → EntitySheetSpec.entity_id
    sheet_name               str   local-private only
    layout_type              Literal["flat_position","account_position",
                                     "display_only"]
    header_row_index         int | None
    value_column_index       int   market_value_usd column
    name_column_index        int   asset name column (label export)
    position_column_mappings dict[str,int]   field → column index
    valuation_date           date | None   fallback before manifest as_of_date
```

---

### Discovery layer

Same two-stage pattern as Phase 14.2.

**Stage 1 — ``discover_investment_summary(path)``**

Read-only structural scan:

* Detect header rows (scan rows 1–10).
* Detect candidate columns by header text (value, cost, commitment,
  manager, asset-class keywords).
* Classify sheets by role: ``account_sheet``, ``aggregate_summary``,
  ``display_only``.
* Detect valuation date from header or sheet name.
* Returns ``InvestmentSummaryDiscoveryResult``.

**Stage 2 — ``build_draft_position_manifest(discovery, *, mode)``**

* ``privacy_safe`` — redacts person/fund identifiers; structural scaffold
  only.
* ``local_private`` — full names preserved; path-safety: refuses
  non-``_local.yaml`` outputs.
* ``column_mappings`` left as ``<TODO>`` — human confirms field → column.
* ``manager_terms`` left empty — human-authored entirely.
* ``income_cash_flow_flag`` proposals marked
  ``# PROPOSED — confirm before use`` (T4).

**CLI:**

```
python -m aa_model.ingestion.discover_investment_summary \
    --workbook PATH \
    --mode {privacy_safe,local_private} \
    [--out PATH] \
    [--dry-run]
```

---

### Ingestion entry point

```python
ingest_investment_summary(
    workbook_path: Path,
    manifest: PositionManifestConfig,
    *,
    manifest_version: str,
) -> PositionIngestionResult
```

```
PositionIngestionResult:
    accounts:    list[AccountRecord]
    positions:   list[PositionRecord]
    diagnostics: PositionIngestionDiagnostics

PositionIngestionDiagnostics:
    workbook_hash                          str
    workbook_version                       str
    manifest_version                       str
    formula_cache_caveat                   str   standing CAVEAT (Phase 14 RT1)
    positions_total                        int
    positions_by_bucket                    dict[str,int]
    positions_by_asset_class               dict[str,int]
    positions_missing_bucket               int
    positions_missing_manager              int
    unfunded_total_usd                     float
    manager_terms_coverage                 dict[str,str]
    positions_with_incomplete_terms        list[str]   position_id only
    position_terms_status                  dict[str,int]
    positions_with_fallback_valuation_date int
    stale_valuation_count                  int
    max_valuation_age_days                 int
    unmatched_rows                         list[int]   row positions only
```

---

### What must NOT be inferred

| Inference | Why not |
|---|---|
| Legal liquidity beyond documented terms | A quarterly fund may be gated or side-pocketed |
| Tax lot treatment or embedded gain | Not present in position labels |
| Whether OpCo / RE appraisal is spendable | Appraisal is not liquidity — standing principle |
| Whether manager-reported NAV is immediately accessible | Gate / notice / side-pocket status must be human-authored |
| Actual redemption availability | Only documented terms assertable; actuals depend on fund conditions |
| ``income_cash_flow_flag`` from asset name | Human-authored in manifest (T4) |
| ``manager_id`` assignment from position name | Human-authored in manifest |

---

### Integration with existing model layers

| Existing layer | Phase 15 connection |
|---|---|
| ``CMAConfig.liquidity`` (Phase 12) | ``liquid_nav`` eventually derived from PositionRecord sums by bucket (T3 mapping); currently still config-asserted until integration phase |
| ``spending_base`` (Phase 12) | Phase 15 enables computing ``liquid_nav`` as derived fact |
| ``distribution_inflow`` producer (Phase 12.5–14) | ``income_cash_flow_flag=True`` positions link to cash-flow layer |
| ``entity_id`` (Phase 14) | ``AccountRecord.entity_id`` FK ties positions to entity sheets |
| PE pacing (future) | Private equity ``PositionRecord`` rows feed pacing layer |
| Liquidity coverage (L20, future) | ``sum(market_value_usd where Phase12 tier in {"liquid"}) / spending_base`` |
| Monte Carlo (L2, deferred) | Requires honest NAV + liquidity + pacing — Phase 15 is prerequisite |

---

### Report section (advisory)

```
## Position universe (Phase 15, advisory)
  workbook_hash:  ...
  as_of_date:     ...
  positions_total: N    accounts_total: N

  NAV by liquidity bucket:
    cash_equivalent:    $X      daily_liquid:       $X
    semi_liquid:        $X      illiquid:           $X
    locked_strategic:   $X      re_stabilized:      $X
    re_development:     $X      re_land:            $X
    opco_strategic:     $X

  NAV by asset class:  [table]
  Unfunded commitments: $X

  Manager terms coverage:
    complete_terms:     N       partial_terms:      N
    missing_terms:      N       unknown_confidence: N

  Stale valuations (> 90d): N    Max valuation age: D days
  Positions missing bucket: N

  CAVEAT: Formula-cache stale-state risk applies (Phase 14 RT1).
  CAVEAT: market_value_usd reflects manager-reported NAV; legal
          liquidity may differ due to gates, side pockets, lockups,
          or notice periods not captured in ManagerTermsRecord.
  CAVEAT: liquidity_bucket reflects human-authored classification;
          economic or legal liquidity is not automatically inferred.
```

---

### New files (implementation targets)

```
src/aa_model/ingestion/schemas_position.py
    AccountRecord, PositionRecord, ManagerTermsRecord,
    PositionManifestConfig, PositionIngestionDiagnostics,
    AccountSheetSpec

src/aa_model/ingestion/investment_summary.py
    ingest_investment_summary()

src/aa_model/ingestion/liquidity_mapping.py
    Phase15→Phase12 bucket mapping + override resolution

src/aa_model/ingestion/discovery_position.py
    discover_investment_summary(), build_draft_position_manifest(),
    InvestmentSummaryDiscoveryResult

src/aa_model/ingestion/discover_investment_summary.py
    CLI entry point

configs/investment_summary_manifest.yaml
    committed scaffold (privacy_safe, <TODO_*> placeholders)

tests/test_phase15_position_ingestion.py
    synthetic fixtures only
```

---

### Test discipline

* Synthetic workbook fixture only — no real workbook, no real position
  or fund names.
* ``PositionRecord`` validates ``liquidity_bucket`` enum; rejects
  ``market_value_usd < 0``.
* ``ManagerTermsRecord`` T5 completeness validation exercises all
  three confidence levels + ``"unknown"`` placeholder path.
* ``gate_pct`` and ``carry_pct`` reject values outside ``[0.0, 1.0]``.
* Positions missing ``liquidity_bucket`` surface in diagnostics without
  raising.
* Stale-valuation count increments correctly in diagnostics.
* ``PositionManifestConfig.model_validate`` round-trips cleanly.
* T3 mapping: default and override paths both tested.
* Discovery dry-run emits no workbook content to stdout.
* Default configs byte-stable across runs.

---

### Reviewer tightenings applied (five)

1. Normalized output always has ``AccountRecord``; flat sheets
   synthesize ``account_id = "synthetic:<sheet_id>"``,
   ``account_type = "direct"``.
2. ``PositionRecord.valuation_date`` required after fallback; stale
   valuation diagnostics added.
3. Phase 15 ``liquidity_bucket`` taxonomy kept; explicit mapping to
   Phase 12 tiers via ``liquidity_tier_overrides``; ``re_stabilized``
   never silently upgrades to ``liquid``.
4. ``income_cash_flow_flag`` human-authored only; discovery proposes
   candidates in ``local_private`` mode with ``# PROPOSED`` marker.
5. ``ManagerTermsRecord`` confidence/completeness validation: partial
   or missing required fields for non-``"unknown"`` confidence raises
   at manifest validation time.

---

### Out of scope for Phase 15

* No Monte Carlo (L2 deferred).
* No automatic legal/tax interpretation.
* No automatic redemption modeling.
* No fee drag calculation.
* No workbook mutation.
* No real workbook committed.
* No live values in docs or tests.
* No L19 status change.
* No Phase 12 ``liquid_nav`` re-wiring yet (integration phase follows
  Phase 15 ingestion).

---

## Phase 16 design (pre-implementation) — L20 liquidity coverage diagnostics

### Motivation

Phase 15 gave the model position-level ``liquidity_bucket`` tags. Phase 16
turns those tags into coverage ratios — the first time the model can answer
**"does the SFO have enough liquid resources to meet its obligations?"**
from actual holdings rather than config assertions.

**L20 gate:**

* PARTIALLY RESOLVED when: coverage computed from synthetic / config
  position data + spending base.
* RESOLVED when: live ingested positions + validated ratios + human review.

**Standing principles preserved:**

```
NAV is not liquidity.
Appraisal value is not spending capacity.
Semi-liquid availability depends on gates, notice periods, and fund
conditions the model cannot assert.
```

---

### Reviewer tightenings (six)

1. ``LiquidityObligationConfig`` is standalone in Phase 16; not wired into
   ``StudyConfig``. StudyConfig wiring deferred to a later orchestration phase.
2. ``LiquidityCoverageConfig`` thresholds are configurable from the start.
   Schema defaults match policy but may be overridden per study / IPS / board.
3. Semi-liquid NAV is advisory-only. Not included in breach coverage,
   liquidity runway, or next-12m obligation ratios. A future phase will
   model notice-period access.
4. ``next_12m_capital_calls_usd`` is never inferred from total unfunded
   commitments. If total unfunded > 0 and next-12m calls are unknown, an
   advisory is emitted.
5. Distinct ``liquid_nav_to_annual_income_estimate`` metric for
   ``distributable_income`` spending-base mode (stock-to-flow).
   ``liquid_to_spending_base`` is ``None`` when the spending base is
   flow-type to prevent stock/flow confusion.
6. ``total_unfunded_commitments_usd`` captured in ``LiquidityCoverageResult``.

---

### Input schemas

**``LiquidityObligationConfig``** — standalone (T1); not in StudyConfig:

```
annual_spend_usd              float | None   direct or from spending base
next_12m_capital_calls_usd    float | None   T4: never inferred from unfunded
next_12m_tax_obligations_usd  float | None   advisory input
next_12m_entity_obligations_usd float | None advisory input
note                          str | None
```

**``LiquidityCoverageConfig``** — thresholds (T2):

```
liquid_coverage_breach_threshold       float   default 1.0
liquid_coverage_warning_threshold      float   default 2.0
illiquid_concentration_warning_pct     float   default 0.60
capital_call_coverage_warning_ratio    float   default 1.0
missing_bucket_warning_threshold       int     default 1
runway_horizon_quarters                int     default 8
```

---

### Computed quantities

NAV sums via Phase 15 → Phase 12 tier mapping
(``liquidity_mapping.resolve_phase12_tier()``):

```
liquid_nav              tier == "liquid"
semi_liquid_nav         tier == "semi_liquid"  (advisory only — T3)
illiquid_nav            tier == "illiquid"
locked_strategic_nav    tier == "locked_strategic"
total_position_nav      sum of all
total_unfunded_commitments_usd   sum of unfunded_commitment_usd (T6)
```

Coverage ratios (``float | None``; ``None`` when denominator unknown or zero):

```
liquid_to_annual_spend               liquid_nav / annual_spend_usd
liquid_to_spending_base              liquid_nav / base_usd   (T5: None if flow-type)
liquid_to_next12m_obligations        liquid_nav / next12m_total
capital_call_coverage                liquid_nav / next_12m_capital_calls_usd
liquid_fraction_of_nav               liquid_nav / total_position_nav
illiquid_fraction_of_nav             (illiquid+locked) / total_position_nav
liquidity_runway_quarters            floor(liquid_nav / quarterly_spend)  T3: liquid-only
liquid_nav_to_annual_income_estimate liquid_nav / base_usd  (T5: flow-mode only)
```

---

### Warning / breach taxonomy

| Type | Condition | Class |
|---|---|---|
| Liquid below annual spend | ``liquid_to_annual_spend < 1.0`` | BREACH |
| Liquid below 2× annual spend | ``< 2.0`` | WARNING |
| Liquid below next-12m obligations | ``< 1.0`` | BREACH |
| Capital call coverage < 1× | ``< 1.0`` | WARNING |
| Illiquid concentration > 60% | ``> 0.60`` | WARNING |
| Untagged positions | ``>= 1`` | WARNING |
| Semi-liquid terms unknown | ``semi_liquid_nav_terms_unknown > 0`` | ADVISORY |
| Stale NAV positions | ``stale_nav_count > 0`` | ADVISORY |
| ``annual_spend_usd`` not provided | field is ``None`` | ADVISORY |
| T4: unfunded without next-12m calls | unfunded > 0, calls = None | ADVISORY |

---

### Entry point

```python
compute_liquidity_coverage(
    positions:             list[PositionRecord],
    obligations:           LiquidityObligationConfig,
    *,
    tier_overrides:        dict[str,str] | None = None,
    manager_terms:         list[ManagerTermsRecord] | None = None,
    spending_base:         SpendingBaseBreakdown | None = None,
    spending_base_is_flow: bool = False,
    stale_nav_count:       int = 0,
    untagged_position_count: int = 0,
    config:                LiquidityCoverageConfig | None = None,
) -> LiquidityCoverageResult
```

Pure function. No ledger reads. No side effects. Byte-stable.

---

### New files

```
src/aa_model/liquidity/__init__.py
src/aa_model/liquidity/coverage.py
    compute_liquidity_coverage()
    LiquidityObligationConfig
    LiquidityCoverageConfig
    LiquidityCoverageResult
    LiquidityCoverageDiagnostics

tests/test_phase16_liquidity_coverage.py   (synthetic fixtures only)
```

---

### Test discipline

* Synthetic ``PositionRecord`` lists with known bucket distributions.
* Coverage ratios computed from known inputs.
* BREACH / WARNING threshold boundary tests.
* Zero total NAV → ``None`` ratios; no ``ZeroDivisionError``.
* Missing obligation inputs → advisory emitted, ratio ``None``.
* T3, T4, T5, T6 tightening paths each tested.
* Byte-stable: identical inputs → identical result.
* 13 tests total. Full suite: 318 green.

---

### Out of scope for Phase 16

* No ``StudyConfig`` wiring (deferred — T1).
* No Monte Carlo.
* No semi-liquid redemption modeling (deferred — T3).
* No PE pacing / call schedule inference (deferred — T4).
* No fee drag.
* No legal / tax interpretation.
* No live workbook ingestion required.
* L20 PARTIALLY RESOLVED on synthetic + config coverage.

---

## Phase 17 — StudyConfig integration (Phases 14–16 orchestration)

**Status:** shipped.
**L-label:** L20 (liquidity coverage orchestration).
**Commit:** pending (this design-lock).

---

### Problem statement

Phases 14–16 each shipped as isolated functional layers:
Phase 14 ingests the cash-flow workbook, Phase 15 ingests the Investment
Summary, Phase 16 computes liquidity coverage from position records.
Phase 17 wires these layers together under ``StudyConfig`` so a single
``run_orchestrator`` invocation can optionally run position ingestion
and emit a liquidity coverage diagnostic alongside the existing
spending-trajectory report.

---

### Reviewer answers and tightenings

1. **Helper placement:** ``load_position_manifest(path)`` lives in
   ``aa_model.ingestion.investment_summary`` (ingestion layer), not in the
   orchestrator. The orchestrator imports it lazily at run time.

2. **Path not inline:** ``PositionIngestionConfig.manifest_path`` is a
   path string. The manifest is not embedded inline in ``StudyConfig``
   (cf. ``WorkbookIngestionConfig.manifest`` which embeds the raw dict).
   Separating path from data keeps the config YAML human-readable and
   allows the manifest YAML to evolve independently.

3. **Fail fast:** ``FileNotFoundError`` is raised at the top of the
   position-ingestion block if either ``workbook_path`` or
   ``manifest_path`` does not exist. No partial-result silencing.

4. **Orchestration helper signature:**
   ``_run_liquidity_coverage(position_result, position_manifest, cfg)``
   threads:
   * ``positions`` from ``position_result.positions``
   * ``manager_terms`` from ``position_manifest.manager_terms``
   * ``tier_overrides`` from ``position_manifest.liquidity_tier_overrides``
   * ``liquidity_obligations`` from ``cfg.liquidity_obligations`` (raw dict
     → ``LiquidityObligationConfig.model_validate``)
   * ``liquidity_coverage_config`` from ``cfg.liquidity_coverage_config``
     (raw dict → ``LiquidityCoverageConfig.model_validate``)
   * ``spending_base_is_flow`` derived from
     ``cfg.spending.guardrail.spending_base == "distributable_income"``

5. **Explicit spending_base_mode:** ``render_coverage_report_section``
   now accepts ``spending_base_mode: str | None`` explicitly (not inferred
   from the result object). The orchestrator derives it from
   ``cfg.spending.guardrail.spending_base`` and passes it through the
   report layer.

---

### Architecture

```
StudyConfig
  └─ position_ingestion: PositionIngestionConfig | None
       ├─ workbook_path: str
       └─ manifest_path: str   ← path to PositionManifestConfig YAML
  └─ liquidity_obligations: dict | None   ← LiquidityObligationConfig raw
  └─ liquidity_coverage_config: dict | None   ← LiquidityCoverageConfig raw

run_orchestrator
  └─ _build_ledger
       └─ (after per-quarter loop)
            ├─ load_position_manifest(manifest_path) → PositionManifestConfig
            ├─ ingest_investment_summary(workbook_path, manifest) → PIR
            └─ _run_liquidity_coverage(PIR, manifest, cfg) → LCR

write_markdown_report
  └─ position_ingestion_result → render_position_report_section
  └─ liquidity_coverage_result → render_coverage_report_section(result, spending_base_mode)
```

**Default-off byte-stable:** ``cfg.position_ingestion = None`` ⇒ no
position ingestion ⇒ ``position_ingestion_result = None`` ⇒
``liquidity_coverage_result = None`` ⇒ no new sections in the report.
All pre-Phase-17 trajectories remain byte-identical.

**``SpendingBaseBreakdown`` integration deferred to Phase 18+:** The
``_run_liquidity_coverage`` helper passes ``spending_base=None``. The
``SpendingBaseBreakdown`` object lives inside the OwlRule per-quarter
state and is not yet surfaced into a form the orchestrator can pass.
This means ``liquid_to_spending_base`` and
``liquid_nav_to_annual_income_estimate`` are ``None`` in the Phase 17
orchestrator path; they are fully exercised in the Phase 16 synthetic
tests.

---

### Key schemas and functions

```
src/aa_model/io/schemas.py
    PositionIngestionConfig
    StudyConfig.position_ingestion (PositionIngestionConfig | None)
    StudyConfig.liquidity_obligations (dict | None)
    StudyConfig.liquidity_coverage_config (dict | None)

src/aa_model/ingestion/investment_summary.py
    load_position_manifest(path) → PositionManifestConfig

src/aa_model/liquidity/coverage.py
    render_coverage_report_section(result, spending_base_mode: str | None)

src/aa_model/integration/orchestrator.py
    _run_liquidity_coverage(position_result, position_manifest, cfg)
    _build_ledger (extended return tuple — adds PIR, LCR)
    run_orchestrator (passes PIR + LCR to write_markdown_report)

src/aa_model/integration/report.py
    write_markdown_report (position_ingestion_result, liquidity_coverage_result params)
    ## Position universe (Phase 15, advisory) section
    ## Liquidity coverage (Phase 16, advisory) section

tests/test_phase17_study_integration.py   (synthetic fixtures only)
```

---

### Test discipline

* Synthetic ``PositionRecord`` lists — no live workbook dependency.
* ``PositionIngestionConfig`` schema validation (valid, missing field, colon).
* ``StudyConfig.position_ingestion`` default-off verified.
* ``load_position_manifest`` fail-fast on missing file; valid YAML round-trip.
* ``_run_liquidity_coverage`` wiring verified end-to-end with synthetic data.
* ``render_coverage_report_section`` spending_base_mode label tested (set and None).
* 9 tests total. Full suite: 284 green (4 pre-existing cvxportfolio failures unrelated).

---

### Out of scope for Phase 17

* No ``SpendingBaseBreakdown`` → ``LiquidityCoverageResult`` integration
  (deferred to Phase 18+).
* No Monte Carlo.
* No PE pacing integration.
* No semi-liquid redemption modeling.
* No L19 / L20 full resolution.

---

## Phase 18 design-lock — SpendingBaseBreakdown bridge

**Commit:** (Phase 18)
**Status:** LOCKED

### Problem

Phase 17's ``_run_liquidity_coverage`` hardcodes ``spending_base=None``,
leaving ``liquid_to_spending_base`` and
``liquid_nav_to_annual_income_estimate`` always ``None`` in study runs even
when the OwlRule has completed a year-boundary evaluation and has a realized
``SpendingBaseBreakdown`` in its diagnostics dict.

### Solution

Two new private functions in ``orchestrator.py`` reconstruct a
``SpendingBaseBreakdown`` object from the Owl diagnostics dict (no new
accessor on OwlRule) and thread it through to
``compute_liquidity_coverage`` via ``_run_liquidity_coverage``.

### Reconstruction contract (no OwlRule accessor)

``OwlRule.diagnostics()`` returns a plain ``dict`` keyed by strings. Phase 18
reads the following fields:

* ``engine``: must equal ``"OwlRule"`` — guard for non-Owl rules.
* ``spending_base_mode``: ``None`` means total_nav (the default); string
  values are the configured spending-base name.
* ``spending_base_run_end_usd``: float, 0.0 when no year-boundary snapshot
  exists yet.
* ``excluded_nav_by_tier_usd``: ``dict[str, float]`` — NAV excluded by
  liquidity tier.
* ``excluded_nav_by_income_flag_usd``: nominally ``dict[bool, float]`` but
  defensively normalized for string keys from JSON/YAML round-trips.
* ``trailing_distributable_income_usd``: float, 0.0 when no snapshot.
* ``distributable_income_by_source_usd``: ``dict[str, float]``.
* ``used_bootstrap_at_run_end``: bool.

### _normalize_bool_keyed_dict

Converts a dict whose keys may be Python ``bool`` or strings
(``"true"``/``"false"``/``"True"``/``"False"``) to ``dict[bool, float]``.
Guards against silent key-type drift when diagnostics pass through
JSON/YAML serialization.

### _extract_spending_base_for_coverage

Returns ``(SpendingBaseBreakdown | None, list[str])`` — breakdown and
bridge advisories.

Return paths:

1. ``spending_diagnostics is None`` or ``engine != "OwlRule"`` → ``(None, [])``.
   Non-Owl rules produce no bridge.
2. ``distributable_income`` mode, ``trailing_distributable_income_usd <= 0.0``
   → ``(None, [run-too-short advisory])``. Run has not yet crossed a
   year-boundary with distributable-income data.
3. ``distributable_income`` mode, valid trailing income → ``(breakdown, [])``
   with bootstrap advisory appended when ``is_bootstrap=True``. The
   breakdown carries ``base_usd = trailing_distributable_income_usd``;
   ``excluded_by_tier_usd`` and ``excluded_by_income_flag_usd`` are empty
   (flow-side base has no NAV exclusions).
4. NAV-side mode (``None``=total_nav or named NAV base),
   ``spending_base_run_end_usd <= 0.0`` → ``(None, [run-too-short advisory])``.
5. NAV-side mode, valid base → ``(breakdown, [])``. The breakdown carries
   ``base_usd``, normalized tier and income-flag exclusion dicts.

### Constraint 6: annual_spend_usd and spending_base are orthogonal

``annual_spend_usd`` (for ``liquid_to_annual_spend``) is set via
``LiquidityObligationConfig`` — an explicit obligation input that has no
dependency on the Owl spending-base reconstruction. The Phase 18 bridge
adds ``liquid_to_spending_base`` (and optionally
``liquid_nav_to_annual_income_estimate``) as a second ratio from the Owl
reconstruction. The two ratios answer different questions and are computed
independently.

### Advisory injection pattern

``LiquidityCoverageDiagnostics.advisories`` is a mutable list on a
non-frozen dataclass. Bridge advisories (bootstrap, run-too-short) are
appended to ``liquidity_coverage_result.diagnostics.advisories`` after
``compute_liquidity_coverage`` returns, inside ``_build_ledger``. This
keeps the pure function ``compute_liquidity_coverage`` free of orchestrator
concerns.

### Default-off byte-stability

When ``cfg.position_ingestion is None`` the bridge is never called.
``_extract_spending_base_for_coverage`` is always invoked after the
per-quarter loop (so spending diagnostics are complete), but its output is
discarded when position ingestion is not configured. Pre-Phase-17
trajectories remain byte-identical.

### Key schemas and functions

```
src/aa_model/integration/orchestrator.py
    _normalize_bool_keyed_dict(d: dict) → dict[bool, float]
    _extract_spending_base_for_coverage(spending_diagnostics, cfg)
        → tuple[SpendingBaseBreakdown | None, list[str]]
    _run_liquidity_coverage (spending_base: object = None param added)
    _build_ledger (calls _extract_spending_base_for_coverage; injects advisories)

tests/test_phase18_spending_base_bridge.py   (synthetic fixtures only)
```

### Test discipline

* ``_normalize_bool_keyed_dict``: Python bool keys pass-through; string key
  normalization (``"true"``/``"False"`` → bool).
* ``_extract_spending_base_for_coverage``: None diagnostics; non-Owl engine;
  NAV-side run-too-short; NAV-side year-boundary; distributable_income
  bootstrap advisory; distributable_income run-too-short.
* 8 tests total. Full suite: 326 green (4 pre-existing cvxportfolio failures
  unrelated to Phase 18).

### Out of scope for Phase 18

* No Monte Carlo.
* No PE pacing integration.
* No semi-liquid redemption modeling.
* No L19 full resolution.
* No changes to ``StudyConfig`` fields.
* No new ``OwlRule`` accessor methods — reconstruction from diagnostics dict
  only.

---

## Phase 19 design-lock — PE pacing → next-12m capital-call obligation bridge

**Commit:** (Phase 19)
**Status:** LOCKED

### Problem

``capital_call_coverage`` is always ``n/a`` in study runs because
``LiquidityObligationConfig.next_12m_capital_calls_usd`` has no
deterministic source. Phase 16 T4 explicitly refused to infer calls
from static unfunded commitments. The right source — deterministic
forward PE pacing projections — was already computed in ``_build_ledger``
but not wired into the liquidity coverage layer.

### Solution

New module ``pe/call_obligation.py`` with a pure function
``derive_pe_capital_call_obligation`` that sums projected ``call_usd``
from ``pe_proj`` over the next-4-quarter window following the position
snapshot ``as_of_date``. The result is threaded into
``_run_liquidity_coverage`` via a new ``pe_call_obligation_usd``
parameter, populating ``LiquidityObligationConfig.next_12m_capital_calls_usd``
deterministically.

### Coverage measurement quarter

``pd.Period(position_manifest.as_of_date, freq="Q-DEC")`` — the quarter
enclosing the position snapshot. No new config field required. The
next-12m window is ``coverage_quarter+1`` through ``coverage_quarter+4``.

### pe_proj schema

Tidy frame from ``PEAdapter.project_horizon`` (PROJECTION_COLUMNS +
sleeve). ``quarter`` column is a string (e.g. ``"2026Q1"``). Both TA
and STAIRS adapters add the ``sleeve`` column.

### Override precedence

1. Explicit ``liquidity_obligations.next_12m_capital_calls_usd`` set by
   user → ``source = "explicit"``. Resolved in ``_build_ledger`` before
   calling ``derive_pe_capital_call_obligation``.
2. PE-derived sum of ``call_usd`` for next-4-quarter window, ``> 0`` →
   ``source = "pe_pacing"``, value populated.
3. PE-derived sum = 0.0 → ``source = "pe_pacing"``,
   ``next_12m_capital_calls_usd = None`` + advisory ("funds past
   commitment period"). T4: a zero-denominator obligation is not a
   useful coverage input; None is more honest.
4. Empty ``pe_proj`` → ``source = "unavailable"``,
   ``next_12m_capital_calls_usd = None`` + advisory.

### T4 boundary preserved

Calls are derived only from the forward pacing model projection. Static
unfunded commitments × a percentage are never used.

### Partial-horizon advisory

When fewer than 4 of the next-12m window quarters appear in ``pe_proj``
(run horizon shorter than coverage window), an advisory lists the missing
quarters. The derived call sum covers only the projected quarters.

### PECallObligationBridgeDiagnostics

``source``, ``coverage_quarter``, ``quarters_included``,
``quarters_in_horizon``, ``fund_count``, ``calls_by_quarter``,
``top_contributors`` (top 5 fund/call pairs), ``advisories``.

### Orchestrator wiring

``_build_ledger`` 11th return element: ``pe_call_bridge_diag``.
``run_orchestrator`` unpacks 11 elements and passes
``pe_call_bridge_diag`` to ``write_markdown_report``.

``_run_liquidity_coverage`` new parameter: ``pe_call_obligation_usd``.
When not ``None``, injected into ``LiquidityObligationConfig`` dict
before validation (precedence already resolved by ``_build_ledger``).

### Report section

``## PE capital-call obligation bridge (Phase 19, advisory)`` rendered
for all three source values when ``pe_call_bridge_diag is not None``.
Shows source, coverage quarter, per-quarter call breakdown, top
contributors, and advisories.

### Default-off byte-stability

``pe_call_bridge_diag = None`` when ``cfg.position_ingestion is None``.
Pre-Phase-17 trajectories unchanged.

### Key schemas and functions

```
src/aa_model/pe/call_obligation.py               (new)
    PECallObligationBridgeDiagnostics
    derive_pe_capital_call_obligation(pe_proj, coverage_quarter)
        → PECallObligationBridgeDiagnostics

src/aa_model/integration/orchestrator.py
    _run_liquidity_coverage (pe_call_obligation_usd param added)
    _build_ledger (Phase 19 wiring; 11-element return tuple)
    run_orchestrator (unpacks 11; passes pe_call_bridge_diag to report)

src/aa_model/integration/report.py
    write_markdown_report (pe_call_bridge_diag param)
    ## PE capital-call obligation bridge section

tests/test_phase19_pe_call_obligation.py         (new, synthetic only)
```

### Test discipline

* Explicit user override preserved (source="explicit").
* PE-derived calls → ``capital_call_coverage`` finite.
* Empty ``pe_proj`` → source="unavailable" + advisory.
* Zero calls in window → ``None`` + advisory.
* Same inputs → same output (deterministic contract).
* Default configs byte-stable (bridge inactive).
* 6 tests total. All 17/18/19 targeted tests: 23/23 green.
  Full suite: 298 green (4 pre-existing cvxportfolio failures
  unrelated to Phase 19).

### Out of scope for Phase 19

* No Monte Carlo.
* No secondary-market or fee modeling.
* No semi-liquid redemption modeling.
* No ``StudyConfig`` schema changes.
* No stochastic pacing.
* No L20 full resolution.

---

## Phase 20 design-lock — PE call-obligation reconciliation to the cash-flow worksheet

**Commit:** Phase 20 / L20

### Motivation

Phase 19 populated ``next_12m_capital_calls_usd`` from PE pacing projections
alone. The standing constraint requires spending / liquidity / PE pacing to
stay aligned with Cashflow Modeling v7.xlsx. Phase 20 inserts the cash-flow
worksheet as the primary obligation source and uses PE pacing as a
deterministic cross-check, preventing the two forecasts from silently
diverging.

### Source precedence

```
explicit_config > cashflow_workbook > pe_pacing_model > unavailable
```

* **explicit_config** — ``cfg.liquidity_obligations.next_12m_capital_calls_usd``
  set by user. Overrides everything. No reconciliation delta computed.
* **cashflow_workbook** — ``CashFlowLineRecord`` rows where
  ``category == "capital_call"`` and ``direction == "outflow"``,
  summed (as positive USD) over the next-4-quarter window.
  Available when ``workbook_ingestion_result`` is not None and at least
  one qualifying line falls in the window.
* **pe_pacing_model** — Phase 19 ``derive_pe_capital_call_obligation``
  result. Used when workbook lines are absent.
* **unavailable** — neither source produced next-12m calls.
  Obligation remains None; advisory emitted.

When both workbook and PE pacing are available, the reconciliation delta is
computed regardless of which source wins the obligation value.

### Category convention

Workbook capital-call lines are identified by ``category == "capital_call"``
on the ``RowClassificationRule`` in the manifest. No entity-type filter:
the category string is the classification boundary (Q5). No schema changes
to ``CashFlowLineRecord`` or ``RowClassificationRule`` — ``category`` is
already a free-form ``str``.

### Workbook lines outside the next-12m window

If qualifying capital-call lines exist but none fall in the next-4-quarter
window, an advisory is emitted and the source falls through to
``pe_pacing_model`` (Q3).

### Reconciliation delta classification

| Band       | Condition               | Action in Phase 20        |
|------------|-------------------------|---------------------------|
| ``n/a``    | Only one source present | No cross-check possible   |
| ``advisory`` | abs(Δ%) < 10%         | Informational             |
| ``warning``  | 10% ≤ abs(Δ%) < 25%   | Advisory in report        |
| ``blocking`` | abs(Δ%) ≥ 25%         | Strong advisory; no halt  |

Denominator = max(workbook_total, pe_total) to avoid distortion.
``blocking`` does not halt execution in Phase 20 (Q4). Hard gates deferred.

### ``WorkbookCallReconciliationDiagnostics``

Replaces ``PECallObligationBridgeDiagnostics`` as the primary report artifact.
The Phase 19 PE bridge result is embedded as ``pe_bridge`` for per-fund
breakdown. Fields: ``next_12m_capital_calls_usd``, ``source_used``,
``coverage_quarter``, ``quarters_in_window``, ``explicit_usd``,
``workbook_calls_by_quarter``, ``workbook_total_usd``, ``pe_bridge``,
``delta_by_quarter``, ``total_delta_usd``, ``total_delta_pct``,
``delta_classification``, ``advisories``.

### Report section

``## PE call-obligation reconciliation (Phase 20, advisory)`` replaces the
Phase 19 bridge section. Renders source, coverage quarter, workbook and PE
side summaries, per-quarter delta table, delta classification badge, and
advisory list.

### Default-off byte-stability

``call_recon_diag = None`` when ``cfg.position_ingestion is None``.
Pre-Phase-17 trajectories unchanged. 11th ``_build_ledger`` tuple element
type changes from ``PECallObligationBridgeDiagnostics | None`` to
``WorkbookCallReconciliationDiagnostics | None``.

### Key schemas and functions

```
src/aa_model/pe/call_reconciliation.py           (new)
    WorkbookCallReconciliationDiagnostics
    aggregate_workbook_capital_calls(cash_flow_lines, coverage_quarter)
        → (dict[str, float], list[str])
    reconcile_call_obligation(workbook_lines, pe_bridge_diag,
                              coverage_quarter, explicit_usd)
        → WorkbookCallReconciliationDiagnostics

src/aa_model/integration/orchestrator.py
    _build_ledger: Phase 19 precedence block replaced by
        derive_pe_capital_call_obligation + reconcile_call_obligation
    11th return element: WorkbookCallReconciliationDiagnostics | None

src/aa_model/integration/report.py
    write_markdown_report: call_recon_diag param replaces pe_call_bridge_diag
    ## PE call-obligation reconciliation section

tests/test_phase20_pe_call_reconciliation.py     (new, synthetic only)
```

### Test discipline

* explicit_config overrides workbook + PE pacing.
* Workbook present → source_used=cashflow_workbook; delta computed.
* Workbook absent → source_used=pe_pacing_model; delta_classification=n/a.
* Delta > 25% → blocking advisory; workbook value still used.
* Both absent → unavailable + advisory.
* Default configs byte-stable (11th element None).
* 6 tests total. All 17/18/19/20 targeted tests: 29/29 green.
  Full suite: 298 green (4 pre-existing cvxportfolio failures
  unrelated to Phase 20).

### Out of scope for Phase 20

* No Monte Carlo.
* No hard blocking gate (deferred to Phase 21).
* No stochastic pacing.
* No workbook mutation.
* No StudyConfig schema changes.
* No legal/tax/entity-governance inference.
* No L20 full resolution.

---

## Phase 21 design-lock — reconciliation gates / policy thresholds

**Commit:** Phase 21 / L20

### Motivation

Phase 20 classified workbook-vs-PE-pacing deltas as advisory/warning/blocking
but treated all four as informational. Phase 21 binds configurable gate actions
to those classifications, allowing a blocking delta to require an explicit
written justification before the run proceeds.

### Gate severity hierarchy

| Level | Behavior |
|---|---|
| `advisory` | Delta in report; run continues |
| `warning` | Prominent advisory; run continues |
| `requires_override` | Run halts unless justification string provided |
| `hard_fail` | Run always halts; no override accepted |

Default mapping: advisory→advisory, warning→warning, blocking→requires_override.
`hard_fail` is opt-in only; never the default.

### Default policy

```yaml
reconciliation_gates:
  warning_pct: 0.10
  blocking_pct: 0.25
  warning_usd: null
  blocking_usd: null
  blocking_action: requires_override
  warning_action: warning
  require_call_source: false
```

### Threshold trigger

A delta triggers the blocking gate if EITHER `blocking_pct` is exceeded OR
`blocking_usd` is exceeded (max(percent, dollar)). Dollar thresholds are
`null` by default — effectively percentage-only until the user configures a
floor. Same logic for warning thresholds. `threshold_triggered` field records
which condition fired: `"pct"` | `"usd"` | `"both"` | `"source_missing"` |
`"none"`.

### explicit_config bypass

When `source_used == "explicit_config"`, gate enforcement is bypassed — no
justification string required, no halt. The reconciliation delta is still
computed and reported. Rationale: the user has already asserted the obligation
value; requiring a second justification is redundant friction.

### Override mechanism

```yaml
liquidity_obligations:
  reconciliation_override:
    capital_calls: "justification string"
```

The justification string is stored verbatim in `ReconciliationGateResult`
and captured in diagnostics. An empty or whitespace-only string is treated
as not provided. The report redacts the raw text to `[justification provided]`
to avoid committing sensitive detail.

### hard_fail vs requires_override

`blocking_action: "hard_fail"` raises even when an override justification is
present. These are distinct policy modes: `requires_override` gives the user
an escape hatch; `hard_fail` does not.

### require_call_source

When `require_call_source: true`, `source_used == "unavailable"` itself
triggers the blocking gate (threshold_triggered="source_missing"). Default
`false` — missing source remains advisory.

### Architecture

`evaluate_reconciliation_gate(recon_diag, gates_cfg, override_justification)`
is a pure function that returns `ReconciliationGateResult`. The orchestrator
raises `ReconciliationGateError` when `gate_result.passes` is False. Gate
logic is testable without side effects.

`WorkbookCallReconciliationDiagnostics.gate_result` carries the
`ReconciliationGateResult` into the report after gate evaluation.

### L20 resolution

L20 is not fully resolved by Phase 21. Resolution requires:
1. Phase 21 gates operational (complete with this commit).
2. A live workbook run producing `source_used="cashflow_workbook"` in the
   report — confirming `category="capital_call"` is implemented in the local
   manifest. Synthetic gate tests cannot confirm worksheet alignment.

### Key schemas and functions

```
src/aa_model/pe/reconciliation_gates.py          (new)
    ReconciliationGatesConfig (Pydantic)
    ReconciliationGateResult (dataclass)
    ReconciliationGateError (ValueError subclass)
    evaluate_reconciliation_gate(recon_diag, gates_cfg, override_justification)
        → ReconciliationGateResult

src/aa_model/pe/call_reconciliation.py
    WorkbookCallReconciliationDiagnostics.gate_result field added

src/aa_model/io/schemas.py
    StudyConfig.reconciliation_gates: dict | None = None

src/aa_model/integration/orchestrator.py
    Gate evaluation block after reconcile_call_obligation
    ReconciliationGateError raised when gate_result.passes is False

src/aa_model/integration/report.py
    ### Reconciliation gate (Phase 21) subsection

tests/test_phase21_reconciliation_gates.py       (new, synthetic only)
```

### Test discipline

* advisory delta passes.
* warning delta passes with advisory.
* blocking delta without override → passes=False; raises ReconciliationGateError.
* blocking delta + override → passes=True; override_applied=True.
* unavailable source: require_call_source controls gate behavior.
* default configs byte-stable (gate not evaluated when position_ingestion=None).
* 6 tests total. All 17/18/19/20/21 targeted tests: 35/35 green.
  Full suite: 298 green (4 pre-existing cvxportfolio failures unrelated).

### Out of scope for Phase 21

* No hard blocking gate by default (opt-in only).
* No Monte Carlo.
* No workbook mutation.
* No legal/tax/entity-governance inference.
* No L20 full resolution (requires live workbook run).

### 2026-05-03 — docs(phase-14): cross-walk workflow doc (workbook → manifest)

* **What.** Adds `docs/phase_14_workbook_crosswalk_workflow.md` as a generic,
  reusable operational guide for turning the committed scaffold
  (`configs/workbook_v7_manifest.yaml`) into a locally-classified, runnable
  workbook ingestion. The doc carries no live data (no real sheet names, no
  row labels, no person/entity identifiers, no values).
* **Why.** The workflow had been re-derived ad-hoc across multiple sessions
  from chat history. Memorializing it as a tracked doc means future sessions
  inherit the steps, the privacy posture, the schema reference, the
  open-risks template, and the L19/L20 resolution gates without re-deriving
  anything.
* **Coverage.**
  - Six-step workflow: discovery probe → cross-walk → local manifest → row
    classification → schema validation → pilot ingestion.
  - Phase 14.2 discovery-scraper invocation (`local_private` vs
    `privacy_safe` modes).
  - Schema reference (entity_type / cash_flow_role / layout_type /
    direction / domain / recurrence_type / certainty / period_header_format
    enum values + hard constraints).
  - Recommended cross-walk artifact set under `data/external/` (gitignored):
    layout summary, row-label inventory, sheet→entity table,
    mapping_crosswalk markdown.
  - Per-sheet `header_row_index` override pattern (for layout outliers
    where a sheet's header row differs from the manifest default).
  - Standing principles list (NAV is not liquidity; upstream classification
    only; worksheet is the spine; etc.).
  - Open-risks template (layout outliers, entity-type ambiguity, joint vs
    individual, project rollup vs entity, capital vs operating LLC,
    aggregate snapshot mis-classification).
  - Phase 14 → Phase 13 producer convention reminder
    (`distribution:<domain>:<id>`,
    `producer_id = f"{workbook_version}__{sheet_name}__{row_label}__{quarter}"`).
  - L19 / L20 resolution gates (live-workbook validation requirements,
    `source_used = "cashflow_workbook"` for the Phase 20 reconciliation).
* **Out of scope.** No code changes. No scaffold changes. No
  classification rules. No live workbook content.
* **Backward-compatible.** Yes — pure documentation addition. Default
  configs and trajectories byte-identical.
* **L19 / L20.** Both held at PARTIALLY RESOLVED. The doc is the
  prerequisite write-up for the operational milestones; the milestones
  themselves (locally-authored manifest + clean live ingestion run) are
  not affected by this commit.

## Phase 22 design-lock — manager terms consumer / diagnostic layer

**Commit:** Phase 22 / L20

Phase 15 introduced `ManagerTermsRecord` with fields for redemption
frequency, notice days, gate/lockup/side-pocket metadata, fee basis, carry,
and capital-call notice. Only `notice_days` was consumed (for the single
`semi_liquid_earliest_notice_days` aggregate advisory in coverage.py).
Phase 22 activates the remaining fields as a pure diagnostic layer.

### Scope

New file: `src/aa_model/liquidity/manager_terms_diagnostics.py`.
No `StudyConfig` schema change — `ManagerTermsRecord` is already
available at the orchestrator via `position_manifest.manager_terms`
from Phase 15/17.

Three sub-diagnostics, one top-level container:

**1. Liquidity horizon (`LiquidityHorizonDiagnostics`)**

Scope: positions whose Phase-12 tier resolves to `"semi_liquid"` via
`resolve_phase12_tier(pos.liquidity_bucket, tier_overrides)`.
Tier overrides are threaded from `position_manifest.liquidity_tier_overrides`
(tightening 1 — consistent with coverage.py).

`effective_window_days` computation (int | None, days from as_of_date):

| `redemption_frequency` | Base days |
|---|---|
| `"daily"` | 0 |
| `"monthly"` | 31 |
| `"quarterly"` | 91 |
| `"semi_annual"` | 182 |
| `"annual"` | 365 |
| `"none"` | None (not redeemable on demand) |
| None or confidence="unknown" | None |

`window = base_days + (notice_days or 0)`. Lockup offset: if
`lockup_end_date > as_of_date`, `window = max(window, lockup_remaining_days)`.

Flags (advisory, non-blocking): `"gate"` (gate_pct > 0), `"side_pocket"`,
`"lockup"` (lockup_end_date in future).

Unknown terms: never zero-filled. None → `terms_unknown` bucket, advisory.

Horizon buckets: `within_90d` (≤90d), `90d_to_1y` (91–365d), `1y_to_3y`
(366–1095d), `beyond_3y` (>1095d), `terms_unknown` (None).

**2. Fee exposure (`FeeExposureDiagnostics`)**

Scope: all positions with a matched `ManagerTermsRecord`, any tier.
Annual drag estimate (1-year horizon):
- `fee_basis="nav"` + bps → `annual_fee_drag_usd = nav × bps / 10_000`
- `fee_basis="committed"` or `"invested"` → None + advisory
- `fee_basis="unknown"` or bps None → None, counts as `fee_unknown_nav`

Carry advisory (per-entry): `carry_pct > 0, hurdle_rate set` →
`"carry X% above Y% hurdle"`. Never feeds coverage ratios.

Field names used are the actual `ManagerTermsRecord` schema names:
`carry_pct`, `hurdle_rate`, `management_fee_bps`, `fee_basis` (tightening 2).

**3. Capital-call notice (`CapitalCallNoticeDiagnostics`)**

Scope: managers where `capital_call_notice_days` is set (any value ≥ 0).
Discriminator is field presence on the manager record, not liquidity tier.
Unfunded per manager: sum of `pos.unfunded_commitment_usd` for matching positions.

Purpose: ops-planning metadata only. Does not gate or modify Phase 20/21
reconciliation gate outcome. No threshold alert in Phase 22 — concentration
threshold deferred to a future phase.

**Top-level container (`ManagerTermsDiagnostics`):**
`total_positions`, `total_nav_usd`, `managers_with_terms` (confidence ≠ unknown),
`managers_without_terms`, the three sub-diagnostics, `coverage_advisories`.

### Integration

`_build_ledger` return tuple: 11 → 12 elements. 12th element:
`ManagerTermsDiagnostics | None` (None when `position_ingestion=None`
— default-off byte-stable). Computed after `_run_liquidity_coverage`,
inside the `if cfg.position_ingestion is not None:` block, using
`position_manifest.manager_terms` and `position_manifest.liquidity_tier_overrides`
already in scope.

Report: `## Manager terms (Phase 22, advisory)` section added via
`render_manager_terms_section()` in `manager_terms_diagnostics.py`.
Section contains three markdown tables (horizon, fee, notice) plus
per-bucket summaries.

### Scope boundaries

* Allocator objective: **unchanged**.
* Semi-liquid: **advisory-only** — `effective_window_days` never reclassifies
  positions into breach coverage or runway. T3 from Phase 16 preserved.
* Fee drag: **advisory only** — `total_fee_drag_usd` never feeds coverage ratios.
* `capital_call_notice_days`: **ops metadata only** — does not gate Phase 20/21.
* No legal/tax/entity-governance inference.
* No Monte Carlo.
* No `StudyConfig` schema additions.
* L20 resolution not affected (gate: `source_used="cashflow_workbook"` is
  independent of this phase).

### Tests

15 tests in `tests/test_phase22_manager_terms_diagnostics.py`. Synthetic
fixtures only. No live workbook. All tests green.

Full suite: 368 passed, 0 failures (4 pre-existing cvxportfolio failures
now resolved by the new test environment; prior baseline was 298 + 4 failures).

---

## Phase 14.3 design — workbook row-scope / data-region support

**Status: shipped**

### Motivation

L20 live validation requires `category="capital_call"` records in the
`2026Q2–2027Q1` coverage window. The only workbook entities with 2026+
quarterly capital-call data are `entity_27` (PB Westplan) and `entity_28`
(PB Westplan v2). Both sheets contain two investor sections (e.g., "Westplan
partners" rows 5–44 and "SE Holland Investors" rows 47–94) with identical
deal-name row labels repeated in each section. The current ingestor dedup key
`(entity_id, quarter, row_label)` cannot distinguish section-1 from section-2
when the same label appears in the same quarter, so ingestion raises
`ValueError: duplicate (entity_id, quarter, row_label)`.

Workbook mutation is out of scope. The fix is a manifest-side scoping field:
allow a sheet to be declared multiple times with non-overlapping row ranges so
each section becomes its own logical entity with a unique `entity_id`.

### What changes

**1. `EntitySheetSpec` — new optional field**

```python
row_range: tuple[int, int] | None = Field(default=None)
```

- 1-indexed, inclusive bounds: `[start_row, end_row]`.
- `None` (default) → no scoping; existing behavior byte-identical.
- YAML spelling: `row_range: [5, 44]`.
- Field validator: `start >= 1`, `end >= start`.
- Does NOT change `header_row_index` semantics; the header row is always
  read from its declared index regardless of `row_range`.

**2. `WorkbookManifestConfig` validators — two changes**

*a. `_sheet_names_globally_unique` — relaxed for entity/RE sheets:*

A `sheet_name` may appear more than once within `entity_sheets` +
`re_partnership_sheets` if and only if all non-`display_only` specs for
that sheet have `row_range` set (enforced by the new
`_multi_sheet_row_ranges_valid` validator). The family_aggregate and
board_snapshot uniqueness rules are unchanged. Cross-role uniqueness
(entity/RE names may not appear in family_aggregate or board_snapshot) is
unchanged.

*b. New validator `_multi_sheet_row_ranges_valid`:*

For each `sheet_name` appearing more than once across `entity_sheets` +
`re_partnership_sheets`:
- Every non-`display_only` spec must have `row_range` set.
- Non-`display_only` row ranges must be non-overlapping: after sorting by
  `row_range[0]`, every `row_range[1]` must be strictly less than the next
  `row_range[0]`.
- `display_only` specs in the same group are exempt from row_range
  (they produce no rows so there is no dedup risk).

Clear error messages on violation (entity_ids + conflicting ranges, but NOT
sheet contents).

**3. `_ingest_entity_sheet` — row-range filtering**

After resolving `header_row_index`, apply row-scope:

```
if spec.row_range is not None:
    rr_start, rr_end = spec.row_range
    # Runtime guard: range must fall inside the body (after the header).
    if rr_start <= effective_header_row_index:
        raise ValueError(...)
    body_rows = rows[rr_start - 1 : rr_end]   # 0-indexed slice
    row_idx_offset = rr_start                  # 1-indexed, for diagnostics
else:
    body_rows = rows[body_start:]              # body_start = header_row_index
    row_idx_offset = body_start + 1
```

`row_idx` in diagnostics (unmatched samples, duplicate errors) always uses
the original worksheet row number, so row positions remain stable across
manifest edits.

The `seen_keys` set `(entity_id, quarter, row_label)` is scoped to a single
`_ingest_entity_sheet` call. Since each scoped spec has a distinct
`entity_id`, there is no cross-spec collision; within a single scoped range
the dedup still fires on any genuine duplicate.

### What does NOT change

- Read-only workbook contract (no mutations).
- `header_row_index` resolution (spec → manifest default → 1).
- Column header parsing and `column_quarters` mapping (global to the sheet).
- `seen_keys` dedup logic within a single spec invocation.
- `entity_id` global-uniqueness validator (`_entity_ids_globally_unique`).
- `REPartnershipSheetSpec` inherits `row_range` from `EntitySheetSpec`
  without any additional changes.
- All existing manifests without `row_range` are byte-stable.
- L20 coverage engine, reconciliation gates, Phase 22 diagnostics:
  all unchanged.

### L20 manifest usage after Phase 14.3

`configs/workbook_v7_manifest_local.yaml` will replace the current
`display_only` declarations:

```yaml
- sheet_name: PB Westplan
  entity_id: entity_27a
  display_name: entity_27a (PB Westplan — Westplan partners section)
  row_range: [5, 44]
  row_classification_rules:
    - row_label_pattern: "remaining new deals' from 2025-2028 budget"
      category: capital_call
      direction: outflow
      ...

- sheet_name: PB Westplan
  entity_id: entity_27b
  display_name: entity_27b (PB Westplan — SE Holland section)
  row_range: [47, 94]
  row_classification_rules:
    - row_label_pattern: "remaining new deals' from 2025-2028 budget"
      category: capital_call
      direction: outflow
      ...
```

Row labels that appear in both sections (same deal name) are now keyed on
different `entity_id` values, eliminating the dedup collision.

### Design tightenings before implementation

The reviewer may apply tightenings; the following are pre-flagged:

1. **Row-range bounds inclusive vs exclusive** — inclusive `[start, end]`
   mirrors Excel row numbering and is less error-prone than exclusive end.
   Implementation should document this explicitly.

2. **Runtime guard vs manifest-time guard** — the check that
   `row_range[0] > effective_header_row_index` requires knowing the resolved
   header index, which may depend on the manifest default at runtime. This is
   a runtime guard; it should raise `ValueError` with entity_id and both
   indices, not silently skip rows.

3. **Display_only + row_range** — if a spec is `layout_type=display_only`
   AND has `row_range` set, the row_range is accepted but silently ignored
   (display_only exits before row iteration). This should be noted in the
   docstring but need not raise an error.

### Tests

8 tests in `tests/test_phase143_row_scope.py`. Synthetic workbook only.

| # | Scenario |
|---|----------|
| 1 | Single spec, no row_range → byte-stable (all body rows emitted) |
| 2 | Single spec, row_range set → only rows in range emitted; rows outside range absent |
| 3 | Two specs, same sheet, non-overlapping ranges → both ingest; duplicate row labels do not collide |
| 4 | Two specs, same sheet, overlapping ranges → `model_validate` raises with clear message |
| 5 | Two specs, same sheet, one missing row_range → `model_validate` raises |
| 6 | row_range start ≤ header_row_index → ingestor raises `ValueError` |
| 7 | row_range end beyond sheet length → no crash; only available rows within range emitted |
| 8 | display_only + row_range → rows skipped (display_only wins); no error |

Full suite after Phase 14.3: **332 passed, 6 skipped** (8 new tests added to prior 324).

**Implementation note (2026-05-03):** One additional fix was required during the test run: `ingest_workbook()` was building `entity_specs` as a `dict[str, EntitySheetSpec]` keyed by `sheet_name`, causing the second spec for a shared sheet to clobber the first. Fixed by switching to set-based membership checks for required/unmapped detection and iterating `manifest.entity_sheets` directly for ingestion. The existing Phase 14 `test_manifest_validators` match pattern "sheet names must be globally unique" was updated to "cross-role duplicates" to align with the revised cross-role validator message.
