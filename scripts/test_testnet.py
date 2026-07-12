"""Test d'intégration contre le testnet Binance Futures — LE portail avant le réel.

Exécute un cycle complet avec de l'argent factice : levier, achat market,
pose de stop, statut, annulation, revente. Chaque étape est vérifiée ; le
script s'arrête à la première anomalie.

Prérequis : compte testnet https://testnet.binancefuture.com puis
    export BINANCE_API_KEY=...   (clés TESTNET, pas les vraies !)
    export BINANCE_API_SECRET=...
Usage : python scripts/test_testnet.py
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btcquant.execution.ccxt_broker import CcxtBroker

STEPS = []


def step(name: str, ok: bool, detail: str = "") -> None:
    STEPS.append((name, ok))
    print(f"  {'✔' if ok else '✘'} {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        print("\nARRÊT : corriger avant de continuer. Rien n'est validé tant que "
              "ce script ne passe pas de bout en bout.")
        sys.exit(1)


def main() -> None:
    if not os.environ.get("BINANCE_API_KEY"):
        sys.exit("BINANCE_API_KEY/SECRET manquants (clés TESTNET).")

    print("═══ Test d'intégration testnet Binance Futures ═══\n")
    broker = CcxtBroker("binance", "BTC/USDT", testnet=True, market="perp", leverage=1)
    step("connexion + levier 1x", True)

    balance = broker.free_quote_balance()
    step("lecture du solde", balance is not None, f"{balance:,.2f} USDT libres")

    ticker = broker.exchange.fetch_ticker("BTC/USDT")
    price = float(ticker["last"])
    qty = round(150.0 / price, 4)  # ~150 $ de notionnel, au-dessus du minimum
    step("prix courant", price > 0, f"{price:,.2f} $ — quantité de test {qty} BTC")

    fill = broker.market_buy(qty, price)
    step("achat market exécuté", fill.qty > 0, f"{fill.qty} BTC @ {fill.price:,.2f}")

    stop_price = fill.price * 0.95
    stop_id = broker.place_stop(fill.qty, stop_price, direction=1)
    step("stop STOP_MARKET posé", stop_id is not None, f"trigger {stop_price:,.2f}")

    status = broker.stop_status(stop_id)
    step("statut du stop lisible", status.get("status") in ("open", "untriggered", "closed"),
         f"status={status.get('status')}")

    broker.cancel_stop(stop_id)
    status = broker.stop_status(stop_id)
    step("stop annulé", status.get("status") in ("canceled", "cancelled", "closed"),
         f"status={status.get('status')}")

    fill2 = broker.market_sell(fill.qty, price)
    step("position refermée (vente market)", fill2.qty > 0,
         f"{fill2.qty} BTC @ {fill2.price:,.2f}")

    print(f"\n✅ {len(STEPS)}/{len(STEPS)} étapes passées — l'exécution futures "
          "est validée sur testnet.")


if __name__ == "__main__":
    main()
