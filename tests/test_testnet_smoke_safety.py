from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

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
