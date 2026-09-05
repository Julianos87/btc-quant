from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pandas as pd

import pytest

from btcquant.execution.broker import ExternalOrderState, Fill, PaperBroker
from btcquant.execution.errors import (
    FinancialApplicationPlanConflict,
    MigrationRequiredError,
    ReconciliationRequired,
)
from btcquant.execution.order_service import OrderExecutionService, SubmittedOrder
from btcquant.execution.financial_application_plan import (
    FinancialApplicationPlan,
    canonical_json,
    parse_logical_order_identity,
    sha256_json,
)
from btcquant.execution.order_state import FinancialTransitionType, LogicalOrderIdentity
from btcquant.execution.runner import LiveRunner, StrategySlot
from btcquant.risk import RiskConfig
from btcquant.strategies.base import Direction, Position, Strategy
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
    *,
    slot: str = "slot",
    stop: float = 90.0,
    planned: str = "2026-08-31T12:00:00Z",
    transition_sequence: int = 0,
    state_transition_sequence: int | None = None,
    state_protection_mode: str = "SOFTWARE",
    protection_mode: str = "SOFTWARE",
    include_state_transition_sequence: bool = True,
    include_state_protection_mode: bool = True,
) -> FinancialApplicationPlan:
    identity = LogicalOrderIdentity(
        "trend",
        slot,
        "2026-08-31T11:00:00Z",
        FinancialTransitionType.ENTER_LONG,
        transition_sequence=transition_sequence,
    )
    payload = _state(slot)
    payload["stop_protection_mode"] = state_protection_mode
    if state_transition_sequence is not None:
        payload["slots"][slot]["financial_transition_seq"] = state_transition_sequence
    if not include_state_transition_sequence:
        payload["slots"][slot].pop("financial_transition_seq", None)
    if not include_state_protection_mode:
        payload.pop("stop_protection_mode", None)
    return FinancialApplicationPlan(
        identity=identity,
        side="BUY",
        requested_qty=1.0,
        reference_price=100.0,
        reason="entry",
        reduce_only=False,
        planned_effect_at=planned,
        pre_state_payload=payload,
        protection_mode=protection_mode,
        entry_direction=1,
        entry_stop_price=stop,
    )


def _position_plan(
    transition: FinancialTransitionType,
    *,
    direction: int,
    side: str,
    reduce_only: bool,
) -> FinancialApplicationPlan:
    generation = "entry=2026-08-31T12:00:00Z|initial_qty=1"
    identity = LogicalOrderIdentity(
        "trend",
        "slot",
        "2026-09-01T11:00:00Z",
        transition,
        generation,
        0,
    )
    position = {
        "entry_time": "2026-08-31T12:00:00+00:00",
        "entry_price": 100.0,
        "qty": 1.0,
        "stop_price": 90.0,
        "direction": direction,
        "bars_held": 0,
        "best_close": 100.0,
        "initial_qty": 1.0,
        "last_add_price": 100.0,
        "pyramid_adds": 0,
    }
    payload = _state()
    payload["slots"]["slot"]["position"] = position
    return FinancialApplicationPlan(
        identity=identity,
        side=side,
        requested_qty=0.5,
        reference_price=100.0,
        reason="pyramid" if transition == FinancialTransitionType.ADD else "exit",
        reduce_only=reduce_only,
        planned_effect_at="2026-09-01T12:00:00Z",
        pre_state_payload=payload,
        protection_mode="SOFTWARE",
    )


@pytest.mark.parametrize(
    ("transition", "direction", "valid_side", "invalid_side", "reduce_only"),
    [
        (FinancialTransitionType.ADD, 1, "BUY", "SELL", False),
        (FinancialTransitionType.ADD, -1, "SELL", "BUY", False),
        (FinancialTransitionType.EXIT, 1, "SELL", "BUY", True),
        (FinancialTransitionType.EXIT, -1, "BUY", "SELL", True),
    ],
)
def test_position_transition_side_contract(
    transition: FinancialTransitionType,
    direction: int,
    valid_side: str,
    invalid_side: str,
    reduce_only: bool,
) -> None:
    plan = _position_plan(
        transition,
        direction=direction,
        side=valid_side,
        reduce_only=reduce_only,
    )
    assert plan.side == valid_side
    with pytest.raises(ValueError, match="side incompatible"):
        _position_plan(
            transition,
            direction=direction,
            side=invalid_side,
            reduce_only=reduce_only,
        )


