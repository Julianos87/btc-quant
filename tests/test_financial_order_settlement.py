from __future__ import annotations

import sqlite3

import pytest

from btcquant.execution.errors import MigrationRequiredError
from btcquant.execution.financial_application_plan import (
    FinancialApplicationPlan,
    PersistedFinancialApplicationPlan,
)
from btcquant.execution.financial_order_settlement import (
    ExternalOrderSettlement,
    ExternalSettlementFillRow,
    FinancialSettlementError,
    SettlementCompletenessProof,
    calculate_financial_order_settlement,
)
from btcquant.execution.order_state import FinancialTransitionType, LogicalOrderIdentity
from btcquant.execution.state_store import SCHEMA_VERSION, StateStore


def _state(position: dict | None = None, entry_fee: float = 0.0) -> dict:
    return {
        "slots": {
            "slot": {
                "cash": 1000.0,
                "position": position,
                "stop_order_id": None,
                "stop_order_local_id": None,
                "stop_intent_id": None,
                "stop_transition": None,
                "entry_fee": entry_fee,
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


def _position(direction: int = 1, qty: float = 1.0) -> dict:
    return {
        "entry_time": "2026-09-05T11:00:00+00:00",
        "entry_price": 100.0,
        "qty": qty,
        "stop_price": 90.0 if direction == 1 else 110.0,
        "direction": direction,
        "bars_held": 2,
        "best_close": 105.0,
        "initial_qty": 1.0,
        "last_add_price": 100.0,
        "pyramid_adds": 0,
    }


def _plan(
    transition: FinancialTransitionType,
    *,
    side: str,
    position: dict | None = None,
    direction: int | None = None,
    requested_qty: float = 1.0,
    reason: str = "entry",
    reduce_only: bool = False,
    entry_fee: float = 0.0,
) -> PersistedFinancialApplicationPlan:
    generation = None if position is None else "entry=2026-09-05T11:00:00+00:00|initial_qty=1"
    identity = LogicalOrderIdentity(
        "trend", "slot", "2026-09-05T12:00:00+00:00", transition, generation, 0
    )
    plan = FinancialApplicationPlan(
        identity=identity,
        side=side,
        requested_qty=requested_qty,
        reference_price=100.0,
        reason=reason,
        reduce_only=reduce_only,
        planned_effect_at="2026-09-05T12:00:00+00:00",
        pre_state_payload=_state(position, entry_fee),
        protection_mode="SOFTWARE",
        entry_direction=direction
        if transition in {FinancialTransitionType.ENTER_LONG, FinancialTransitionType.ENTER_SHORT}
        else None,
        entry_stop_price=90.0
        if transition == FinancialTransitionType.ENTER_LONG
        else 110.0
        if transition == FinancialTransitionType.ENTER_SHORT
        else None,
    )
    return PersistedFinancialApplicationPlan(
        local_order_id=7,
        intent_id=identity.intent_id,
        plan=plan,
        created_at="2026-09-05T12:00:00+00:00",
    )


def _fill(
    *,
    side: str = "BUY",
    quantity: float = 1.0,
    price: float = 100.0,
    fee: float = 0.01,
    at: str = "2026-09-05T12:01:00Z",
    raw_hash: str = "a" * 64,
) -> ExternalSettlementFillRow:
    return ExternalSettlementFillRow(
        external_order_id="oid-7",
        account_scope="acct-testnet",
        instrument="BTC/USDC:USDC",
        side=side,
        quantity=quantity,
        price=price,
        fee=fee,
        fee_asset="USDC",
        venue_event_at=at,
        raw_payload_hash=raw_hash,
        client_order_id="cloid-7",
        reported_trade_id_candidate="123",
    )


def _settlement(
    plan: PersistedFinancialApplicationPlan,
    fills: tuple[ExternalSettlementFillRow, ...],
    *,
    response_count: int | None = None,
    retention_oldest: str = "2026-09-05T12:00:00Z",
) -> ExternalOrderSettlement:
    count = len(fills) if response_count is None else response_count
    proof = SettlementCompletenessProof(
        local_order_id=plan.local_order_id,
        intent_id=plan.intent_id,
        venue="hyperliquid",
        environment="testnet",
        account_scope="acct-testnet",
        instrument="BTC/USDC:USDC",
        side=plan.plan.side,
        client_order_id="cloid-7",
        external_order_id="oid-7",
        terminal_status="FILLED",
        terminal_status_event_at="2026-09-05T12:02:00Z",
        window_start="2026-09-05T12:00:00Z",
        window_end="2026-09-05T12:03:00Z",
        response_count=count,
        aggregate_by_time=False,
        malformed_entry_count=0,
        retention_response_count=1,
        retention_oldest_event_at=retention_oldest,
    )
    return ExternalOrderSettlement(
        local_order_id=plan.local_order_id,
        intent_id=plan.intent_id,
        venue="hyperliquid",
        environment="testnet",
        account_scope="acct-testnet",
        instrument="BTC/USDC:USDC",
        side=plan.plan.side,
        client_order_id="cloid-7",
        external_order_id="oid-7",
        terminal_status="FILLED",
        terminal_status_event_at="2026-09-05T12:02:00Z",
        completeness=proof,
        fills=fills,
    )


def test_incomplete_retention_witness_blocks_financial_settlement() -> None:
    plan = _plan(FinancialTransitionType.ENTER_LONG, side="BUY", direction=1)
    settlement = _settlement(plan, (_fill(),), retention_oldest="2026-09-05T12:01:00Z")
    assert not settlement.completeness.is_complete
    with pytest.raises(FinancialSettlementError, match="RETENTION_COVERAGE"):
        calculate_financial_order_settlement(settlement, plan)


def test_response_limit_blocks_completeness() -> None:
    plan = _plan(FinancialTransitionType.ENTER_LONG, side="BUY", direction=1)
    settlement = _settlement(plan, (_fill(),), response_count=2000)
    assert settlement.completeness.response_limit_reached
    assert not settlement.completeness.is_complete


def test_fill_multiset_and_settlement_key_are_permutation_stable() -> None:
    plan = _plan(FinancialTransitionType.ENTER_LONG, side="BUY", direction=1)
    first = _fill(raw_hash="a" * 64)
    second = _fill(at="2026-09-05T12:01:01Z", raw_hash="b" * 64)
    left = _settlement(plan, (first, second))
    right = _settlement(plan, (second, first))
    assert left.raw_fill_count == 2
    assert left.canonical_fill_multiset == (first, second)
    assert left.fill_multiset_sha256 == right.fill_multiset_sha256
    assert left.settlement_key == right.settlement_key


def test_enter_recomposes_vwap_and_signed_fee() -> None:
    plan = _plan(FinancialTransitionType.ENTER_LONG, side="BUY", direction=1)
    first = _fill(quantity=0.4, price=100.0, fee=-0.01)

    second = _fill(
        quantity=0.6, price=110.0, fee=0.02, at="2026-09-05T12:02:00Z", raw_hash="b" * 64
    )
    result = calculate_financial_order_settlement(_settlement(plan, (first, second)), plan)
    position = result.state_after_payload["slots"]["slot"]["position"]
    assert result.quantity == pytest.approx(1.0)
    assert result.total_fee == pytest.approx(0.01)
    assert position["entry_price"] == pytest.approx(106.0)
    assert position["entry_time"] == "2026-09-05T12:01:00+00:00"
    assert result.cash_delta == pytest.approx(-0.01)


@pytest.mark.parametrize(
    ("transition", "direction", "side", "reason", "reduce_only"),
    [
        (FinancialTransitionType.ADD, 1, "BUY", "pyramid", False),
        (FinancialTransitionType.ADD, -1, "SELL", "pyramid", False),
        (FinancialTransitionType.EXIT, 1, "SELL", "exit", True),
        (FinancialTransitionType.EXIT, -1, "BUY", "exit", True),
    ],
)
def test_position_settlement_supports_add_and_exit_sides(
    transition: FinancialTransitionType,
    direction: int,
    side: str,
    reason: str,
    reduce_only: bool,
) -> None:
    plan = _plan(
        transition,
        side=side,
        direction=direction,
        position=_position(direction, 1.0),
        requested_qty=0.5,
        reason=reason,
        reduce_only=reduce_only,
    )
    row = _fill(side=side, quantity=0.5, price=110.0, fee=-0.01)
    result = calculate_financial_order_settlement(_settlement(plan, (row,)), plan)
    assert result.quantity == pytest.approx(0.5)


def test_exit_original_entry_fee_share_and_replay_are_order_invariant() -> None:
    plan = _plan(
        FinancialTransitionType.EXIT,
        side="SELL",
        direction=1,
        position=_position(),
        requested_qty=1.0,
        reason="exit",
        reduce_only=True,
        entry_fee=0.2,
    )
    first = _fill(side="SELL", quantity=0.4, price=110.0, fee=0.01, raw_hash="a" * 64)
    second = _fill(
        side="SELL",
        quantity=0.6,
        price=90.0,
        fee=-0.02,
        at="2026-09-05T12:02:00Z",
        raw_hash="b" * 64,
    )
    one = calculate_financial_order_settlement(_settlement(plan, (first, second)), plan)
    two = calculate_financial_order_settlement(_settlement(plan, (second, first)), plan)
    assert one.state_after_sha256 == two.state_after_sha256
    assert one.trade_payload == two.trade_payload
    assert one.trade_payload["pnl"] == pytest.approx(-2.19)


def _seed_order_for_settlement(store: StateStore, settlement: ExternalOrderSettlement) -> None:
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """
            INSERT INTO orders(
                id, engine, slot, intent_id, logical_order_key, order_type, side,
                requested_qty, reference_price, status, reason, created_at, updated_at
            ) VALUES(?, 'trend', 'slot', ?, 'settlement-test', 'MARKET', ?, ?,
                      100.0, 'PENDING', 'entry', '2026-09-05T12:00:00+00:00',
                      '2026-09-05T12:00:00+00:00')
            """,
            (
                settlement.local_order_id,
                settlement.intent_id,
                settlement.side,
                1.0,
            ),
        )


def test_v12_persists_terminal_settlement_and_journals_each_invocation(
    tmp_path,
) -> None:
    store = StateStore(tmp_path / "state.db")
    settlement = _settlement(
        _plan(FinancialTransitionType.ENTER_LONG, side="BUY", direction=1), (_fill(),)
    )
    _seed_order_for_settlement(store, settlement)

    first, first_created = store.persist_external_order_settlement(
        settlement,
        engine="trend",
        observed_at="2026-09-05T12:03:00Z",
    )
    second, second_created = store.persist_external_order_settlement(
        settlement,
        engine="trend",
        observed_at="2026-09-05T12:04:00+00:00",
    )

    assert SCHEMA_VERSION == 14
    assert first == settlement
    assert second == settlement
    assert first_created is True
    assert second_created is False
    with sqlite3.connect(store.path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM external_order_settlements").fetchone()[0] == 1
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM events WHERE event_type = "
                "'external_order_settlement_persisted'"
            ).fetchone()[0]
            == 2
        )
    assert store.get_external_order_settlements(7) == [settlement]


def test_v12_settlement_and_event_rollback_together(tmp_path, monkeypatch) -> None:
    store = StateStore(tmp_path / "state.db")
    settlement = _settlement(
        _plan(FinancialTransitionType.ENTER_LONG, side="BUY", direction=1), (_fill(),)
    )
    _seed_order_for_settlement(store, settlement)

    class SimulatedPowerLoss(BaseException):
        pass

    def fail_after_settlement(*args, **kwargs):
        raise SimulatedPowerLoss()

    monkeypatch.setattr(store, "_insert_event", fail_after_settlement)
    with pytest.raises(SimulatedPowerLoss):
        store.persist_external_order_settlement(settlement, engine="trend")

    with sqlite3.connect(store.path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM external_order_settlements").fetchone()[0] == 0
        )
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_v11_to_v12_migration_is_explicit_and_preserves_old_tables(tmp_path) -> None:
    path = tmp_path / "state.db"
    StateStore(path)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE external_order_settlements")
        connection.execute("UPDATE metadata SET value='11' WHERE key='schema_version'")
    with pytest.raises(MigrationRequiredError):
        StateStore(path)
    migrated = StateStore(path, allow_migration=True)
    assert migrated.get_external_order_settlements(7) == []
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[
                0
            ]
            == "14"
        )
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='external_order_settlements'"
            ).fetchone()
            is not None
        )


