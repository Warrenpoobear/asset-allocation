"""Owl spending rule (SPEC §6 Phase 3c, Phase 4a fix for L15 / L18,
Phase 11 fix for L16).

"Owl" is the project codename for a Guyton-Klinger–style guardrail
spending policy. There is no external "Owl" Python library.

Phase 4a behavior — realized-NAV feedback
=========================================

For each quarter ``t = 0..N-1``:

* **q0 (initialization)**: return ``cfg.annual_spend_usd / 4``. No
  guardrail check, no inflation step, no special ledger event. Per
  Phase 4 design: *q0 is initialization, not a guardrail decision.*
* **mid-year (t % 4 != 0)**: return the same quarterly amount this
  rule emitted at ``t - 1`` (within-year constancy).
* **year boundary (t > 0, t % 4 == 0)**:

      annual_spend_t = annual_spend_{year_t-1} · (1 + inflation_pct)
      nav_realized   = ledger.end_nav_through(t - 1).sum()
      current_rate   = annual_spend_t / nav_realized
      initial_rate   = cfg.annual_spend_usd / sum(ledger.initial_nav.values())

      if current_rate < initial_rate · (1 - lower_band_pct):
          annual_spend_t *= (1 + raise_pct)
      elif current_rate > initial_rate · (1 + upper_band_pct):
          annual_spend_t *= (1 - cut_pct)

      # Phase 11 / L16 — optional absolute-dollar clamps:
      if gr.absolute_min_annual_usd is not None:
          annual_spend_t = max(annual_spend_t, gr.absolute_min_annual_usd)
      if gr.absolute_max_annual_usd is not None:
          annual_spend_t = min(annual_spend_t, gr.absolute_max_annual_usd)

  Both ``annual_spend_{year_t-1}`` and ``nav_realized`` are read
  from the closed ledger via :meth:`QuarterlyLedger.closed_through`
  and :meth:`QuarterlyLedger.end_nav_through` — no shared state, no
  forecast assumption.

Phase 11 / L16 — absolute-dollar clamps
=======================================

When ``gr.absolute_min_annual_usd`` and / or ``absolute_max_annual_usd``
are set, the trigger output is clamped to a dollar floor / ceiling
that does NOT scale with initial NAV. This breaks the rate-based
scale-invariance documented as L16: under proportional setup
(``annual_spend ∝ initial_nav``) the rate-band test cancels NAV
algebraically, so a $100M and $1B household with the same
proportional setup get identical Owl trajectories. Adding a
dollar-denominated decision (the absolute clamps) breaks that.

**Phase 11 fixes scale-invariance only. It does NOT resolve
spending-base realism (L19).** Owl still measures rate against
**total modeled NAV**, including illiquid private real estate, opco
equity, etc. For a Gen3-Gen5 SFO this may overstate spending
capacity. See MODEL_DOCUMENTATION.md §Use-case context + §Phase 11
design.

Phase 4 discipline
==================

* **Pure (with one allowance)**: no module-level state. The Owl rule
  retains a per-instance counter of clamp activations
  (``_min_clamp_activations`` and ``_max_clamp_activations``) so the
  report can surface them; this state is reset on each call to
  :meth:`quarterly_outflow_at` for a fresh ``params.start_quarter``
  and accumulates across quarters within a single run. The state
  does NOT influence the trigger output and is purely diagnostic.
* **No ledger mutation.** May read the ledger passed into the
  ``SpendingRule`` interface (per Phase 3+ adapter discipline) but
  may not retain, mutate, or access global state.
* **Source filter.** Reads only its own prior spend rows
  (``source == "spending:owl"``); other rules' history is ignored
  even if a config switch left it in the ledger.
* **Closed-prior-quarter view only.** Sees ``ledger[quarter <= q-1]``;
  never reads the current quarter's flows.

Resolves L15 (forecast-only NAV), L18 (Owl misreads inflation
shock as headroom), and L16 (scale-invariance, Phase 11; partial —
spending-base realism L19 remains open).
"""

from __future__ import annotations

import pandas as pd

from aa_model.integration.ledger import QuarterlyLedger
from aa_model.spending.base import SpendingParams, SpendingRule
from aa_model.spending.rules import _quarter_offset, _read_own_prior_spend


