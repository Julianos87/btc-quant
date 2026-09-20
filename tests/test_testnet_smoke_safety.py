from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from btcquant.execution.errors import EngineInstanceAlreadyRunning
from btcquant.execution.instance_lock import EngineInstanceLock
from btcquant.execution.state_store import StateStore
from scripts import test_testnet


class _RemoteBroker:
    def __init__(self, position: float) -> None:
        self.position = position
        self.exchange = SimpleNamespace(
            urls={"api": {"private": "https://api.hyperliquid-testnet.xyz"}}
        )

    def net_position(self, symbol: str) -> float:
        assert symbol == test_testnet.SYMBOL
        return self.position


def _open_state() -> dict[str, object]:
    return test_testnet._state_payload(
        cash=1000.0,
        position={
            "entry_time": "2026-09-20T00:00:00+00:00",
            "entry_price": 100.0,
            "qty": 0.1,
            "stop_price": 95.0,
            "direction": 1,
            "bars_held": 0,
            "best_close": 100.0,
            "initial_qty": 0.1,
            "last_add_price": 100.0,
            "pyramid_adds": 0,
        },
        transition_sequence=1,
    )


def test_non_flat_preflight_does_not_create_or_mutate_smoke_state(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    configured = state_dir / "btcquant-testnet.db"
    store = StateStore(configured)
    existing = _open_state()
    store.save_engine_state("trend", existing)

    with pytest.raises(RuntimeError, match="compte doit être plat"):
        test_testnet._testnet_preflight(_RemoteBroker(1.0))

    assert store.load_engine_state("trend") == existing
    assert not (state_dir / "testnet-smoke").exists()


def test_smoke_database_is_fresh_and_cannot_alias_configured_testnet(tmp_path: Path) -> None:
    configured = tmp_path / "state" / "btcquant-testnet.db"
    configured_store = StateStore(configured)
    configured_state = test_testnet._state_payload(
        cash=5432.0,
        position=None,
        transition_sequence=7,
    )
    configured_store.save_engine_state("trend", configured_state)

    smoke_path = test_testnet._smoke_database_path(tmp_path)
    assert smoke_path != configured
    assert smoke_path.parent == tmp_path / "state" / "testnet-smoke"

    smoke_store = StateStore(smoke_path)
    smoke_store.save_engine_state(
        "trend",
        test_testnet._state_payload(cash=1000.0, position=None, transition_sequence=0),
    )
    assert configured_store.load_engine_state("trend") == configured_state


def test_cleanup_rejects_remote_flat_but_local_open(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "smoke.db")
    store.save_engine_state("trend", _open_state())

    with pytest.raises(RuntimeError, match="position locale encore ouverte"):
        test_testnet._assert_smoke_cleanup(store, _RemoteBroker(0.0))


def test_cleanup_rejects_unresolved_orders_even_when_both_positions_are_flat(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "smoke.db")
    store.save_engine_state(
        "trend",
        test_testnet._state_payload(cash=1000.0, position=None, transition_sequence=0),
    )
    store.begin_order(
        "trend",
        test_testnet.SMOKE_SLOT,
        "smoke-unresolved",
        "STOP",
        "SELL",
        0.1,
        "cleanup_regression",
    )

    with pytest.raises(RuntimeError, match="ordres locaux non résolus"):
        test_testnet._assert_smoke_cleanup(store, _RemoteBroker(0.0))


def test_cleanup_accepts_remote_and_local_flat_without_unresolved_orders(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "smoke.db")
    store.save_engine_state(
        "trend",
        test_testnet._state_payload(cash=1000.0, position=None, transition_sequence=0),
    )

    test_testnet._assert_smoke_cleanup(store, _RemoteBroker(0.0))


def test_external_smoke_settlement_delegates_to_durable_runtime() -> None:
    calls: list[tuple[int, str]] = []
    runtime = SimpleNamespace(
        reconcile_order=lambda order_id, *, observed_at: calls.append((order_id, observed_at))
    )
    clock = SimpleNamespace(utc_now=lambda: datetime(2026, 9, 20, 12, 0, tzinfo=UTC))
    submitted = SimpleNamespace(is_terminal=True, order_id=42)

    test_testnet._settle_external_market_order(runtime, submitted, clock)

    assert calls == [(42, "2026-09-20T12:00:00+00:00")]


def test_first_cleanup_sequence_is_loaded_from_durable_entry_state(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "smoke.db")
    entry_state = _open_state()
    store.save_engine_state("trend", entry_state)

    loaded = test_testnet._load_smoke_state(store)

    assert test_testnet._smoke_transition_sequence(loaded) == 1


def test_stop_rejection_path_performs_one_reduce_only_cleanup(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "smoke.db")
    store.save_engine_state("trend", _open_state())
    broker = _RemoteBroker(0.1)
    local_stop_id = store.begin_order(
        "trend",
        test_testnet.SMOKE_SLOT,
        "rejected-stop",
        "STOP",
        "SELL",
        0.1,
        "p1_smoke_stop",
    )
    test_testnet._finalize_smoke_stop(store, broker, local_stop_id, None)
    submissions: list[test_testnet.SubmitMarketCommand] = []

    def submit_market(command: test_testnet.SubmitMarketCommand) -> SimpleNamespace:
        submissions.append(command)
        broker.position = 0.0
        store.save_engine_state(
            "trend",
            test_testnet._state_payload(cash=990.0, position=None, transition_sequence=2),
        )
        return SimpleNamespace(
            is_terminal=True,
            order_id=99,
            fill=SimpleNamespace(qty=0.1),
            transition_sequence=1,
        )

    orders = SimpleNamespace(submit_market=submit_market)
    runtime = SimpleNamespace(
        reconcile_order=lambda order_id, *, observed_at: None,
    )
    clock = SimpleNamespace(utc_now=lambda: datetime(2026, 9, 20, 12, 0, tzinfo=UTC))

    test_testnet._cleanup_smoke_position(
        store=store,
        broker=broker,
        orders=orders,
        runtime=runtime,
        clock=clock,
        price=100.0,
        close_checkpoint="2026-09-20T00:01:00+00:00",
        position_generation="entry=2026-09-20T00:00:00+00:00|initial_qty=0.10000000000000001",
        next_close_sequence=1,
        opened=True,
    )

    assert len(submissions) == 1
    assert submissions[0].reduce_only is True
    assert submissions[0].transition_sequence == 1
    assert broker.position == 0.0


def test_ambiguous_stop_without_lookup_proof_stays_unresolved(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "smoke.db")
    store.save_engine_state("trend", _open_state())
    local_stop_id = store.begin_order(
        "trend",
        test_testnet.SMOKE_SLOT,
        "ambiguous-stop",
        "STOP",
        "SELL",
        0.1,
        "p1_smoke_stop",
    )
    lookup_calls: list[str] = []
    broker = SimpleNamespace(
        lookup_order=lambda intent: lookup_calls.append(intent) or None,
    )

    with pytest.raises(RuntimeError, match="lookup absent"):
        test_testnet._finalize_smoke_stop(
            store,
            broker,
            local_stop_id,
            None,
            stop_intent="ambiguous-stop",
            placement_ambiguous=True,
        )

    assert lookup_calls == ["ambiguous-stop"]
    order = store.read_orders("trend")[0]
    assert order["status"] == "PENDING"
    assert order["local_state"] == "PENDING_RECONCILIATION"
    assert order["external_state"] == "UNKNOWN"
    assert order["broker_order_id"] is None
    assert store.unresolved_orders("trend")


def test_ambiguous_stop_is_canceled_only_after_lookup_confirmation(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "smoke.db")
    store.save_engine_state("trend", _open_state())
    local_stop_id = store.begin_order(
        "trend",
        test_testnet.SMOKE_SLOT,
        "ambiguous-stop-cancel",
        "STOP",
        "SELL",
        0.1,
        "p1_smoke_stop",
    )
    snapshots = iter(
        [
            SimpleNamespace(status="OPEN", broker_order_id="remote-stop"),
            SimpleNamespace(status="CANCELED", broker_order_id="remote-stop"),
        ]
    )
    lookups: list[str] = []
    cancellations: list[str] = []
    broker = SimpleNamespace(
        lookup_order=lambda intent: lookups.append(intent) or next(snapshots),
        cancel_stop=lambda order_id: cancellations.append(order_id),
    )

    test_testnet._finalize_smoke_stop(
        store,
        broker,
        local_stop_id,
        None,
        stop_intent="ambiguous-stop-cancel",
        placement_ambiguous=True,
    )

    assert lookups == ["ambiguous-stop-cancel", "ambiguous-stop-cancel"]
    assert cancellations == ["remote-stop"]
    order = store.read_orders("trend")[0]
    assert order["status"] == "CANCELED"
    assert order["external_state"] == "CANCELED"
    assert store.unresolved_orders("trend") == []


def test_smoke_uses_the_engine_testnet_lock_before_any_submission(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    engine_lock = EngineInstanceLock(
        state_dir / test_testnet.TESTNET_ENGINE_DATABASE_NAME,
        "trend",
    )
    engine_lock.acquire()
    smoke_lock = test_testnet._smoke_instance_lock(tmp_path)
    try:
        with pytest.raises(EngineInstanceAlreadyRunning):
            smoke_lock.acquire()
    finally:
        engine_lock.release()
