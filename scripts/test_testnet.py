"""Test d'intégration contre le testnet Binance Futures — LE portail avant le réel.

Exécute un cycle complet avec de l'argent factice : levier, achat market,
pose de stop, statut, annulation, revente. Chaque étape est vérifiée ; le
script s'arrête à la première anomalie.

Prérequis : compte testnet https://testnet.binancefuture.com puis
    export BINANCE_API_KEY=...   (clés TESTNET, pas les vraies !)
    export BINANCE_API_SECRET=...
Usage : python scripts/test_testnet.py

Le nettoyage est exécuté dans ``finally`` : une assertion intermédiaire ne
doit jamais laisser volontairement une position de test ouverte.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btcquant.execution.ccxt_broker import CcxtBroker
from btcquant.execution.carry_broker import CarryBroker, CarrySagaStatus
from btcquant.execution.readiness import require_passed_qualification
from btcquant.execution.safety import TESTNET_CONFIRMATION
from btcquant.execution.state_store import StateStore

STEPS = []


def step(name: str, ok: bool, detail: str = "") -> None:
    STEPS.append((name, ok))
    print(f"  {'✔' if ok else '✘'} {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        print(
            "\nARRÊT : corriger avant de continuer. Rien n'est validé tant que "
            "ce script ne passe pas de bout en bout."
        )
        sys.exit(1)


def main() -> None:
    require_passed_qualification(StateStore(ROOT / "state" / "btcquant.db"))
    if not os.environ.get("BINANCE_API_KEY"):
        sys.exit("BINANCE_API_KEY/SECRET manquants (clés TESTNET).")
    if os.environ.get("BTCQUANT_ENABLE_TESTNET") != TESTNET_CONFIRMATION:
        sys.exit(
            "Confirmation manquante : définir "
            f"BTCQUANT_ENABLE_TESTNET={TESTNET_CONFIRMATION} pour cette session."
        )

    print("═══ Test d'intégration testnet Binance Futures ═══\n")
    broker = CcxtBroker("binance", "BTC/USDT", testnet=True, market="perp", leverage=1)
    step("connexion + levier 1x", True)
    opened_qty = 0.0
    stop_id = None
    price = 0.0
    try:
        balance = broker.free_quote_balance()
        step("lecture du solde", balance is not None, f"{balance:,.2f} USDT libres")

        ticker = broker.exchange.fetch_ticker("BTC/USDT")
        price = float(ticker["last"])
        qty = round(150.0 / price, 4)
        step("prix courant", price > 0, f"{price:,.2f} $ — quantité de test {qty} BTC")

        fill = broker.market_buy(qty, price)
        opened_qty = fill.qty
        step("achat market exécuté", fill.qty > 0, f"{fill.qty} BTC @ {fill.price:,.2f}")

        stop_price = fill.price * 0.95
        stop_id = broker.place_stop(fill.qty, stop_price, direction=1)
        step("stop STOP_MARKET posé", stop_id is not None, f"trigger {stop_price:,.2f}")

        status = broker.stop_status(stop_id)
        step(
            "statut du stop lisible",
            status.get("status") in ("open", "untriggered", "closed"),
            f"status={status.get('status')}",
        )
        broker.cancel_stop(stop_id)
        stop_id = None
        fill2 = broker.market_sell(fill.qty, price)
        opened_qty = max(0.0, opened_qty - fill2.qty)
        step(
            "position refermée (vente market)",
            opened_qty <= 1e-8,
            f"{fill2.qty} BTC @ {fill2.price:,.2f}",
        )
    finally:
        if stop_id is not None:
            broker.cancel_stop(stop_id)
        if opened_qty > 1e-8 and price > 0:
            broker.market_sell(opened_qty, price)

    # ── volet carry : double-jambe spot + perp ──────────────────────────────
    print("\n--- Cash-and-carry (spot + perp) ---")
    cb = CarryBroker(symbol="BTC/USDT", testnet=True, leverage=3)
    carry_qty = 0.0
    try:
        step("CarryBroker connecté (spot + perp)", True)
        res = cb.open_position(150.0)
        carry_qty = res.neutral_qty
        step(
            "ouverture double-jambe",
            res.status in (CarrySagaStatus.FILLED, CarrySagaStatus.PARTIAL)
            and res.is_balanced
            and carry_qty > 0,
            f"{carry_qty} BTC neutre, statut={res.status}",
        )
        step("réconciliation des jambes", cb.reconcile())
        closed = cb.close_position(carry_qty)
        carry_qty = closed.neutral_qty
        step(
            "fermeture double-jambe",
            closed.status == CarrySagaStatus.FILLED and carry_qty <= 1e-8,
            f"statut={closed.status}",
        )
    finally:
        if carry_qty > 1e-8:
            cleanup = cb.close_position(carry_qty)
            if cleanup.neutral_qty > 1e-8 or not cleanup.is_balanced:
                raise RuntimeError(
                    "NETTOYAGE TESTNET INCOMPLET : vérifier immédiatement les deux comptes"
                )

    print(
        f"\n✅ {len(STEPS)}/{len(STEPS)} étapes passées — exécution futures ET "
        "carry double-jambe validées sur testnet."
    )


if __name__ == "__main__":
    main()
