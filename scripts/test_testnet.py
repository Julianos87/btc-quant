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

from datetime import UTC, datetime
import math
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btcquant.console import enable_utf8_output

enable_utf8_output()

from btcquant.execution.ccxt_broker import CcxtBroker
from btcquant.execution.financial_application_plan import FinancialApplicationPlan
from btcquant.execution.order_service import OrderExecutionService, SubmitMarketCommand
from btcquant.execution.order_state import FinancialTransitionType, LogicalOrderIdentity
from btcquant.execution.state_contract import STOP_PROTECTION_EXCHANGE
from btcquant.execution.state_store import StateStore

SYMBOL = "BTC/USDC:USDC"
SMOKE_SLOT = "p1-smoke"


def _state_payload(
    *,
    cash: float,
    position: dict[str, object] | None,
    transition_sequence: int,
) -> dict[str, object]:
    return {
        "slots": {
            SMOKE_SLOT: {
                "cash": cash,
                "position": position,
                "stop_order_id": None,
                "stop_order_local_id": None,
                "stop_intent_id": None,
                "stop_transition": None,
                "entry_fee": 0.0,
                "last_bar_ts": None,
                "financial_transition_seq": transition_sequence,
            }
        },
        "peak_equity": cash,
        "halted": False,
        "day": None,
        "day_start_equity": cash,
        "daily_lockout": False,
        "reconciliation_required": False,
        "last_funding_ts": None,
        "stop_protection_mode": STOP_PROTECTION_EXCHANGE,
    }


