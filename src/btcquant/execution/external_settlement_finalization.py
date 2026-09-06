"""Atomic finalization contracts for externally settled orders.

The finalizer is deliberately after settlement application.  It never acquires
network evidence and never decides that an absent lookup means zero effect.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .state_store import StateStore


class ExternalSettlementFinalizationStatus(StrEnum):
    FINALIZED = "FINALIZED"
    ALREADY_FINALIZED = "ALREADY_FINALIZED"


class ExternalZeroEffectFinalizationStatus(StrEnum):
    FINALIZED = "FINALIZED"
    ALREADY_FINALIZED = "ALREADY_FINALIZED"


@dataclass(frozen=True)
class ExternalSettlementFinalizationResult:
    """Immutable result of one external finalization CAS attempt."""

    local_order_id: int
    status: ExternalSettlementFinalizationStatus | str
    settlement_key: str
    application_key: str
    finalization_event_id: int
    transition_sequence_before: int
    transition_sequence_after: int
    blocking_reason: str | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.local_order_id, bool)
            or not isinstance(self.local_order_id, int)
            or self.local_order_id <= 0
        ):
            raise ValueError("local_order_id must be a positive integer")
        object.__setattr__(self, "status", ExternalSettlementFinalizationStatus(self.status))
        for name in ("settlement_key", "application_key"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
            object.__setattr__(self, name, value.strip())
        if (
            isinstance(self.finalization_event_id, bool)
            or not isinstance(self.finalization_event_id, int)
            or self.finalization_event_id <= 0
        ):
            raise ValueError("finalization_event_id must be positive")
        for name in ("transition_sequence_before", "transition_sequence_after"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.transition_sequence_after != self.transition_sequence_before + 1:
            raise ValueError("finalization must advance the transition sequence exactly once")
        if self.blocking_reason is not None:
            raise ValueError("a successful finalization cannot have a blocking reason")


class ExternalSettlementFinalizer:
    """Small application facade over StateStore's atomic finalization CAS."""

    def __init__(self, store: StateStore) -> None:
        if store is None:
            raise TypeError("store is required")
        self._store = store

    def finalize(
        self,
        local_order_id: int,
        *,
        settlement_key: str,
    ) -> ExternalSettlementFinalizationResult:
        return self._store.finalize_external_order_atomically(
            local_order_id=local_order_id,
            settlement_key=settlement_key,
        )

    def finalize_zero_effect(
        self,
        local_order_id: int,
        *,
        submission_key: str,
    ) -> ExternalZeroEffectFinalizationResult:
        return self._store.finalize_external_zero_effect_atomically(
            local_order_id=local_order_id,
            submission_key=submission_key,
        )


__all__ = [
    "ExternalSettlementFinalizationResult",
    "ExternalSettlementFinalizationStatus",
    "ExternalSettlementFinalizer",
    "ExternalZeroEffectFinalizationResult",
    "ExternalZeroEffectFinalizationStatus",
]


@dataclass(frozen=True)
class ExternalZeroEffectFinalizationResult:
    """Immutable result of deterministic no-effect finalization."""

    local_order_id: int
    status: ExternalZeroEffectFinalizationStatus | str
    submission_key: str
    finalization_event_id: int
    transition_sequence_before: int
    transition_sequence_after: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.local_order_id, bool)
            or not isinstance(self.local_order_id, int)
            or self.local_order_id <= 0
        ):
            raise ValueError("local_order_id must be a positive integer")
        object.__setattr__(self, "status", ExternalZeroEffectFinalizationStatus(self.status))
        if not isinstance(self.submission_key, str) or not self.submission_key.strip():
            raise ValueError("submission_key must be a non-empty string")
        object.__setattr__(self, "submission_key", self.submission_key.strip())
        if (
            isinstance(self.finalization_event_id, bool)
            or not isinstance(self.finalization_event_id, int)
            or self.finalization_event_id <= 0
        ):
            raise ValueError("finalization_event_id must be positive")
        for name in ("transition_sequence_before", "transition_sequence_after"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.transition_sequence_after != self.transition_sequence_before:
            raise ValueError("zero-effect finalization cannot advance financial sequence")
