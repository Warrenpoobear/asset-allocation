"""SpendingRule ABC + parameter container. SPEC §9 + §Phase 4 design.

Phase 4a migrated the ABC from a horizon-level pre-compute
(``quarterly_outflows``) to a per-quarter decision (``quarterly_outflow_at``)
to support path-dependent rules that observe the closed prior quarter.

Per-quarter contract (Phase 4 design / state-flow contract):

* The rule is called with the running :class:`QuarterlyLedger` and the
  quarter to compute spending for. It must observe only the closed
  prior quarter, e.g. via ``ledger.closed_through(quarter - 1)`` or
  ``ledger.end_nav_through(quarter - 1)``.
* It must NOT mutate or finalize the ledger.
* Path-dependent rules that read prior ``spend`` rows must filter by
  ``source == self.SOURCE_ID`` to avoid reacting to history produced
  by a different rule.
* At ``quarter == params.start_quarter`` the rule returns its
  initialization value (typically ``cfg.annual_spend_usd / 4``) with
  no guardrail check, no inflation step, no special ledger event —
  the rule owns q0 initialization end-to-end.

Backward compatibility: the legacy ``quarterly_outflows(ledger, params)``
API is preserved as a default wrapper that constructs a synthetic
working ledger and iterates ``quarterly_outflow_at`` per quarter,
appending each result as a ``spend`` row so subsequent iterations
observe the prior quarter as closed. Path-dependent rules that need
realized return / PE / rebalance flows in prior quarters will see only
their own spend rows in the wrapper case — useful for unit tests of
path-dependent recursion, not a substitute for full orchestrator
context.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import pandas as pd

from aa_model.integration.ledger import QuarterlyLedger
from aa_model.io.schemas import SpendingConfig


@dataclass(frozen=True)
class SpendingParams:
    """Inputs the rule needs beyond the ledger itself."""

    config: SpendingConfig
    start_quarter: pd.Period
    num_quarters: int


class SpendingRule(ABC):
    #: Identifier emitted on this rule's ``spend`` ledger rows. Path-dependent
    #: rules read prior rows by filtering on this value (Phase 4 design /
    #: prior-spend-row source filter). Subclasses must override.
    SOURCE_ID: str = "spending:base"

    @abstractmethod
    def quarterly_outflow_at(
        self,
        ledger: QuarterlyLedger,
        params: SpendingParams,
        quarter: pd.Period,
    ) -> float:
        """Compute spending for ``quarter`` from the ledger closed through
        ``quarter - 1``. See module docstring for the per-quarter contract.
        """

    def quarterly_outflows(self, ledger: QuarterlyLedger, params: SpendingParams) -> pd.Series:
        """Default wrapper — iterates :meth:`quarterly_outflow_at` over the
        horizon, threading each quarter's output back into a synthetic
        working ledger so the next iteration observes the prior quarter as
        closed. Phase 4 orchestrator uses :meth:`quarterly_outflow_at`
        directly; this wrapper preserves the Phase 1-3 horizon-level API for
        callers that haven't migrated.
        """
        horizon = [params.start_quarter + i for i in range(params.num_quarters)]
        work = QuarterlyLedger(
            run_id="_wrapper_",
            initial_nav=ledger.initial_nav,
            start_quarter=params.start_quarter,
        )
        out: list[float] = []
        for q in horizon:
            v = float(self.quarterly_outflow_at(work, params, q))
            if v != 0.0:
                work.add(
                    quarter=q,
                    bucket="cash",
                    flow_type="spend",
                    amount_usd=-v,
                    source=self.SOURCE_ID,
                )
            out.append(v)
        return pd.Series(out, index=horizon, dtype=float, name="quarterly_outflow_usd")