def _rewrite_settlement_tables_to_v13(path) -> None:
    """Build an actual v13 settlement schema around existing rows."""
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("DROP INDEX IF EXISTS idx_financial_settlement_applications_order_id")
        connection.execute("DROP INDEX IF EXISTS idx_external_order_settlements_order_id")
        connection.execute(
            "ALTER TABLE financial_settlement_applications "
            "RENAME TO financial_settlement_applications_v13_source"
        )
        connection.execute(
            "ALTER TABLE external_order_settlements RENAME TO external_order_settlements_v13_source"
        )
        connection.execute(
            """
            CREATE TABLE external_order_settlements (
                settlement_key TEXT PRIMARY KEY,
                settlement_version INTEGER NOT NULL CHECK(settlement_version = 1),
                local_order_id INTEGER NOT NULL,
                intent_id TEXT NOT NULL,
                venue TEXT NOT NULL,
                environment TEXT NOT NULL,
                account_scope TEXT NOT NULL,
                instrument TEXT NOT NULL,
                side TEXT NOT NULL CHECK(side IN ('BUY', 'SELL')),
                client_order_id TEXT NOT NULL,
                external_order_id TEXT NOT NULL,
                terminal_status TEXT NOT NULL CHECK(terminal_status IN (
                    'FILLED', 'PARTIAL_TERMINAL', 'CANCELED', 'REJECTED', 'EXPIRED'
                )),
                terminal_status_event_at TEXT NOT NULL,
                completeness_version INTEGER NOT NULL CHECK(completeness_version = 1),
                completeness_payload TEXT NOT NULL CHECK(json_valid(completeness_payload)),
                raw_fill_count INTEGER NOT NULL CHECK(raw_fill_count >= 0),
                fill_multiset_sha256 TEXT NOT NULL,
                settlement_payload TEXT NOT NULL CHECK(json_valid(settlement_payload)),
                observed_at TEXT NOT NULL,
                persisted_at TEXT NOT NULL,
                FOREIGN KEY(local_order_id) REFERENCES orders(id)
            )
            """
        )
        settlement_columns = (
            "settlement_key",
            "settlement_version",
            "local_order_id",
            "intent_id",
            "venue",
            "environment",
            "account_scope",
            "instrument",
            "side",
            "client_order_id",
            "external_order_id",
            "terminal_status",
            "terminal_status_event_at",
            "completeness_version",
            "completeness_payload",
            "raw_fill_count",
            "fill_multiset_sha256",
            "settlement_payload",
            "observed_at",
            "persisted_at",
        )
        columns_sql = ", ".join(settlement_columns)
        connection.execute(
            f"INSERT INTO external_order_settlements({columns_sql}) "
            f"SELECT {columns_sql} FROM external_order_settlements_v13_source"
        )
        connection.execute(
            """
            CREATE INDEX idx_external_order_settlements_order_id
                ON external_order_settlements(local_order_id, persisted_at, settlement_key)
            """
        )
        connection.execute(
            """
            CREATE TABLE financial_settlement_applications (
                application_key TEXT PRIMARY KEY,
                application_version INTEGER NOT NULL CHECK(application_version = 1),
                settlement_key TEXT NOT NULL,
                local_order_id INTEGER NOT NULL,
                intent_id TEXT NOT NULL,
                plan_key TEXT NOT NULL,
                state_before_sha256 TEXT NOT NULL,
                state_after_sha256 TEXT NOT NULL,
                result_payload TEXT NOT NULL CHECK(json_valid(result_payload)),
                result_sha256 TEXT NOT NULL,
                applied_at TEXT NOT NULL,
                UNIQUE(local_order_id, plan_key, application_version),
                UNIQUE(local_order_id, settlement_key, application_version),
                FOREIGN KEY(local_order_id) REFERENCES orders(id),
                FOREIGN KEY(settlement_key) REFERENCES external_order_settlements(settlement_key),
                FOREIGN KEY(plan_key) REFERENCES financial_application_plans(plan_key)
            )
            """
        )
        child_columns = (
            "application_key",
            "application_version",
            "settlement_key",
            "local_order_id",
            "intent_id",
            "plan_key",
            "state_before_sha256",
            "state_after_sha256",
            "result_payload",
            "result_sha256",
            "applied_at",
        )
        child_columns_sql = ", ".join(child_columns)
        connection.execute(
            f"INSERT INTO financial_settlement_applications({child_columns_sql}) "
            f"SELECT {child_columns_sql} FROM financial_settlement_applications_v13_source"
        )
        connection.execute(
            """
            CREATE INDEX idx_financial_settlement_applications_order_id
                ON financial_settlement_applications(local_order_id, applied_at, application_key)
            """
        )
        connection.execute("DROP TABLE financial_settlement_applications_v13_source")
        connection.execute("DROP TABLE external_order_settlements_v13_source")
        connection.execute("UPDATE metadata SET value='13' WHERE key='schema_version'")
        connection.commit()


