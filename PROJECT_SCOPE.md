# PROJECT_SCOPE — Wake Robin Liquidity Architecture

> **Authoritative scope statement for this repository.** Project codename
> in documentation: **Wake Robin Liquidity Architecture**. Repository
> directory name (`asset-allocation/`) is retained unchanged for
> continuity; do not interpret it as the scope.
>
> When `MODEL_DOCUMENTATION.md` and this file disagree on what the
> project *is*, this file wins. `MODEL_DOCUMENTATION.md` is authoritative
> for **how the model is built and behaves**; this file is authoritative
> for **what the model is for and what it must eventually cover**.

---

## 1. What this project actually is

This is **not** a generic asset-allocation framework. It is a
deterministic, multi-engine modeling stack for a **Gen3–Gen5 single-
family office (SFO)** with the following structural features that
distinguish it from endowment, mass-affluent, and standard MPT
asset-allocation tooling:

* Multi-entity household — operating LLCs, trusts (CRUTs, gift trusts,
  family trusts), generation-skipping vehicles, and individuals — each
  with its own cash-flow profile, tax character, and beneficiary set.
* A material illiquid balance sheet — **private real estate
  (development + stabilized), operating-company (OpCo) interests,
  development land, and private equity** — alongside public sleeves.
* **NAV is not liquidity.** Total NAV materially overstates spending
  capacity in this household. Spending, rebalancing, and policy
  decisions must be made against a separately-modeled **spendable-
  resource base**, not against total NAV.
* Real cash-flow obligations (distributions, capital calls, development
  funding, OpCo capital needs) that must be reconciled against a
  **liquidity tier** of the balance sheet rather than against
  appraisal value or paper NAV.

Every modeling decision in this repository is downstream of those four
facts. They are codified as the standing **§Use-case context** in
`MODEL_DOCUMENTATION.md` and as the four-line principle:

```
NAV is not liquidity.
Appraisal value is not spending capacity.
Development / land value is not distributable income.
OpCo value is not automatically portfolio liquidity.
```

That principle is load-bearing in every phase of work that follows;
it is not a preface to skim past.

---

## 2. Reference architecture

The reference architecture for the full system is the
**Wake Robin Liquidity Architecture** diagram tracked in this repo at:

* `docs/wake_robin_liquidity_architecture.png` (canonical render)
* `docs/wake_robin_liquidity_architecture.svg` (vector source)

The diagram lays out three columns — **Inputs → Engines → Outputs** —
covering position data, obligations, private-asset (Liv) pipeline, PE
commitments, inflows, policy rules, stress inputs, and the modeling
engines that consume them (liquidity tiering, cash-flow projection,
PE pacing, capital-call modeling, scenario overlay) producing
entity-level forecasts, coverage ratios, breach alerts, family
aggregate, and stress outputs.

When this document and the diagram disagree, **the diagram wins on
structure** (what pipes connect to what); this document wins on
scope (what is in the project versus what is out of scope).

---

## 3. Scope domains (what the project must eventually cover)

The project is organized into **seven layers**, listed in dependency
order. The current code covers a subset; the rest are planned but not
yet built. Each layer must honor the four-line principle in §1.

### 3.1 Entity layer
* Multi-entity SFO chart (operating LLCs, holding LLCs, individual
  trusts including CRUT and gift trusts, family trusts, generation-
  skipping trusts, multi-generation individual accounts).
* Ownership graph (who owns / benefits from what entity).
* Tax character per entity (ordinary income, capital gains treatment,
  pass-through vs. trust-level taxation).
* Distribution rules and obligations (CRUT payouts, trust
  distributions, scheduled gifts).

**Status: not yet built.** First concrete consumer of the entity
layer is the cash-flow ingestion step in §3.3.

### 3.2 Account / position layer
* Per-account holdings reconciled to entity ownership.
* Manager / fund identity (resolved through Phase 9 naming).
* Asset-class taxonomy that matches the categorization workbook
  (see §5): **Fixed Income, Equity, Private Equity, Real Estate,
  Absolute Return, Cash & Cash Alternatives**.
