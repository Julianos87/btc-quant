"""Abstraction broker : exécution papier ou réelle derrière la même interface.

`ref_price` est le prix de référence courant fourni par l'appelant : le
broker papier s'en sert pour simuler le fill (± slippage), le broker réel
l'ignore et exécute au marché.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..domain.execution import ExecutionConfig, ExecutionSimulator, MarketOrder, OrderSide

log = logging.getLogger(__name__)


@dataclass
class Fill:
    price: float
    qty: float
    fee: float
    broker_order_id: str | None = None


@dataclass(frozen=True)
class BrokerOrderSnapshot:
    client_order_id: str
    broker_order_id: str | None
    status: str
    filled_qty: float
    price: float | None = None
    fee: float = 0.0


@dataclass(frozen=True)
class ProtectiveOrderSnapshot:
    """Vue normalisée d'un ordre stop, indépendante du format CCXT brut."""

    broker_order_id: str
    status: str
    requested_qty: float
    filled_qty: float
    remaining_qty: float
    average_price: float | None = None
    fee: float = 0.0


class Broker(ABC):
    #: True si le broker pose de vrais ordres stop côté exchange ;
    #: sinon le runner surveille un stop "logiciel" à chaque tick.
    supports_stop_orders: bool = False
    supports_order_lookup: bool = False
    supports_position_reconciliation: bool = False

    @abstractmethod
    def market_buy(self, qty: float, ref_price: float) -> Fill: ...

    @abstractmethod
    def market_sell(self, qty: float, ref_price: float) -> Fill: ...

    def execute_market(
        self,
        side: str,
        qty: float,
        ref_price: float,
        *,
        client_order_id: str | None = None,
        reduce_only: bool = False,
        available_volume: float | None = None,
        delayed_price: float | None = None,
        volatility_annual: float | None = None,
    ) -> Fill:
        """Point d'entrée commun ; les brokers réels gardent leur implémentation."""

        del client_order_id, reduce_only, available_volume, delayed_price, volatility_annual
        order_side = OrderSide(side)
        if order_side == OrderSide.BUY:
            return self.market_buy(qty, ref_price)
        if order_side == OrderSide.SELL:
            return self.market_sell(qty, ref_price)
        raise AssertionError("OrderSide contient une valeur non gérée")

    def place_stop(
        self,
        qty: float,
        stop_price: float,
        direction: int = 1,
        *,
        client_order_id: str | None = None,
    ) -> str | None:
        """Pose un stop de protection côté exchange.

        ``client_order_id`` doit être stable pendant toute reprise d'une même
        intention. Un broker externe peut ainsi retrouver un ordre dont la
        réponse s'est perdue sans en créer un second.
        """
        del qty, stop_price, direction, client_order_id
        return None

    def cancel_stop(self, order_id: str) -> None:
        pass

    def stop_status(self, order_id: str) -> dict:
        """Retourne l'état brut d'un stop exchange."""
        raise NotImplementedError("Ce broker ne prend pas en charge les stops exchange")

    def protective_order_snapshot(self, order_id: str) -> ProtectiveOrderSnapshot:
        """Normalise les statuts et quantités d'un stop renvoyé par l'exchange."""

        raw = self.stop_status(order_id)
        raw_status = str(raw.get("status") or "").lower()
        requested = float(raw.get("amount") or 0.0)
        filled = float(raw.get("filled") or 0.0)
        remaining_raw = raw.get("remaining")
        remaining = (
            float(remaining_raw) if remaining_raw is not None else max(0.0, requested - filled)
        )
        if raw_status == "closed":
            status = "FILLED" if filled > 0 and remaining <= 1e-12 else "PARTIAL"
        elif filled > 0:
            status = "PARTIAL"
        elif raw_status in ("canceled", "cancelled"):
            status = "CANCELED"
        elif raw_status in ("rejected", "expired"):
            status = raw_status.upper()
        elif raw_status in ("open", "new", "untriggered"):
            status = "OPEN"
        else:
            status = "UNKNOWN"
        fee = sum(float(item.get("cost") or 0.0) for item in raw.get("fees") or [])
        if not fee and raw.get("fee"):
            fee = float(raw["fee"].get("cost") or 0.0)
        average = raw.get("average")
        return ProtectiveOrderSnapshot(
            broker_order_id=str(raw.get("id") or order_id),
            status=status,
            requested_qty=requested,
            filled_qty=filled,
            remaining_qty=remaining,
            average_price=float(average) if average is not None else None,
            fee=fee,
        )

    def free_quote_balance(self) -> float | None:
        """Solde disponible en devise de cotation (None si non applicable)."""
        return None

    def lookup_order(self, client_order_id: str) -> BrokerOrderSnapshot | None:
        """Recherche fiable d'un ordre externe par identifiant client."""

        raise NotImplementedError("Ce broker ne prend pas en charge la recherche d'ordres")

    def net_position(self, symbol: str) -> float:
        """Position nette distante signée, via un port indépendant de CCXT."""

        raise NotImplementedError("Ce broker ne prend pas en charge la réconciliation")


class PaperBroker(Broker):
    """Adaptateur paper autour du simulateur d'exécution commun."""

    supports_stop_orders = False

    def __init__(
        self,
        fee_rate: float = 0.001,
        slippage_bps: float = 5.0,
        *,
        simulator: ExecutionSimulator | None = None,
    ) -> None:
        self.simulator = simulator or ExecutionSimulator(
            ExecutionConfig(fee_rate=fee_rate, slippage_bps=slippage_bps)
        )
        self.fee_rate = self.simulator.config.fee_rate
        self.slippage = self.simulator.config.slippage_bps / 10_000.0
        self._sequence = 0

    def market_buy(self, qty: float, ref_price: float) -> Fill:
        return self.execute_market("BUY", qty, ref_price)

    def market_sell(self, qty: float, ref_price: float) -> Fill:
        return self.execute_market("SELL", qty, ref_price)

    def execute_market(
        self,
        side: str,
        qty: float,
        ref_price: float,
        *,
        client_order_id: str | None = None,
        reduce_only: bool = False,
        available_volume: float | None = None,
        delayed_price: float | None = None,
        volatility_annual: float | None = None,
    ) -> Fill:
        del reduce_only
        order_side = OrderSide(side)
        if client_order_id is None:
            self._sequence += 1
            client_order_id = f"paper-direct-{self._sequence}"
        result = self.simulator.execute_market(
            MarketOrder(
                order_id=client_order_id,
                side=order_side,
                qty=qty,
                reference_price=ref_price,
                available_volume=available_volume,
                delayed_price=delayed_price,
                volatility_annual=volatility_annual,
            )
        )
        log.info(
            "[PAPER] %s %s %.6f/%.6f @ %.2f (frais %.2f)",
            result.status,
            order_side,
            result.qty,
            qty,
            result.price,
            result.fee,
        )
        return Fill(price=result.price, qty=result.qty, fee=result.fee)