def test_v13_to_v14_migration_preserves_real_settlement_and_application_rows(tmp_path) -> None:
    path = tmp_path / "state.db"
    store, plan, settlement = _prepare_settlement_application_store(tmp_path)
    store.apply_external_settlement_atomically(
        local_order_id=plan.local_order_id,
        settlement_key=settlement.settlement_key,
    )
    _rewrite_settlement_tables_to_v13(path)

    with pytest.raises(MigrationRequiredError):
        StateStore(path)
    migrated = StateStore(path, allow_migration=True)
    assert migrated.get_external_order_settlements(plan.local_order_id) == [settlement]
    applications = migrated.get_financial_settlement_applications(plan.local_order_id)
    assert len(applications) == 1
    assert applications[0].settlement_key == settlement.settlement_key
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[
                0
            ]
            == "14"
        )

        ddl = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='external_order_settlements'"
        ).fetchone()[0]
        assert "completeness_version IN (1, 2)" in ddl
        assert (
            connection.execute("SELECT COUNT(*) FROM external_order_settlements").fetchone()[0] == 1
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM financial_settlement_applications").fetchone()[
                0
            ]
            == 1
        )


def test_v13_to_v14_migration_baseexception_rolls_back_real_tables(tmp_path, monkeypatch) -> None:
    path = tmp_path / "state.db"
    store, plan, settlement = _prepare_settlement_application_store(tmp_path)
    store.apply_external_settlement_atomically(
        local_order_id=plan.local_order_id,
        settlement_key=settlement.settlement_key,
    )
    _rewrite_settlement_tables_to_v13(path)

    class SimulatedPowerLoss(BaseException):
        pass

    original = StateStore._migrate_v14.__func__

    def fail_after_migration(cls, connection):
        original(cls, connection)
        raise SimulatedPowerLoss()

    monkeypatch.setattr(StateStore, "_migrate_v14", classmethod(fail_after_migration))
    with pytest.raises(SimulatedPowerLoss):
        StateStore(path, allow_migration=True)
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[
                0
            ]
            == "13"
        )
        ddl = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='external_order_settlements'"
        ).fetchone()[0]
        assert "completeness_version = 1" in ddl
        assert (
            connection.execute("SELECT COUNT(*) FROM external_order_settlements").fetchone()[0] == 1
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM financial_settlement_applications").fetchone()[
                0
            ]
            == 1
        )
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='external_order_settlements_v13_source'"
            ).fetchone()
            is None
        )


