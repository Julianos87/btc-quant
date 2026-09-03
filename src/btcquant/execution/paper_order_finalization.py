"""Pure finalization policy for authoritative local PAPER executions.

The policy is deliberately separate from evidence acquisition and financial
application.  It only decides whether a durable PAPER order has enough local
facts to leave ``PENDING_RECONCILIATION``.  External orders and zero-effect
claims are never eligible here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from collections.abc import Iterable

from .order_state import ExternalOrderState, LocalOrderState


class PaperFinalizationStatus(StrEnum):
    FINALIZABLE = "FINALIZABLE"
    ALREADY_FINALIZED = "ALREADY_FINALIZED"
    NOT_READY = "NOT_READY"


@dataclass(frozen=True)
class PaperFinalizationDecision:
    """Deterministic result of the local PAPER finalization policy."""

    status: PaperFinalizationStatus | str
    reason: str
    expected_fill_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", PaperFinalizationStatus(self.status))
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("reason must be a non-empty string")
        keys = tuple(self.expected_fill_keys)
        if any(not isinstance(key, str) or not key for key in keys):
            raise ValueError("expected_fill_keys must contain non-empty strings")
        if tuple(sorted(set(keys))) != keys:
            raise ValueError("expected_fill_keys must be sorted and unique")
        object.__setattr__(self, "expected_fill_keys", keys)


def _sorted_keys(values: Iterable[str]) -> tuple[str, ...]:
    keys = tuple(values)
    if any(not isinstance(key, str) or not key for key in keys):
        raise ValueError("fill keys must be non-empty strings")
    return tuple(sorted(set(keys)))


def decide_paper_finalization(
    *,
    local_state: LocalOrderState | str,
    external_state: ExternalOrderState | str | None,
    paper_evidence_present: bool,
    paper_fill_keys: Iterable[str],
    financially_applicable_fill_keys: Iterable[str],
    financially_ambiguous_fill_keys: Iterable[str],
    applied_fill_keys: Iterable[str],
    binding_complete: bool,
    evidence_conflicts: Iterable[str],
    current_transition_sequence: int,
    expected_transition_sequence: int,
) -> PaperFinalizationDecision:
    """Return whether a local PAPER order may be finalized.

    A positive local terminal response is authoritative only after its
    immutable PAPER evidence exists and every non-ambiguous fill is present in
    the durable financial application chain.  No absence-based or external
    zero-effect path is represented by this policy.
    """

    local = LocalOrderState(local_state)
    if local == LocalOrderState.TERMINAL:
        return PaperFinalizationDecision(
            PaperFinalizationStatus.ALREADY_FINALIZED,
            "PAPER_ORDER_ALREADY_TERMINAL",
        )
    if local != LocalOrderState.PENDING_RECONCILIATION:
        return PaperFinalizationDecision(
            PaperFinalizationStatus.NOT_READY,
            "PAPER_ORDER_NOT_RECONCILIATION_READY",
        )
    if not isinstance(paper_evidence_present, bool) or not paper_evidence_present:
        return PaperFinalizationDecision(
            PaperFinalizationStatus.NOT_READY,
            "PAPER_EVIDENCE_MISSING",
        )
    if not isinstance(binding_complete, bool) or not binding_complete:
        return PaperFinalizationDecision(
            PaperFinalizationStatus.NOT_READY,
            "PAPER_BINDING_INCOMPLETE",
        )
    conflicts = tuple(sorted(set(str(value) for value in evidence_conflicts)))
    if conflicts:
        return PaperFinalizationDecision(
            PaperFinalizationStatus.NOT_READY,
            "PAPER_EVIDENCE_CONFLICT",
        )
    if external_state is None:
        return PaperFinalizationDecision(
            PaperFinalizationStatus.NOT_READY,
            "PAPER_TERMINAL_STATE_MISSING",
        )
    try:
        terminal = (
            external_state
            if isinstance(external_state, ExternalOrderState)
            else ExternalOrderState(external_state)
        )
    except ValueError:
        return PaperFinalizationDecision(
            PaperFinalizationStatus.NOT_READY,
            "PAPER_TERMINAL_STATE_MISSING",
        )
    if terminal not in {ExternalOrderState.FILLED, ExternalOrderState.PARTIAL_TERMINAL}:
        return PaperFinalizationDecision(
            PaperFinalizationStatus.NOT_READY,
            "PAPER_POSITIVE_TERMINAL_EVIDENCE_REQUIRED",
        )
    paper_keys = _sorted_keys(paper_fill_keys)
    applicable = _sorted_keys(financially_applicable_fill_keys)
    ambiguous = _sorted_keys(financially_ambiguous_fill_keys)
    applied = _sorted_keys(applied_fill_keys)
    if not paper_keys:
        return PaperFinalizationDecision(
            PaperFinalizationStatus.NOT_READY,
            "PAPER_POSITIVE_FILL_REQUIRED",
        )
    if ambiguous:
        return PaperFinalizationDecision(
            PaperFinalizationStatus.NOT_READY,
            "PAPER_FILL_IDENTITY_AMBIGUOUS",
        )
    if applicable != paper_keys:
        return PaperFinalizationDecision(
            PaperFinalizationStatus.NOT_READY,
            "PAPER_FILL_SET_NOT_FINANCIALLY_APPLICABLE",
        )
    if applied != applicable:
        return PaperFinalizationDecision(
            PaperFinalizationStatus.NOT_READY,
            "PAPER_FILL_APPLICATION_INCOMPLETE",
            applicable,
        )
    if current_transition_sequence != expected_transition_sequence:
        return PaperFinalizationDecision(
            PaperFinalizationStatus.NOT_READY,
            "PAPER_TRANSITION_SEQUENCE_CONFLICT",
            applicable,
        )
    return PaperFinalizationDecision(
        PaperFinalizationStatus.FINALIZABLE,
        "PAPER_POSITIVE_EFFECT_DURABLY_APPLIED",
        applicable,
    )


__all__ = [
    "PaperFinalizationDecision",
    "PaperFinalizationStatus",
    "decide_paper_finalization",
]