def _rewrite_persisted_plan_payload(
    store: StateStore, order_id: int, mutate: Callable[[dict], None]
) -> None:
    with sqlite3.connect(store.path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM financial_application_plans WHERE local_order_id = ?",
            (order_id,),
        ).fetchone()
        assert row is not None
        payload = json.loads(str(row["pre_state_payload"]))
        mutate(payload)
        pre_state_sha256 = sha256_json(payload)
        plan_key = sha256_json(
            {
                "application_version": int(row["application_version"]),
                "logical_order_key": str(row["logical_order_key"]),
                "side": str(row["side"]),
                "requested_qty": float(row["requested_qty"]),
                "reference_price": float(row["reference_price"]),
                "reason": str(row["reason"]),
                "reduce_only": bool(row["reduce_only"]),
                "planned_effect_at": str(row["planned_effect_at"]),
                "entry_direction": row["entry_direction"],
                "entry_stop_price": row["entry_stop_price"],
                "pre_state_sha256": pre_state_sha256,
                "protection_mode": str(row["protection_mode"]),
            }
        )
        connection.execute(
            "UPDATE financial_application_plans SET pre_state_payload=?, "
            "pre_state_sha256=?, plan_key=? WHERE local_order_id=?",
            (canonical_json(payload), pre_state_sha256, plan_key, order_id),
        )
        connection.commit()


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
    assert SCHEMA_VERSION == 13
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[
                0
            ]
            == "13"
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


def test_pre_state_payload_is_deeply_immutable_and_hashes_are_stable() -> None:
    source = _state()
    source["slots"]["slot"]["nested"] = {"values": [1, {"cash": 7.0}]}
    plan = _entry_plan()
    # Rebuild with the richer source so nested containers are covered.
    plan = FinancialApplicationPlan(
        identity=plan.identity,
        side=plan.side,
        requested_qty=plan.requested_qty,
        reference_price=plan.reference_price,
        reason=plan.reason,
        reduce_only=plan.reduce_only,
        planned_effect_at=plan.planned_effect_at,
        pre_state_payload=source,
        protection_mode=plan.protection_mode,
        entry_direction=plan.entry_direction,
        entry_stop_price=plan.entry_stop_price,
    )
    before_state_hash = plan.pre_state_sha256
    before_plan_key = plan.plan_key
    source["slots"]["slot"]["nested"]["values"][1]["cash"] = 99.0
    source["slots"]["slot"]["nested"]["values"].append(3)
    assert plan.pre_state_payload["slots"]["slot"]["nested"]["values"][1]["cash"] == 7.0
    assert isinstance(plan.pre_state_payload["slots"]["slot"]["nested"]["values"], tuple)
    assert plan.pre_state_payload["slots"]["slot"]["nested"]["values"][0] == 1
    with pytest.raises(TypeError):
        plan.pre_state_payload["slots"]["slot"]["nested"]["values"][1]["cash"] = 8.0
    with pytest.raises((AttributeError, TypeError)):
        plan.pre_state_payload["slots"]["slot"]["nested"]["values"].append(4)
    assert plan.pre_state_sha256 == before_state_hash
    assert plan.plan_key == before_plan_key


@pytest.mark.parametrize(
    ("table", "column", "value"),
    [
        ("orders", "intent_id", "corrupted-intent"),
        ("orders", "engine", "corrupted-engine"),
        ("orders", "slot", "corrupted-slot"),
        ("orders", "side", "SELL"),
        ("orders", "requested_qty", 2.0),
        ("orders", "reference_price", 101.0),
        ("orders", "reason", "corrupted-reason"),
        ("orders", "order_type", "LIMIT"),
        ("financial_application_plans", "transition_type", "EXIT"),
        ("financial_application_plans", "decision_checkpoint", "2026-08-31T13:00:00+00:00"),
        ("financial_application_plans", "position_generation", "corrupted-generation"),
        ("financial_application_plans", "transition_sequence", 9),
        ("financial_application_plans", "engine", "corrupted-engine"),
        ("financial_application_plans", "slot", "corrupted-slot"),
    ],
)
def test_plan_read_validates_order_and_denormalized_columns(
    tmp_path: Path, table: str, column: str, value: object
) -> None:
    store = StateStore(tmp_path / "state.db")
    plan = _entry_plan()
    reservation = store.reserve_market_order_with_application_plan(plan.identity, plan=plan)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            f"UPDATE {table} SET {column} = ? WHERE "
            f"{'id' if table == 'orders' else 'local_order_id'} = ?",
            (value, reservation.order_id),
        )
        connection.commit()
    with pytest.raises(FinancialApplicationPlanConflict):
        store.get_financial_application_plan(reservation.order_id)


