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
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btcquant.console import enable_utf8_output

enable_utf8_output()

from btcquant.execution.ccxt_broker import CcxtBroker
from btcquant.execution.clock import SystemClock
from btcquant.execution.external_settlement_runtime import ExternalSettlementRuntime
from btcquant.execution.financial_application_plan import FinancialApplicationPlan
from btcquant.execution.instance_lock import EngineInstanceLock
from btcquant.execution.order_service import (
    OrderExecutionService,
    SubmitMarketCommand,
    SubmittedOrder,
)
from btcquant.execution.order_state import FinancialTransitionType, LogicalOrderIdentity
from btcquant.execution.state_contract import STOP_PROTECTION_EXCHANGE
from btcquant.execution.state_store import StateStore

SYMBOL = "BTC/USDC:USDC"
SMOKE_SLOT = "p1-smoke"
TESTNET_ENGINE_DATABASE_NAME = "btcquant-testnet.db"
SMOKE_DATABASE_PREFIX = "btcquant-testnet-smoke-"


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


def _smoke_database_path(root: Path = ROOT) -> Path:
    """Return a fresh, persistent database that can never alias TESTNET."""

    directory = root / "state" / "testnet-smoke"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{SMOKE_DATABASE_PREFIX}{uuid.uuid4().hex}.db"


def _testnet_engine_database(root: Path = ROOT) -> Path:
    return root / "state" / TESTNET_ENGINE_DATABASE_NAME


def _smoke_instance_lock(root: Path = ROOT) -> EngineInstanceLock:
    """Use the engine's lock while keeping smoke persistence isolated."""

    return EngineInstanceLock(_testnet_engine_database(root), "trend")


def _testnet_preflight(broker: CcxtBroker) -> tuple[float, str]:
    """Read every external precondition before creating any smoke state."""

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
    return price, entry_checkpoint


def _load_smoke_state(store: StateStore) -> dict[str, Any]:
    state = store.load_engine_state("trend")
    if not isinstance(state, dict):
        raise RuntimeError("État local du smoke test absent ou invalide")
    return state


def _smoke_position(state: dict[str, Any]) -> dict[str, Any]:
    slots = state.get("slots")
    if not isinstance(slots, dict) or not isinstance(slots.get(SMOKE_SLOT), dict):
        raise RuntimeError("Slot local du smoke test absent ou invalide")
    position = slots[SMOKE_SLOT].get("position")
    if not isinstance(position, dict):
        raise RuntimeError("Position locale attendue pour la clôture de secours")
    return position


def _smoke_transition_sequence(state: dict[str, Any]) -> int:
    slots = state.get("slots")
    slot = slots.get(SMOKE_SLOT) if isinstance(slots, dict) else None
    sequence = slot.get("financial_transition_seq") if isinstance(slot, dict) else None
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
        raise RuntimeError("Séquence financière locale absente ou invalide")
    return sequence


def _settle_external_market_order(
    runtime: ExternalSettlementRuntime,
    submitted: SubmittedOrder,
    clock: SystemClock,
) -> None:
    """Apply and durably finalize one terminal external market observation."""

    if not submitted.is_terminal:
        raise RuntimeError("Ordre testnet non terminal : réconciliation requise")
    runtime.reconcile_order(
        submitted.order_id,
        observed_at=clock.utc_now().isoformat(),
    )


def _assert_smoke_cleanup(store: StateStore, broker: CcxtBroker) -> None:
    """Require remote flatness, local flatness and no unresolved order."""

    remote = broker.net_position(SYMBOL)
    if abs(remote) > 1e-12:
        raise RuntimeError(f"NETTOYAGE TESTNET INCOMPLET : position restante {remote} BTC")
    state = _load_smoke_state(store)
    slots = state.get("slots")
    slot = slots.get(SMOKE_SLOT) if isinstance(slots, dict) else None
    if not isinstance(slot, dict) or slot.get("position") is not None:
        raise RuntimeError("NETTOYAGE TESTNET INCOMPLET : position locale encore ouverte")
    unresolved = store.unresolved_orders("trend")
    if unresolved:
        identifiers = ", ".join(str(order["id"]) for order in unresolved)
        raise RuntimeError(
            f"NETTOYAGE TESTNET INCOMPLET : ordres locaux non résolus ({identifiers})"
        )


def _finalize_smoke_stop(
    store: StateStore,
    broker: CcxtBroker,
    local_stop_id: int | None,
    stop_id: str | None,
) -> None:
    if stop_id is not None:
        broker.cancel_stop(stop_id)
        if local_stop_id is not None:
            store.complete_order(
                local_stop_id,
                status="CANCELED",
                broker_order_id=stop_id,
            )
    elif local_stop_id is not None:
        store.complete_order(
            local_stop_id,
            status="REJECTED",
            error="Stop testnet rejeté avant toute émission externe",
        )


