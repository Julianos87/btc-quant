"""Explicit retry authorization policy for execution attempts.

This policy is intentionally more restrictive than the historical aggregate
status shortcut. External retry remains disabled until a separately audited
zero-effect proof source is admitted. Local PAPER retry is a distinct domain
and is only eligible when its local proof is explicit.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SafeRetryStatus(StrEnum):
    ALLOWED = "ALLOWED"
    BLOCKED = "BLOCKED"


class SafeRetryProofSource(StrEnum):
    PAPER_LOCAL_SUBMISSION_RESPONSE = "PAPER_LOCAL_SUBMISSION_RESPONSE"
    EXTERNAL_AUTHORITATIVE_ZERO_EFFECT = "EXTERNAL_AUTHORITATIVE_ZERO_EFFECT"


@dataclass(frozen=True)
class SafeRetryDecision:
    status: SafeRetryStatus | str
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", SafeRetryStatus(self.status))
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("reason must be a non-empty string")

    @property
    def allowed(self) -> bool:
        return self.status == SafeRetryStatus.ALLOWED


def decide_safe_retry(
    *,
    external_execution: bool,
    local_state: str,
    external_state: str | None,
    status: str,
    filled_qty: float,
    remaining_qty: float,
    zero_effect_proven: bool,
    proof_source: SafeRetryProofSource | str | None,
) -> SafeRetryDecision:
    """Return an operationally explicit, fail-closed retry decision."""

    if not zero_effect_proven:
        return SafeRetryDecision(SafeRetryStatus.BLOCKED, "ZERO_EFFECT_PROOF_REQUIRED")
    try:
        source = SafeRetryProofSource(proof_source) if proof_source is not None else None
    except ValueError:
        source = None
    if external_execution:
        # No external source is currently admitted: Hyperliquid zero-effect
        # semantics remain an open gate even when status and quantities look
        # terminal.
        if source != SafeRetryProofSource.EXTERNAL_AUTHORITATIVE_ZERO_EFFECT:
            return SafeRetryDecision(
                SafeRetryStatus.BLOCKED,
                "EXTERNAL_ZERO_EFFECT_SOURCE_NOT_AUTHORIZED",
            )
        return SafeRetryDecision(
            SafeRetryStatus.BLOCKED,
            "EXTERNAL_SAFE_RETRY_GATE_OPEN",
        )
    if source != SafeRetryProofSource.PAPER_LOCAL_SUBMISSION_RESPONSE:
        return SafeRetryDecision(SafeRetryStatus.BLOCKED, "PAPER_ZERO_EFFECT_SOURCE_REQUIRED")
    if local_state != "TERMINAL" or status not in {"FAILED", "RECOVERED_ABORTED"}:
        return SafeRetryDecision(SafeRetryStatus.BLOCKED, "PAPER_ATTEMPT_NOT_RETRYABLE")
    if external_state is not None or filled_qty != 0.0 or remaining_qty != 0.0:
        return SafeRetryDecision(SafeRetryStatus.BLOCKED, "PAPER_EFFECT_OR_REMOTE_STATE_PRESENT")
    return SafeRetryDecision(SafeRetryStatus.ALLOWED, "PAPER_ZERO_EFFECT_PROVEN")


__all__ = [
    "SafeRetryDecision",
    "SafeRetryProofSource",
    "SafeRetryStatus",
    "decide_safe_retry",
]
