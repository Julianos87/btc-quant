"""Adapters for the single authoritative atomic financial writer."""

from __future__ import annotations

from .financial_fill_application import FinancialFillCommitResult
from .state_store import StateStore


class StateStoreAtomicFinancialWriter:
    """Expose the existing StateStore E3 method through a narrow port.

    This adapter intentionally contains no financial logic.  It prevents
    reconciliation orchestration from depending on the rest of StateStore
    merely to invoke the already-proven transaction.
    """

    def __init__(self, store: StateStore) -> None:
        if not isinstance(store, StateStore):
            raise TypeError("store must be a StateStore")
        self._store = store

    def apply_financial_fill_atomically(
        self, *, local_order_id: int, fill_key: str
    ) -> FinancialFillCommitResult:
        return self._store.apply_financial_fill_atomically(
            local_order_id=local_order_id,
            fill_key=fill_key,
        )


__all__ = ["StateStoreAtomicFinancialWriter"]
