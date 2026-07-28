"""Smoke test destructif et nettoyant du testnet Hyperliquid.

Ce script émet de vrais ordres sur le TESTNET uniquement. Il vérifie :
connexion API wallet, sandbox, position initialement plate, ordre IOC,
stop-market reduce-only, lookup par cloid, annulation puis clôture reduce-only.

Prérequis (ne jamais coller les valeurs dans un terminal partagé ou Git) :
    HYPERLIQUID_WALLET_ADDRESS  adresse publique du compte testnet
    HYPERLIQUID_PRIVATE_KEY     clé privée d'un API wallet testnet dédié
    BTCQUANT_ENABLE_TESTNET=I_ACCEPT_TESTNET_ORDERS
    qualification paper v2 PASS dans state/btcquant.db
"""

from __future__ import annotations

import math
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btcquant.console import enable_utf8_output

enable_utf8_output()

from btcquant.execution.ccxt_broker import CcxtBroker
from btcquant.execution.order_service import OrderExecutionService
from btcquant.execution.state_store import StateStore

SYMBOL = "BTC/USDC:USDC"


def _smoke_quantity(broker: CcxtBroker, price: float) -> float:
    """Retourne la plus petite taille tradable au-dessus de 12 USDC."""

    market = broker.exchange.market(SYMBOL)
    minimum = float(((market.get("limits") or {}).get("amount") or {}).get("min") or 0.0)
    raw = max(minimum, 12.0 / price)
    quantity = float(broker.exchange.amount_to_precision(SYMBOL, raw))
    if quantity <= 0 or not math.isfinite(quantity):
        raise RuntimeError("Impossible de calculer une quantité testnet valide")
    return quantity


def main() -> None:
    print("═══ Smoke test Hyperliquid TESTNET (ordres externes) ═══")
    broker = CcxtBroker(
        "hyperliquid",
        SYMBOL,
        testnet=True,
        market="perp",
        leverage=1,
        qualification_state_path=ROOT / "state" / "btcquant.db",
    )
    store = StateStore(ROOT / "state" / "btcquant-testnet.db")
    orders = OrderExecutionService(store, broker)
    if "testnet" not in str(broker.exchange.urls["api"]["private"]).lower():
        raise RuntimeError("SÉCURITÉ : endpoint Hyperliquid non-testnet, abandon immédiat")
    initial_position = broker.net_position(SYMBOL)
    if abs(initial_position) > 1e-12:
        raise RuntimeError(
            f"Le compte doit être plat avant le smoke test (position={initial_position})"
        )

    price = float(broker.exchange.fetch_ohlcv(SYMBOL, "1m", limit=1)[-1][4])
    quantity = _smoke_quantity(broker, price)
    stop_id: str | None = None
    opened = False
    try:
        entry_result = orders.submit_market(
            engine="trend",
            slot="p1-smoke",
            side="BUY",
            qty=quantity,
            reference_price=price,
            reason="p1_smoke_entry",
        )
        entry = entry_result.fill
        store.complete_order(
            entry_result.order_id,
            status=entry_result.status,
            filled_qty=entry.qty,
            price=entry.price,
            fee=entry.fee,
            broker_order_id=entry.broker_order_id,
        )
        if entry.qty <= 0 or entry.broker_order_id is None:
            raise RuntimeError("Entrée testnet non exécutée")
        opened = True
        print(f"PASS entrée IOC : {entry.qty:.8f} BTC")

        stop_intent = f"p1-smoke-stop-{uuid.uuid4().hex}"
        local_stop_id = store.begin_order(
            "trend",
            "p1-smoke",
            stop_intent,
            "STOP",
            "SELL",
            entry.qty,
            "p1_smoke_stop",
            reference_price=entry.price * 0.95,
        )
        stop_id = broker.place_stop(
            entry.qty,
            entry.price * 0.95,
            direction=1,
            client_order_id=stop_intent,
        )
        if stop_id is None:
            raise RuntimeError("Stop testnet créé sans identifiant récupérable")
        stop = broker.protective_order_snapshot(stop_id)
        if stop.status != "OPEN" or abs(stop.requested_qty - entry.qty) > 1e-9:
            raise RuntimeError(
                f"Stop non protecteur : status={stop.status}, qty={stop.requested_qty}"
            )
        found = broker.lookup_order(stop_intent)
        if found is None or found.broker_order_id != stop_id or found.status != "OPEN":
            raise RuntimeError("Lookup du stop par cloid incohérent")
        store.complete_order(
            local_stop_id,
            status="OPEN",
            broker_order_id=stop_id,
        )
        print(f"PASS stop-market reduce-only + cloid : {stop_id}")

        broker.cancel_stop(stop_id)
        store.complete_order(local_stop_id, status="CANCELED", broker_order_id=stop_id)
        stop_id = None
        close_result = orders.submit_market(
            engine="trend",
            slot="p1-smoke",
            side="SELL",
            qty=entry.qty,
            reference_price=entry.price,
            reason="p1_smoke_close",
            reduce_only=True,
        )
        close = close_result.fill
        store.complete_order(
            close_result.order_id,
            status=close_result.status,
            filled_qty=close.qty,
            price=close.price,
            fee=close.fee,
            broker_order_id=close.broker_order_id,
        )
        if close.qty <= 0:
            raise RuntimeError("Clôture reduce-only non exécutée")
        opened = False
        print(f"PASS clôture reduce-only : {close.qty:.8f} BTC")
    finally:
        if stop_id is not None:
            broker.cancel_stop(stop_id)
        remote = broker.net_position(SYMBOL)
        if opened and abs(remote) > 1e-12:
            side = "SELL" if remote > 0 else "BUY"
            broker.execute_market(
                side,
                abs(remote),
                price,
                client_order_id=f"p1-smoke-emergency-{uuid.uuid4().hex}",
                reduce_only=True,
            )
            remote = broker.net_position(SYMBOL)
        if abs(remote) > 1e-12:
            raise RuntimeError(f"NETTOYAGE TESTNET INCOMPLET : position restante {remote} BTC")
    print("═══ PASS : portail Hyperliquid testnet validé et compte remis à plat ═══")


if __name__ == "__main__":
    main()