def test_v12_migration_baseexception_rolls_back_table_and_metadata(tmp_path, monkeypatch) -> None:

    path = tmp_path / "state.db"
    StateStore(path)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE external_order_settlements")
        connection.execute("UPDATE metadata SET value='11' WHERE key='schema_version'")

    class SimulatedPowerLoss(BaseException):
        pass

    original = StateStore._ensure_external_order_settlement_schema

    def fail_after_schema_creation(connection):
        original(connection)
        raise SimulatedPowerLoss()

    monkeypatch.setattr(
        StateStore,
        "_ensure_external_order_settlement_schema",
        staticmethod(fail_after_schema_creation),
    )
    with pytest.raises(SimulatedPowerLoss):
        StateStore(path, allow_migration=True)

    with sqlite3.connect(path) as connection:
        assert (
            connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[
                0
            ]
            == "11"
        )
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='external_order_settlements'"
            ).fetchone()
            is None
        )


def _stored_plan_for_application() -> PersistedFinancialApplicationPlan:
    source = _plan(FinancialTransitionType.ENTER_LONG, side="BUY", direction=1)
    return PersistedFinancialApplicationPlan(
        local_order_id=1,
        intent_id=source.intent_id,
        plan=source.plan,
        created_at=source.created_at,
    )


def _prepare_settlement_application_store(
    tmp_path,
) -> tuple[StateStore, PersistedFinancialApplicationPlan, ExternalOrderSettlement]:
    store = StateStore(tmp_path / "state.db")
    plan = _stored_plan_for_application()
    reservation = store.reserve_market_order_with_application_plan(
        plan.plan.identity,
        plan=plan.plan,
    )
    assert reservation.order_id == plan.local_order_id
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE orders SET local_state='PENDING_RECONCILIATION' WHERE id=?",
            (plan.local_order_id,),
        )
    settlement = _settlement(plan, (_fill(raw_hash="c" * 64),))
    store.persist_external_order_settlement(
        settlement,
        engine="trend",
        observed_at="2026-09-05T12:03:00Z",
    )
    return store, plan, settlement


