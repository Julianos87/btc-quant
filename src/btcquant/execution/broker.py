"""Abstraction broker : exécution papier ou réelle derrière la même interface.

`ref_price` est le prix de référence courant fourni par l'appelant : le
broker papier s'en sert pour simuler le fill (± slippage), le broker réel
l'ignore et exécute au marché.
"""

from __future__ import annotations

import logging
import math
import re
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..domain.execution import (
    ExecutionConfig,
    ExecutionSimulator,
    FillStatus,
    MarketOrder,
    OrderSide,
)
from .order_state import ExternalOrderState
from .units import exchange_float, nonnegative_exchange_float

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Fill:
    price: float
    qty: float
    fee: float
    broker_order_id: str | None = None


@dataclass(frozen=True)
class BrokerOrderResult:
    """Résultat broker sans inférence locale à partir de la quantité remplie."""

    fill: Fill
    status: ExternalOrderState
    requested_qty: float
    remaining_qty: float

    #: CCXT response conservée sans interprétation pour la preuve de
    #: soumission. Les brokers PAPER n'en ont normalement pas besoin.
    raw_response: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", ExternalOrderState(self.status))
        for name, value in (
            ("requested_qty", self.requested_qty),
            ("remaining_qty", self.remaining_qty),
            ("filled_qty", self.fill.qty),
        ):
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} doit être un nombre fini positif ou nul")
        if (
            isinstance(self.fill.fee, bool)
            or not isinstance(self.fill.fee, (int, float))
            or not math.isfinite(self.fill.fee)
        ):
            raise ValueError("fee doit être un nombre fini signé")
        if self.requested_qty <= 0:
            raise ValueError("requested_qty doit être strictement positive")
        tolerance = max(1e-9, self.requested_qty * 1e-9)
        if self.fill.qty > self.requested_qty + tolerance:
            raise ValueError("filled_qty dépasse requested_qty")
        if self.fill.qty + self.remaining_qty > self.requested_qty + tolerance:
            raise ValueError("filled_qty + remaining_qty dépasse requested_qty")
        if self.fill.qty > 0 and (not math.isfinite(self.fill.price) or self.fill.price <= 0):
            raise ValueError("Un fill positif exige un prix fini strictement positif")
        if self.status == ExternalOrderState.OPEN and (
            self.fill.qty > 1e-12 or self.remaining_qty <= 0
        ):
            raise ValueError("OPEN exige filled_qty=0 et remaining_qty>0")
        if self.status == ExternalOrderState.PARTIAL_OPEN and (
            self.fill.qty <= 0 or self.remaining_qty <= 0
        ):
            raise ValueError("PARTIAL_OPEN exige un fill et un reste strictement positifs")
        if self.status == ExternalOrderState.FILLED and (
            self.remaining_qty > tolerance or self.fill.qty < self.requested_qty - tolerance
        ):
            raise ValueError("FILLED exige la quantité demandée et aucun reste")
        if self.status == ExternalOrderState.PARTIAL_TERMINAL and (
            self.fill.qty <= 0
            or self.fill.qty >= self.requested_qty - tolerance
            or self.remaining_qty > tolerance
        ):
            raise ValueError("PARTIAL_TERMINAL exige un fill incomplet et aucun reste actif")
        if (
            self.status
            in {
                ExternalOrderState.CANCELED,
                ExternalOrderState.REJECTED,
                ExternalOrderState.EXPIRED,
            }
            and self.remaining_qty > tolerance
        ):
            # Un ordre peut être partiellement exécuté avant que l'exchange ne
            # confirme son annulation, rejet tardif ou expiration. La cause
            # terminale reste celle annoncée par l'exchange et le fill est
            # conservé ; seul un reste encore actif serait contradictoire.
            raise ValueError(f"{self.status.value} exige un reste nul")

    @property
    def is_terminal(self) -> bool:
        return self.status.is_terminal


