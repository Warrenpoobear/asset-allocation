"""Phase 13 / L19 producer-side — distribution_inflow producer.

Closes the consumer-producer loop opened by Phase 12.5: Owl can now
run end-to-end on ``spending_base="distributable_income"`` with
hand-authored or config-driven family-office income data.

Adapter family
==============

Matches the existing five-family adapter pattern (AllocationAdapter,
ImplementationAdapter, SpendingRule, PEAdapter — and now
DistributionProducer):

* :class:`DistributionProducer` — the ABC.
* :class:`ConfigDrivenProducer` — Phase 13 concrete; reads a
  :class:`aa_model.io.schemas.DistributionProducerConfig` and emits
  the entries matching each requested quarter.
* :func:`make_distribution_producer` — engine-keyed factory.
  Engine ``"config"`` is the only Phase 13 option; Phase 14 will
  introduce ``"workbook"``.

Boundaries (locked in Phase 13 design block)
============================================

* **Phase 13 trusts upstream classification.** It does NOT determine
  legal / tax / entity-governance distributability (Phase 12.5
  reviewer tightening 1). The spec author marks the entry as
  family-office-distributable; Phase 13 obeys.
* **Phase 13 does NOT model cash-movement / inter-entity transfer
  mechanics** (Phase 13 reviewer tightening 1). Configured entries
  are treated as already approved, distributable, and payable to
  the modeled liquidity pool. Trust-payout calendars, distribution
  waterfalls, withholding, banking settlements, and entity ownership
  graphs are Phase 14+ work.
* **PE distributions are excluded by default.** They remain
  ``pe_distribution`` rows; Phase 13.x or later may add an opt-in.
* **Recurrence + confidence are diagnostic-only.** The ledger row
  schema is unchanged; recurrence and confidence live on the
  producer-side diagnostics dataclass and surface via the report.
* **Duplicate (source, quarter) pairs are allowed** (Phase 13
  reviewer tightening 2). Uniqueness is on ``producer_id`` only.

State-flow contract (Phase 4a preservation)
===========================================

The producer reads only its static config + the requested quarter.
No ledger reads. No module state. ``emit_for_quarter(q)`` is a
deterministic, pure function of ``(config, q)`` — idempotent and
free of side effects beyond returning the emissions list and a
diagnostics delta.

The orchestrator wires the producer once per quarter inside the
existing per-quarter loop. ``distribution_inflow`` sits in
``FLOW_ORDER`` between ``inflow`` and ``return``, so canonical
intra-quarter sort handles ordering regardless of when ``add()``
is called. The emitted q-row appears in
``ledger.closed_through(q)`` for the q+1 spending decision —
preserving the closed-prior-quarter contract Owl relies on.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    from aa_model.io.schemas import (
        DistributionEntryConfig,
        DistributionProducerConfig,
    )


@dataclass(frozen=True)
class DistributionEmission:
    """One emitted ``distribution_inflow`` row.

    The orchestrator translates each emission into a ledger ``add()``
    call. Producer-side metadata (``domain``, ``recurrence_type``,
    ``confidence``, ``producer_id``) flows separately into the
    diagnostics path; the ledger row itself carries only
    ``(amount_usd, source, bucket="cash")`` so the ledger schema is
    unchanged from Phase 12.5.
    """

    amount_usd: float
    source: str
    domain: str
    recurrence_type: str  # "recurring" | "one_time"
    confidence: str  # "contractual" | "forecast" | "scenario"
    producer_id: str


@dataclass(frozen=True)
class DistributionProducerDiagnosticsDelta:
    """Per-quarter contributions returned by ``emit_for_quarter``.

    The orchestrator merges these into a run-level
    :class:`DistributionProducerDiagnostics` accumulator.
    """

    emitted_by_domain_usd: dict[str, float] = field(default_factory=dict)
    emitted_by_source_usd: dict[str, float] = field(default_factory=dict)
    emitted_by_recurrence_usd: dict[str, float] = field(default_factory=dict)
    emitted_by_confidence_usd: dict[str, float] = field(default_factory=dict)
    excluded_restricted_count: int = 0
    excluded_restricted_usd: float = 0.0


@dataclass
class DistributionProducerDiagnostics:
    """Run-level accumulator. Merges per-quarter deltas; computes
    summary properties used by the report renderer.
    """

    emitted_by_domain_usd: dict[str, float] = field(default_factory=dict)
    emitted_by_source_usd: dict[str, float] = field(default_factory=dict)
    emitted_by_recurrence_usd: dict[str, float] = field(default_factory=dict)
    emitted_by_confidence_usd: dict[str, float] = field(default_factory=dict)
    excluded_restricted_count: int = 0
    excluded_restricted_usd: float = 0.0

    def merge(self, delta: DistributionProducerDiagnosticsDelta) -> None:
        for k, v in delta.emitted_by_domain_usd.items():
            self.emitted_by_domain_usd[k] = (
                self.emitted_by_domain_usd.get(k, 0.0) + float(v)
            )
        for k, v in delta.emitted_by_source_usd.items():
            self.emitted_by_source_usd[k] = (
                self.emitted_by_source_usd.get(k, 0.0) + float(v)
            )
        for k, v in delta.emitted_by_recurrence_usd.items():
            self.emitted_by_recurrence_usd[k] = (
                self.emitted_by_recurrence_usd.get(k, 0.0) + float(v)
            )
        for k, v in delta.emitted_by_confidence_usd.items():
            self.emitted_by_confidence_usd[k] = (
                self.emitted_by_confidence_usd.get(k, 0.0) + float(v)
            )
        self.excluded_restricted_count += int(delta.excluded_restricted_count)
        self.excluded_restricted_usd += float(delta.excluded_restricted_usd)

    @property
    def total_emitted_usd(self) -> float:
        return float(sum(self.emitted_by_domain_usd.values()))

    @property
    def one_time_share_pct(self) -> float:
        total = self.total_emitted_usd
        if total <= 0.0:
            return 0.0
        return float(self.emitted_by_recurrence_usd.get("one_time", 0.0)) / total

    @property
    def forecast_scenario_share_pct(self) -> float:
        total = self.total_emitted_usd
        if total <= 0.0:
            return 0.0
        forecast = float(self.emitted_by_confidence_usd.get("forecast", 0.0))
        scenario = float(self.emitted_by_confidence_usd.get("scenario", 0.0))
        return (forecast + scenario) / total

    @property
    def top_3_source_concentration_pct(self) -> float:
        total = self.total_emitted_usd
        if total <= 0.0:
            return 0.0
        top3 = sorted(self.emitted_by_source_usd.values(), reverse=True)[:3]
        return float(sum(top3)) / total

    def top_n_sources(self, n: int = 3) -> list[tuple[str, float]]:
        items = sorted(
            self.emitted_by_source_usd.items(), key=lambda kv: kv[1], reverse=True
        )
        return [(k, float(v)) for k, v in items[:n]]


class DistributionProducer(ABC):
    """Emits ``distribution_inflow`` rows for a given quarter.

    Pure: ``emit_for_quarter(q)`` is a deterministic function of the
    producer's config + q. No ledger reads. No module state. Phase
    4a state-flow contract preserved.
    """

    @abstractmethod
    def emit_for_quarter(
        self, quarter: pd.Period
    ) -> tuple[
        list[DistributionEmission],
        DistributionProducerDiagnosticsDelta,
    ]:
        """Return (emissions, per-quarter diagnostics delta) for ``quarter``.

        Restricted entries are filtered at emit time and surface only
        in the diagnostics delta (``excluded_restricted_*``); they are
        NEVER returned in the emissions list.
        """


def _emit_source(entry: DistributionEntryConfig) -> str:
    """Phase 12.5 reviewer-tightening-2 source convention, enforced
    at emit time:

        source = "distribution:<domain>:<asset_id-or-entity_id>"
    """
    ident = entry.asset_id if entry.asset_id is not None else entry.entity_id
    return f"distribution:{entry.domain}:{ident}"


class ConfigDrivenProducer(DistributionProducer):
    """Phase 13 concrete adapter. Reads a
    :class:`aa_model.io.schemas.DistributionProducerConfig` and
    yields emissions for the requested quarter.

    The config is validated upstream (schema-level hard validators
    catch URL-unsafe ids, non-finite amounts, unparseable quarters,
    and domain-recurrence violations). This producer trusts the
    validated input and applies only emission-time logic:

    * ``restricted=True`` → skip emission; record in diagnostics
    * everything else → emit one row per matching entry
    """

    def __init__(self, cfg: DistributionProducerConfig) -> None:
        self._cfg = cfg
        # Pre-parse quarter strings to Period once, so per-quarter
        # filtering is O(N) over entries with no per-call parsing.
        self._entry_quarters: list[pd.Period] = [
            pd.Period(e.quarter, freq="Q-DEC") for e in cfg.entries
        ]

    def emit_for_quarter(
        self, quarter: pd.Period
    ) -> tuple[
        list[DistributionEmission],
        DistributionProducerDiagnosticsDelta,
    ]:
        emissions: list[DistributionEmission] = []
        by_domain: dict[str, float] = {}
        by_source: dict[str, float] = {}
        by_recurrence: dict[str, float] = {}
        by_confidence: dict[str, float] = {}
        excluded_count = 0
        excluded_usd = 0.0

        for entry, entry_q in zip(self._cfg.entries, self._entry_quarters, strict=True):
            if entry_q != quarter:
                continue
            if entry.restricted:
                excluded_count += 1
                excluded_usd += float(entry.amount_usd)
                continue
            source = _emit_source(entry)
            amt = float(entry.amount_usd)
            emissions.append(
                DistributionEmission(
                    amount_usd=amt,
                    source=source,
                    domain=entry.domain,
                    recurrence_type=entry.recurrence_type,
                    confidence=entry.confidence,
                    producer_id=entry.producer_id,
                )
            )
            by_domain[entry.domain] = by_domain.get(entry.domain, 0.0) + amt
            by_source[source] = by_source.get(source, 0.0) + amt
            by_recurrence[entry.recurrence_type] = (
                by_recurrence.get(entry.recurrence_type, 0.0) + amt
            )
            by_confidence[entry.confidence] = (
                by_confidence.get(entry.confidence, 0.0) + amt
            )

        return emissions, DistributionProducerDiagnosticsDelta(
            emitted_by_domain_usd=by_domain,
            emitted_by_source_usd=by_source,
            emitted_by_recurrence_usd=by_recurrence,
            emitted_by_confidence_usd=by_confidence,
            excluded_restricted_count=excluded_count,
            excluded_restricted_usd=excluded_usd,
        )


def make_distribution_producer(
    cfg: DistributionProducerConfig,
    *,
    engine: str = "config",
) -> DistributionProducer:
    """Factory for the producer adapter family.

    Phase 13 ships ``engine="config"``; Phase 14 added
    ``engine="workbook"`` (the workbook-derived
    :class:`DistributionProducerConfig` produced by
    :func:`aa_model.ingestion.workbook.workbook_lines_to_producer_config`
    is consumed here).
    """
    if engine == "config":
        return ConfigDrivenProducer(cfg)
    if engine == "workbook":
        # Local import to avoid a circular dependency: ingestion/
        # imports the producer module; the producer factory only
        # imports ingestion when the workbook engine is selected.
        from aa_model.ingestion.workbook_producer import WorkbookDrivenProducer
        return WorkbookDrivenProducer(cfg)
    raise ValueError(
        f"unknown distribution producer engine {engine!r}; "
        f"valid: ['config', 'workbook']"
    )
