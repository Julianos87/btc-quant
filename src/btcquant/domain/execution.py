"""Modèle d'exécution métier déterministe partagé par backtest et paper."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class FillStatus(StrEnum):
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True)
class ExecutionConfig:
    fee_rate: float = 0.001
    slippage_bps: float = 5.0
    rejection_rate: float = 0.0
    max_volume_participation: float | None = None
    market_impact_bps: float = 0.0
    volatility_impact_bps: float = 0.0
    volatility_reference_annual: float = 0.40
    volatility_multiplier_cap: float = 3.0
    latency_ms: int = 0
    min_qty: float = 0.0
    quantity_step: float = 0.0
    min_notional: float = 0.0
    liquidity_model: str = "aggregate"
    simulation_profile: str = "custom"
    seed: int = 0

    def __post_init__(self) -> None:
        if not math.isfinite(self.fee_rate) or self.fee_rate < 0:
            raise ValueError("fee_rate doit être positif ou nul")
        if (
            not math.isfinite(self.slippage_bps)
            or not math.isfinite(self.market_impact_bps)
            or not math.isfinite(self.volatility_impact_bps)
            or self.slippage_bps < 0
            or self.market_impact_bps < 0
            or self.volatility_impact_bps < 0
        ):
            raise ValueError("slippage et impacts doivent être positifs ou nuls")
        if (
            not math.isfinite(self.volatility_reference_annual)
            or self.volatility_reference_annual <= 0
        ):
            raise ValueError("volatility_reference_annual doit être strictement positive")
        if not math.isfinite(self.volatility_multiplier_cap) or self.volatility_multiplier_cap <= 0:
            raise ValueError("volatility_multiplier_cap doit être strictement positif")
        if not math.isfinite(self.rejection_rate) or not 0.0 <= self.rejection_rate <= 1.0:
            raise ValueError("rejection_rate doit être compris entre 0 et 1")
        if self.max_volume_participation is not None and not (
            0.0 < self.max_volume_participation <= 1.0
        ):
            raise ValueError("max_volume_participation doit être dans ]0, 1]")
        if not isinstance(self.latency_ms, int):
            raise TypeError("latency_ms doit être un entier")
        if (
            not math.isfinite(self.min_qty)
            or not math.isfinite(self.quantity_step)
            or not math.isfinite(self.min_notional)
            or self.latency_ms < 0
            or self.min_qty < 0
            or self.quantity_step < 0
            or self.min_notional < 0
        ):
            raise ValueError("latence, pas, minimum et notionnel doivent être positifs ou nuls")
        if self.liquidity_model not in {"aggregate", "order_book"}:
            raise ValueError("liquidity_model doit valoir aggregate ou order_book")
        if not isinstance(self.simulation_profile, str) or not self.simulation_profile.strip():
            raise ValueError("simulation_profile doit être une chaîne non vide")
        if not isinstance(self.seed, int):
            raise TypeError("seed doit être un entier")


@dataclass(frozen=True)
class MarketOrder:
    order_id: str
    side: OrderSide
    qty: float
    reference_price: float
    available_volume: float | None = None
    delayed_price: float | None = None
    volatility_annual: float | None = None
    order_book: Mapping[str, object] | None = None


@dataclass(frozen=True)
class SimulatedFill:
    status: FillStatus
    price: float
    qty: float
    fee: float
    requested_qty: float
    latency_ms: int
    liquidity_model: str = "aggregate"
    liquidity_source: str | None = None


class ExecutionSimulator:
    """Simule un ordre au marché de façon reproductible et idempotente.

    Le rejet pseudo-aléatoire dépend uniquement de ``seed`` et ``order_id`` :
    recréer le simulateur après un crash produit donc le même résultat.
    """

    def __init__(self, config: ExecutionConfig | None = None) -> None:
        self.config = config or ExecutionConfig()
        self._results: dict[str, tuple[MarketOrder, SimulatedFill]] = {}
        # Remaining quantity is tracked per recorded book snapshot and level.
        # A new order cannot reuse volume from the same snapshot.
        self._book_remaining: dict[str, list[float]] = {}

    def fresh(self) -> ExecutionSimulator:
        """Crée une session vide avec exactement la même configuration."""

        return ExecutionSimulator(self.config)

    def quote_price(
        self,
        side: OrderSide,
        reference_price: float,
        *,
        delayed_price: float | None = None,
        participation: float = 0.0,
        volatility_annual: float | None = None,
    ) -> float:
        """Calcule le prix défavorable d'un fill sans modifier l'état."""

        side = OrderSide(side)
        self._validate_positive_finite("reference_price", reference_price)
        if delayed_price is not None:
            self._validate_positive_finite("delayed_price", delayed_price)
        if not 0.0 <= participation <= 1.0:
            raise ValueError("participation doit être comprise entre 0 et 1")
        if volatility_annual is not None:
            self._validate_non_negative_finite("volatility_annual", volatility_annual)
        base_price = (
            delayed_price
            if self.config.latency_ms > 0 and delayed_price is not None
            else reference_price
        )
        volatility_multiplier = (
            min(
                volatility_annual / self.config.volatility_reference_annual,
                self.config.volatility_multiplier_cap,
            )
            if volatility_annual is not None
            else 0.0
        )
        adverse_bps = (
            self.config.slippage_bps
            + self.config.volatility_impact_bps * volatility_multiplier
            + self.config.market_impact_bps * participation
        )
        direction = 1.0 if side == OrderSide.BUY else -1.0
        return base_price * (1.0 + direction * adverse_bps / 10_000.0)

    def execute_market(self, order: MarketOrder) -> SimulatedFill:
        """Exécute ou rejoue idempotemment un ordre au marché."""

        cached = self._results.get(order.order_id)
        if cached is not None:
            previous_order, previous_fill = cached
            if previous_order != order:
                raise ValueError(
                    f"Conflit d'idempotence pour l'ordre {order.order_id!r} : "
                    "même identifiant, paramètres différents"
                )
            return previous_fill

        self._validate_order(order)
        if self._draw(order.order_id, "rejection") < self.config.rejection_rate:
            result = self._empty_fill(order, FillStatus.REJECTED)
            self._results[order.order_id] = (order, result)
            return result

        if self.config.liquidity_model == "order_book":
            result = self._execute_against_order_book(order)
        else:
            fill_qty = order.qty
            participation = 0.0
            if (
                self.config.max_volume_participation is not None
                and order.available_volume is not None
            ):
                capacity = order.available_volume * self.config.max_volume_participation
                fill_qty = min(fill_qty, capacity)
            if order.available_volume is not None and order.available_volume > 0:
                participation = min(fill_qty / order.available_volume, 1.0)
            fill_qty = self._round_quantity(fill_qty)

            if fill_qty <= 0:
                result = self._empty_fill(order, FillStatus.EXPIRED)
            elif fill_qty < self.config.min_qty:
                result = self._empty_fill(order, FillStatus.REJECTED)
            else:
                price = self.quote_price(
                    order.side,
                    order.reference_price,
                    delayed_price=order.delayed_price,
                    participation=participation,
                    volatility_annual=order.volatility_annual,
                )
                if fill_qty * price < self.config.min_notional:
                    result = self._empty_fill(order, FillStatus.REJECTED)
                else:
                    status = FillStatus.PARTIAL if fill_qty < order.qty else FillStatus.FILLED
                    result = SimulatedFill(
                        status=status,
                        price=price,
                        qty=fill_qty,
                        fee=fill_qty * price * self.config.fee_rate,
                        requested_qty=order.qty,
                        latency_ms=self.config.latency_ms,
                        liquidity_model="aggregate",
                        liquidity_source="volume_aggregate",
                    )
        self._results[order.order_id] = (order, result)
        return result

    def _round_quantity(self, quantity: float) -> float:
        step = self.config.quantity_step
        if step <= 0:
            return quantity
        return math.floor((quantity + 1e-15) / step) * step

    @staticmethod
    def _book_snapshot(
        order: MarketOrder,
    ) -> tuple[str, list[tuple[float, float]], list[tuple[float, float]]]:
        raw = order.order_book
        if not isinstance(raw, Mapping):
            raise ValueError("Un carnet enregistré est obligatoire pour liquidity_model=order_book")
        bids_raw = raw.get("bids")
        asks_raw = raw.get("asks")
        if not isinstance(bids_raw, (list, tuple)) or not isinstance(asks_raw, (list, tuple)):
            raise ValueError("Carnet enregistré incomplet : bids/asks absents")

        def levels(value: object) -> list[tuple[float, float]]:
            out: list[tuple[float, float]] = []
            if not isinstance(value, (list, tuple)):
                return out
            for item in value:
                if not isinstance(item, (list, tuple)) or len(item) < 2:
                    raise ValueError("Niveau de carnet invalide")
                price, quantity = float(item[0]), float(item[1])
                if (
                    not math.isfinite(price)
                    or not math.isfinite(quantity)
                    or price <= 0
                    or quantity < 0
                ):
                    raise ValueError("Niveau de carnet non fini ou non positif")
                if quantity > 0:
                    out.append((price, quantity))
            return out

        bids = levels(bids_raw)
        asks = levels(asks_raw)
        if not bids and not asks:
            raise ValueError("Carnet enregistré vide")
        snapshot_id = raw.get("snapshot_id") or raw.get("timestamp")
        if snapshot_id is None:
            snapshot_id = hashlib.sha256(
                json.dumps(raw, sort_keys=True, default=str, separators=(",", ":")).encode()
            ).hexdigest()
        return str(snapshot_id), bids, asks

    def _execute_against_order_book(self, order: MarketOrder) -> SimulatedFill:
        snapshot_id, bids, asks = self._book_snapshot(order)
        levels = asks if order.side == OrderSide.BUY else bids
        # The venue book is expected best-first; sort defensively without
        # inventing liquidity or changing a level's quantity.
        levels = sorted(levels, key=lambda item: item[0], reverse=order.side == OrderSide.SELL)
        remaining_key = f"{snapshot_id}:{order.side.value}"
        remaining = self._book_remaining.setdefault(
            remaining_key, [quantity for _, quantity in levels]
        )
        if len(remaining) != len(levels):
            raise ValueError("Même snapshot_id avec une structure de carnet différente")
        requested = self._round_quantity(order.qty)
        if requested <= 0 or requested < self.config.min_qty:
            return self._empty_fill(order, FillStatus.REJECTED)
        left = requested
        filled = 0.0
        notional = 0.0
        for index, (price, _quantity) in enumerate(levels):
            if left <= 1e-15:
                break
            take = min(left, remaining[index])
            if take <= 0:
                continue
            remaining[index] -= take
            left -= take
            filled += take
            notional += take * price
        if filled <= 0:
            return self._empty_fill(order, FillStatus.EXPIRED)
        average = notional / filled
        if filled * average < self.config.min_notional:
            # A rejected minimum-notional order must not consume the book.
            for index, (_price, _quantity) in enumerate(levels):
                consumed = min(requested, max(0.0, _quantity - remaining[index]))
                remaining[index] += consumed
            return self._empty_fill(order, FillStatus.REJECTED)
        status = FillStatus.PARTIAL if filled < order.qty else FillStatus.FILLED
        return SimulatedFill(
            status=status,
            price=average,
            qty=filled,
            fee=notional * self.config.fee_rate,
            requested_qty=order.qty,
            latency_ms=self.config.latency_ms,
            liquidity_model="order_book",
            liquidity_source=f"recorded_book:{snapshot_id}",
        )

    @staticmethod
    def stop_trigger_price(
        *,
        direction: int,
        open_price: float,
        high_price: float,
        low_price: float,
        stop_price: float,
    ) -> float | None:
        """Prix de référence conservateur d'un stop touché, gaps inclus."""

        for name, value in (
            ("open_price", open_price),
            ("high_price", high_price),
            ("low_price", low_price),
            ("stop_price", stop_price),
        ):
            ExecutionSimulator._validate_positive_finite(name, value)
        if high_price < low_price or not low_price <= open_price <= high_price:
            raise ValueError("OHLC incohérent : low <= open <= high doit être respecté")
        if direction == 1:
            return min(open_price, stop_price) if low_price <= stop_price else None
        if direction == -1:
            return max(open_price, stop_price) if high_price >= stop_price else None
        raise ValueError("direction doit valoir +1 ou -1")

    def _validate_order(self, order: MarketOrder) -> None:
        if not order.order_id:
            raise ValueError("order_id ne peut pas être vide")
        OrderSide(order.side)
        self._validate_positive_finite("qty", order.qty)
        self._validate_positive_finite("reference_price", order.reference_price)
        if self.config.latency_ms > 0 and order.delayed_price is None:
            raise ValueError(
                "delayed_price est requis pour simuler une latence non nulle "
                "(aucune latence fictive silencieuse)"
            )
        if order.available_volume is not None:
            self._validate_non_negative_finite("available_volume", order.available_volume)
        if order.delayed_price is not None:
            self._validate_positive_finite("delayed_price", order.delayed_price)
        if order.volatility_annual is not None:
            self._validate_non_negative_finite("volatility_annual", order.volatility_annual)

    def _empty_fill(self, order: MarketOrder, status: FillStatus) -> SimulatedFill:
        return SimulatedFill(
            status=status,
            price=order.reference_price,
            qty=0.0,
            fee=0.0,
            requested_qty=order.qty,
            latency_ms=self.config.latency_ms,
            liquidity_model=self.config.liquidity_model,
            liquidity_source=(
                "recorded_book"
                if self.config.liquidity_model == "order_book"
                else "volume_aggregate"
            ),
        )

    def _draw(self, order_id: str, purpose: str) -> float:
        raw = f"{self.config.seed}:{purpose}:{order_id}".encode()
        value = int.from_bytes(hashlib.sha256(raw).digest()[:8], "big")
        return value / 2**64

    @staticmethod
    def _validate_positive_finite(name: str, value: float) -> None:
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} doit être un nombre fini strictement positif")

    @staticmethod
    def _validate_non_negative_finite(name: str, value: float) -> None:
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} doit être un nombre fini positif ou nul")
