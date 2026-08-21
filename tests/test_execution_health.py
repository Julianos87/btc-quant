"""Métriques, incidents dédupliqués et vieillissement du journal d'exécution."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from btcquant.execution.health import (
    HealthThresholds,
    execution_health,
    sync_execution_incidents,
)
from btcquant.execution.state_store import StateStore


def add_order(
    store: StateStore,
    intent: str,
    *,
    side: str = "BUY",
    requested: float = 1.0,
    reference: float = 100.0,
    status: str = "FILLED",
    filled: float = 1.0,
    price: float | None = 100.05,
) -> int:
    order_id = store.begin_order(
        "trend",
        "strategy",
        intent,
        "MARKET",
        side,
        requested,
        "test",
        reference_price=reference,
    )
    store.complete_order(
        order_id,
        status=status,
        filled_qty=filled,
        price=price,
    )
    return order_id


def test_execution_metrics_cover_fills_rejections_slippage_and_stale_orders(tmp_path):
    store = StateStore(tmp_path / "btcquant.db")
    add_order(store, "filled", price=100.10)
    add_order(
        store,
        "partial",
        side="SELL",
        status="PARTIAL",
        filled=0.5,
        price=99.80,
    )
    add_order(store, "rejected", status="REJECTED", filled=0.0, price=None)
    unbalanced_id = add_order(
        store,
        "unbalanced",
        status="UNBALANCED",
        filled=0.2,
        price=100.0,
    )
    pending_id = store.begin_order(
        "trend",
        "strategy",
        "pending",
        "MARKET",
        "BUY",
        1.0,
        "test",
        reference_price=100.0,
    )
    stop_id = store.begin_order(
        "trend",
        "strategy",
        "protective-stop",
        "STOP",
        "SELL",
        1.0,
        "ratchet",
        reference_price=95.0,
    )
    store.complete_order(stop_id, status="CANCELED", broker_order_id="remote-stop")

    health = execution_health(
        store,
        "trend",
        HealthThresholds(stale_pending_seconds=300),
        now=datetime.now(UTC) + timedelta(minutes=10),
    )

    assert health.orders_analyzed == 3
    assert health.fill_ratio == pytest.approx(0.5)
    assert health.rejection_rate == pytest.approx(1 / 3)
    assert health.partial_rate == pytest.approx(1 / 3)
    assert health.average_slippage_bps == pytest.approx(15.0)
    assert health.p95_slippage_bps == pytest.approx(20.0)
    assert health.unbalanced_order_ids == (unbalanced_id,)
    assert health.stale_pending_order_ids == (pending_id,)


def test_incidents_are_deduplicated_resolved_and_reopened(tmp_path):
    store = StateStore(tmp_path / "btcquant.db")
    order_id = add_order(
        store,
        "unbalanced",
        status="UNBALANCED",
        filled=0.5,
        price=100.0,
    )
    health = execution_health(store, "trend")

    first_notifications = sync_execution_incidents(store, health)
    second_notifications = sync_execution_incidents(store, health)

    assert any(item["kind"] == "unbalanced_orders" for item in first_notifications)
    assert second_notifications == []
    incident = next(
        item for item in store.read_incidents(open_only=True) if item["kind"] == "unbalanced_orders"
    )
    assert incident["occurrences"] == 2

    store.complete_order(order_id, status="RECOVERED_ABORTED")
    healthy = execution_health(store, "trend")
    sync_execution_incidents(
        store,
        healthy,
        HealthThresholds(
            rejection_rate_warning=1.0,
            partial_rate_warning=1.0,
            slippage_bps_warning=10_000.0,
        ),
    )
    assert not any(
        item["kind"] == "unbalanced_orders" for item in store.read_incidents(open_only=True)
    )

    store.complete_order(order_id, status="UNBALANCED", filled_qty=0.5, price=100.0)
    reopened = sync_execution_incidents(store, execution_health(store, "trend"))
    assert any(item["kind"] == "unbalanced_orders" for item in reopened)


def test_nominal_paper_soak_window_remains_stable(tmp_path):
    store = StateStore(tmp_path / "btcquant.db")
    for index in range(250):
        add_order(
            store,
            f"soak-{index}",
            side="BUY" if index % 2 == 0 else "SELL",
            price=100.05 if index % 2 == 0 else 99.95,
        )

    health = execution_health(
        store,
        "trend",
        HealthThresholds(order_window=200),
    )
    notifications = sync_execution_incidents(store, health)

    assert health.orders_analyzed == 200
    assert health.fill_ratio == pytest.approx(1.0)
    assert health.rejection_rate == 0.0
    assert health.partial_rate == 0.0
    assert health.average_slippage_bps == pytest.approx(5.0)
    assert notifications == []
    assert store.read_incidents(open_only=True) == []


def test_position_without_confirmed_stop_is_a_critical_incident(tmp_path):
    store = StateStore(tmp_path / "btcquant.db")
    store.save_engine_state(
        "trend",
        {
            "slots": {
                "trend_ls_55": {
                    "position": {"qty": 0.01},
                    "stop_order_id": None,
                    "stop_transition": None,
                }
            }
        },
    )

    health = execution_health(store, "trend")
    notifications = sync_execution_incidents(store, health)

    assert health.unprotected_slots == ("trend_ls_55",)
    assert any(item["kind"] == "unprotected_position" for item in notifications)
    assert any(
        item["severity"] == "CRITICAL" and item["kind"] == "unprotected_position"
        for item in store.read_incidents(open_only=True)
    )


def test_replacement_keeps_old_stop_protection_but_alerts_pending_transition(tmp_path):
    store = StateStore(tmp_path / "btcquant.db")
    store.save_engine_state(
        "trend",
        {
            "slots": {
                "trend_ls_55": {
                    "position": {"qty": 0.01},
                    "stop_order_id": "old-stop",
                    "stop_transition": {
                        "phase": "PLACING",
                        "previous_stop_id": "old-stop",
                    },
                }
            },
            "stop_protection_mode": "EXCHANGE",
        },
    )

    health = execution_health(store, "trend")
    notifications = sync_execution_incidents(store, health)

    assert health.unprotected_slots == ()
    assert health.stop_transition_slots == ("trend_ls_55",)
    assert any(item["kind"] == "stop_transition_pending" for item in notifications)


# ── échecs répétés de la boucle principale ──────────────────────────────────
# Le catch-all de `run_forever` protège d'une erreur transitoire, mais masquait
# les pannes durables : un checkpoint non sérialisable échouait à chaque tick
# sans jamais produire autre chose qu'une ligne de log. Le watchdog voyait
# l'état périmé 10 min plus tard, sans la cause.


def _idle_runner(tmp_path):
    from btcquant.execution.broker import PaperBroker
    from btcquant.execution.runner import LiveRunner
    from btcquant.risk import RiskConfig

    return LiveRunner(
        [],
        PaperBroker(),
        RiskConfig(initial_capital=10_000.0),
        "binance",
        "BTC/USDT",
        tmp_path / "btcquant.db",
        notifier=lambda _message: True,
    )


def test_isolated_loop_failure_does_not_raise_an_incident(tmp_path):
    from btcquant.execution.runner import LOOP_FAILURES_BEFORE_INCIDENT

    runner = _idle_runner(tmp_path)

    runner._record_loop_failure(RuntimeError("réseau"), LOOP_FAILURES_BEFORE_INCIDENT - 1)

    assert not runner.store.read_incidents(open_only=True)


def test_repeated_loop_failures_open_an_incident_naming_the_cause(tmp_path):
    from btcquant.execution.runner import LOOP_FAILURES_BEFORE_INCIDENT

    messages: list[str] = []
    runner = _idle_runner(tmp_path)
    runner.notifier = messages.append

    runner._record_loop_failure(
        TypeError("checkpoint non sérialisable"), LOOP_FAILURES_BEFORE_INCIDENT
    )

    incidents = runner.store.read_incidents(open_only=True)
    assert [item["fingerprint"] for item in incidents] == ["execution:trend:loop_failure"]
    assert "checkpoint non sérialisable" in incidents[0]["message"]
    assert messages and "checkpoint non sérialisable" in messages[0]


def test_loop_failure_incident_is_notified_once(tmp_path):
    from btcquant.execution.runner import LOOP_FAILURES_BEFORE_INCIDENT

    messages: list[str] = []
    runner = _idle_runner(tmp_path)
    runner.notifier = messages.append

    for attempt in range(LOOP_FAILURES_BEFORE_INCIDENT, LOOP_FAILURES_BEFORE_INCIDENT + 4):
        runner._record_loop_failure(RuntimeError("panne durable"), attempt)

    assert len(messages) == 1
