# SFO Asset Allocation Study Model — Build Spec

**Repo:** `Warrenpoobear/asset-allocation` · **Local:** `C:\Projects\asset allocation\asset-allocation`
**Stack:** Python 3.12, WSL2 dev env (aarch64; check wheels before installing solvers)
**Status:** Empty git repo on `main`. No commits.

This spec is the build brief for Claude Code. It defines the spine, gates, and adapter contracts. Do not deviate without an explicit spec amendment commit.

---

## 1. Goal and scope

Build a modular Python research package that lets a single-family-office team study three coupled questions on a single integrated quarterly cash-flow ledger:

1. **Public-asset allocation** — strategic policy, constraints, rebalancing realism.
2. **Spending and liquidity** — withdrawal rules, reserve floors, coverage ratios.
3. **PE pacing** — calls, distributions, NAV, recommitment, sleeve drift.

The package is a **research tool**, not a production trading or accounting system. Outputs are reports and CSV/Parquet artifacts, not orders or filings.

---

## 2. Architecture principles (load-bearing — do not weaken)

1. **Ledger as spine.** The quarterly integrated cash-flow ledger is the central object. Every module produces or consumes rows on it. Build it first.
2. **Schema-first.** All inputs (CMA, constraints, spending rules, PE assumptions, scenarios) are validated YAML/JSON. Failure mode is loud, not silent.
3. **Adapters, not dependencies.** External libs (Riskfolio-Lib, cvxportfolio, skfolio, Owl, STAIRS) sit behind adapter interfaces with a stub implementation. The package must run end-to-end with **zero external optimizer libraries** in Phase 1.
4. **Deterministic by default.** Every run produces a manifest: config hash, fixture/data snapshot hash, library versions, seed, run timestamp, output paths. Reruns are bit-identical given the same inputs.
5. **Phase gates.** Each phase has entry/exit criteria below. Do not start phase N+1 until phase N exits clean.

---

## 3. Configuration defaults (open questions encoded as config)

The following are **unspecified** by the user and must be exposed in `configs/base.yaml` with these documented defaults. Do not hard-code them anywhere else.

| Config key | Default | Notes |
|---|---|---|
| `governance.size_usd` | `100_000_000` | Sizing-only; affects min ticket and PE pacing |
| `governance.tax.jurisdiction` | `US` | Drives spending-rule tax handling |
| `governance.license` | `MIT` | Repo `LICENSE` file |
| `solver.preferred` | `clarabel` | Fallback chain: `clarabel → scs → osqp` |
| `liquidity.floor_months` | `18` | Months of spending in cash + ST bonds |
| `pe.sleeve_target_pct` | `0.25` | PE share of total |
| `pe.scope` | `["buyout"]` | Subset of `[buyout, venture, growth, infra, re, pc]` |
| `rebalance.frequency` | `quarterly` | Aligns with ledger grain |
| `currency` | `USD` | No multi-currency in v1 |

When the user later specifies any of these, update `configs/base.yaml` and bump a config version. Do not retrofit silently.

---

## 4. Repository layout (final target)

