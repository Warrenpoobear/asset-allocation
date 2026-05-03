"""Phase 21 / L20 — reconciliation gate evaluation for capital-call obligation.

Defines configurable policy thresholds that bind gate actions to the
Phase 20 delta classifications (advisory / warning / blocking / n/a).

Gate severity hierarchy:
  advisory          — delta surfaced in report; run continues
  warning           — prominent advisory; run continues
  requires_override — run halts unless a justification string is provided
  hard_fail         — run always halts; no override accepted

Default policy: blocking → requires_override. hard_fail is opt-in only.

Design invariant: this module contains only pure evaluation logic.
evaluate_reconciliation_gate returns a ReconciliationGateResult; the
orchestrator is responsible for raising ReconciliationGateError when
gate_result.passes is False. This keeps gate logic testable without
side effects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field, model_validator

if TYPE_CHECKING:
    from aa_model.pe.call_reconciliation import WorkbookCallReconciliationDiagnostics

_ACTION_LITERAL = Literal["advisory", "warning", "requires_override", "hard_fail"]


class ReconciliationGatesConfig(BaseModel):
    """Phase 21 / L20 — configurable thresholds and actions for reconciliation gates.

    Stored as a raw dict in StudyConfig.reconciliation_gates; validated at
    run time by the orchestrator. Default values reproduce Phase 20 advisory-only
    behavior except for blocking, which defaults to requires_override.
    """

    # Percentage thresholds — pct of max(workbook_total, pe_total).
    warning_pct: float = Field(default=0.10, ge=0.0, le=1.0)
    blocking_pct: float = Field(default=0.25, ge=0.0, le=1.0)

    # Optional absolute dollar floors — gate triggers if EITHER pct OR usd exceeded.
    # None by default; effectively percentage-only until user configures a floor.
    warning_usd: float | None = Field(default=None, ge=0.0)
    blocking_usd: float | None = Field(default=None, ge=0.0)

    # Gate actions for each severity level.
    advisory_action: Literal["advisory"] = "advisory"
    warning_action: Literal["advisory", "warning"] = "warning"
    blocking_action: _ACTION_LITERAL = "requires_override"

    # When True, source_used="unavailable" itself triggers the blocking gate.
    require_call_source: bool = False

    @model_validator(mode="after")
    def _blocking_not_weaker_than_warning(self) -> ReconciliationGatesConfig:
        if self.blocking_pct < self.warning_pct:
            raise ValueError(
                f"blocking_pct ({self.blocking_pct}) must be >= warning_pct "
                f"({self.warning_pct})"
            )
        return self


@dataclass
class ReconciliationGateResult:
    """Phase 21 / L20 — policy verdict on a WorkbookCallReconciliationDiagnostics.

    Returned by evaluate_reconciliation_gate. Carried on
    WorkbookCallReconciliationDiagnostics.gate_result for the report.
    """

    passes: bool  # False → orchestrator raises ReconciliationGateError
    gate_action: str  # "advisory" | "warning" | "requires_override" | "hard_fail"
    delta_classification: str  # re-derived from configurable thresholds
    threshold_triggered: str  # "pct" | "usd" | "both" | "source_missing" | "none"
    override_applied: bool
    override_justification: str | None  # raw text only in diagnostics; report redacts
    advisories: list[str] = field(default_factory=list)


class ReconciliationGateError(ValueError):
    """Raised by the orchestrator when a reconciliation gate requires override
    and no justification is provided, or when blocking_action='hard_fail'."""


def _reclassify(
    recon_diag: WorkbookCallReconciliationDiagnostics,
    cfg: ReconciliationGatesConfig,
) -> tuple[str, str]:
    """Re-classify delta using configurable thresholds.

    Returns (delta_classification, threshold_triggered).
    Independent of the Phase 20 delta_classification field so tightening
    the gate config does not require touching call_reconciliation.py.
    """
    if recon_diag.source_used == "unavailable" and cfg.require_call_source:
        return "blocking", "source_missing"

    delta_pct = recon_diag.total_delta_pct
    delta_usd = (
        abs(recon_diag.total_delta_usd) if recon_diag.total_delta_usd is not None else None
    )

    if delta_pct is None:
        return "n/a", "none"

    pct_val = delta_pct / 100.0  # total_delta_pct is stored as a percentage float
    pct_blocks = pct_val >= cfg.blocking_pct
    pct_warns = pct_val >= cfg.warning_pct

    usd_blocks = cfg.blocking_usd is not None and delta_usd is not None and delta_usd >= cfg.blocking_usd
    usd_warns = cfg.warning_usd is not None and delta_usd is not None and delta_usd >= cfg.warning_usd

    if pct_blocks and usd_blocks:
        return "blocking", "both"
    if pct_blocks:
        return "blocking", "pct"
    if usd_blocks:
        return "blocking", "usd"
    if pct_warns and usd_warns:
        return "warning", "both"
    if pct_warns:
        return "warning", "pct"
    if usd_warns:
        return "warning", "usd"
    return "advisory", "none"


def evaluate_reconciliation_gate(
    recon_diag: WorkbookCallReconciliationDiagnostics,
    gates_cfg: ReconciliationGatesConfig,
    override_justification: str | None,
) -> ReconciliationGateResult:
    """Evaluate the reconciliation gate policy against a reconciliation result.

    Parameters
    ----------
    recon_diag:
        Phase 20 WorkbookCallReconciliationDiagnostics from reconcile_call_obligation.
    gates_cfg:
        ReconciliationGatesConfig — thresholds and actions.
    override_justification:
        User-provided string from cfg.liquidity_obligations.reconciliation_override.capital_calls.
        None when not set. Empty string is treated as not provided.

    Returns
    -------
    ReconciliationGateResult. passes=False means the orchestrator should raise
    ReconciliationGateError. This function never raises itself.
    """
    advisories: list[str] = []

    # explicit_config bypasses enforcement — delta still computed and reportable.
    if recon_diag.source_used == "explicit_config":
        return ReconciliationGateResult(
            passes=True,
            gate_action="advisory",
            delta_classification="n/a",
            threshold_triggered="none",
            override_applied=False,
            override_justification=None,
            advisories=["explicit_config source bypasses gate enforcement"],
        )

    delta_classification, threshold_triggered = _reclassify(recon_diag, gates_cfg)

    # Map classification → action.
    if delta_classification == "blocking" or threshold_triggered == "source_missing":
        action = gates_cfg.blocking_action
    elif delta_classification == "warning":
        action = gates_cfg.warning_action
    else:
        action = gates_cfg.advisory_action

    justification_present = bool(override_justification and override_justification.strip())

    if action == "hard_fail":
        # hard_fail ignores override — always fails.
        advisories.append(
            f"gate hard_fail: delta {delta_classification} "
            f"(threshold_triggered={threshold_triggered}); "
            f"no override accepted for hard_fail policy"
        )
        return ReconciliationGateResult(
            passes=False,
            gate_action=action,
            delta_classification=delta_classification,
            threshold_triggered=threshold_triggered,
            override_applied=False,
            override_justification=None,
            advisories=advisories,
        )

    if action == "requires_override":
        if justification_present:
            advisories.append(
                f"gate override applied: delta {delta_classification} "
                f"(threshold_triggered={threshold_triggered}); "
                f"justification provided — run proceeds"
            )
            return ReconciliationGateResult(
                passes=True,
                gate_action=action,
                delta_classification=delta_classification,
                threshold_triggered=threshold_triggered,
                override_applied=True,
                override_justification=override_justification,
                advisories=advisories,
            )
        else:
            advisories.append(
                f"gate requires_override: delta {delta_classification} "
                f"(threshold_triggered={threshold_triggered}); "
                f"set liquidity_obligations.reconciliation_override.capital_calls"
            )
            return ReconciliationGateResult(
                passes=False,
                gate_action=action,
                delta_classification=delta_classification,
                threshold_triggered=threshold_triggered,
                override_applied=False,
                override_justification=None,
                advisories=advisories,
            )

    # advisory or warning — always passes.
    if action == "warning":
        advisories.append(
            f"gate warning: delta {delta_classification} "
            f"(threshold_triggered={threshold_triggered})"
        )
    return ReconciliationGateResult(
        passes=True,
        gate_action=action,
        delta_classification=delta_classification,
        threshold_triggered=threshold_triggered,
        override_applied=False,
        override_justification=None,
        advisories=advisories,
    )