* Per-position metadata required by downstream layers — commitment,
  unfunded commitment, current balance, time horizon, liquidity
  granularity, liquidity bucket (1–5), cash-flow-producing flag,
  expected standard deviation, performance-vs-expected status.

**Status: partial.** The current allocation engine consumes the
asset-class taxonomy implicitly via CMA configs; per-position metadata
is not yet ingested. The Investment Summary workbook (§5) is the
canonical source.

### 3.3 Cash-flow layer
* Entity-by-entity quarterly cash-flow forecast.
* Inflows (distributions, dividends, rents, OpCo distributions,
  scheduled gifts received, sale proceeds).
* Outflows (taxes, scheduled spending, capital calls, development
  funding, gifts made, debt service, charitable distributions).
* Reconciliation against the §3.6 liquidity tier — *which* sleeve
  funds *which* outflow.

**Status: not yet built.** The Cashflow Modeling workbook (§5) is
the canonical reference; ingestion is the next concrete deliverable
after L19 closes (Phase 12 base-side has shipped; Phase 12.5 flow-
side is in flight at time of last scope-doc refresh).

### 3.4 PE pacing layer
* Commitment, call, distribution, and NAV projection per fund.
* Pacing model coupled to public market state (STAIRS, Phase 7).
* Recommitment / new-commitment policy.

**Status: shipped in deterministic form (Phase 1 TA model + Phase 7
STAIRS adapter, illiquidity overlay in Phase 8).** Not yet wired to
the entity layer for per-entity capital-call scheduling.

### 3.5 RE + OpCo layer
* Stabilized real estate (cash-flow producing): rent roll, NOI,
  appraisal carry, debt service.
* Development real estate: capital-need schedule, draw timing,
  monetization assumptions, land carry.
* Operating-company interests: distribution policy, retained-earnings
  reinvestment, capital-call risk, exit assumptions.

**Status: not yet built.** Currently absorbed into PE/illiquid
buckets at the allocation layer; this is *insufficient* for the SFO
use case and is one of the gating layers for an honest spendable-
resource model. The standing principle in §1 explicitly forbids
treating appraisal value as spending capacity.

### 3.6 Liquidity layer
* Five-tier liquidity classification (Daily / Monthly / Quarterly /
  At-maturity / Locked) reconciled to position metadata.
* Liquid NAV / Income-producing NAV / Locked NAV separation
  (already named in `MODEL_DOCUMENTATION.md` §Use-case context).
* Coverage ratios — period obligations vs. period-available
  liquidity by tier.
* Breach alerts when projected outflows exceed tier 1–2 capacity.

**Status: partial.** Phase 8's illiquidity overlay made
"Liquid NAV residual rebalances; PE buckets do not" structurally
binding for the rebalancer, which is the first place the model
honors the principle. Tier-by-tier coverage and breach alerting
are not yet built.

### 3.7 Allocation / policy layer
* Target weights, bands, and rebalance policy.
* Spending rule (flat-real, smoothing, Owl / Guyton-Klinger).
* Scenario / stress overlay (correlation shocks, return shocks,
  PE timing scenarios, illiquidity shocks).
* Policy-loss optimizer (Phase 4b cost-aware allocator).

**Status: shipped.** This is the layer the original "asset
allocation" framing covered; under the SFO scope it is one of seven
layers, not the whole project.

---

## 4. Where the current model is on this scope

| Layer | Status |
| --- | --- |
| 3.1 Entity | not yet built |
| 3.2 Account / position | partial — taxonomy in CMA, metadata not ingested |
| 3.3 Cash-flow | not yet built |
| 3.4 PE pacing | shipped (Phases 1, 7, 8) |
| 3.5 RE + OpCo | not yet built |
| 3.6 Liquidity | partial — illiquidity overlay (Phase 8); tiers not built |
| 3.7 Allocation / policy | shipped (Phases 1–11) |

The **Limitations table** in `MODEL_DOCUMENTATION.md` reflects this
honestly: L19 (spending base realism) is the active modeling fix
(base-side closed in Phase 12, flow-side in Phase 12.5), and is the
entry point into the unbuilt layers above.

---

## 5. External integration targets (read-only)

