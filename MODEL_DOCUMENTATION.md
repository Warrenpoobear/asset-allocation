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
├─ implementation/  ImplementationAdapter ABC, StubImplementation (zero-cost)
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

### Ledger invariants (SPEC §5.1)

1. **Canonical intra-quarter ordering.** Within each `(quarter, bucket)`,
   rows are sorted by `flow_type` in this order, ties broken by `source`
   ascending:
   `inflow → return → pe_call → pe_distribution → pe_nav_mark → spend → rebalance`.
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
   `sum(amount_usd over flow_type ∈ {inflow, spend})` equals the household's
   net external wire that quarter (passed into `validate()` by the
   orchestrator).
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
   `Δ(total_NAV_quarter) == sum(return + pe_nav_mark + inflow + spend amounts)`.
   Equivalently: total portfolio NAV moves only via market P&L and external
   cash, never via internal flows.
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

`make_rule(name)` is the factory.

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
5. **Zero-cost rebalancing.** `StubImplementation` trades exactly the
   gap between current and target dollar allocations; trades sum to zero
   per quarter. No transaction costs, no slippage, no execution delay.
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
  >    (per-bucket weight difference `< ε`),
  >
  > **OR** explicitly document the deviation in the Change Log under
  > the version bump's entry, including: the bucket(s) whose weight
  > moved, the magnitude of the move, the upstream change that caused
  > it (riskfolio release notes link), and the decision (accept the
  > new behavior, pin to the old version, or block the bump).

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