@dataclass(frozen=True)
class BrokerOrderSnapshot:
    client_order_id: str
    broker_order_id: str | None
    status: ExternalOrderState | str
    filled_qty: float
    price: float | None = None
    fee: float = 0.0
    requested_qty: float | None = None
    remaining_qty: float | None = None


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
    #: Un adaptateur inconnu est considéré externe par défaut. Seul un broker
    #: purement local peut déclarer l'absence durable d'effet hors processus.
    external_execution: bool = True
    #: True si le broker pose de vrais ordres stop côté exchange ;
    #: sinon le runner surveille un stop "logiciel" à chaque tick.
    supports_stop_orders: bool = False
    supports_order_lookup: bool = False
    supports_position_reconciliation: bool = False

    def normalize_market_quantity(
        self,
        qty: float,
        ref_price: float,
        *,
        reduce_only: bool = False,
    ) -> float:
        """Return the canonical quantity persisted before market submission."""
        del ref_price, reduce_only
        return exchange_float(qty, name="quantité demandée", positive=True)

    def venue_client_order_id(self, intent_id: str) -> str:
        """Return the venue-facing client id for one durable local intent."""

        return intent_id

    @abstractmethod
    def market_buy(self, qty: float, ref_price: float) -> BrokerOrderResult: ...

    @abstractmethod
    def market_sell(self, qty: float, ref_price: float) -> BrokerOrderResult: ...

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
        order_book: Mapping[str, object] | None = None,
        volatility_annual: float | None = None,
    ) -> BrokerOrderResult:
        """Point d'entrée commun ; les brokers réels gardent leur implémentation."""

        if self.external_execution:
            raise NotImplementedError(
                "Un broker externe doit implémenter execute_market et préserver client_order_id"
            )
        del (
            client_order_id,
            reduce_only,
            available_volume,
            delayed_price,
            order_book,
            volatility_annual,
        )
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
        if not isinstance(raw, Mapping):
            raise ValueError("stop order CCXT doit être un mapping")
        raw_status = str(raw.get("status") or "").lower()
        if raw.get("amount") is None:
            raise ValueError("stop order.amount absent : quantité demandée inconnue")
        if raw.get("filled") is None:
            raise ValueError("stop order.filled absent : quantité exécutée inconnue")
        requested = exchange_float(raw["amount"], name="stop order.amount", positive=True)
        filled = nonnegative_exchange_float(raw["filled"], name="stop order.filled")
        remaining_raw = raw.get("remaining")
        remaining = (
            nonnegative_exchange_float(remaining_raw, name="stop order.remaining")
            if remaining_raw is not None
            else max(0.0, requested - filled)
        )
        if filled > requested + max(1e-9, requested * 1e-9):
            raise ValueError("stop order.filled dépasse order.amount")
        if raw_status in ("canceled", "cancelled", "rejected", "expired") and filled <= 1e-9:
            # Une terminalité sans fill ne conserve aucun reste actif, même
            # si l'adaptateur n'a pas fourni le champ remaining.
            remaining = 0.0
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
        detailed_fees = raw.get("fees")
        single_fee = raw.get("fee")
        fee = 0.0
        if detailed_fees is not None:
            if not isinstance(detailed_fees, list):
                raise ValueError("stop order.fees doit être une liste CCXT")
            for index, item in enumerate(detailed_fees):
                if not isinstance(item, Mapping) or item.get("cost") is None:
                    raise ValueError(f"stop order.fees[{index}].cost absent ou invalide")
                fee += exchange_float(item["cost"], name=f"stop order.fees[{index}].cost")
        # Detailed records remain authoritative even when fees and rebates net to zero.
        # An empty list means no detailed record and permits the singular fallback.
        if not detailed_fees and single_fee is not None:
            if not isinstance(single_fee, Mapping) or single_fee.get("cost") is None:
                raise ValueError("stop order.fee.cost absent ou invalide")
            fee = exchange_float(single_fee["cost"], name="stop order.fee.cost")
        if filled > 0 and detailed_fees is None and single_fee is None:
            raise ValueError("stop order fee evidence absente pour un fill positif")
        average = raw.get("average")
        if filled > 0 and average is None:
            raise ValueError("stop order.average absent pour un fill positif")
        return ProtectiveOrderSnapshot(
            broker_order_id=str(raw.get("id") or order_id),
            status=status,
            requested_qty=requested,
            filled_qty=filled,
            remaining_qty=remaining,
            average_price=(
                exchange_float(average, name="stop order.average", positive=True)
                if average is not None
                else None
            ),
            fee=fee,
        )

    def free_quote_balance(self) -> float | None:
        """Solde disponible en devise de cotation (None si non applicable)."""
        return None

    @staticmethod
    def _stop_float(value: object) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("valeur numérique de stop invalide")
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("valeur numérique de stop non finie")
        return result

    @staticmethod
    def _stop_int(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("valeur entière de stop invalide")
        return int(value)

    def lookup_order(self, client_order_id: str) -> BrokerOrderSnapshot | None:
        """Recherche fiable d'un ordre externe par identifiant client."""

        raise NotImplementedError("Ce broker ne prend pas en charge la recherche d'ordres")

    def net_position(self, symbol: str) -> float:
        """Position nette distante signée, via un port indépendant de CCXT."""

        raise NotImplementedError("Ce broker ne prend pas en charge la réconciliation")

    def position_quantity_quantum(self, symbol: str) -> float | None:
        """Quantum local de quantité pour la réconciliation de position.

        None signifie que le broker ne peut pas prouver un quantum
        instrumenté sans inférer une valeur. La réconciliation doit alors
        rester fermée pour tout écart non nul. Cette méthode est purement
        locale et ne doit pas déclencher de lecture réseau.
        """

        del symbol
        return None


class PaperBroker(Broker):
    """Adaptateur PAPER autour du simulateur d'exécution commun.

    ``simulate_exchange_stops`` deliberately defaults to false for direct
    unit users. The runtime PAPER profile enables it explicitly so stop
    lifecycle tests exercise the same runner saga as an external venue.
    """

    supports_stop_orders = False
    external_execution = False

    def __init__(
        self,
        fee_rate: float = 0.001,
        slippage_bps: float = 5.0,
        *,
        simulator: ExecutionSimulator | None = None,
        simulate_exchange_stops: bool = False,
    ) -> None:
        self.simulator = simulator or ExecutionSimulator(
            ExecutionConfig(fee_rate=fee_rate, slippage_bps=slippage_bps)
        )
        self.fee_rate = self.simulator.config.fee_rate
        self.slippage = self.simulator.config.slippage_bps / 10_000.0
        self.supports_stop_orders = bool(simulate_exchange_stops)
        # Preserve explicit test/adaptor overrides on subclasses.
        self.supports_order_lookup = bool(
            simulate_exchange_stops or type(self).supports_order_lookup
        )
        self._sequence = 0
        self._stops: dict[str, dict[str, object]] = {}
        self._stop_by_client_id: dict[str, str] = {}

    def restore_protective_stop(
        self,
        order_id: str,
        *,
        qty: float,
        stop_price: float,
        direction: int,
        client_order_id: str | None = None,
    ) -> None:
        """Rehydrate a confirmed PAPER stop after a process restart."""

        if not self.supports_stop_orders:
            return
        if order_id in self._stops:
            return
        client = client_order_id or order_id
        existing_for_client = self._stop_by_client_id.get(client)
        if existing_for_client is not None and existing_for_client != order_id:
            raise ValueError(f"Identifiant client de stop déjà associé : {client}")
        match = re.fullmatch(r"paper-stop-(\d+)", str(order_id))
        if match is not None:
            self._sequence = max(self._sequence, int(match.group(1)))
        self._stops[order_id] = {
            "id": order_id,
            "client_order_id": client,
            "qty": float(qty),
            "stop_price": float(stop_price),
            "direction": int(direction),
            "status": "OPEN",
            "filled_qty": 0.0,
            "remaining_qty": float(qty),
            "average_price": None,
            "fee": 0.0,
        }
        self._stop_by_client_id[client] = order_id

    def place_stop(
        self,
        qty: float,
        stop_price: float,
        direction: int = 1,
        *,
        client_order_id: str | None = None,
    ) -> str | None:
        if not self.supports_stop_orders:
            return super().place_stop(qty, stop_price, direction, client_order_id=client_order_id)
        if qty <= 0 or stop_price <= 0 or direction not in (-1, 1):
            raise ValueError("Stop PAPER invalide")
        if client_order_id and client_order_id in self._stop_by_client_id:
            return self._stop_by_client_id[client_order_id]
        # A restart restores stops before new placements. Never overwrite
        # a durable stop if a legacy counter starts below a restored id.
        while True:
            self._sequence += 1
            order_id = f"paper-stop-{self._sequence}"
            if order_id not in self._stops:
                break
        client = client_order_id or order_id
        self._stops[order_id] = {
            "id": order_id,
            "client_order_id": client,
            "qty": float(qty),
            "stop_price": float(stop_price),
            "direction": int(direction),
            "status": "OPEN",
            "filled_qty": 0.0,
            "remaining_qty": float(qty),
            "average_price": None,
            "fee": 0.0,
        }
        self._stop_by_client_id[client] = order_id
        return order_id

    def cancel_stop(self, order_id: str) -> None:
        if not self.supports_stop_orders:
            return
        stop = self._stops.get(str(order_id))
        if stop is None:
            return
        if stop["status"] == "OPEN":
            stop["status"] = "CANCELED"
            stop["remaining_qty"] = 0.0

    def lookup_order(self, client_order_id: str) -> BrokerOrderSnapshot | None:
        order_id = self._stop_by_client_id.get(client_order_id)
        if order_id is None:
            return None
        stop = self._stops[order_id]
        status = {
            "OPEN": ExternalOrderState.OPEN,
            "CANCELED": ExternalOrderState.CANCELED,
            "FILLED": ExternalOrderState.FILLED,
            "PARTIAL": ExternalOrderState.PARTIAL_TERMINAL,
            "AMBIGUOUS": ExternalOrderState.UNKNOWN,
        }[str(stop["status"])]
        return BrokerOrderSnapshot(
            client_order_id=client_order_id,
            broker_order_id=order_id,
            status=status,
            filled_qty=self._stop_float(stop["filled_qty"]),
            price=(
                self._stop_float(stop["average_price"])
                if stop["average_price"] is not None
                else None
            ),
            fee=self._stop_float(stop["fee"]),
            requested_qty=self._stop_float(stop["qty"]),
            remaining_qty=self._stop_float(stop["remaining_qty"]),
        )

    def protective_order_snapshot(self, order_id: str) -> ProtectiveOrderSnapshot:
        stop = self._stops.get(str(order_id))
        if stop is None:
            return ProtectiveOrderSnapshot(
                broker_order_id=str(order_id),
                status="CANCELED",
                requested_qty=0.0,
                filled_qty=0.0,
                remaining_qty=0.0,
            )
        return ProtectiveOrderSnapshot(
            broker_order_id=str(order_id),
            status=str(stop["status"]),
            requested_qty=self._stop_float(stop["qty"]),
            filled_qty=self._stop_float(stop["filled_qty"]),
            remaining_qty=self._stop_float(stop["remaining_qty"]),
            average_price=(
                self._stop_float(stop["average_price"])
                if stop["average_price"] is not None
                else None
            ),
            fee=self._stop_float(stop["fee"]),
        )

    def _trigger_stop(
        self,
        stop: dict[str, object],
        trigger_price: float,
        *,
        available_volume: float | None = None,
        order_book: Mapping[str, object] | None = None,
    ) -> None:
        if stop["status"] != "OPEN":
            return
        config = self.simulator.config
        if config.latency_ms > 0 or (config.liquidity_model == "order_book" and order_book is None):
            stop["status"] = "AMBIGUOUS"
            return
        side = OrderSide.SELL if self._stop_int(stop["direction"]) == 1 else OrderSide.BUY
        result = self.simulator.execute_market(
            MarketOrder(
                order_id=f"{stop['id']}:trigger",
                side=side,
                qty=self._stop_float(stop["qty"]),
                reference_price=trigger_price,
                available_volume=available_volume,
                order_book=order_book,
            )
        )
        stop["filled_qty"] = float(result.qty)
        stop["remaining_qty"] = 0.0
        stop["average_price"] = float(result.price) if result.qty > 0 else None
        stop["fee"] = float(result.fee)
        stop["status"] = (
            "FILLED"
            if result.status == FillStatus.FILLED
            else "PARTIAL"
            if result.status == FillStatus.PARTIAL
            else "CANCELED"
            if result.status in {FillStatus.REJECTED, FillStatus.EXPIRED}
            else "AMBIGUOUS"
        )

    def observe_market_price(
        self,
        price: float,
        *,
        available_volume: float | None = None,
        order_book: Mapping[str, object] | None = None,
    ) -> None:
        if not self.supports_stop_orders:
            return
        for stop in self._stops.values():
            direction = self._stop_int(stop["direction"])
            hit = (
                price <= self._stop_float(stop["stop_price"])
                if direction == 1
                else price >= self._stop_float(stop["stop_price"])
            )
            if hit:
                self._trigger_stop(
                    stop,
                    price,
                    available_volume=available_volume,
                    order_book=order_book,
                )

    def observe_market_bar(
        self,
        open_price: float,
        high_price: float,
        low_price: float,
        *,
        available_volume: float | None = None,
        order_book: Mapping[str, object] | None = None,
    ) -> None:
        if not self.supports_stop_orders:
            return
        for stop in self._stops.values():
            trigger = ExecutionSimulator.stop_trigger_price(
                direction=self._stop_int(stop["direction"]),
                open_price=open_price,
                high_price=high_price,
                low_price=low_price,
                stop_price=self._stop_float(stop["stop_price"]),
            )
            if trigger is not None:
                self._trigger_stop(
                    stop,
                    trigger,
                    available_volume=available_volume,
                    order_book=order_book,
                )

    def market_buy(self, qty: float, ref_price: float) -> BrokerOrderResult:
        return self.execute_market("BUY", qty, ref_price)

    def market_sell(self, qty: float, ref_price: float) -> BrokerOrderResult:
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
        order_book: Mapping[str, object] | None = None,
        volatility_annual: float | None = None,
    ) -> BrokerOrderResult:
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
                order_book=order_book,
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
        status = {
            FillStatus.FILLED: ExternalOrderState.FILLED,
            FillStatus.PARTIAL: ExternalOrderState.PARTIAL_TERMINAL,
            FillStatus.REJECTED: ExternalOrderState.REJECTED,
            FillStatus.EXPIRED: ExternalOrderState.EXPIRED,
        }[result.status]
        return BrokerOrderResult(
            fill=Fill(price=result.price, qty=result.qty, fee=result.fee),
            status=status,
            requested_qty=result.requested_qty,
            # Le simulateur paper ne conserve jamais la tranche non remplie.
            remaining_qty=0.0,
        )