```
asset-allocation/
├─ README.md                    # short — points at SPEC.md and configs/
├─ SPEC.md                      # this file
├─ CLAUDE.md                    # session conventions (Phase 1 deliverable)
├─ LICENSE                      # MIT (default)
├─ pyproject.toml
├─ requirements.txt
├─ requirements-dev.txt
├─ .env.example
├─ .gitignore
├─ configs/
│  ├─ base.yaml                 # global defaults + overrides
│  ├─ public_allocation.yaml
│  ├─ spending.yaml
│  ├─ pe_pacing.yaml
│  └─ scenarios.yaml
├─ data/
│  ├─ raw/                      # immutable inputs, gitignored except .gitkeep
│  ├─ interim/                  # gitignored
│  ├─ processed/                # gitignored
│  ├─ external/                 # vendor data, gitignored
│  └─ fixtures/                 # tiny deterministic toy data, COMMITTED
├─ notebooks/
│  ├─ 01_public_allocation.ipynb
│  ├─ 02_spending_liquidity.ipynb
│  ├─ 03_pe_pacing.ipynb
│  └─ 04_integrated_sfo_study.ipynb
├─ src/aa_model/
│  ├─ __init__.py               # exposes __version__
│  ├─ io/
│  │  ├─ schemas.py             # pydantic v2 models for ALL configs
│  │  ├─ loaders.py             # YAML + parquet/csv loaders
│  │  └─ validation.py          # cross-config invariants
│  ├─ assumptions/
│  │  ├─ cma.py                 # capital market assumptions
│  │  └─ scenario_builder.py
│  ├─ allocation/
│  │  ├─ base.py                # AllocationAdapter ABC
│  │  ├─ stub.py                # 60/40 fallback, NO external deps
│  │  ├─ riskfolio_adapter.py   # stub in P1, real in P3
│  │  ├─ pypfopt_adapter.py     # stub in P1, real in P3
│  │  ├─ skfolio_adapter.py     # stub in P1, real in P3
│  │  └─ constraints.py
│  ├─ implementation/
│  │  ├─ base.py                # ImplementationAdapter ABC
│  │  ├─ stub.py                # zero-cost rebalancer
│  │  └─ cvxportfolio_adapter.py # stub in P1, real in P3
│  ├─ spending/
│  │  ├─ base.py                # SpendingRule ABC
│  │  ├─ rules.py               # flat_real, smoothing, guardrail
│  │  ├─ liquidity.py           # reserve floor + coverage math
│  │  └─ owl_adapter.py         # stub in P1, real in P3
│  ├─ pe/
│  │  ├─ ta_model.py            # Takahashi-Alexander, FIRST-CLASS in P1
│  │  ├─ pacing.py              # commitment + recommitment rules
│  │  └─ stairs_adapter.py      # stub in P1, optional in P3
│  ├─ integration/
│  │  ├─ ledger.py              # QuarterlyLedger — the spine
│  │  ├─ orchestrator.py        # runs all engines in order
│  │  ├─ manifest.py            # reproducibility manifest
│  │  └─ report.py              # markdown + HTML
│  └─ cli/
│     └─ main.py                # `aa-model run --config ...`
├─ scripts/
│  ├─ run_public.py
│  ├─ run_spending.py
│  ├─ run_pe.py
│  └─ run_sfo_study.py
├─ tests/
│  ├─ conftest.py               # fixture loaders
│  ├─ test_schemas.py
│  ├─ test_ledger.py            # ledger arithmetic invariants
│  ├─ test_allocation_stub.py
│  ├─ test_spending_rules.py
│  ├─ test_pe_ta.py             # TA model regression vs. golden CSV
│  ├─ test_orchestrator.py      # end-to-end on fixtures
│  └─ test_manifest.py          # reproducibility hash test
└─ .github/workflows/
   └─ ci.yml                    # ruff + mypy (loose) + pytest
```

---

## 5. Core data contracts

### 5.1 Quarterly ledger (`integration/ledger.py`)

The ledger is a tidy long-format DataFrame. Each row is a single atomic event with its own start- and end-state, so rows are self-auditing and intra-quarter ordering is explicit, not implicit.

| column | dtype | example |
|---|---|---|
| `quarter` | `Period[Q]` | `2026Q2` |
| `bucket` | str | `public_equity`, `public_bond`, `cash`, `pe_buyout` |
| `flow_type` | str | see canonical order below |
| `amount_usd` | float | signed dollar impact: outflows negative, inflows positive |
| `nav_start_usd` | float | bucket NAV immediately before this flow |
| `nav_end_usd` | float | bucket NAV immediately after this flow |
| `source` | str | producing module name |
| `run_id` | str | manifest run id |

**Canonical intra-quarter ordering.** The orchestrator MUST apply flows in this order within each `(quarter, bucket)`; ties broken by `source` ascending:

1. `inflow` — external contributions to the household
2. `return` — mark-to-market on liquid buckets (dollar P&L on `nav_start_usd`)
3. `pe_call` — capital deployed into PE
4. `pe_distribution` — capital returned from PE
5. `pe_nav_mark` — PE NAV growth + yield
6. `spend` — withdrawals out of the household
7. `rebalance` — intra-portfolio transfer; sums to zero across buckets within the quarter