Two external workbooks are the canonical data sources for the layers
that are not yet built. They are **read-only integration targets**:
the project will eventually ingest them through validated loaders,
normalize them into pydantic v2 schemas under `src/aa_model/io/`, and
project them through the modeling stack. They are **not committed to
this repository** — they contain live family financial data, live
person names, and live dollar values, and are out of scope for git
tracking.

### 5.1 Entity-level cash-flow forecast

* **Workbook:** `Cashflow Modeling v7.xlsx`
* **Path (read-only):**
  `C:\Users\DarrenSchulz\Brooks Capital Management\Accounting - Documents\Cashflow\Cashflow Modeling v7.xlsx`
* **Role:** canonical reference for the §3.3 cash-flow layer and the
  entity chart in §3.1. Must be ingested through a schema-validated
  loader; never read inline by modeling code.
* **Structural domains** (sheet inventory, no live values):
  * Family aggregate roll-ups (Summary, Cash Flow, Assumptions).
  * Operating LLCs and holding entities.
  * Trust vehicles (CRUT, family trusts, gift trusts) and individual
    accounts spanning multiple generations.
  * Real estate development partnership distributions across multiple
    project sub-ledgers.
  * Multi-quarter actuals + multi-year forecast horizon.
  * Period-by-period board snapshots tracking forecast revisions.
* **Validation use:** known-good fixture for the cash-flow ingestion
  step. Outputs of the future cash-flow engine must reconcile to this
  workbook's family aggregate within a documented tolerance, with any
  divergence explained in a Change Log entry.

### 5.2 Position universe + categorization

* **Workbook:** `Investment Summary for Categorization March 2026.xlsx`
* **Path (read-only):**
  `C:\Users\DarrenSchulz\Brooks Capital Management\Investment - Documents\Investment Summary for Categorization March 2026.xlsx`
* **Role:** canonical position universe and metadata source for §3.2.
  Defines the **investments that will be modelled** — every
  position-level record the system reasons over flows from this
  workbook.
* **Structural columns (Data tab):** Manager, Fund Manager Status,
  Fund, Manager Inception Date, Asset Allocation Class, Description,
  Sector, Commitment, Unfunded Commitment, Current Balance, Invested
  Entities, Entity Grouping, Tax character (Ordinary Income /
  Capital Gains), Cash Flow Producing flag, Time Horizon, Liquidity
  granularity (Daily / Monthly / Quarterly / At Maturity), Liquidity
  Bucket (1–5), Performance vs Expected, Comments, Standard Deviation
  (Expected).
* **Structural taxonomy:**
  * **Asset classes:** Fixed Income, Private Equity, Equity, Real
    Estate, Absolute Return, Cash & Cash Alternatives.
  * **Liquidity buckets:** five-tier classification (1 = most liquid;
    aligns with §3.6 liquidity layer).
  * **Liquidity granularity:** Daily, Monthly, Quarterly, At Maturity.
  * **Time horizon:** S, M, M-to-L, L.
  * **Cash-flow-producing:** binary flag per position.
* **Allocation tab:** target-weight matrix across the entity chart
  (operating LLCs, multiple trusts including CRUT and gift trusts,
  generation-spanning family trusts, individual accounts, kid /
  grandchild trusts). This is the canonical mapping from §3.7
  policy targets to §3.1 entities.
* **Manager Contact List tab:** manager-level metadata (out of scope
  for modeling; may inform the Phase 9 metadata enrichment layer).
* **Validation use:** known-good fixture for the position-loader and
  asset-class-taxonomy validators. The CMA asset-class set used by
  the allocator must be a subset of (or explicit mapping from) this
  workbook's Asset Allocation Class values, with any deviation
  documented.

### 5.3 What is committed vs. what is not

| Artifact | Committed | Why |
| --- | --- | --- |
| `docs/wake_robin_liquidity_architecture.png` / `.svg` | yes | Reference architecture; structural, no live data |
| `PROJECT_SCOPE.md` (this file) | yes | Authoritative scope, structural only |
| `MODEL_DOCUMENTATION.md` | yes | Authoritative model documentation |
| `Cashflow Modeling v7.xlsx` | **no** | Live family cash-flow data |
| `Investment Summary for Categorization March 2026.xlsx` | **no** | Live positions, dollar balances, manager identities |
| Person-identifying details beyond entity *type* | **no** | Out of scope for repo-tracked artifacts |

