"""Affirmative zero-effect evidence for the local PAPER execution domain.

This contract is deliberately narrower than any external-exchange policy. A
local PAPER broker is synchronous and has no venue-side effect; a persisted
submission response with a deterministic rejection and no fill can therefore
be recorded as affirmative local zero-effect evidence. Transport failures,
absence, and all external brokers remain unresolved.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import math

from .order_state import ExternalOrderState


PAPER_ZERO_EFFECT_EVIDENCE_VERSION = "paper-local-zero-effect-v1"
PAPER_ZERO_EFFECT_EVENT_TYPE = "PAPER_ZERO_EFFECT_PROVEN"
PAPER_ZERO_EFFECT_AGGREGATE_TYPE = "paper_zero_effect_evidence"


class PaperZeroEffectStatus(StrEnum):
    PROVEN = "PROVEN"
    NOT_PROVEN = "NOT_PROVEN"


@dataclass(frozen=True)
class PaperZeroEffectDecision:
    status: PaperZeroEffectStatus | str
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", PaperZeroEffectStatus(self.status))
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("reason must be a non-empty string")


def decide_paper_zero_effect(
    *,
    external_execution: bool,
    evidence_persisted: bool,
    external_state: ExternalOrderState | str | None,
    filled_qty: float | None,
    remaining_qty: float | None,
    individual_fill_present: bool,
) -> PaperZeroEffectDecision:
    """Classify only an affirmative local PAPER rejection as zero effect."""

    if external_execution:
        return PaperZeroEffectDecision(
            PaperZeroEffectStatus.NOT_PROVEN,
            "EXTERNAL_ZERO_EFFECT_NOT_AUTHORIZED",
        )
    if not evidence_persisted:
        return PaperZeroEffectDecision(
            PaperZeroEffectStatus.NOT_PROVEN,
            "PAPER_ZERO_EFFECT_EVIDENCE_MISSING",
        )
    try:
        status = ExternalOrderState(external_state) if external_state is not None else None
    except ValueError:
        status = None
    if status != ExternalOrderState.REJECTED:
        return PaperZeroEffectDecision(
            PaperZeroEffectStatus.NOT_PROVEN,
            "PAPER_ZERO_EFFECT_REJECTION_REQUIRED",
        )
    if (
        individual_fill_present
        or filled_qty is None
        or remaining_qty is None
        or not math.isfinite(filled_qty)
        or not math.isfinite(remaining_qty)
        or filled_qty != 0.0
        or remaining_qty != 0.0
    ):
        return PaperZeroEffectDecision(
            PaperZeroEffectStatus.NOT_PROVEN,
            "PAPER_ZERO_EFFECT_POSITIVE_OR_REMAINING_QUANTITY",
        )
    return PaperZeroEffectDecision(
        PaperZeroEffectStatus.PROVEN,
        "PAPER_LOCAL_REJECTION_BEFORE_EFFECT",
    )


__all__ = [
    "PAPER_ZERO_EFFECT_AGGREGATE_TYPE",
    "PAPER_ZERO_EFFECT_EVENT_TYPE",
    "PAPER_ZERO_EFFECT_EVIDENCE_VERSION",
    "PaperZeroEffectDecision",
    "PaperZeroEffectStatus",
    "decide_paper_zero_effect",
]