**Invariants enforced in tests:**
- **Per-row consistency:** `nav_end_usd == nav_start_usd + amount_usd` for every row (returns and pe_nav_marks express their P&L as the dollar `amount_usd`, so this holds uniformly).
- **Chain consistency:** within each `(run_id, bucket)` chain in canonical order, `nav_start_usd[row_n] == nav_end_usd[row_{n-1}]`. The first row of each `(bucket, quarter)` has `nav_start_usd` equal to the bucket's `nav_end_usd` at end of the prior quarter, or `0.0` if the bucket did not previously exist.
- **Per-bucket flow tie-out:** for each `(run_id, quarter, bucket)`, `sum(amount_usd) == nav_end_usd[last_row] - nav_start_usd[first_row]`. Mathematically implied by per-row + chain consistency, but enforced explicitly because it surfaces intra-quarter ordering bugs and phantom-flow bugs in a single, easy-to-read assertion.
- **External cash flow tie-out:** for each `(run_id, quarter)`, the sum of `amount_usd` over `flow_type in {inflow, spend}` equals the household's net external wire that quarter.
- **Rebalance is zero-sum:** for each `(run_id, quarter)`, the sum of `amount_usd` over `flow_type == "rebalance"` is exactly `0.0` (within `1e-6`).
- **Total NAV conservation:** for each `(run_id, quarter)`,
  `sum(nav_end_usd_at_end_of_quarter across all buckets) == sum(nav_end_usd_at_end_of_prior_quarter) + sum(return + pe_nav_mark amounts) + sum(inflow + spend amounts)`.
  Equivalently: total portfolio NAV moves only via market P&L and external cash, never via internal flows.
- **No NaN** in `amount_usd`, `nav_start_usd`, `nav_end_usd`.
- `run_id` is unique per orchestrator invocation.

### 5.2 Schemas (`io/schemas.py`)

Use `pydantic v2`. One model per config file. Cross-config invariants live in `io/validation.py` (e.g., "PE target + public weights sum to 1.0", "spending floor ≤ ceiling"). Validation runs in the orchestrator before any engine fires.

### 5.3 Manifest (`integration/manifest.py`)

Every orchestrator run writes `data/processed/runs/<run_id>/manifest.json`:

```json
{
  "run_id": "2026-05-01T13-22-04Z-a3f9",
  "config_hash": "sha256:...",
  "fixtures_hash": "sha256:...",
  "library_versions": {"aa_model": "0.1.0", "numpy": "1.26.4", ...},
  "seed": 42,
  "started_at": "...",
  "finished_at": "...",
  "outputs": ["ledger.parquet", "report.html", ...]
}
```

A reproducibility test asserts that two runs with identical inputs produce identical `config_hash`, `fixtures_hash`, and ledger Parquet bytes.

---

## 6. Phase plan (gated)

### Phase 1 — Spine (ship first, no external optimizers)

**Build:**
- Repo skeleton, `pyproject.toml`, MIT license, `.gitignore`, `requirements.txt`, `CLAUDE.md`.
- All schemas + cross-config validation.
- `QuarterlyLedger` with arithmetic + invariants.
- `allocation/stub.py` — config-driven fixed weights. Reads `configs/public_allocation.yaml::stub_weights`, a dict `{bucket: weight}` covering every bucket in the ledger; weights must sum to `1.0` within `1e-9` or schema validation fails. Default fixture config exercises all four buckets to stress integration logic: `{public_equity: 0.50, public_bond: 0.20, cash: 0.05, pe_buyout: 0.25}`. No 60/40 hard-code anywhere.
- `spending/rules.py` — flat-real and smoothing rules only.
- `pe/ta_model.py` — full Takahashi-Alexander (deterministic). Default parameters pinned in `configs/pe_pacing.yaml::ta_defaults` and overridable per-fund:
  - `lifetime_years = 12`
  - `commitment_period_years = 4`
  - `rate_of_contribution = [0.25, 0.30, 0.25, 0.20]` (year-by-year share of commitment called; sums to 1.0; flat within each year)
  - `bow = 2.5`
  - `yield_pct = 0.0`
  - `growth_pct = 0.13`
  Golden CSV is generated by `tests/generate_ta_golden.py` from a single fund: `commitment_usd = 100_000_000`, `vintage = 2024Q1`, default params, projected over 48 quarters. The CSV is committed to `tests/golden/ta_single_fund.csv`. The TA regression test asserts byte-equality between a freshly generated DataFrame and the committed CSV.
