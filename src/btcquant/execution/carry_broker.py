"""Exécuteur cash-and-carry réel : deux jambes simultanées (spot long + perp short).

Structure delta-neutre : on achète du BTC au comptant ET on vend le même
notionnel en perpétuel. Le risque de prix s'annule ; on encaisse le funding
versé aux shorts. Le levier ne s'applique qu'à la jambe perp (le collatéral
est le spot détenu, via le portfolio/cross margin de l'exchange).

⚠️ Ce module manipule DEUX comptes (spot + futures) et doit être validé sur
testnet avant tout usage réel. Le risque opérationnel principal est
l'exécution partielle d'une seule jambe (on se retrouve directionnel) : chaque
ouverture/fermeture vérifie que les deux jambes sont passées, et tente de
défaire la jambe orpheline en cas d'échec de l'autre.

Sécurité : clés API dans l'environnement (BINANCE_API_KEY/SECRET), sandbox
activable, retries réseau, clientOrderId déterministe (idempotence).
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import ccxt

from ..notify import notify
from .safety import require_live_execution_enabled
from .resilience import RetryPolicy
from .units import exchange_float

log = logging.getLogger(__name__)

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


class CarryBroker:
    def __init__(
        self,
        symbol: str = "BTC/USDT",
        testnet: bool = True,
        leverage: int = 3,
    ) -> None:
        require_live_execution_enabled(testnet=testnet)
        key = os.environ.get("BINANCE_API_KEY")
        secret = os.environ.get("BINANCE_API_SECRET")
        if not key or not secret:
            raise RuntimeError("Clés API manquantes (BINANCE_API_KEY / BINANCE_API_SECRET).")

        self.symbol = symbol
        self.perp_symbol = f"{symbol}:{symbol.split('/')[1]}"  # BTC/USDT:USDT
        common = {"apiKey": key, "secret": secret, "enableRateLimit": True, "timeout": 30_000}
        self.spot: ccxt.Exchange = ccxt.binance({**common, "options": {"defaultType": "spot"}})
        self.perp: ccxt.Exchange = ccxt.binanceusdm(common)
        self._retry = RetryPolicy()
        if testnet:
            self.spot.set_sandbox_mode(True)
            self.perp.set_sandbox_mode(True)
            log.warning("CarryBroker en mode SANDBOX/TESTNET")
        self.spot.load_markets()
        self.perp.load_markets()
        self._with_retries(self.perp.set_leverage, max(1, int(leverage)), self.perp_symbol)

    # ── helpers ──────────────────────────────────────────────────────────────
    def _with_retries(self, fn, *args, **kwargs):
        retry = getattr(self, "_retry", None)
        if retry is None:
            retry = self._retry = RetryPolicy()
        return retry.call(
            fn,
            *args,
            retry_on=(ccxt.NetworkError, ccxt.ExchangeNotAvailable, ccxt.RequestTimeout),
            **kwargs,
        )

    @staticmethod
    def _coid(intent_id: str, tag: str) -> str:
        digest = hashlib.sha256(f"{intent_id}:{tag}".encode()).hexdigest()[:20]
        return f"btcq-{tag}-{digest}"

    def _common_qty(self, raw_qty: float) -> float:
        spot_qty = exchange_float(
            self.spot.amount_to_precision(self.symbol, raw_qty),
            name="quantité spot normalisée",
            positive=True,
        )
        perp_qty = exchange_float(
            self.perp.amount_to_precision(self.perp_symbol, raw_qty),
            name="quantité perp normalisée",
            positive=True,
        )
        common = min(spot_qty, perp_qty)
        spot_common = exchange_float(
            self.spot.amount_to_precision(self.symbol, common),
            name="quantité spot commune",
            positive=True,
        )
        perp_common = exchange_float(
            self.perp.amount_to_precision(self.perp_symbol, common),
            name="quantité perp commune",
            positive=True,
        )
        if abs(spot_common - perp_common) > QTY_TOLERANCE:
            raise ValueError(f"Précisions spot/perp incompatibles : {spot_common} vs {perp_common}")
        return min(spot_common, perp_common)

    def _market_fill(
        self,
        exchange: ccxt.Exchange,
        symbol: str,
        *,
        leg: str,
        side: str,
        qty: float,
        client_order_id: str,
        reduce_only: bool = False,
    ) -> CarryLegFill:
        params: dict[str, Any] = {"newClientOrderId": client_order_id}
        if reduce_only:
            params["reduceOnly"] = True
        # Un timeout est ambigu : la saga doit rester transitionnelle et être
        # rapprochée, jamais renvoyer aveuglément la jambe.
        order = exchange.create_order(
            symbol,
            "market",
            side,
            qty,
            None,
            params,
        )
        deadline = time.monotonic() + 30.0
        while str(order.get("status") or "").lower() not in (
            "closed",
            "canceled",
            "cancelled",
            "rejected",
            "expired",
        ):
            if time.monotonic() >= deadline:
                break
            time.sleep(0.5)
            order = self._with_retries(exchange.fetch_order, order["id"], symbol)
        return CarryLegFill(
            leg=leg,
            side=side.upper(),
            requested_qty=qty,
            filled_qty=float(order.get("filled") or 0.0),
            average_price=(float(order["average"]) if order.get("average") is not None else None),
            broker_order_id=str(order["id"]) if order.get("id") is not None else None,
        )

    def last_price(self) -> float:
        return float(self.perp.fetch_ticker(self.perp_symbol)["last"])

    # ── ouverture / fermeture des deux jambes ────────────────────────────────
    def open_position(
        self,
        notional_usdt: float,
        *,
        intent_id: str | None = None,
    ) -> CarrySagaResult:
        """Saga d'ouverture : spot, hedge perp, puis compensation si nécessaire."""

        intent = intent_id or f"carry-open-{uuid.uuid4().hex}"
        price = self.last_price()
        try:
            qty = self._common_qty(notional_usdt / price)
        except Exception as error:
            return CarrySagaResult(
                CarrySagaStatus.REJECTED,
                0.0,
                0.0,
                error=f"{type(error).__name__}: {error}",
            )
        if qty <= 0:
            return CarrySagaResult(
                CarrySagaStatus.REJECTED,
                0.0,
                0.0,
                error="Quantité commune nulle",
            )

        try:
            spot_fill = self._market_fill(
                self.spot,
                self.symbol,
                leg="SPOT",
                side="buy",
                qty=qty,
                client_order_id=self._coid(intent, "spotbuy"),
            )
        except Exception as error:
            return CarrySagaResult(
                CarrySagaStatus.REJECTED,
                0.0,
                0.0,
                error=f"Jambe spot : {type(error).__name__}: {error}",
            )
        if spot_fill.filled_qty <= QTY_TOLERANCE:
            return CarrySagaResult(
                CarrySagaStatus.REJECTED,
                0.0,
                0.0,
                spot_fill=spot_fill,
                error="Aucun fill spot",
            )

        hedge_qty = float(self.perp.amount_to_precision(self.perp_symbol, spot_fill.filled_qty))
        perp_fill: CarryLegFill | None
        perp_error: str | None
        try:
            perp_fill = self._market_fill(
                self.perp,
                self.perp_symbol,
                leg="PERP",
                side="sell",
                qty=hedge_qty,
                client_order_id=self._coid(intent, "perpshort"),
            )
        except Exception as error:
            perp_fill = None
            perp_error = f"{type(error).__name__}: {error}"
        else:
            perp_error = None

        spot_qty = spot_fill.filled_qty
        perp_qty = perp_fill.filled_qty if perp_fill else 0.0
        compensation = None
        excess_spot = max(0.0, spot_qty - perp_qty)
        if excess_spot > QTY_TOLERANCE:
            try:
                unwind_qty = float(self.spot.amount_to_precision(self.symbol, excess_spot))
                compensation = self._market_fill(
                    self.spot,
                    self.symbol,
                    leg="SPOT_COMPENSATION",
                    side="sell",
                    qty=unwind_qty,
                    client_order_id=self._coid(intent, "spotunwind"),
                )
                spot_qty -= compensation.filled_qty
            except Exception as error:
                return CarrySagaResult(
                    CarrySagaStatus.UNBALANCED,
                    spot_qty,
                    perp_qty,
                    spot_fill,
                    perp_fill,
                    error=(f"Hedge={perp_error}; compensation={type(error).__name__}: {error}"),
                )

        balanced = abs(spot_qty - perp_qty) <= QTY_TOLERANCE
        status = (
            CarrySagaStatus.FILLED
            if balanced and spot_qty >= qty - QTY_TOLERANCE
            else CarrySagaStatus.PARTIAL
            if balanced and spot_qty > QTY_TOLERANCE
            else CarrySagaStatus.REJECTED
            if balanced
            else CarrySagaStatus.UNBALANCED
        )
        return CarrySagaResult(
            status,
            spot_qty,
            perp_qty,
            spot_fill,
            perp_fill,
            compensation,
            perp_error,
        )

    def close_position(
        self,
        qty: float,
        *,
        intent_id: str | None = None,
    ) -> CarrySagaResult:
        """Ferme le perp puis la même quantité spot ; toute divergence est exposée."""

        intent = intent_id or f"carry-close-{uuid.uuid4().hex}"
        try:
            perp_fill = self._market_fill(
                self.perp,
                self.perp_symbol,
                leg="PERP",
                side="buy",
                qty=qty,
                client_order_id=self._coid(intent, "perpcover"),
                reduce_only=True,
            )
        except Exception as error:
            return CarrySagaResult(
                CarrySagaStatus.REJECTED,
                qty,
                qty,
                error=f"Rachat perp : {type(error).__name__}: {error}",
            )
        if perp_fill.filled_qty <= QTY_TOLERANCE:
            return CarrySagaResult(
                CarrySagaStatus.REJECTED,
                qty,
                qty,
                perp_fill=perp_fill,
                error="Aucun fill de couverture perp",
            )
        spot_sell_qty = float(self.spot.amount_to_precision(self.symbol, perp_fill.filled_qty))
        try:
            spot_fill = self._market_fill(
                self.spot,
                self.symbol,
                leg="SPOT",
                side="sell",
                qty=spot_sell_qty,
                client_order_id=self._coid(intent, "spotsell"),
            )
        except Exception as error:
            return CarrySagaResult(
                CarrySagaStatus.UNBALANCED,
                qty,
                max(0.0, qty - perp_fill.filled_qty),
                perp_fill=perp_fill,
                error=f"Vente spot : {type(error).__name__}: {error}",
            )
        remaining_spot = max(0.0, qty - spot_fill.filled_qty)
        remaining_perp = max(0.0, qty - perp_fill.filled_qty)
        if abs(remaining_spot - remaining_perp) > QTY_TOLERANCE:
            return CarrySagaResult(
                CarrySagaStatus.UNBALANCED,
                remaining_spot,
                remaining_perp,
                spot_fill,
                perp_fill,
                error="Fills de fermeture déséquilibrés",
            )
        status = (
            CarrySagaStatus.FILLED if remaining_spot <= QTY_TOLERANCE else CarrySagaStatus.PARTIAL
        )
        return CarrySagaResult(
            status,
            remaining_spot,
            remaining_perp,
            spot_fill,
            perp_fill,
        )

    def reconcile(self) -> bool:
        """Vérifie que spot détenu ≈ short perp (position réellement neutre)."""
        try:
            bal = self._with_retries(self.spot.fetch_balance)
            spot_btc = float(bal.get("total", {}).get("BTC", 0.0))
            positions = self._with_retries(self.perp.fetch_positions, [self.perp_symbol])
            perp_short = 0.0
            for p in positions:
                if p.get("side") == "short":
                    perp_short += float(p.get("contracts") or 0.0)
        except Exception as e:
            log.error("Réconciliation carry impossible : %s", e)
            notify(f"⛔ Carry live : réconciliation impossible ({e})")
            return False
        diff = abs(spot_btc - perp_short)
        if diff > 1e-4:
            notify(
                f"⚠ Carry live : déséquilibre des jambes — spot {spot_btc:.5f} BTC "
                f"vs short {perp_short:.5f} BTC (écart {diff:.5f}). Vérifier."
            )
            return False
        return True
