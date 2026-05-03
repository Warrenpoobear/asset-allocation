"""Phase 14 / L19 — workbook-driven distribution producer.

Satisfies the Phase 13 :class:`aa_model.producers.distribution.DistributionProducer`
ABC. Internally delegates to the Phase 13 :class:`ConfigDrivenProducer`
on a :class:`DistributionProducerConfig` derived from workbook
ingestion via
:func:`aa_model.ingestion.workbook.workbook_lines_to_producer_config`.

Why a thin wrapper rather than a direct config swap:

* It satisfies the Phase 13 ABC commitment that the producer adapter
  family extends with engine="workbook" — visible at the factory.
* It keeps the bridge function visible at the orchestrator wiring
  layer (the orchestrator builds the config explicitly, then passes
  it here), so the workbook → producer path is auditable in one
  place rather than buried inside an ingest-and-emit black box.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pandas as pd

from aa_model.producers.distribution import (
    ConfigDrivenProducer,
    DistributionEmission,
    DistributionProducer,
    DistributionProducerDiagnosticsDelta,
)

if TYPE_CHECKING:
    from aa_model.io.schemas import DistributionProducerConfig


class WorkbookDrivenProducer(DistributionProducer):
    """Phase 14 concrete adapter.

    Delegates to :class:`ConfigDrivenProducer` on the workbook-derived
    :class:`DistributionProducerConfig`. Pure: no ledger reads, no
    module state. Same Phase 4a state-flow contract as the Phase 13
    config-driven adapter.
    """

    def __init__(self, cfg: DistributionProducerConfig) -> None:
        # ConfigDrivenProducer pre-parses entry quarters; reuse it
        # rather than duplicate the logic.
        self._inner = ConfigDrivenProducer(cfg)

    def emit_for_quarter(
        self, quarter: pd.Period
    ) -> tuple[
        list[DistributionEmission],
        DistributionProducerDiagnosticsDelta,
    ]:
        return self._inner.emit_for_quarter(quarter)
