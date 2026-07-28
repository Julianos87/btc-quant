"""Contrat minimal d'un résultat d'exécution carry double-jambe.

Le runtime actuel reste volontairement paper-only. Ces types décrivent les
résultats attendus par le runner sans conserver l'ancien adaptateur Binance
inutilisé.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


QTY_TOLERANCE = 1e-8


class CarrySagaStatus(StrEnum):
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    REJECTED = "REJECTED"
    UNBALANCED = "UNBALANCED"


@dataclass(frozen=True)
class CarryLegFill:
    leg: str
    side: str
    requested_qty: float
    filled_qty: float
    average_price: float | None
    broker_order_id: str | None


@dataclass(frozen=True)
class CarrySagaResult:
    status: CarrySagaStatus
    spot_qty: float
    perp_qty: float
    spot_fill: CarryLegFill | None = None
    perp_fill: CarryLegFill | None = None
    compensation_fill: CarryLegFill | None = None
    error: str | None = None

    @property
    def neutral_qty(self) -> float:
        return min(self.spot_qty, self.perp_qty)

    @property
    def is_balanced(self) -> bool:
        return abs(self.spot_qty - self.perp_qty) <= QTY_TOLERANCE
