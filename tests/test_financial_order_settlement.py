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

    assert SCHEMA_VERSION == 12
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
            == "12"
        )
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='external_order_settlements'"
            ).fetchone()
            is not None
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
