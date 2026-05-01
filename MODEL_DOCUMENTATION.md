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

### L1 — PE timing scenarios mechanically affect returns

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

### L3 — Stub-vs-riskfolio weights are not numerically comparable

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

### L4 — Riskfolio default CMA fallback is a placeholder

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

### L6 — `correlation_shock` scenario is omitted

* **Model behavior.** `make_scenarios` returns five scenarios, not the
  six suggested in SPEC §6. There is no covariance matrix to perturb.
* **Real-world interpretation.** Realistic stress testing requires
  modeling cross-bucket correlations, especially the equity-bond and
  equity-PE links that tighten in drawdowns. Not addressable until a
  stochastic CMA lands.

### L7 — Smoothing rule with `weight=0` freezes spending

* **Model behavior.** With `smoothing.weight = 0`, `SmoothingRule`
  freezes spending at `target_0 = annual_spend_usd / 4` for the entire
  horizon — no inflation re-anchoring.
* **Real-world interpretation.** This matches the EWMA formula
  literally (`spend_t = 0 · target_t + 1 · spend_{t-1} = spend_0`), but
  it is unlikely to be what a user wants. Users wanting "flat real with
  inflation" should set `rule = "flat_real"`. Documented in the rule's
  docstring.

### L8 — Rebalancer treats PE as a liquid sleeve

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

### L9 — Heavy install footprint for `riskfolio` extra

* **Model behavior.** `pip install -e ".[riskfolio]"` pulls 80+
  packages including `matplotlib`, `numba`, `vectorbt`, `ipywidgets`,
  `plotly`, `dateparser`, `regex`. Most are unused by `RiskfolioAdapter`
  itself.
* **Real-world interpretation.** Out of our control — these are
  declared in `riskfolio-lib`'s `setup.py`. The adapter is opt-in via
  `[project.optional-dependencies] riskfolio` so the core install set
  remains lean.

### L10 — `/mnt/c` filesystem is unsuitable for `.venv`

* **Model behavior.** Installing the riskfolio extra into a venv at
  `/mnt/c/Projects/asset allocation/asset-allocation/.venv` took 11+
  minutes in disk-wait state. The same install on `~/.venvs/aa-model`
  (a Linux filesystem) took 40 seconds.
* **Real-world interpretation.** WSL2 + NTFS translation makes
  many-small-file operations very slow. The project-local `.venv` is now
  a symlink to a Linux-fs venv. Documented to save the next user the
  same 10 minutes.

### L11 — Synthetic 2-row dummy returns frame in Riskfolio adapter

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

### L12 — Non-fatal "convert self.cov to a positive definite matrix" warning

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

### L13 — Cvxportfolio adapter has no path dependence

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

### L15 — Owl reacts to forecasted NAV, not realized NAV

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

### L17 — Cross-engine metric comparability is not meaningful

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

### L18 — Owl misreads inflation shock as "headroom" and raises spending

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

### L14 — Only linear transaction cost is modeled

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
* **Pairing.** Tightly coupled to **L8** (rebalancer treats PE as a
  liquid sleeve). Fixing L14 alone — adding per-bucket bps with PE
  at 1500 bps — would surface the L8 fiction (rebalancer freely
  selling PE) as a 15% drag every quarter; that's worse than the
  current uniform-bps regime. L8 and L14 must move together: a real
  PE-aware rebalance gate first, then per-bucket cost rates.

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
| **4b — Cost-aware implementation** | optimizer-level cost penalty in `cvxportfolio` adapter; allocator and implementation get per-quarter ABCs (`target_at`, `rebalance_at`); transaction costs remain external (no flow-type changes). Still no fixed-point. | new numerical anchor for cost-aware partial-trade vector; L13 marked resolved. |
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