@pytest.mark.parametrize("column", ["plan_key", "pre_state_sha256"])
def test_plan_read_rejects_corrupted_hash(tmp_path: Path, column: str) -> None:
    store = StateStore(tmp_path / "state.db")
    plan = _entry_plan()
    reservation = store.reserve_market_order_with_application_plan(plan.identity, plan=plan)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            f"UPDATE financial_application_plans SET {column} = ? WHERE local_order_id = ?",
            ("0" * 64, reservation.order_id),
        )
        connection.commit()
    with pytest.raises(FinancialApplicationPlanConflict):
        store.get_financial_application_plan(reservation.order_id)


def test_v9_fresh_schema_failure_rolls_back_table_and_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "fresh.db"
    original = StateStore._ensure_financial_application_plan_schema

    def fail(connection):
        original(connection)
        raise RuntimeError("injected v9 failure")

    monkeypatch.setattr(
        StateStore,
        "_ensure_financial_application_plan_schema",
        staticmethod(fail),
    )
    with pytest.raises(RuntimeError, match="injected v9 failure"):
        StateStore(database)
    with sqlite3.connect(database) as connection:
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE name='financial_application_plans'"
        ).fetchone()
        version = connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()
    assert table is None
    assert version is None or version[0] != "9"


def test_v8_to_v9_schema_failure_rolls_back_new_table_and_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "v8.db"
    StateStore(database)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE financial_application_plans")
        connection.execute("UPDATE metadata SET value='8' WHERE key='schema_version'")
        connection.commit()
    original = StateStore._ensure_financial_application_plan_schema

    def fail(connection):
        original(connection)
        raise RuntimeError("injected v9 migration failure")

    monkeypatch.setattr(
        StateStore,
        "_ensure_financial_application_plan_schema",
        staticmethod(fail),
    )
    with pytest.raises(RuntimeError, match="injected v9 migration failure"):
        StateStore(database, allow_migration=True)
    with sqlite3.connect(database) as connection:
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE name='financial_application_plans'"
        ).fetchone()
        version = connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()
    assert table is None
    assert version[0] == "8"


class _PlanStrategy(Strategy):
    name = "plan-replay"
    timeframe = "1h"

    @staticmethod
    def default_params() -> dict:
        return {}

    def prepare(self, frame):
        return frame

    def entry_signal(self, row) -> int:
        return 0

    def initial_stop(self, row, entry_price: float, direction: int = 1) -> float:
        return entry_price - 10.0


class _PlanVenue:
    payments_per_day = 24
    payments_per_year = 24 * 365

    def last_price(self) -> float:
        return 100.0

    def fetch_ohlcv(self, timeframe: str, limit: int = 1000) -> list[list]:
        return []

    def funding_rate_8h(self) -> float:
        return 0.0

    def funding_history(self, days: float) -> pd.Series:
        return pd.Series(dtype=float)

    def funding_history_since(self, since: pd.Timestamp) -> pd.Series:
        return pd.Series(dtype=float)


class _AdvancingClock:
    def __init__(self) -> None:
        self.now = pd.Timestamp("2026-08-31T12:00:00Z")

    def utc_now(self) -> pd.Timestamp:
        return self.now

    def time(self) -> float:
        return self.now.timestamp()

    def monotonic(self) -> float:
        return self.now.timestamp()


class _FailOncePaperBroker(PaperBroker):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0
        self.client_order_ids: list[str | None] = []

    def execute_market(self, side, qty, ref_price, **kwargs):
        self.calls += 1
        self.client_order_ids.append(kwargs.get("client_order_id"))
        if self.calls == 1:
            raise RuntimeError("local paper failure")
        return super().execute_market(side, qty, ref_price, **kwargs)


def _runner_for_plan_replay(tmp_path: Path):
    clock = _AdvancingClock()
    broker = _FailOncePaperBroker()
    slot = StrategySlot(_PlanStrategy(), 1.0, 1_000.0)
    runner = LiveRunner(
        [slot],
        broker,
        RiskConfig(initial_capital=1_000.0),
        "offline",
        "BTC/USDT",
        tmp_path / "state.json",
        venue=_PlanVenue(),
        clock=clock,
    )
    return runner, slot, broker, clock