def test_v13_atomic_settlement_application_is_idempotent(tmp_path) -> None:
    store, plan, settlement = _prepare_settlement_application_store(tmp_path)

    first = store.apply_external_settlement_atomically(
        local_order_id=plan.local_order_id,
        settlement_key=settlement.settlement_key,
    )
    second = store.apply_external_settlement_atomically(
        local_order_id=plan.local_order_id,
        settlement_key=settlement.settlement_key,
    )

    assert first.applied is True
    assert first.already_applied is False
    assert first.event_id is not None
    assert first.trade_inserted is False
    assert second.applied is False
    assert second.already_applied is True
    assert second.event_id is None
    assert second.application == first.application
    assert len(store.get_financial_settlement_applications(plan.local_order_id)) == 1
    state = store.load_engine_state("trend")
    assert state["slots"]["slot"]["cash"] == pytest.approx(1000.0 - settlement.total_fee)
    with sqlite3.connect(store.path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM events WHERE event_type='EXTERNAL_ORDER_SETTLEMENT_APPLIED'"
            ).fetchone()[0]
            == 1
        )


def test_v13_atomic_settlement_application_rolls_back_on_baseexception(
    tmp_path,
    monkeypatch,
) -> None:
    store, plan, settlement = _prepare_settlement_application_store(tmp_path)
    before_state = store.load_engine_state("trend")
    with sqlite3.connect(store.path) as connection:
        before_positions = connection.execute(
            "SELECT engine, slot, status, cash, qty FROM positions ORDER BY engine, slot"
        ).fetchall()
        before_events = connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='EXTERNAL_ORDER_SETTLEMENT_APPLIED'"
        ).fetchone()[0]

    class SimulatedPowerLoss(BaseException):
        pass

    def fail_before_commit(*args, **kwargs):
        raise SimulatedPowerLoss()

    monkeypatch.setattr(store, "_insert_event", fail_before_commit)
    with pytest.raises(SimulatedPowerLoss):
        store.apply_external_settlement_atomically(
            local_order_id=plan.local_order_id,
            settlement_key=settlement.settlement_key,
        )

    assert store.load_engine_state("trend") == before_state
    with sqlite3.connect(store.path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM financial_settlement_applications").fetchone()[
                0
            ]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM events WHERE event_type='EXTERNAL_ORDER_SETTLEMENT_APPLIED'"
            ).fetchone()[0]
            == before_events
        )
        assert (
            connection.execute(
                "SELECT engine, slot, status, cash, qty FROM positions ORDER BY engine, slot"
            ).fetchall()
            == before_positions
        )