class OwlRule(SpendingRule):
    """Guyton-Klinger guardrail spending against realized prior-quarter NAV."""

    SOURCE_ID = "spending:owl"

    def __init__(self) -> None:
        # Phase 11 / L16 diagnostic counters: how many year-boundary
        # quarters had the absolute floor / ceiling clamp activate.
        # Surfaced via diagnostics() for the report. Does NOT influence
        # the trigger output.
        self._min_clamp_activations: int = 0
        self._max_clamp_activations: int = 0
        self._last_start_quarter: pd.Period | None = None

    def diagnostics(self) -> dict:
        """Return Phase 11 clamp-activation counts. Resets when the rule
        sees a new ``params.start_quarter`` (i.e., a new run)."""
        return {
            "engine": "OwlRule",
            "min_clamp_activations": self._min_clamp_activations,
            "max_clamp_activations": self._max_clamp_activations,
        }

    def quarterly_outflow_at(
        self,
        ledger: QuarterlyLedger,
        params: SpendingParams,
        quarter: pd.Period,
    ) -> float:
        cfg = params.config
        if cfg.guardrail is None:
            raise ValueError("OwlRule requires spending.guardrail config")
        gr = cfg.guardrail

        initial_nav_total = float(sum(ledger.initial_nav.values()))
        if initial_nav_total <= 0.0:
            raise ValueError(f"OwlRule requires positive initial NAV; got {initial_nav_total}")

        # Reset clamp activations on a new run (new start_quarter).
        if self._last_start_quarter != params.start_quarter:
            self._min_clamp_activations = 0
            self._max_clamp_activations = 0
            self._last_start_quarter = params.start_quarter

        # q0 initialization — no guardrail, no inflation, no ledger read.
        # Floor / ceiling clip is applied so a rule emitting a clipped value
        # at q0 still satisfies its config bounds and the wrapper's prior-row
        # recovery sees the clipped value at q1.
        if quarter == params.start_quarter:
            quarterly = cfg.annual_spend_usd / 4.0
            return max(cfg.floor_usd, min(cfg.ceiling_usd, quarterly))

        offset = _quarter_offset(quarter, params.start_quarter)
        quarter_in_year = offset % 4
        prior_q = quarter - 1
        prior_quarterly = _read_own_prior_spend(ledger, self.SOURCE_ID, prior_q)

        # Mid-year: within-year constancy. Same quarterly as prior.
        if quarter_in_year != 0:
            return max(cfg.floor_usd, min(cfg.ceiling_usd, prior_quarterly))

        # Year boundary: inflate, then guardrail-check vs realized NAV.
        prior_annual = prior_quarterly * 4.0
        annual_spend = prior_annual * (1.0 + cfg.inflation_pct)

        nav_realized = float(ledger.end_nav_through(prior_q).sum())
        if nav_realized > 0.0:
            initial_rate = cfg.annual_spend_usd / initial_nav_total
            current_rate = annual_spend / nav_realized
            if current_rate < initial_rate * (1.0 - gr.lower_band_pct):
                annual_spend *= 1.0 + gr.raise_pct
            elif current_rate > initial_rate * (1.0 + gr.upper_band_pct):
                annual_spend *= 1.0 - gr.cut_pct

        # Phase 11 / L16: optional absolute-dollar clamps. Break the
        # rate-based scale-invariance by introducing dollar-denominated
        # decisions in the trigger output. Activations are tracked for
        # the report diagnostic. **Does NOT address L19 spending-base
        # realism — Owl still measures rate against total NAV.**
        if gr.absolute_min_annual_usd is not None:
            if annual_spend < gr.absolute_min_annual_usd:
                annual_spend = float(gr.absolute_min_annual_usd)
                self._min_clamp_activations += 1
        if gr.absolute_max_annual_usd is not None:
            if annual_spend > gr.absolute_max_annual_usd:
                annual_spend = float(gr.absolute_max_annual_usd)
                self._max_clamp_activations += 1

        quarterly = annual_spend / 4.0
        return max(cfg.floor_usd, min(cfg.ceiling_usd, quarterly))
