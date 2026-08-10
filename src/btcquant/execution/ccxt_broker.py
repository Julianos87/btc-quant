"""Broker externe via ccxt — Hyperliquid ou Binance.

Sécurité et fiabilité :
- clés API lues dans les variables d'environnement, jamais dans le code/config ;
- mode sandbox (testnet) activable ;
- retries avec backoff exponentiel sur les erreurs réseau ;
- clientOrderId déterministe → un retry ne peut pas doubler un ordre (idempotence) ;
- arrondi aux précisions de l'exchange et respect du notionnel minimal ;
- vrais ordres stop côté exchange, ``reduceOnly``, pour protéger la position
  même si le bot tombe ;
- en futures, le levier est un PARAMÈTRE (``leverage``), transmis tel quel à
  l'exchange. Il n'est pas verrouillé ici : c'est l'appelant qui le fixe, et
  ``entrypoints.trend`` impose 1x sur le seul chemin externe autorisé
  (Hyperliquid testnet). Ne pas confondre avec ``RiskConfig.max_leverage``,
  qui plafonne le notionnel côté sizing.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from pathlib import Path

import ccxt

from .broker import Broker, BrokerOrderResult, BrokerOrderSnapshot, Fill
from .order_state import ExternalOrderState
from .resilience import RetryPolicy
from .safety import require_live_execution_enabled
from .units import decimal_notional, decimal_value, exchange_float

log = logging.getLogger(__name__)


class CcxtBroker(Broker):
    external_execution = True
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
        qualification_state_path: str | Path = "state/btcquant.db",
    ) -> None:
        require_live_execution_enabled(
            testnet=testnet,
            state_path=qualification_state_path,
        )
        self.market_kind = market
        self.exchange_id = exchange_id
        if exchange_id == "hyperliquid":
            if market != "perp":
                raise ValueError("Le broker Hyperliquid qualifié ne prend en charge que les perps")
            wallet_address = os.environ.get("HYPERLIQUID_WALLET_ADDRESS", "")
            private_key = os.environ.get("HYPERLIQUID_PRIVATE_KEY", "")
            if not re.fullmatch(r"0x[0-9a-fA-F]{40}", wallet_address):
                raise RuntimeError(
                    "HYPERLIQUID_WALLET_ADDRESS doit être l'adresse publique du compte "
                    "principal ou sous-compte (42 caractères hexadécimaux)."
                )
            if not re.fullmatch(r"0x[0-9a-fA-F]{64}", private_key):
                raise RuntimeError(
                    "HYPERLIQUID_PRIVATE_KEY doit être la clé privée d'un API wallet dédié "
                    "(jamais la clé du portefeuille principal)."
                )
            klass = ccxt.hyperliquid
            credentials = {
                "walletAddress": wallet_address,
                "privateKey": private_key,
            }
        else:
            api_key = os.environ.get("BINANCE_API_KEY")
            api_secret = os.environ.get("BINANCE_API_SECRET")
            if not api_key or not api_secret:
                raise RuntimeError(
                    "Clés API manquantes : définir BINANCE_API_KEY et BINANCE_API_SECRET "
                    "dans l'environnement (jamais dans config.yaml)."
                )
            credentials = {"apiKey": api_key, "secret": api_secret}
            if market == "perp":
                # binanceusdm = futures perpétuels USDT-M
                klass = ccxt.binanceusdm if exchange_id == "binance" else getattr(ccxt, exchange_id)
            else:
                klass = getattr(ccxt, exchange_id)
        self.exchange: ccxt.Exchange = klass(
            {
                **credentials,
                "enableRateLimit": True,
                "timeout": 30_000,
                # 1 % maximum pour les IOC simulant les marchés Hyperliquid.
                "options": {"defaultSlippage": 0.01},
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

    def _result_from_order(
        self,
        order: dict,
        fallback_price: float,
        requested_qty: float,
    ) -> BrokerOrderResult:
        """Normalise explicitement statut et reste sans déduire un rejet de ``filled``."""

        fill = self._fill_from_order(order, fallback_price)
        raw_status = str(order.get("status") or "").lower()
        exchange_requested = float(order.get("amount") or requested_qty)
        if exchange_requested <= 0:
            exchange_requested = requested_qty
        remaining_raw = order.get("remaining")
        if remaining_raw is None:
            remaining = (
                max(0.0, exchange_requested - fill.qty)
                if raw_status in ("open", "new", "pending")
                else 0.0
            )
        else:
            remaining = max(0.0, float(remaining_raw))

        if raw_status == "closed":
            status = (
                ExternalOrderState.FILLED
                if fill.qty >= exchange_requested - 1e-9 and remaining <= 1e-9
                else ExternalOrderState.PARTIAL_TERMINAL
                if fill.qty > 0
                # ``closed`` prouve la terminalité, mais pas sa cause. Sans
                # fill, l'assimiler à canceled ou rejected inventerait un fait.
                else ExternalOrderState.UNKNOWN
            )
            remaining = 0.0
        elif raw_status in ("canceled", "cancelled"):
            status = ExternalOrderState.CANCELED
            remaining = 0.0
        elif raw_status == "rejected":
            status = ExternalOrderState.REJECTED
            remaining = 0.0
        elif raw_status == "expired":
            status = ExternalOrderState.EXPIRED
            remaining = 0.0
        elif raw_status in ("open", "new", "pending"):
            remaining = max(remaining, exchange_requested - fill.qty)
            if remaining <= 1e-9:
                # Un exchange qui annonce simultanément ``open`` et aucun
                # reste fournit un état contradictoire. Ce n'est ni une preuve
                # de fill terminal ni une preuve de rejet.
                status = ExternalOrderState.UNKNOWN
            else:
                status = (
                    ExternalOrderState.PARTIAL_OPEN if fill.qty > 0 else ExternalOrderState.OPEN
                )
        else:
            status = ExternalOrderState.UNKNOWN

        return BrokerOrderResult(
            fill=fill,
            status=status,
            requested_qty=requested_qty,
            remaining_qty=remaining,
        )

    @staticmethod
    def _external_client_order_id(intent_id: str, exchange_id: str = "binance") -> str:
        """Identifiant stable respectant la limite courte des exchanges."""

        digest = hashlib.sha256(intent_id.encode()).hexdigest()
        if exchange_id == "hyperliquid":
            # Hyperliquid exige exactement un entier hexadécimal 128 bits.
            return f"0x{digest[:32]}"
        if intent_id.startswith("btq-mkt-"):
            # Nouvelles intentions : 4 caractères de préfixe + 32 hex = limite
            # Binance de 36 caractères, soit 128 bits de collision-résistance.
            return f"btq-{digest[:32]}"
        # Compatibilité de reprise : les versions <= v4 utilisaient 28 hex.
        # Modifier leur représentation empêcherait de retrouver un ordre déjà
        # accepté avant la migration.
        return f"btq-{digest[:28]}"

    def _market_order(
        self,
        side: str,
        qty: float,
        ref_price: float,
        client_order_id: str | None = None,
        *,
        reduce_only: bool = False,
    ) -> BrokerOrderResult:
        qty = self._round_qty(qty)
        self._check_min_notional(qty, ref_price)
        exchange_id = getattr(self, "exchange_id", "binance")
        if client_order_id is None:
            raise ValueError(
                "Un ordre market externe exige un client_order_id réservé par OrderExecutionService"
            )
        local_intent = client_order_id
        external_client_id = self._external_client_order_id(local_intent, exchange_id)
        client_key = "clientOrderId" if exchange_id == "hyperliquid" else "newClientOrderId"
        params: dict[str, object] = {client_key: external_client_id}
        if reduce_only:
            params["reduceOnly"] = True
        # Ne jamais rejouer create_order après un timeout ambigu : le runner
        # garde l'intention PENDING et la rapproche via clientOrderId.
        price = ref_price if exchange_id == "hyperliquid" else None
        order = self.exchange.create_order(self.symbol, "market", side, qty, price, params)
        order = self._wait_closed(order)
        result = self._result_from_order(order, ref_price, qty)
        fill = result.fill
        log.info(
            "[LIVE] %s %s %.6f/%.6f @ %.2f (frais %.4f)",
            result.status,
            side.upper(),
            fill.qty,
            qty,
            fill.price,
            fill.fee,
        )
        return result

    # ── interface Broker ─────────────────────────────────────────────────────
    def market_buy(self, qty: float, ref_price: float) -> BrokerOrderResult:
        return self._market_order("buy", qty, ref_price)

    def market_sell(self, qty: float, ref_price: float) -> BrokerOrderResult:
        return self._market_order("sell", qty, ref_price)

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
    ) -> BrokerOrderResult:
        del available_volume, delayed_price, volatility_annual
        normalized_side = side.lower()
        if normalized_side not in ("buy", "sell"):
            raise ValueError(f"Côté d'ordre invalide : {side!r}")
        return self._market_order(
            normalized_side,
            qty,
            ref_price,
            client_order_id,
            reduce_only=reduce_only,
        )

    def lookup_order(self, client_order_id: str) -> BrokerOrderSnapshot | None:
        exchange_id = getattr(self, "exchange_id", "binance")
        external_id = self._external_client_order_id(client_order_id, exchange_id)
        try:
            params = (
                {"clientOrderId": external_id}
                if exchange_id == "hyperliquid"
                else {"origClientOrderId": external_id}
            )
            order = self._with_retries(self.exchange.fetch_order, external_id, self.symbol, params)
        except ccxt.OrderNotFound:
            return None
        requested = float(order.get("amount") or 0.0)
        if requested <= 0:
            requested = max(
                float(order.get("filled") or 0.0) + float(order.get("remaining") or 0.0),
                1e-12,
            )
        result = self._result_from_order(
            order,
            float(order.get("price") or 0.0),
            requested,
        )
        fill = result.fill
        return BrokerOrderSnapshot(
            client_order_id=client_order_id,
            broker_order_id=fill.broker_order_id,
            status=result.status,
            filled_qty=fill.qty,
            price=fill.price if fill.qty > 0 else None,
            fee=fill.fee,
            requested_qty=result.requested_qty,
            remaining_qty=result.remaining_qty,
        )

    def place_stop(
        self,
        qty: float,
        stop_price: float,
        direction: int = 1,
        *,
        client_order_id: str | None = None,
    ) -> str | None:
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
        exchange_id = getattr(self, "exchange_id", "binance")
        local_intent = client_order_id or self._client_order_id("stop")
        external_client_id = self._external_client_order_id(local_intent, exchange_id)
        if exchange_id == "hyperliquid":
            params = {
                "stopLossPrice": stop_price,
                "reduceOnly": True,
                "clientOrderId": external_client_id,
            }
            # CCXT transforme le market trigger en ordre stop-market natif.
            # Le prix sert uniquement à calculer la limite de slippage du market
            # déclenché ; le trigger reste stopLossPrice.
            order = self.exchange.create_order(
                self.symbol,
                "market",
                side,
                qty,
                stop_price,
                params,
            )
        elif self.market_kind == "perp":
            params = {
                "stopPrice": stop_price,
                "reduceOnly": True,
                "newClientOrderId": external_client_id,
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
                "newClientOrderId": external_client_id,
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
        order_id = order.get("id")
        if order_id is None and client_order_id is not None:
            # Hyperliquid peut répondre ``waitingForTrigger`` sans oid. Le
            # cloid est immédiatement durable, mais son index info peut avoir
            # un léger retard. Attendre l'index sans jamais recréer l'ordre.
            for attempt in range(10):
                snapshot = self.lookup_order(client_order_id)
                if snapshot is not None and snapshot.broker_order_id is not None:
                    order_id = snapshot.broker_order_id
                    break
                if attempt < 9:
                    time.sleep(0.25)
        return str(order_id) if order_id is not None else None

    def cancel_stop(self, order_id: str) -> None:
        try:
            self._with_retries(self.exchange.cancel_order, order_id, self.symbol)
        except ccxt.OrderNotFound:
            # L'absence dans cette requête ne prouve aucune cause terminale.
            # Le runner doit effectuer le lookup et échouer fermé si la preuve
            # n'est pas disponible.
            log.warning("Stop %s introuvable : terminalité non prouvée", order_id)
            raise

    def stop_status(self, order_id: str) -> dict:
        return self._with_retries(self.exchange.fetch_order, order_id, self.symbol)

    def free_quote_balance(self) -> float | None:
        balance = self._with_retries(self.exchange.fetch_balance)
        quote = self.symbol.split("/")[1].split(":")[0]
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
        """Attend un statut terminal sans en déduire l'issue financière."""
        deadline = time.time() + timeout_s
        terminal = {"closed", "canceled", "cancelled", "rejected", "expired"}
        while str(order.get("status") or "").lower() not in terminal and time.time() < deadline:
            time.sleep(1.0)
            order = self._with_retries(self.exchange.fetch_order, order["id"], self.symbol)
        if str(order.get("status") or "").lower() not in terminal:
            log.error(
                "Ordre %s non clôturé après %ss : %s",
                order.get("id"),
                timeout_s,
                order.get("status"),
            )
        return order