When ingestion code lands, it will read these workbooks from the
read-only paths above and emit normalized fixtures under
`tests/fixtures/` with synthetic / scrubbed values for test use. The
live workbooks themselves stay out of git.

---

## 6. Roadmap implications

The roadmap in `MODEL_DOCUMENTATION.md` §Limitations / Roadmap
implications is authoritative for ordering. Under this scope statement:

1. **L19 — Spending base realism.** Owl currently measures rate
   against total NAV; for this household total NAV materially
   overstates spendable resources. L19 is the active modeling fix
   (base-side configurable denominator shipped in Phase 12;
   flow-side `distribution_inflow` ledger flow + `distributable_
   income` base in Phase 12.5) and the bridge into the unbuilt
   layers below.
2. **Cash-flow ingestion + entity schema (§3.1, §3.3).** Validated
   pydantic schemas for the entity chart and the per-entity cash-
   flow forecast; loaders for the Cashflow Modeling workbook;
   reconciliation tests against the workbook's family aggregate.
3. **Position ingestion (§3.2).** Loader for the Investment Summary
   workbook; mapping from its Asset Allocation Class taxonomy onto
   the existing CMA asset-class set; per-position metadata hydrated
   into the model.
4. **RE + OpCo pipeline (§3.5).** Capital-need schedules, NOI for
   stabilized RE, monetization assumptions, OpCo distribution
   policy. This is what eventually unblocks an honest §3.6
   liquidity tier.
5. **Liquidity tiering (§3.6 full build).** Coverage ratios,
   breach alerts, tier-by-tier capacity calculations.
6. **L2 — Monte Carlo + stochastic regime.** Structurally
   unblocked post-Phase 11 but explicitly deferred until the
   deterministic SFO layers above are honest. Sequencing this
   before the SFO layers would dress up unrealistic deterministic
   assumptions in stochastic clothing.
7. **L5 — `flow_id` schema upgrade.** Rides whichever later
   phase first needs multi-call-per-fund-per-quarter or
   recommitment / secondary-purchase pacing.

The seven `ACCEPTED LIMITATION` / `ENVIRONMENT NOTE` entries
(L3, L7, L9–L12, L17) are documented status calls and are not
roadmap items; do not interpret them as backlog.

---

## 7. Out of scope

The following are **explicitly out of scope** and should not be
introduced without an updated scope statement:

* Order-routing, custodian connectivity, or any execution layer.
  The model emits target weights and rebalancing flows; it does
  not place trades.
* Tax-lot accounting, wash-sale tracking, or per-lot harvesting
  optimization. The model treats taxes through entity-level tax
  character only.
* Performance attribution at the security level. Modeled at the
  asset-class / sleeve / manager level.
* Real-time market data feeds. CMA inputs are reviewed and
  refreshed periodically, not streamed.
* Beneficiary / estate-planning optimization (Roth conversion
  ladders, GRAT structuring, gift-tax-exclusion sequencing).
  The entity chart represents the *current* legal structure;
  optimizing the structure itself is a separate problem.
* Direct-indexing, factor tilts, or active-share optimization
  inside public sleeves. Public sleeves are modeled at asset-class
  granularity.

If any of the above become in scope, this file must be updated
**before** code lands.

---

## 8. Authority and update protocol

This file is the authoritative scope statement. To change it:

1. Open a docs-only PR titled `docs(scope): …`.
2. Update this file *and* the corresponding section of
   `MODEL_DOCUMENTATION.md` (typically §Use-case context and the
   Limitations / Roadmap section) in the same commit.
3. Add a `Change Log` entry to `MODEL_DOCUMENTATION.md` recording
   the scope change, the reason, and the new authority statement.
4. Do not change scope inside an implementation commit. Scope-lock
   commits and implementation commits are kept disjoint.

---

*Scope statement first locked: 2026-05-02 (docs/scope-lock commit).
Project codename used in documentation only; repository directory
name unchanged.*