def _market_command(
    *,
    state: dict[str, object],
    side: str,
    qty: float,
    reference_price: float,
    reason: str,
    decision_checkpoint: str,
    transition_type: FinancialTransitionType,
    position_generation: str | None = None,
    transition_sequence: int = 0,
    reduce_only: bool = False,
    entry_stop_price: float | None = None,
) -> SubmitMarketCommand:
    identity = LogicalOrderIdentity(
        engine="trend",
        slot=SMOKE_SLOT,
        decision_checkpoint=decision_checkpoint,
        transition_type=transition_type,
        position_generation=position_generation,
        transition_sequence=transition_sequence,
    )
    plan = FinancialApplicationPlan(
        identity=identity,
        side=side,
        requested_qty=qty,
        reference_price=reference_price,
        reason=reason,
        reduce_only=reduce_only,
        planned_effect_at=datetime.now(UTC).isoformat(),
        pre_state_payload=state,
        protection_mode=STOP_PROTECTION_EXCHANGE,
        entry_direction=(
            1
            if transition_type == FinancialTransitionType.ENTER_LONG
            else -1
            if transition_type == FinancialTransitionType.ENTER_SHORT
            else None
        ),
        entry_stop_price=entry_stop_price,
    )
    return SubmitMarketCommand(
        engine="trend",
        slot=SMOKE_SLOT,
        side=side,
        qty=qty,
        reference_price=reference_price,
        reason=reason,
        decision_checkpoint=decision_checkpoint,
        transition_type=transition_type,
        position_generation=position_generation,
        transition_sequence=transition_sequence,
        reduce_only=reduce_only,
        application_plan=plan,
    )


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
    flat_state = _state_payload(cash=1000.0, position=None, transition_sequence=0)
    store.save_engine_state("trend", flat_state)
    if "testnet" not in str(broker.exchange.urls["api"]["private"]).lower():
        raise RuntimeError("SÉCURITÉ : endpoint Hyperliquid non-testnet, abandon immédiat")
    initial_position = broker.net_position(SYMBOL)
    if abs(initial_position) > 1e-12:
        raise RuntimeError(
            f"Le compte doit être plat avant le smoke test (position={initial_position})"
        )

    candle = broker.exchange.fetch_ohlcv(SYMBOL, "1m", limit=1)[-1]
    price = float(candle[4])
    entry_checkpoint = datetime.fromtimestamp(float(candle[0]) / 1000.0, UTC).isoformat()
    quantity = _smoke_quantity(broker, price)
    stop_id: str | None = None
    close_checkpoint: str | None = None
    position_generation: str | None = None
    next_close_sequence: int | None = 0
    opened = False
    try:
        entry_result = orders.submit_market(
            _market_command(
                state=flat_state,
                side="BUY",
                qty=quantity,
                reference_price=price,
                reason="p1_smoke_entry",
                decision_checkpoint=entry_checkpoint,
                transition_type=FinancialTransitionType.ENTER_LONG,
                entry_stop_price=price * 0.95,
            )
        )
        if not entry_result.is_terminal:
            raise RuntimeError("Entrée testnet non terminale : réconciliation requise")
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
        position_generation = (
            f"entry={entry_result.application_plan.planned_effect_at}|initial_qty={entry.qty:.17g}"
        )
        close_checkpoint = datetime.now(UTC).isoformat()
        entry_state = _state_payload(
            cash=1000.0,
            position={
                "entry_time": entry_result.application_plan.planned_effect_at,
                "entry_price": entry.price,
                "qty": entry.qty,
                "stop_price": entry.price * 0.95,
                "direction": 1,
                "bars_held": 0,
                "best_close": entry.price,
                "initial_qty": entry.qty,
                "last_add_price": entry.price,
                "pyramid_adds": 0,
            },
            transition_sequence=1,
        )
        store.save_engine_state("trend", entry_state)
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
        # Tant que cet appel n'a pas fourni puis persisté une preuve terminale,
        # le finally ne doit jamais fabriquer une autre identité de clôture.
        next_close_sequence = None
        close_result = orders.submit_market(
            _market_command(
                state=entry_state,
                side="SELL",
                qty=entry.qty,
                reference_price=entry.price,
                reason="p1_smoke_close",
                decision_checkpoint=close_checkpoint,
                transition_type=FinancialTransitionType.EXIT,
                position_generation=position_generation,
                transition_sequence=1,
                reduce_only=True,
            )
        )
        if not close_result.is_terminal:
            raise RuntimeError("Clôture testnet non terminale : réconciliation requise")
        close = close_result.fill
        store.complete_order(
            close_result.order_id,
            status=close_result.status,
            filled_qty=close.qty,
            price=close.price,
            fee=close.fee,
            broker_order_id=close.broker_order_id,
        )
        next_close_sequence = close_result.transition_sequence + 1
        if close.qty <= 0:
            raise RuntimeError("Clôture reduce-only non exécutée")
        opened = abs(broker.net_position(SYMBOL)) > 1e-12
        store.save_engine_state(
            "trend",
            _state_payload(cash=1000.0, position=None, transition_sequence=next_close_sequence),
        )
        print(f"PASS clôture reduce-only : {close.qty:.8f} BTC")
    finally:
        if stop_id is not None:
            broker.cancel_stop(stop_id)
        remote = broker.net_position(SYMBOL)
        if opened and abs(remote) > 1e-12:
            side = "SELL" if remote > 0 else "BUY"
            if (
                close_checkpoint is None
                or position_generation is None
                or next_close_sequence is None
            ):
                raise RuntimeError(
                    "Nettoyage interdit : identité absente ou clôture précédente ambiguë"
                )
            emergency_position = dict(entry_state["slots"][SMOKE_SLOT]["position"])
            emergency_position["qty"] = abs(remote)
            emergency_state = _state_payload(
                cash=1000.0,
                position=emergency_position,
                transition_sequence=next_close_sequence,
            )
            emergency = orders.submit_market(
                _market_command(
                    state=emergency_state,
                    side=side,
                    qty=abs(remote),
                    reference_price=price,
                    reason="p1_smoke_close",
                    decision_checkpoint=close_checkpoint,
                    transition_type=FinancialTransitionType.EXIT,
                    position_generation=position_generation,
                    transition_sequence=next_close_sequence,
                    reduce_only=True,
                )
            )
            if not emergency.is_terminal:
                raise RuntimeError("Clôture testnet ambiguë : réconciliation requise")
            remote = broker.net_position(SYMBOL)
        if abs(remote) > 1e-12:
            raise RuntimeError(f"NETTOYAGE TESTNET INCOMPLET : position restante {remote} BTC")
    print("═══ PASS : portail Hyperliquid testnet validé et compte remis à plat ═══")


if __name__ == "__main__":
    main()
