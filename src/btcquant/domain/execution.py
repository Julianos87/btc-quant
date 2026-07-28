"""Modèle d'exécution métier déterministe partagé par backtest et paper."""

from __future__ import annotations

import hashlib
import math
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
        if not math.isfinite(self.min_qty) or self.latency_ms < 0 or self.min_qty < 0:
            raise ValueError("latency_ms et min_qty doivent être positifs ou nuls")
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


@dataclass(frozen=True)
class SimulatedFill:
    status: FillStatus
    price: float
    qty: float
    fee: float
    requested_qty: float
    latency_ms: int


class ExecutionSimulator:
    """Simule un ordre au marché de façon reproductible et idempotente.

    Le rejet pseudo-aléatoire dépend uniquement de ``seed`` et ``order_id`` :
    recréer le simulateur après un crash produit donc le même résultat.
    """

    def __init__(self, config: ExecutionConfig | None = None) -> None:
        self.config = config or ExecutionConfig()
        self._results: dict[str, tuple[MarketOrder, SimulatedFill]] = {}

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

        fill_qty = order.qty
        participation = 0.0
        if self.config.max_volume_participation is not None and order.available_volume is not None:
            capacity = order.available_volume * self.config.max_volume_participation
            fill_qty = min(fill_qty, capacity)
        if order.available_volume is not None and order.available_volume > 0:
            participation = min(fill_qty / order.available_volume, 1.0)

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
            status = FillStatus.PARTIAL if fill_qty < order.qty else FillStatus.FILLED
            result = SimulatedFill(
                status=status,
                price=price,
                qty=fill_qty,
                fee=fill_qty * price * self.config.fee_rate,
                requested_qty=order.qty,
                latency_ms=self.config.latency_ms,
            )
        self._results[order.order_id] = (order, result)
        return result

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