def _cleanup_smoke_position(
    *,
    store: StateStore,
    broker: CcxtBroker,
    orders: OrderExecutionService,
    runtime: ExternalSettlementRuntime,
    clock: SystemClock,
    price: float,
    close_checkpoint: str | None,
    position_generation: str | None,
    next_close_sequence: int | None,
    opened: bool,
) -> None:
    """Close an open smoke position only with a durable, unambiguous identity."""

    remote = broker.net_position(SYMBOL)
    if opened and abs(remote) > 1e-12:
        side = "SELL" if remote > 0 else "BUY"
        if close_checkpoint is None or position_generation is None or next_close_sequence is None:
            raise RuntimeError(
                "Nettoyage interdit : identité absente ou clôture précédente ambiguë"
            )
        current_state = _load_smoke_state(store)
        current_position = _smoke_position(current_state)
        local_qty = float(current_position["qty"])
        if not math.isclose(local_qty, abs(remote), rel_tol=0.0, abs_tol=1e-9):
            raise RuntimeError(
                "Nettoyage interdit : position locale et distante divergentes "
                f"({local_qty} != {abs(remote)})"
            )
        if _smoke_transition_sequence(current_state) != next_close_sequence:
            raise RuntimeError("Nettoyage interdit : séquence locale de clôture incohérente")
        emergency = orders.submit_market(
            _market_command(
                state=current_state,
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
        _settle_external_market_order(runtime, emergency, clock)
    _assert_smoke_cleanup(store, broker)


def main() -> None:
    smoke_lock = _smoke_instance_lock()
    smoke_lock.acquire()
    try:
        print("═══ Smoke test Hyperliquid TESTNET (ordres externes) ═══")
        broker = CcxtBroker(
            "hyperliquid",
            SYMBOL,
            testnet=True,
            market="perp",
            leverage=1,
            qualification_state_path=ROOT / "state" / "btcquant.db",
        )
        price, entry_checkpoint = _testnet_preflight(broker)
        store = StateStore(_smoke_database_path())
        orders = OrderExecutionService(store, broker)
        clock = SystemClock()
        runtime = ExternalSettlementRuntime(store, broker, clock)
        flat_state = _state_payload(cash=1000.0, position=None, transition_sequence=0)
        store.save_engine_state("trend", flat_state)
        quantity = _smoke_quantity(broker, price)
        stop_id: str | None = None
        local_stop_id: int | None = None
        close_checkpoint: str | None = None
        position_generation: str | None = None
        next_close_sequence: int | None = None
        opened = False
        entry_state: dict[str, Any] | None = None
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
            _settle_external_market_order(runtime, entry_result, clock)
            entry = entry_result.fill
            if entry.qty <= 0 or entry.broker_order_id is None:
                raise RuntimeError("Entrée testnet non exécutée")
            opened = True
            entry_state = _load_smoke_state(store)
            entry_position = _smoke_position(entry_state)
            entry_qty = float(entry_position["qty"])
            entry_price = float(entry_position["entry_price"])
            position_generation = (
                f"entry={entry_position['entry_time']}|"
                f"initial_qty={float(entry_position['initial_qty']):.17g}"
            )
            close_checkpoint = datetime.now(UTC).isoformat()
            # L'entrée réglée a durablement consommé sa séquence. Cette valeur
            # autorise la toute première clôture, notamment si le stop est
            # rejeté avant qu'une clôture normale ne soit engagée.
            next_close_sequence = _smoke_transition_sequence(entry_state)
            print(f"PASS entrée IOC : {entry.qty:.8f} BTC")

            stop_intent = f"p1-smoke-stop-{uuid.uuid4().hex}"
            local_stop_id = store.begin_order(
                "trend",
                "p1-smoke",
                stop_intent,
                "STOP",
                "SELL",
                entry_qty,
                "p1_smoke_stop",
                reference_price=entry_price * 0.95,
            )
            stop_id = broker.place_stop(
                entry_qty,
                entry_price * 0.95,
                direction=1,
                client_order_id=stop_intent,
            )
            if stop_id is None:
                raise RuntimeError("Stop testnet créé sans identifiant récupérable")
            stop = broker.protective_order_snapshot(stop_id)
            if stop.status != "OPEN" or abs(stop.requested_qty - entry_qty) > 1e-9:
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
            local_stop_id = None
            stop_id = None
            if entry_state is None:
                raise RuntimeError("État d’entrée durable absent avant la clôture")
            if next_close_sequence is None:
                raise RuntimeError("Séquence d’entrée durable absente avant la clôture")
            close_sequence = next_close_sequence
            # Une clôture devient ambiguë dès que sa soumission est engagée.
            # Une erreur avant cette ligne conserve l'autorisation initiale;
            # une erreur après cette ligne impose la réconciliation.
            next_close_sequence = None
            close_state = entry_state
            close_result = orders.submit_market(
                _market_command(
                    state=close_state,
                    side="SELL",
                    qty=entry_qty,
                    reference_price=entry_price,
                    reason="p1_smoke_close",
                    decision_checkpoint=close_checkpoint,
                    transition_type=FinancialTransitionType.EXIT,
                    position_generation=position_generation,
                    transition_sequence=close_sequence,
                    reduce_only=True,
                )
            )
            _settle_external_market_order(runtime, close_result, clock)
            close = close_result.fill
            next_close_sequence = close_result.transition_sequence + 1
            if close.qty <= 0:
                raise RuntimeError("Clôture reduce-only non exécutée")
            print(f"PASS clôture reduce-only : {close.qty:.8f} BTC")
        finally:
            _finalize_smoke_stop(store, broker, local_stop_id, stop_id)
            _cleanup_smoke_position(
                store=store,
                broker=broker,
                orders=orders,
                runtime=runtime,
                clock=clock,
                price=price,
                close_checkpoint=close_checkpoint,
                position_generation=position_generation,
                next_close_sequence=next_close_sequence,
                opened=opened,
            )
        print("═══ PASS : portail Hyperliquid testnet validé et compte remis à plat ═══")

    finally:
        smoke_lock.release()


if __name__ == "__main__":
    main()