- `pe/pacing.py` — fixed commitment schedule (no recommitment optimizer yet).
- Toy fixture set in `data/fixtures/` covering 20 quarters, with **two scenario configs** committed under `data/fixtures/scenarios/`:
  - `base.yaml` — deterministic positive-drift baseline.
  - `drawdown.yaml` — a `-25%` public-equity shock at quarter 8, with linear recovery to trend over the following 4 quarters; all other buckets unchanged.
  Both scenarios must run cleanly through the orchestrator and pass every ledger invariant.
- Orchestrator + CLI + manifest.
- `scripts/run_sfo_study.py` runs end-to-end on fixtures.
- Tests for: schemas, ledger invariants, TA golden-CSV regression, end-to-end orchestrator, manifest reproducibility.
- CI on push (ruff, pytest).

**Exit gate:** `pytest -q` green; `python scripts/run_sfo_study.py --config configs/base.yaml` produces `data/processed/runs/<run_id>/{ledger.parquet, report.md, manifest.json}` deterministically across two consecutive runs; the same script run against `data/fixtures/scenarios/drawdown.yaml` also passes every ledger invariant; total-NAV conservation holds in both scenarios.

### Phase 2 — Scenarios and stress

**Build:**
- `assumptions/scenario_builder.py` — public drawdown, delayed PE distributions, clustered calls, inflation shock, correlation shock.
- `spending/liquidity.py` — coverage ratio, reserve shortfall frequency, worst-draw window.
- Scenario sweeps in orchestrator (parallel via `joblib`).
- Comparison report: per-scenario summary table + small-multiples plots.
- Tests: scenario reproducibility, coverage-ratio math against hand-worked example.

**Exit gate:** scenario sweep over ≥5 scenarios completes in <60s on fixtures and produces a single `comparison.html`.

### Phase 3 — External adapters (one at a time, behind feature flags)

Each adapter is a separate PR. Order:
1. `riskfolio_adapter.py` (P3a)
2. `cvxportfolio_adapter.py` (P3b)
3. `owl_adapter.py` (P3c)
4. `stairs_adapter.py` (P3d, optional)
5. `skfolio_adapter.py` / `pypfopt_adapter.py` (P3e, optional)

**For each adapter:**
- Adapter must implement the same ABC the stub implements.
- A parity test runs the same fixture through stub and adapter; outputs must match documented tolerance bands (not bit-equal — these are different optimizers).
- Adapter is opt-in via `configs/base.yaml`: `allocation.engine: stub | riskfolio | pypfopt | skfolio`.
- WSL2 aarch64 wheel availability is checked and documented in adapter docstring before adding to `requirements.txt`. If no wheel, mark adapter as `linux-x86_64 only` and keep stub as default.

**Exit gate per adapter:** parity test passes; `aa-model run` works with `engine: <adapter_name>` against fixtures.

### Phase 4 — Notebooks and reports

- Four notebooks (`01_*` … `04_*`) consume orchestrator outputs only; they do **not** re-implement modeling.
- HTML report template via Jinja2.
- Notebook smoke tests in CI (`nbmake` or equivalent) on fixture configs.

**Exit gate:** all four notebooks execute top-to-bottom in CI.

---

## 7. Testing and CI requirements

