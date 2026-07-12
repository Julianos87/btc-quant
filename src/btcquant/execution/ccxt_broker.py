"""Broker réel via ccxt — spot Binance ou futures perpétuels USDT-M.

Sécurité et fiabilité :
- clés API lues dans les variables d'environnement, jamais dans le code/config ;
- mode sandbox (testnet) activable ;
- retries avec backoff exponentiel sur les erreurs réseau ;
- clientOrderId déterministe → un retry ne peut pas doubler un ordre (idempotence) ;
- arrondi aux précisions de l'exchange et respect du notionnel minimal ;
- vrais ordres stop côté exchange (STOP_LOSS_LIMIT en spot, STOP_MARKET
  reduceOnly en futures) pour protéger la position même si le bot tombe ;
- en futures : levier verrouillé à 1x — le système est conçu sans levier.
"""

from __future__ import annotations

import logging
import os
import time

import ccxt

from .broker import Broker, Fill

log = logging.getLogger(__name__)

MAX_RETRIES = 4


class CcxtBroker(Broker):
    supports_stop_orders = True

    def __init__(
        self,
        exchange_id: str = "binance",
        symbol: str = "BTC/USDT",
        testnet: bool = True,
        market: str = "spot",  # "spot" | "perp"
        leverage: int = 1,
    ) -> None:
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
        self.exchange.load_markets()
        if market == "perp":
            self._with_retries(self.exchange.set_leverage, max(1, int(leverage)), self.symbol)
            log.info("Levier futures réglé à %dx sur %s", max(1, int(leverage)), self.symbol)
        self._order_seq = 0

    # ── helpers ──────────────────────────────────────────────────────────────
    def _with_retries(self, fn, *args, **kwargs):
        for attempt in range(MAX_RETRIES):
            try:
                return fn(*args, **kwargs)
            except (ccxt.NetworkError, ccxt.ExchangeNotAvailable, ccxt.RequestTimeout) as e:
                wait = 2**attempt
                log.warning("Erreur réseau %s, retry dans %ss", e.__class__.__name__, wait)
                time.sleep(wait)
        return fn(*args, **kwargs)  # dernière tentative, l'exception remonte

    def _client_order_id(self, tag: str) -> str:
        self._order_seq += 1
        return f"btcquant-{tag}-{int(time.time())}-{self._order_seq}"

    def _round_qty(self, qty: float) -> float:
        return float(self.exchange.amount_to_precision(self.symbol, qty))

    def _check_min_notional(self, qty: float, price: float) -> None:
        market = self.exchange.market(self.symbol)
        min_cost = (market.get("limits", {}).get("cost") or {}).get("min")
        if min_cost and qty * price < min_cost:
            raise ValueError(
                f"Notionnel {qty * price:.2f} sous le minimum exchange ({min_cost})"
            )

    def _fill_from_order(self, order: dict, fallback_price: float) -> Fill:
        price = order.get("average") or order.get("price") or fallback_price
        qty = order.get("filled") or order.get("amount") or 0.0
        fee = 0.0
        for f in order.get("fees") or []:
            fee += f.get("cost") or 0.0
        return Fill(price=float(price), qty=float(qty), fee=float(fee))

    def _market_order(self, side: str, qty: float, ref_price: float) -> Fill:
        qty = self._round_qty(qty)
        self._check_min_notional(qty, ref_price)
        params = {"newClientOrderId": self._client_order_id(side)}
        order = self._with_retries(
            self.exchange.create_order, self.symbol, "market", side, qty, None, params
        )
        order = self._wait_closed(order)
        fill = self._fill_from_order(order, ref_price)
        log.info("[LIVE] %s %.6f @ %.2f (frais %.4f)", side.upper(), fill.qty, fill.price, fill.fee)
        return fill

    # ── interface Broker ─────────────────────────────────────────────────────
    def market_buy(self, qty: float, ref_price: float) -> Fill:
        return self._market_order("buy", qty, ref_price)

    def market_sell(self, qty: float, ref_price: float) -> Fill:
        return self._market_order("sell", qty, ref_price)

    def place_stop(self, qty: float, stop_price: float, direction: int = 1) -> str | None:
        """Stop de protection côté exchange.

        Long (direction=1)  : vend si le prix descend au stop.
        Short (direction=-1): rachète si le prix monte au stop (futures).
        """
        qty = self._round_qty(qty)
        side = "sell" if direction == 1 else "buy"
        stop_price = float(self.exchange.price_to_precision(self.symbol, stop_price))
        if self.market_kind == "perp":
            params = {
                "stopPrice": stop_price,
                "reduceOnly": True,
                "newClientOrderId": self._client_order_id("stop"),
            }
            order = self._with_retries(
                self.exchange.create_order,
                self.symbol, "STOP_MARKET", side, qty, None, params,
            )
        else:
            if direction == -1:
                raise ValueError("Stop short impossible en spot")
            limit_price = float(
                self.exchange.price_to_precision(self.symbol, stop_price * 0.995)
            )
            params = {
                "stopPrice": stop_price,
                "newClientOrderId": self._client_order_id("stop"),
            }
            order = self._with_retries(
                self.exchange.create_order,
                self.symbol, "STOP_LOSS_LIMIT", side, qty, limit_price, params,
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

    def _wait_closed(self, order: dict, timeout_s: float = 30.0) -> dict:
        """Attend qu'un ordre market soit intégralement exécuté."""
        deadline = time.time() + timeout_s
        while order.get("status") not in ("closed", "canceled") and time.time() < deadline:
            time.sleep(1.0)
            order = self._with_retries(self.exchange.fetch_order, order["id"], self.symbol)
        if order.get("status") != "closed":
            log.error("Ordre %s non clôturé après %ss : %s", order.get("id"), timeout_s, order.get("status"))
        return order
