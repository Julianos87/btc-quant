"""Broker réel via ccxt — spot Binance ou futures perpétuels USDT-M.

Sécurité et fiabilité :
- clés API lues dans les variables d'environnement, jamais dans le code/config ;
- mode sandbox (testnet) activable ;
- retries avec backoff exponentiel sur les erreurs réseau ;
- clientOrderId déterministe → un retry ne peut pas doubler un ordre (idempotence) ;
- arrondi aux précisions de l'exchange et respect du notionnel minimal ;
- vrais ordres stop côté exchange (STOP_LOSS market en spot Binance, STOP_MARKET
  reduceOnly en futures) pour protéger la position même si le bot tombe ;
- en futures : levier verrouillé à 1x — le système est conçu sans levier.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time

import ccxt

from .broker import Broker, BrokerOrderSnapshot, Fill
from .resilience import RetryPolicy
from .safety import require_live_execution_enabled
from .units import decimal_notional, decimal_value, exchange_float

log = logging.getLogger(__name__)


class CcxtBroker(Broker):
    supports_stop_orders = True
    supports_order_lookup = True
    supports_position_reconciliation = True

    def __init__(
        self,
        exchange_id: str = "binance",
        symbol: str = "BTC/USDT",
        testnet: bool = True,
        market: str = "spot",  # "spot" | "perp"
        leverage: int = 1,
    ) -> None:
        require_live_execution_enabled(testnet=testnet)
        api_key = os.environ.get("BINANCE_API_KEY")
        api_secret = os.environ.get("BINANCE_API_SECRET")
        if not api_key or not api_secret:
            raise RuntimeError(
                "Clés API manquantes : définir BINANCE_API_KEY et BINANCE_API_SECRET "
                "dans l'environnement (jamais dans config.yaml)."
            )
        self.market_kind = market
        if market == "perp":
            # binanceusdm = futures perpétuels USDT-M
            klass = ccxt.binanceusdm if exchange_id == "binance" else getattr(ccxt, exchange_id)
        else:
            klass = getattr(ccxt, exchange_id)
        self.exchange: ccxt.Exchange = klass(
            {
                "apiKey": api_key,
                "secret": api_secret,
                "enableRateLimit": True,
                "timeout": 30_000,
            }
        )
        if testnet:
            self.exchange.set_sandbox_mode(True)
            log.warning("Broker en mode SANDBOX/TESTNET (%s)", market)
        self.symbol = symbol
        self._retry = RetryPolicy()
        self.exchange.load_markets()
        if market == "perp":
            self._with_retries(self.exchange.set_leverage, max(1, int(leverage)), self.symbol)
            log.info("Levier futures réglé à %dx sur %s", max(1, int(leverage)), self.symbol)
        self._order_seq = 0

    # ── helpers ──────────────────────────────────────────────────────────────
    def _with_retries(self, fn, *args, **kwargs):
        retry = getattr(self, "_retry", None)
        if retry is None:  # facilite les adaptateurs/tests construits sans __init__
            retry = self._retry = RetryPolicy()
        return retry.call(
            fn,
            *args,
            retry_on=(ccxt.NetworkError, ccxt.ExchangeNotAvailable, ccxt.RequestTimeout),
            **kwargs,
        )

    def _client_order_id(self, tag: str) -> str:
        self._order_seq += 1
        return f"btcquant-{tag}-{int(time.time())}-{self._order_seq}"

    def _round_qty(self, qty: float) -> float:
        return exchange_float(
            self.exchange.amount_to_precision(self.symbol, qty),
            name="quantité normalisée",
            positive=True,
        )

    def _check_min_notional(self, qty: float, price: float) -> None:
        market = self.exchange.market(self.symbol)
        min_cost = (market.get("limits", {}).get("cost") or {}).get("min")
        notional = decimal_notional(qty, price)
        if min_cost is not None and notional < decimal_value(
            min_cost,
            name="notionnel minimal",
            positive=True,
        ):
            raise ValueError(f"Notionnel {notional} sous le minimum exchange ({min_cost})")

    def _fill_from_order(self, order: dict, fallback_price: float) -> Fill:
        price = order.get("average") or order.get("price") or fallback_price
        # qty = quantité RÉELLEMENT exécutée, jamais la quantité demandée :
        # retomber sur `amount` fabriquerait une position fantôme si l'ordre
        # n'a pas (encore) été rempli. filled=0 → Fill.qty=0, l'appelant gère.
        qty = order.get("filled") or 0.0
        fee = 0.0
        for f in order.get("fees") or []:
            fee += f.get("cost") or 0.0
        if not fee and order.get("fee"):
            fee = order["fee"].get("cost") or 0.0
        broker_order_id = str(order["id"]) if order.get("id") is not None else None
        return Fill(
            price=float(price),
            qty=float(qty),
            fee=float(fee),
            broker_order_id=broker_order_id,
        )

    @staticmethod
    def _external_client_order_id(intent_id: str) -> str:
        """Identifiant stable respectant la limite courte des exchanges."""

        return f"btq-{hashlib.sha256(intent_id.encode()).hexdigest()[:28]}"

    def _market_order(
        self,
        side: str,
        qty: float,
        ref_price: float,
        client_order_id: str | None = None,
    ) -> Fill:
        qty = self._round_qty(qty)
        self._check_min_notional(qty, ref_price)
        external_client_id = (
            self._external_client_order_id(client_order_id)
            if client_order_id is not None
            else self._client_order_id(side)
        )
        params = {"newClientOrderId": external_client_id}
        # Ne jamais rejouer create_order après un timeout ambigu : le runner
        # garde l'intention PENDING et la rapproche via clientOrderId.
        order = self.exchange.create_order(self.symbol, "market", side, qty, None, params)
        order = self._wait_closed(order)
        fill = self._fill_from_order(order, ref_price)
        log.info("[LIVE] %s %.6f @ %.2f (frais %.4f)", side.upper(), fill.qty, fill.price, fill.fee)
        return fill

    # ── interface Broker ─────────────────────────────────────────────────────
    def market_buy(self, qty: float, ref_price: float) -> Fill:
        return self._market_order("buy", qty, ref_price)

    def market_sell(self, qty: float, ref_price: float) -> Fill:
        return self._market_order("sell", qty, ref_price)

    def execute_market(
        self,
        side: str,
        qty: float,
        ref_price: float,
        *,
        client_order_id: str | None = None,
        available_volume: float | None = None,
        delayed_price: float | None = None,
    ) -> Fill:
        del available_volume, delayed_price
        normalized_side = side.lower()
        if normalized_side not in ("buy", "sell"):
            raise ValueError(f"Côté d'ordre invalide : {side!r}")
        return self._market_order(normalized_side, qty, ref_price, client_order_id)

    def lookup_order(self, client_order_id: str) -> BrokerOrderSnapshot | None:
        external_id = self._external_client_order_id(client_order_id)
        try:
            order = self._with_retries(
                self.exchange.fetch_order,
                external_id,
                self.symbol,
                {"origClientOrderId": external_id},
            )
        except ccxt.OrderNotFound:
            return None
        fill = self._fill_from_order(order, float(order.get("price") or 0.0))
        raw_status = str(order.get("status") or "").lower()
        requested = float(order.get("amount") or 0.0)
        if raw_status == "closed":
            status = "FILLED" if fill.qty >= requested - 1e-12 else "PARTIAL"
        elif raw_status in ("canceled", "cancelled"):
            status = "CANCELED"
        elif raw_status in ("rejected", "expired"):
            status = raw_status.upper()
        else:
            status = "OPEN"
        return BrokerOrderSnapshot(
            client_order_id=client_order_id,
            broker_order_id=fill.broker_order_id,
            status=status,
            filled_qty=fill.qty,
            price=fill.price if fill.qty > 0 else None,
            fee=fill.fee,
        )

    def place_stop(self, qty: float, stop_price: float, direction: int = 1) -> str | None:
        """Stop de protection côté exchange.

        Long (direction=1)  : vend si le prix descend au stop.
        Short (direction=-1): rachète si le prix monte au stop (futures).
        """
        qty = self._round_qty(qty)
        side = "sell" if direction == 1 else "buy"
        stop_price = exchange_float(
            self.exchange.price_to_precision(self.symbol, stop_price),
            name="prix stop normalisé",
            positive=True,
        )
        if self.market_kind == "perp":
            params = {
                "stopPrice": stop_price,
                "reduceOnly": True,
                "newClientOrderId": self._client_order_id("stop"),
            }
            order = self.exchange.create_order(
                self.symbol,
                "STOP_MARKET",
                side,
                qty,
                None,
                params,
            )
        else:
            if direction == -1:
                raise ValueError("Stop short impossible en spot")
            # Binance Spot distingue STOP_LOSS (ordre market au déclenchement)
            # de STOP_LOSS_LIMIT. La variante LIMIT peut rester pendante lors
            # d'un gap violent et ne constitue donc pas une protection
            # acceptable pour le moteur live.
            market = self.exchange.market(self.symbol)
            supported = set((market.get("info") or {}).get("orderTypes") or ())
            if supported and "STOP_LOSS" not in supported:
                raise RuntimeError(
                    f"{self.symbol} ne déclare pas STOP_LOSS market : "
                    "exécution spot refusée faute de stop protecteur garanti"
                )
            params = {
                "stopPrice": stop_price,
                "newClientOrderId": self._client_order_id("stop"),
            }
            order = self.exchange.create_order(
                self.symbol,
                "STOP_LOSS",
                side,
                qty,
                None,
                params,
            )
        log.info("[LIVE] STOP %s posé : %.6f @ trigger %.2f", side.upper(), qty, stop_price)
        return order["id"]

    def cancel_stop(self, order_id: str) -> None:
        try:
            self._with_retries(self.exchange.cancel_order, order_id, self.symbol)
        except ccxt.OrderNotFound:
            log.info("Stop %s introuvable (déjà exécuté ou annulé)", order_id)

    def stop_status(self, order_id: str) -> dict:
        return self._with_retries(self.exchange.fetch_order, order_id, self.symbol)

    def free_quote_balance(self) -> float | None:
        balance = self._with_retries(self.exchange.fetch_balance)
        quote = self.symbol.split("/")[1]
        return float(balance.get("free", {}).get(quote, 0.0))

    def net_position(self, symbol: str) -> float:
        positions = self._with_retries(self.exchange.fetch_positions, [symbol])
        remote_net = 0.0
        for position in positions:
            qty = float(position.get("contracts") or 0.0)
            side = position.get("side")
            remote_net += qty if side == "long" else -qty if side == "short" else 0.0
        return remote_net

    def _wait_closed(self, order: dict, timeout_s: float = 30.0) -> dict:
        """Attend qu'un ordre market soit intégralement exécuté."""
        deadline = time.time() + timeout_s
        while order.get("status") not in ("closed", "canceled") and time.time() < deadline:
            time.sleep(1.0)
            order = self._with_retries(self.exchange.fetch_order, order["id"], self.symbol)
        if order.get("status") != "closed":
            log.error(
                "Ordre %s non clôturé après %ss : %s",
                order.get("id"),
                timeout_s,
                order.get("status"),
            )
        return order