- `ruff check` + `ruff format --check` must pass.
- `pytest -q` must pass with ≥80% coverage on `src/aa_model/integration/` and `src/aa_model/pe/ta_model.py` (the highest-leverage modules).
- TA model has a **golden regression test**: `tests/golden/ta_single_fund.csv` is generated from the pinned default parameters in §6 Phase 1 by `tests/generate_ta_golden.py`; the regression test asserts byte-equality.
- Ledger invariant tests run on both fixture scenarios (`base`, `drawdown`).
- End-to-end orchestrator test runs in <10 seconds on fixtures.
- Reproducibility test: two consecutive `aa-model run` invocations on the same config produce byte-identical `ledger.parquet`.
- CI runs on `ubuntu-latest` only in v1 (WSL2 aarch64 is dev-only; Windows ARM CI not required).

---

## 8. Reproducibility requirements

- All seeded RNGs use `numpy.random.default_rng(seed)` derived from `configs/base.yaml::seed`. No global `np.random.seed`.
- Every output directory is named `data/processed/runs/<run_id>/` and is **never overwritten**. Reruns get a new `run_id`.
- The manifest `config_hash` is `sha256` over the canonicalized YAML (sorted keys, fixed indent). The `fixtures_hash` is `sha256` over the sorted concat of fixture file bytes.
- `pyproject.toml` pins `aa_model` version; `requirements.txt` pins direct deps with `==`.
- `aa-model run` accepts `--dry-run` which validates configs and prints the manifest preview without writing outputs.

---

## 9. Adapter contracts (Phase 1 stubs)

All adapters live behind a small ABC. Stubs are the reference implementation; external libs must conform.

```python
# src/aa_model/allocation/base.py
class AllocationAdapter(ABC):
    @abstractmethod
    def fit(self, returns: pd.DataFrame, cma: CMA, constraints: Constraints) -> None: ...
    @abstractmethod
    def weights(self) -> pd.Series: ...   # index = asset, values sum to 1.0
    @abstractmethod
    def diagnostics(self) -> dict: ...    # solver status, dual values, etc.
```

```python
# src/aa_model/spending/base.py
class SpendingRule(ABC):
    @abstractmethod
    def quarterly_outflows(self, ledger: QuarterlyLedger, params: SpendingParams) -> pd.Series: ...
```

```python
# src/aa_model/implementation/base.py
class ImplementationAdapter(ABC):
    @abstractmethod
    def rebalance(self, current: pd.Series, target: pd.Series, costs: CostModel) -> RebalanceResult: ...
```

The PE TA model is **not** behind an adapter in Phase 1 — it is the canonical implementation. STAIRS is wrapped behind `pe/stairs_adapter.py` only when added in Phase 3d.

---

## 10. Phase 1 prompt for Claude Code

Use this prompt to start the build. Do not enlarge it.

> Implement Phase 1 of `SPEC.md` in this repo. Constraints:
> 1. No external optimizer libraries — use the stub allocator. Only stdlib + `numpy`, `pandas`, `pydantic>=2`, `pyyaml`, `pyarrow`, `jinja2`, `pytest`, `ruff`.
> 2. Build in this order: `io/schemas.py` → `integration/ledger.py` → `pe/ta_model.py` (with golden CSV) → `allocation/stub.py` → `spending/rules.py` → `integration/orchestrator.py` → `cli/main.py` → tests → CI.
> 3. Commit at every order step above with a small commit message. Do not squash.
> 4. The exit gate is: `pytest -q` green AND `python scripts/run_sfo_study.py --config configs/base.yaml` produces a deterministic `ledger.parquet` across two consecutive runs.
> 5. Do not start Phase 2.
> 6. If any spec item is ambiguous, stop and ask. Do not invent.
> 7. Implement the minimum that passes the tests in §7 and the exit gate in §6. No speculative abstractions, no premature generalization, no Phase 2/3 features in disguise. If you find yourself writing a base class with one subclass, collapse it.

---

## 11. Out of scope for v1

- Tax-lot accounting, after-tax returns, charitable structures.
- Multi-currency.
- Real-time data feeds, broker APIs, order management.
- Manager-level due diligence, fee modeling beyond a single bps figure.
- Real estate operating cash flows; private credit interest accruals.
- Regime-switching CMA, ML return forecasting.
- Web UI.

Anything above requires a spec amendment before implementation.
