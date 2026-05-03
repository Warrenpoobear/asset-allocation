"""Phase 14 / L19 — workbook ingestion package.

Reads the operating cash-flow workbook (``Cashflow Modeling v7.xlsx``)
as a read-only integration target. Produces normalized
:class:`EntityRecord` and :class:`CashFlowLineRecord` tables; bridges
qualifying lines into a Phase 13 ``DistributionProducerConfig``.

The workbook is **never mutated**. Live values, person names, and
forecast tables are NEVER copied into the repo (PROJECT_SCOPE.md §5.3).
"""