def test_v13_settlement_application_conflict_does_not_apply_second_settlement(
    tmp_path,
) -> None:
    store, plan, first_settlement = _prepare_settlement_application_store(tmp_path)
    second_settlement = _settlement(
        plan,
        (_fill(quantity=0.5, price=101.0, raw_hash="d" * 64),),
    )
    store.persist_external_order_settlement(
        second_settlement,
        engine="trend",
        observed_at="2026-09-05T12:04:00Z",
    )
    store.apply_external_settlement_atomically(
        local_order_id=plan.local_order_id,
        settlement_key=first_settlement.settlement_key,
    )
    with pytest.raises(FinancialSettlementError, match="APPLICATION_CONFLICT"):
        store.apply_external_settlement_atomically(
            local_order_id=plan.local_order_id,
            settlement_key=second_settlement.settlement_key,
        )
    assert len(store.get_financial_settlement_applications(plan.local_order_id)) == 1


def test_v12_to_v13_migration_baseexception_rolls_back_application_table(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "state.db"
    StateStore(path)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE financial_settlement_applications")
        connection.execute("UPDATE metadata SET value='12' WHERE key='schema_version'")

    class SimulatedPowerLoss(BaseException):
        pass

    original = StateStore._ensure_financial_settlement_application_schema

    def fail_after_schema_creation(connection):
        original(connection)
        raise SimulatedPowerLoss()

    monkeypatch.setattr(
        StateStore,
        "_ensure_financial_settlement_application_schema",
        staticmethod(fail_after_schema_creation),
    )
    with pytest.raises(SimulatedPowerLoss):
        StateStore(path, allow_migration=True)

    with sqlite3.connect(path) as connection:
        assert (
            connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[
                0
            ]
            == "12"
        )
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='financial_settlement_applications'"
            ).fetchone()
            is None
        )


def test_v13_schema_has_application_constraints(tmp_path) -> None:
    store = StateStore(tmp_path / "state.db")
    with sqlite3.connect(store.path) as connection:
        table_info = connection.execute(
            "PRAGMA table_info(financial_settlement_applications)"
        ).fetchall()
        indexes = connection.execute(
            "PRAGMA index_list(financial_settlement_applications)"
        ).fetchall()
        unique_signatures = set()
        for index in indexes:
            if index[2] != 1:
                continue
            columns = tuple(
                row[2] for row in connection.execute(f"PRAGMA index_info({index[1]!r})").fetchall()
            )
            unique_signatures.add(columns)
        foreign_keys = {
            (row[3], row[2], row[4])
            for row in connection.execute(
                "PRAGMA foreign_key_list(financial_settlement_applications)"
            ).fetchall()
        }
    assert [row[1] for row in table_info if row[5]] == ["application_key"]
    assert {
        ("local_order_id", "plan_key", "application_version"),
        ("local_order_id", "settlement_key", "application_version"),
    } <= unique_signatures
    assert {
        ("local_order_id", "orders", "id"),
        ("settlement_key", "external_order_settlements", "settlement_key"),
        ("plan_key", "financial_application_plans", "plan_key"),
    } <= foreign_keys
