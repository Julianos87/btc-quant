"""Characterization and architecture tests for the order persistence boundary."""

from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from btcquant.execution.order_repository import OrderRepository
from btcquant.execution.order_state import (
    ExternalOrderState,
    FinancialTransitionType,
    LogicalOrderIdentity,
)
from btcquant.execution.state_store import StateStore


def _identity(sequence: int = 0) -> LogicalOrderIdentity:
    return LogicalOrderIdentity(
        engine="trend",
        slot="repo-test",
        decision_checkpoint="2026-09-14T10:00:00+00:00",
        transition_type=FinancialTransitionType.ENTER_LONG,
        transition_sequence=sequence,
    )


def test_order_repository_isolated_from_external_and_business_services():
    path = Path(__file__).parents[1] / "src/btcquant/execution/order_repository.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    imported.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    forbidden = {
        "ccxt",
        "btcquant.broker",
        "btcquant.execution.ccxt_broker",
        "btcquant.execution.strategy",
        "btcquant.execution.financial_fill_application",
        "btcquant.execution.financial_order_settlement",
        "btcquant.execution.reconciliation_coordinator",
        "btcquant.notify",
    }
    assert imported.isdisjoint(forbidden)
    assert not any(
        isinstance(node, ast.ImportFrom) and node.module == "btcquant.execution.state_store"
        for node in ast.walk(tree)
    )


def test_intent_reservation_is_idempotent_and_uses_repository(tmp_path):
    store = StateStore(tmp_path / "state.db")
    first = store.reserve_market_order(
        _identity(), side="BUY", requested_qty=1.0, reason="entry", reference_price=100.0
    )
    second = store.reserve_market_order(
        _identity(), side="BUY", requested_qty=1.0, reason="entry", reference_price=100.0
    )

    assert isinstance(store._order_repository, OrderRepository)
    assert first.acquired is True
    assert second.acquired is False
    assert second.order_id == first.order_id
    assert len(store.read_orders("trend")) == 1


def test_ambiguous_submission_is_durable_and_not_retryable(tmp_path):
    store = StateStore(tmp_path / "state.db")
    reservation = store.reserve_market_order(
        _identity(), side="BUY", requested_qty=1.0, reason="entry", reference_price=100.0
    )
    store.mark_order_submitting(reservation.order_id)
    store.record_submission_error(
        reservation.order_id, error="timeout after request", ambiguous=True
    )

    order = store.read_order_by_intent(reservation.intent_id)
    assert order is not None
    assert order["status"] == "PENDING"
    assert order["local_state"] == "PENDING_RECONCILIATION"
    assert order["external_state"] == ExternalOrderState.UNKNOWN.value
    assert store.unresolved_orders("trend")


def test_concurrent_same_intent_creates_one_row(tmp_path):
    database = tmp_path / "state.db"
    StateStore(database)

    def reserve() -> tuple[int, bool]:
        store = StateStore(database, initialize=False)
        result = store.reserve_market_order(
            _identity(), side="BUY", requested_qty=1.0, reason="entry", reference_price=100.0
        )
        return result.order_id, result.acquired

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: reserve(), range(2)))

    assert sorted(order_id for order_id, _ in results) == [1, 1]
    assert sorted(acquired for _, acquired in results) == [False, True]
    assert len(StateStore(database).read_orders("trend")) == 1


@pytest.mark.parametrize("external_state", [ExternalOrderState.FILLED, ExternalOrderState.REJECTED])
def test_terminalization_preserves_remaining_quantity_contract(tmp_path, external_state):
    store = StateStore(tmp_path / "state.db")
    reservation = store.reserve_market_order(
        _identity(), side="BUY", requested_qty=1.0, reason="entry", reference_price=100.0
    )
    store.mark_order_submitting(reservation.order_id)
    store.record_order_observation(
        reservation.order_id,
        external_state=external_state,
        filled_qty=1.0 if external_state == ExternalOrderState.FILLED else 0.0,
        remaining_qty=0.0 if external_state == ExternalOrderState.FILLED else 1.0,
        price=100.0,
        fee=0.1,
        broker_order_id="remote-1",
    )
    store.complete_order(
        reservation.order_id,
        status="FILLED" if external_state == ExternalOrderState.FILLED else "REJECTED",
        filled_qty=1.0 if external_state == ExternalOrderState.FILLED else 0.0,
        remaining_qty=0.0 if external_state == ExternalOrderState.FILLED else 1.0,
        price=100.0,
        fee=0.1,
        broker_order_id="remote-1",
        external_state=external_state,
    )

    order = store.read_orders("trend")[0]
    assert order["local_state"] == "TERMINAL"
    assert order["remaining_qty"] == pytest.approx(
        0.0 if external_state == ExternalOrderState.FILLED else 1.0
    )
