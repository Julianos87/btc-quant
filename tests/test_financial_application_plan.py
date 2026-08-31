from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from btcquant.execution.broker import PaperBroker
from btcquant.execution.errors import FinancialApplicationPlanConflict, MigrationRequiredError
from btcquant.execution.order_service import OrderExecutionService
from btcquant.execution.financial_application_plan import (
    FinancialApplicationPlan,
    canonical_json,
    parse_logical_order_identity,
)
from btcquant.execution.order_state import FinancialTransitionType, LogicalOrderIdentity
from btcquant.execution.state_store import SCHEMA_VERSION, StateStore


def _state(slot: str = "slot", position: dict | None = None) -> dict:
    return {
        "slots": {
            slot: {
                "cash": 1000.0,
                "position": position,
                "stop_order_id": None,
                "stop_order_local_id": None,
                "stop_intent_id": None,
                "stop_transition": None,
                "entry_fee": 0.0,
                "last_bar_ts": None,
                "financial_transition_seq": 0,
            }
        },
        "peak_equity": 1000.0,
        "halted": False,
        "day": None,
        "day_start_equity": 1000.0,
        "daily_lockout": False,
        "reconciliation_required": False,
        "last_funding_ts": None,
        "stop_protection_mode": "SOFTWARE",
    }


def _entry_plan(
    *, slot: str = "slot", stop: float = 90.0, planned: str = "2026-08-31T12:00:00Z"
) -> FinancialApplicationPlan:
    identity = LogicalOrderIdentity(
        "trend", slot, "2026-08-31T11:00:00Z", FinancialTransitionType.ENTER_LONG
    )
    return FinancialApplicationPlan(
        identity=identity,
        side="BUY",
        requested_qty=1.0,
        reference_price=100.0,
        reason="entry",
        reduce_only=False,
        planned_effect_at=planned,
        pre_state_payload=_state(slot),
        protection_mode="SOFTWARE",
        entry_direction=1,
        entry_stop_price=stop,
    )


def test_atomic_reservation_persists_order_plan_checkpoint_and_events(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    plan = _entry_plan()
    reservation = store.reserve_market_order_with_application_plan(plan.identity, plan=plan)
    persisted = store.get_financial_application_plan(reservation.order_id)
    assert reservation.acquired is True
    assert persisted is not None and persisted.plan.plan_key == plan.plan_key
    assert store.load_engine_state("trend") == json.loads(canonical_json(plan.pre_state_payload))
    with sqlite3.connect(store.path) as connection:
        events = [row[0] for row in connection.execute("SELECT event_type FROM events ORDER BY id")]
    assert events == [
        "order_intent_reserved",
        "financial_application_plan_persisted",
        "order_pre_submission_checkpoint",
    ]


def test_same_plan_is_idempotent_and_different_plan_conflicts(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    plan = _entry_plan()
    first = store.reserve_market_order_with_application_plan(plan.identity, plan=plan)
    second = store.reserve_market_order_with_application_plan(plan.identity, plan=plan)
    assert first.order_id == second.order_id and second.acquired is False
    with pytest.raises(FinancialApplicationPlanConflict):
        store.reserve_market_order_with_application_plan(plan.identity, plan=_entry_plan(stop=91.0))


def test_atomic_failure_leaves_no_partial_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = StateStore(tmp_path / "state.db")
    plan = _entry_plan()
    original = store._insert_event

    def fail(
        connection,
        engine,
        event_type,
        payload,
        aggregate_type=None,
        aggregate_id=None,
        correlation_id=None,
    ):
        if event_type == "order_pre_submission_checkpoint":
            raise RuntimeError("injected")
        return original(
            connection, engine, event_type, payload, aggregate_type, aggregate_id, correlation_id
        )

    monkeypatch.setattr(store, "_insert_event", fail)
    with pytest.raises(RuntimeError, match="injected"):
        store.reserve_market_order_with_application_plan(plan.identity, plan=plan)
    assert store.read_orders("trend") == []
    assert store.get_financial_application_plan_by_intent(plan.identity.intent_id) is None
    assert store.load_engine_state("trend") is None


def test_strict_logical_identity_rejects_noncanonical_or_mismatched() -> None:
    plan = _entry_plan()
    assert (
        parse_logical_order_identity(
            plan.identity.logical_key,
            intent_id=plan.identity.intent_id,
            engine="trend",
            slot="slot",
        )
        == plan.identity
    )
    raw = json.loads(plan.identity.logical_key)
    raw["extra"] = True
    with pytest.raises(ValueError, match="LOGICAL_IDENTITY_CONFLICT"):
        parse_logical_order_identity(
            json.dumps(raw), intent_id=plan.identity.intent_id, engine="trend", slot="slot"
        )
    with pytest.raises(ValueError, match="LOGICAL_IDENTITY_CONFLICT"):
        parse_logical_order_identity(
            plan.identity.logical_key, intent_id="wrong", engine="trend", slot="slot"
        )


def test_schema_v8_requires_explicit_migration_and_v9_is_readable(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    legacy = StateStore(database)
    identity = LogicalOrderIdentity(
        "trend", "legacy", "2026-08-30T00:00:00Z", FinancialTransitionType.ENTER_LONG
    )
    legacy_order = legacy.reserve_market_order(
        identity, side="BUY", requested_qty=1.0, reference_price=100.0, reason="legacy"
    )
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE financial_application_plans")
        connection.execute("UPDATE metadata SET value = '8' WHERE key = 'schema_version'")
        connection.commit()
    with pytest.raises(MigrationRequiredError):
        StateStore(database)
    StateStore(database, allow_migration=True)
    assert SCHEMA_VERSION == 9
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[
                0
            ]
            == "9"
        )
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='financial_application_plans'"
        ).fetchone()
    reopened = StateStore(database, read_only=True)
    assert reopened.read_orders("trend")[0]["id"] == legacy_order.order_id
    assert reopened.get_financial_application_plan(legacy_order.order_id) is None


def test_broker_is_not_called_without_a_durable_plan(tmp_path: Path) -> None:
    class CountingBroker(PaperBroker):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def execute_market(self, *args, **kwargs):
            self.calls += 1
            return super().execute_market(*args, **kwargs)

    broker = CountingBroker()
    with pytest.raises(ValueError, match="plan financier durable"):
        OrderExecutionService(StateStore(tmp_path / "state.db"), broker).submit_market(
            engine="trend",
            slot="slot",
            side="BUY",
            qty=1.0,
            reference_price=100.0,
            reason="entry",
            decision_checkpoint="2026-08-31T11:00:00Z",
            transition_type=FinancialTransitionType.ENTER_LONG,
        )
    assert broker.calls == 0


def test_baseexception_rolls_back_the_entire_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class SimulatedPowerLoss(BaseException):
        pass

    store = StateStore(tmp_path / "state.db")
    plan = _entry_plan()
    original = store._insert_event

    def fail(
        connection,
        engine,
        event_type,
        payload,
        aggregate_type=None,
        aggregate_id=None,
        correlation_id=None,
    ):
        if event_type == "financial_application_plan_persisted":
            raise SimulatedPowerLoss()
        return original(
            connection, engine, event_type, payload, aggregate_type, aggregate_id, correlation_id
        )

    monkeypatch.setattr(store, "_insert_event", fail)
    with pytest.raises(SimulatedPowerLoss):
        store.reserve_market_order_with_application_plan(plan.identity, plan=plan)
    assert store.read_orders("trend") == []
    assert store.load_engine_state("trend") is None
