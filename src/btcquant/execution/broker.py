"""Abstraction broker : exécution papier ou réelle derrière la même interface.

`ref_price` est le prix de référence courant fourni par l'appelant : le
broker papier s'en sert pour simuler le fill (± slippage), le broker réel
l'ignore et exécute au marché.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class Fill:
    price: float
    qty: float
    fee: float


class Broker(ABC):
    #: True si le broker pose de vrais ordres stop côté exchange ;
    #: sinon le runner surveille un stop "logiciel" à chaque tick.
    supports_stop_orders: bool = False

    @abstractmethod
    def market_buy(self, qty: float, ref_price: float) -> Fill: ...

    @abstractmethod
    def market_sell(self, qty: float, ref_price: float) -> Fill: ...

    def place_stop(self, qty: float, stop_price: float, direction: int = 1) -> str | None:
        """Pose un stop de protection côté exchange. Retourne l'id d'ordre."""
        return None

    def cancel_stop(self, order_id: str) -> None:
        pass

    def free_quote_balance(self) -> float | None:
        """Solde disponible en devise de cotation (None si non applicable)."""
        return None


class PaperBroker(Broker):
    """Simulation de fills au prix de référence ± slippage, avec frais."""

    supports_stop_orders = False

    def __init__(self, fee_rate: float = 0.001, slippage_bps: float = 5.0) -> None:
        self.fee_rate = fee_rate
        self.slippage = slippage_bps / 10_000.0

    def market_buy(self, qty: float, ref_price: float) -> Fill:
        price = ref_price * (1.0 + self.slippage)
        fee = qty * price * self.fee_rate
        log.info("[PAPER] BUY %.6f @ %.2f (frais %.2f)", qty, price, fee)
        return Fill(price=price, qty=qty, fee=fee)

    def market_sell(self, qty: float, ref_price: float) -> Fill:
        price = ref_price * (1.0 - self.slippage)
        fee = qty * price * self.fee_rate
        log.info("[PAPER] SELL %.6f @ %.2f (frais %.2f)", qty, price, fee)
        return Fill(price=price, qty=qty, fee=fee)
