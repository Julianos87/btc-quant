"""Frontière transactionnelle entre intention locale et appel broker."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass

from .broker import Broker, Fill
from .state_store import StateStore


@dataclass(frozen=True)
class SubmittedOrder:
    fill: Fill
    order_id: int
    intent_id: str
    status: str


class OrderExecutionService:
    def __init__(
        self,
        store: StateStore,
        broker: Broker,
        *,
        intent_factory: Callable[[], str] | None = None,
    ) -> None:
        self.store = store
        self.broker = broker
        self.intent_factory = intent_factory or (lambda: uuid.uuid4().hex)

    def submit_market(
        self,
        *,
        engine: str,
        slot: str,
        side: str,
        qty: float,
        reference_price: float,
        reason: str,
        reduce_only: bool = False,
        available_volume: float | None = None,
        volatility_annual: float | None = None,
    ) -> SubmittedOrder:
        intent_id = f"{engine}-{slot}-{self.intent_factory()}"
        order_id = self.store.begin_order(
            engine,
            slot,
            intent_id,
            "MARKET",
            side,
            qty,
            reason,
            reference_price=reference_price,
        )
        try:
            fill = self.broker.execute_market(
                side,
                qty,
                reference_price,
                client_order_id=intent_id,
                reduce_only=reduce_only,
                available_volume=available_volume,
                volatility_annual=volatility_annual,
            )
        except Exception as error:
            status = "PENDING" if self.broker.supports_order_lookup else "FAILED"
            suffix = (
                " (résultat externe ambigu, réconciliation requise)" if status == "PENDING" else ""
            )
            self.store.complete_order(
                order_id,
                status=status,
                error=f"{type(error).__name__}: {error}{suffix}",
            )
            raise
        status = "REJECTED" if fill.qty <= 0 else "PARTIAL" if fill.qty < qty - 1e-9 else "FILLED"
        return SubmittedOrder(fill, order_id, intent_id, status)