def test_runner_pyramid_builds_a_long_add_plan_with_buy_side(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, slot, _broker, _clock = _runner_for_plan_replay(tmp_path)
    slot.position = Position(
        entry_time=pd.Timestamp("2026-08-31T10:00:00Z"),
        entry_price=100.0,
        qty=1.0,
        stop_price=90.0,
        direction=Direction.LONG,
        bars_held=0,
        best_close=100.0,
        initial_qty=1.0,
        last_add_price=100.0,
        pyramid_adds=0,
    )
    captured: dict[str, object] = {}

    def submit(**kwargs):
        plan = kwargs["application_plan"]
        captured["plan"] = plan
        return SubmittedOrder(
            fill=Fill(price=104.0, qty=0.1, fee=0.0, broker_order_id="paper-1"),
            order_id=1,
            intent_id=plan.identity.intent_id,
            logical_order_key=plan.identity.logical_key,
            status="FILLED",
            external_state=ExternalOrderState.FILLED,
            remaining_qty=0.0,
            is_terminal=True,
            transition_sequence=plan.identity.transition_sequence,
            application_plan=plan,
        )

    monkeypatch.setattr(runner.order_service, "submit_market", submit)
    monkeypatch.setattr(runner, "_complete_market_order_and_checkpoint", lambda *_a, **_k: None)
    monkeypatch.setattr(runner, "_reconcile_paper_submission", lambda *_a, **_k: None)
    runner._pyramid_position(
        slot,
        pd.Series({"volume": 1_000.0, "_rvol": float("nan")}),
        100.0,
        0.1,
        decision_checkpoint="2026-08-31T12:00:00Z",
    )
    plan = captured["plan"]
    assert isinstance(plan, FinancialApplicationPlan)
    assert plan.identity.transition_type == FinancialTransitionType.ADD
    assert plan.side == "BUY"


def test_runner_reuses_existing_plan_when_paper_reclaim_clock_advances(tmp_path: Path) -> None:
    runner, slot, broker, clock = _runner_for_plan_replay(tmp_path)
    arguments = dict(
        side="BUY",
        qty=1.0,
        ref_price=100.0,
        reason="entry",
        decision_checkpoint="2026-08-31T11:00:00Z",
        transition_type=FinancialTransitionType.ENTER_LONG,
        position_generation=None,
        entry_direction=1,
        entry_stop_price=90.0,
    )
    with pytest.raises(RuntimeError, match="local paper failure"):
        runner._execute_market_order(slot, available_volume=None, **arguments)
    first = runner.store.get_financial_application_plan_by_intent(
        LogicalOrderIdentity(
            "trend",
            slot.strategy.name,
            arguments["decision_checkpoint"],
            FinancialTransitionType.ENTER_LONG,
        ).intent_id
    )
    assert first is not None
    clock.now = pd.Timestamp("2026-08-31T13:00:00Z")
    submitted = runner._execute_market_order(slot, available_volume=None, **arguments)
    second = runner.store.get_financial_application_plan_by_intent(first.intent_id)
    assert second is not None
    assert submitted.order_id == first.local_order_id
    assert second.plan.plan_key == first.plan.plan_key
    assert second.plan.planned_effect_at == first.plan.planned_effect_at
    assert broker.calls == 2
    assert broker.client_order_ids == [first.intent_id, first.intent_id]


def test_runner_rejects_replay_when_current_state_differs_from_plan(
    tmp_path: Path,
) -> None:
    runner, slot, broker, clock = _runner_for_plan_replay(tmp_path)
    arguments = dict(
        side="BUY",
        qty=1.0,
        ref_price=100.0,
        reason="entry",
        decision_checkpoint="2026-08-31T11:00:00Z",
        transition_type=FinancialTransitionType.ENTER_LONG,
        position_generation=None,
        entry_direction=1,
        entry_stop_price=90.0,
    )
    with pytest.raises(RuntimeError, match="local paper failure"):
        runner._execute_market_order(slot, available_volume=None, **arguments)
    slot.cash += 1.0
    clock.now = pd.Timestamp("2026-08-31T13:00:00Z")
    with pytest.raises(ReconciliationRequired, match="état courant divergent"):
        runner._execute_market_order(slot, available_volume=None, **arguments)
    assert broker.calls == 1


def test_legacy_order_without_plan_remains_fail_closed(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    plan = _entry_plan()
    legacy = store.reserve_market_order(
        plan.identity,
        side=plan.side,
        requested_qty=plan.requested_qty,
        reference_price=plan.reference_price,
        reason=plan.reason,
    )

    class CountingPaperBroker(PaperBroker):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def execute_market(self, *args, **kwargs):
            self.calls += 1
            return super().execute_market(*args, **kwargs)

    broker = CountingPaperBroker()
    with pytest.raises(
        FinancialApplicationPlanConflict, match="LEGACY_APPLICATION_CONTEXT_INCOMPLETE"
    ):
        OrderExecutionService(store, broker).submit_market(
            engine="trend",
            slot=plan.identity.slot,
            side=plan.side,
            qty=plan.requested_qty,
            reference_price=plan.reference_price,
            reason=plan.reason,
            decision_checkpoint=plan.identity.decision_checkpoint,
            transition_type=plan.identity.transition_type,
            position_generation=plan.identity.position_generation,
            transition_sequence=plan.identity.transition_sequence,
            reduce_only=plan.reduce_only,
            application_plan=plan,
        )
    assert legacy.acquired is True
    assert broker.calls == 0


@pytest.mark.parametrize(
    ("state_sequence", "include_sequence"),
    [(2, True), (None, False)],
)
def test_modern_plan_requires_matching_explicit_transition_sequence(
    state_sequence: int | None, include_sequence: bool
) -> None:
    with pytest.raises(ValueError, match="FINANCIAL_TRANSITION_SEQUENCE_CONFLICT"):
        _entry_plan(
            transition_sequence=3,
            state_transition_sequence=state_sequence,
            include_state_transition_sequence=include_sequence,
        )


def test_modern_plan_accepts_matching_explicit_transition_sequence() -> None:
    plan = _entry_plan(transition_sequence=3, state_transition_sequence=3)
    assert plan.identity.transition_sequence == 3
    assert plan.pre_state_payload["slots"]["slot"]["financial_transition_seq"] == 3


@pytest.mark.parametrize(
    ("plan_mode", "state_mode"),
    [("SOFTWARE", "EXCHANGE"), ("EXCHANGE", "SOFTWARE")],
)
def test_modern_plan_requires_matching_protection_mode(plan_mode: str, state_mode: str) -> None:
    with pytest.raises(ValueError, match="PROTECTION_MODE_CONFLICT"):
        _entry_plan(protection_mode=plan_mode, state_protection_mode=state_mode)


def test_modern_plan_requires_explicit_protection_mode() -> None:
    with pytest.raises(ValueError, match="PROTECTION_MODE_CONFLICT"):
        _entry_plan(include_state_protection_mode=False)


def test_modern_plan_accepts_matching_protection_mode() -> None:
    plan = _entry_plan(protection_mode="EXCHANGE", state_protection_mode="EXCHANGE")
    assert plan.protection_mode == "EXCHANGE"
    assert plan.pre_state_payload["stop_protection_mode"] == "EXCHANGE"


def test_plan_read_rejects_semantically_corrupted_transition_sequence_even_with_new_hashes(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state.db")
    plan = _entry_plan(transition_sequence=3, state_transition_sequence=3)
    reservation = store.reserve_market_order_with_application_plan(plan.identity, plan=plan)

    def corrupt_sequence(payload: dict) -> None:
        payload["slots"]["slot"]["financial_transition_seq"] = 2

    _rewrite_persisted_plan_payload(store, reservation.order_id, corrupt_sequence)
    with pytest.raises(
        FinancialApplicationPlanConflict, match="FINANCIAL_TRANSITION_SEQUENCE_CONFLICT"
    ):
        store.get_financial_application_plan(reservation.order_id)


def test_plan_read_rejects_semantically_corrupted_protection_mode_even_with_new_hashes(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state.db")
    plan = _entry_plan()
    reservation = store.reserve_market_order_with_application_plan(plan.identity, plan=plan)

    def corrupt_mode(payload: dict) -> None:
        payload["stop_protection_mode"] = "EXCHANGE"

    _rewrite_persisted_plan_payload(store, reservation.order_id, corrupt_mode)
    with pytest.raises(FinancialApplicationPlanConflict, match="PROTECTION_MODE_CONFLICT"):
        store.get_financial_application_plan(reservation.order_id)


def test_legacy_state_validation_keeps_plan_fields_optional() -> None:
    from btcquant.execution.state_contract import validate_trend_state

    payload = _state()
    payload["slots"]["slot"].pop("financial_transition_seq")
    payload.pop("stop_protection_mode")
    assert validate_trend_state(payload)["slots"]["slot"]["cash"] == 1000.0
