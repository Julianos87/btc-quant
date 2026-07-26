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
