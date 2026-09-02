from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier

import pytest

from btcquant.execution.external_evidence import ExternalFill
from btcquant.execution.financial_fill_application import (
    FinancialApplicationLedgerConflict,
    FinancialFillApplicationError,
)
from btcquant.execution.financial_application_plan import canonical_json
from btcquant.execution.order_state import FinancialTransitionType
from btcquant.execution.state_store import StateStore
from test_financial_fill_application import _fill, _persisted, _plan


def _prepare(
    tmp_path: Path,
    *,
    transition: FinancialTransitionType = FinancialTransitionType.ENTER_LONG,
    position: dict | None = None,
    requested_qty: float = 1.0,
    fill_quantity: float | None = None,
    side: str = "BUY",
    reason: str = "entry",
    reduce_only: bool = False,
) -> tuple[StateStore, object, ExternalFill]:
    store = StateStore(tmp_path / "state.db")
    plan = _plan(
        transition,
        position=position,
        requested_qty=requested_qty,
        side=side,
        reason=reason,
        reduce_only=reduce_only,
    )
    persisted = _persisted(store, plan)
    fill = replace(
        _fill(
            persisted,
            quantity=requested_qty if fill_quantity is None else fill_quantity,
            price=110.0,
            fee=-0.02,
        ),
        external_order_id="oid-A",
    )
    store.append_external_fill(fill)
    payload = {
        "local_order_id": persisted.local_order_id,
        "intent_id": persisted.intent_id,
        "venue": "hyperliquid",
        "account_scope": "main",
        "instrument": "BTC/USDC:USDC",
        "side": side,
        "engine": "trend",
        "expected_client_order_id": "cloid-A",
        "expected_external_order_id": "oid-A",
        "outcome": "FOUND",
        "reason": None,
        "retryable": False,
        "start_time_ms": 1_700_000_000_000,
        "end_time_ms": 1_700_000_010_000,
        "response_count": 1,
        "matched_count": 1,
        "response_limit": 2000,
        "response_limit_reached": False,
        "retention_limit": 10_000,
        "absence_authoritative": False,
        "matched_fills": [
            {
                "raw_payload_hash": fill.raw_payload_hash,
                "reported_trade_id_candidate": "tid-A",
            }
        ],
    }
    store.persist_external_fill_lookup_evidence(
        fills=(),
        engine="trend",
        aggregate_id=persisted.intent_id,
        payload=payload,
        event_type="external_fill_lookup_found",
    )
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE orders SET local_state='PENDING_RECONCILIATION' WHERE id=?",
            (persisted.local_order_id,),
        )
    return store, persisted, fill


def _counts(store: StateStore, order_id: int) -> tuple[int, int, int, int]:
    with sqlite3.connect(store.path) as connection:
        ledger = connection.execute(
            "SELECT COUNT(*) FROM financial_fill_applications WHERE local_order_id=?",
            (order_id,),
        ).fetchone()[0]
        trades = connection.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        events = connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='FINANCIAL_FILL_APPLIED' "
            "AND aggregate_id=?",
            (str(order_id),),
        ).fetchone()[0]
        positions = connection.execute(
            "SELECT COUNT(*) FROM positions WHERE engine='trend'"
        ).fetchone()[0]
    return int(ledger), int(trades), int(events), int(positions)


def _positions_snapshot(store: StateStore) -> tuple[tuple[object, ...], ...]:
    columns = (
        "engine",
        "slot",
        "status",
        "cash",
        "entry_time",
        "entry_price",
        "qty",
        "stop_price",
        "direction",
        "bars_held",
        "best_close",
        "stop_order_id",
        "entry_fee",
        "last_bar_ts",
    )
    with sqlite3.connect(store.path) as connection:
        rows = connection.execute(
            "SELECT " + ", ".join(columns) + " FROM positions ORDER BY engine, slot"
        ).fetchall()
    return tuple(tuple(row) for row in rows)


def test_enter_writer_commits_ledger_state_projection_and_audit(tmp_path: Path) -> None:
    store, persisted, fill = _prepare(tmp_path)
    before = store.read_orders("trend")[0]
    result = store.apply_financial_fill_atomically(
        local_order_id=persisted.local_order_id, fill_key=fill.fill_key or ""
    )

    assert result.applied is True
    assert result.already_applied is False
    assert result.event_id is not None
    assert _counts(store, persisted.local_order_id) == (1, 0, 1, 1)
    assert (
        store.read_financial_fill_application_chain(persisted.local_order_id)[0].result
        == result.application.result
    )
    assert store.load_engine_state("trend") == json.loads(
        canonical_json(result.application.result.state_after_payload)
    )
    after = store.read_orders("trend")[0]
    for field in (
        "filled_qty",
        "remaining_qty",
        "price",
        "fee",
        "external_state",
        "status",
        "local_state",
    ):
        assert after[field] == before[field]


def test_exit_writer_inserts_one_trade_without_finalizing_order(tmp_path: Path) -> None:
    position = {
        "entry_time": "2026-08-31T12:00:00+00:00",
        "entry_price": 100.0,
        "qty": 1.0,
        "stop_price": 90.0,
        "direction": 1,
        "bars_held": 3,
        "best_close": 110.0,
        "initial_qty": 1.0,
        "last_add_price": 100.0,
        "pyramid_adds": 0,
    }
    store, persisted, fill = _prepare(
        tmp_path,
        transition=FinancialTransitionType.EXIT,
        position=position,
        side="SELL",
        reason="exit",
        reduce_only=True,
    )
    result = store.apply_financial_fill_atomically(
        local_order_id=persisted.local_order_id, fill_key=fill.fill_key or ""
    )
    assert result.trade_inserted is True
    assert _counts(store, persisted.local_order_id) == (1, 1, 1, 1)
    trade = store.read_trades()[0]
    assert trade["exit_ts"] == result.application.result.trade_payload["exit_ts"]
    assert trade["pnl"] == pytest.approx(result.application.result.trade_payload["pnl"])
    assert store.read_orders("trend")[0]["local_state"] == "PENDING_RECONCILIATION"


def test_same_fill_replay_is_a_noop(tmp_path: Path) -> None:
    store, persisted, fill = _prepare(tmp_path)
    first = store.apply_financial_fill_atomically(
        local_order_id=persisted.local_order_id, fill_key=fill.fill_key or ""
    )
    counts_before = _counts(store, persisted.local_order_id)
    second = store.apply_financial_fill_atomically(
        local_order_id=persisted.local_order_id, fill_key=fill.fill_key or ""
    )
    assert first.application.application_key == second.application.application_key
    assert second.applied is False
    assert second.already_applied is True
    assert second.event_id is None
    assert _counts(store, persisted.local_order_id) == counts_before


def test_writer_rolls_back_after_ledger_stage_failure(tmp_path: Path, monkeypatch) -> None:
    store, persisted, fill = _prepare(tmp_path)
    before_state = store.load_engine_state("trend")
    before_positions = _positions_snapshot(store)
    before_counts = _counts(store, persisted.local_order_id)

    def fail(*args, **kwargs):
        raise RuntimeError("injected after ledger stage")

    monkeypatch.setattr(store, "_update_engine_state_cas_in_transaction", fail)
    with pytest.raises(RuntimeError, match="injected"):
        store.apply_financial_fill_atomically(
            local_order_id=persisted.local_order_id, fill_key=fill.fill_key or ""
        )
    assert _counts(store, persisted.local_order_id) == before_counts
    assert store.load_engine_state("trend") == before_state
    assert _positions_snapshot(store) == before_positions


def test_writer_rolls_back_after_event_stage_failure(tmp_path: Path, monkeypatch) -> None:
    store, persisted, fill = _prepare(tmp_path)
    before_state = store.load_engine_state("trend")
    before_positions = _positions_snapshot(store)
    before_counts = _counts(store, persisted.local_order_id)

    def fail(*args, **kwargs):
        raise RuntimeError("injected event failure")

    monkeypatch.setattr(store, "_insert_event", fail)
    with pytest.raises(RuntimeError, match="event failure"):
        store.apply_financial_fill_atomically(
            local_order_id=persisted.local_order_id, fill_key=fill.fill_key or ""
        )
    assert _counts(store, persisted.local_order_id) == before_counts
    assert store.load_engine_state("trend") == before_state
    assert _positions_snapshot(store) == before_positions


def test_writer_requires_reconciliation_state(tmp_path: Path) -> None:
    store, persisted, fill = _prepare(tmp_path)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE orders SET local_state='INTENT_CREATED' WHERE id=?",
            (persisted.local_order_id,),
        )
    with pytest.raises(FinancialFillApplicationError, match="NOT_RECONCILIATION_READY"):
        store.apply_financial_fill_atomically(
            local_order_id=persisted.local_order_id, fill_key=fill.fill_key or ""
        )


def _append_fill_lookup(
    store: StateStore,
    persisted: object,
    fills: tuple[ExternalFill, ...],
    candidates: tuple[str | None, ...] = (),
) -> None:
    assert len(fills) == len(candidates)
    payload = {
        "local_order_id": persisted.local_order_id,
        "intent_id": persisted.intent_id,
        "venue": "hyperliquid",
        "account_scope": "main",
        "instrument": "BTC/USDC:USDC",
        "side": persisted.plan.side,
        "engine": "trend",
        "expected_client_order_id": "cloid-A",
        "expected_external_order_id": "oid-A",
        "outcome": "FOUND",
        "reason": None,
        "retryable": False,
        "start_time_ms": 1_700_000_000_000,
        "end_time_ms": 1_700_000_010_000,
        "response_count": len(fills),
        "matched_count": len(fills),
        "response_limit": 2000,
        "response_limit_reached": False,
        "retention_limit": 10_000,
        "absence_authoritative": False,
        "matched_fills": [
            {
                "raw_payload_hash": fill.raw_payload_hash,
                "reported_trade_id_candidate": candidate,
            }
            for fill, candidate in zip(fills, candidates, strict=True)
        ],
    }
    store.persist_external_fill_lookup_evidence(
        fills=(),
        engine="trend",
        aggregate_id=persisted.intent_id,
        payload=payload,
        event_type="external_fill_lookup_found",
    )


def _distinct_fill(
    persisted: object,
    *,
    quantity: float,
    price: float,
    fee: float,
    event_at: str,
    venue_fill_id: str,
    raw_hash: str,
) -> ExternalFill:
    return replace(
        _fill(
            persisted,
            quantity=quantity,
            price=price,
            fee=fee,
            event_at=event_at,
            venue_fill_id=venue_fill_id,
        ),
        external_order_id="oid-A",
        raw_payload_hash=raw_hash,
    )


def _assert_failed_write_is_invisible(
    store: StateStore,
    order_id: int,
    before_state: dict,
    before_counts: tuple[int, int, int, int],
    before_positions: tuple[tuple[object, ...], ...],
) -> None:
    assert _counts(store, order_id) == before_counts
    assert store.load_engine_state("trend") == before_state
    assert _positions_snapshot(store) == before_positions


def test_progressive_enter_is_atomic_and_recomposed(tmp_path: Path) -> None:
    store, persisted, first = _prepare(tmp_path, fill_quantity=0.4)
    second = _distinct_fill(
        persisted,
        quantity=0.6,
        price=102.0,
        fee=-0.02,
        event_at="2026-09-01T12:02:00Z",
        venue_fill_id="venue-fill-2",
        raw_hash="b" * 64,
    )
    store.append_external_fill(second)
    _append_fill_lookup(store, persisted, (second,), (None,))

    first_result = store.apply_financial_fill_atomically(
        local_order_id=persisted.local_order_id, fill_key=first.fill_key or ""
    )
    second_result = store.apply_financial_fill_atomically(
        local_order_id=persisted.local_order_id, fill_key=second.fill_key or ""
    )

    chain = store.read_financial_fill_application_chain(persisted.local_order_id)
    assert [record.application_index for record in chain] == [0, 1]
    assert _counts(store, persisted.local_order_id) == (2, 0, 2, 1)
    assert second_result.application.state_after_sha256 == chain[-1].state_after_sha256
    state = store.load_engine_state("trend")
    assert state is not None
    slot = state["slots"]["slot"]
    assert slot["position"]["qty"] == pytest.approx(1.0)
    assert slot["position"]["entry_price"] == pytest.approx(105.2)
    assert slot["entry_fee"] == pytest.approx(-0.04)
    assert slot["cash"] == pytest.approx(1_000.04)
    assert first_result.application.previous_application_key is None
    assert (
        second_result.application.previous_application_key
        == first_result.application.application_key
    )


def test_add_progressive_fills_update_position_once_per_plan(tmp_path: Path) -> None:
    position = {
        "entry_time": "2026-08-31T12:00:00+00:00",
        "entry_price": 100.0,
        "qty": 1.0,
        "stop_price": 90.0,
        "direction": 1,
        "bars_held": 3,
        "best_close": 110.0,
        "initial_qty": 1.0,
        "last_add_price": 100.0,
        "pyramid_adds": 2,
    }
    store, persisted, first = _prepare(
        tmp_path,
        transition=FinancialTransitionType.ADD,
        position=position,
        requested_qty=0.5,
        fill_quantity=0.25,
        side="BUY",
        reason="pyramid",
    )
    second = _distinct_fill(
        persisted,
        quantity=0.25,
        price=102.0,
        fee=-0.02,
        event_at="2026-09-01T12:02:00Z",
        venue_fill_id="venue-fill-2",
        raw_hash="b" * 64,
    )
    store.append_external_fill(second)
    _append_fill_lookup(store, persisted, (second,), (None,))

    store.apply_financial_fill_atomically(
        local_order_id=persisted.local_order_id, fill_key=first.fill_key or ""
    )
    store.apply_financial_fill_atomically(
        local_order_id=persisted.local_order_id, fill_key=second.fill_key or ""
    )

    state = store.load_engine_state("trend")
    assert state is not None
    slot = state["slots"]["slot"]
    assert slot["position"]["qty"] == pytest.approx(1.5)
    assert slot["position"]["entry_price"] == pytest.approx(102.0)
    assert slot["position"]["last_add_price"] == pytest.approx(106.0)
    assert slot["position"]["pyramid_adds"] == 3
    assert slot["entry_fee"] == pytest.approx(0.96)
    assert slot["cash"] == pytest.approx(1_000.04)
    assert _counts(store, persisted.local_order_id) == (2, 0, 2, 1)


def test_full_exit_progressive_fills_preserve_stop_metadata(tmp_path: Path) -> None:
    position = {
        "entry_time": "2026-08-31T12:00:00+00:00",
        "entry_price": 100.0,
        "qty": 1.0,
        "stop_price": 90.0,
        "direction": 1,
        "bars_held": 3,
        "best_close": 110.0,
        "initial_qty": 1.0,
        "last_add_price": 100.0,
        "pyramid_adds": 0,
    }
    store, persisted, first = _prepare(
        tmp_path,
        transition=FinancialTransitionType.EXIT,
        position=position,
        requested_qty=1.0,
        fill_quantity=0.5,
        side="SELL",
        reason="exit",
        reduce_only=True,
    )
    second = _distinct_fill(
        persisted,
        quantity=0.5,
        price=90.0,
        fee=-0.02,
        event_at="2026-09-01T12:01:30Z",
        venue_fill_id="venue-fill-2",
        raw_hash="b" * 64,
    )
    store.append_external_fill(second)
    _append_fill_lookup(store, persisted, (second,), (None,))

    store.apply_financial_fill_atomically(
        local_order_id=persisted.local_order_id, fill_key=first.fill_key or ""
    )
    store.apply_financial_fill_atomically(
        local_order_id=persisted.local_order_id, fill_key=second.fill_key or ""
    )

    state = store.load_engine_state("trend")
    assert state is not None
    slot = state["slots"]["slot"]
    assert slot["position"] is None
    assert slot["entry_fee"] == pytest.approx(0.0)
    assert slot["cash"] == pytest.approx(1_000.04)
    assert slot["stop_order_id"] == "stop-1"
    assert len(store.read_trades()) == 2
    assert _counts(store, persisted.local_order_id) == (2, 2, 2, 1)
    assert store.read_orders("trend")[0]["local_state"] == "PENDING_RECONCILIATION"


def test_late_economic_fill_is_accepted_after_earlier_arrival(tmp_path: Path) -> None:
    store, persisted, first = _prepare(
        tmp_path,
        requested_qty=1.0,
        fill_quantity=0.4,
    )
    late = _distinct_fill(
        persisted,
        quantity=0.6,
        price=102.0,
        fee=-0.02,
        event_at="2026-09-01T12:00:00Z",
        venue_fill_id="venue-fill-2",
        raw_hash="b" * 64,
    )
    store.append_external_fill(late)
    _append_fill_lookup(store, persisted, (late,), (None,))

    first_result = store.apply_financial_fill_atomically(
        local_order_id=persisted.local_order_id, fill_key=first.fill_key or ""
    )
    late_result = store.apply_financial_fill_atomically(
        local_order_id=persisted.local_order_id, fill_key=late.fill_key or ""
    )

    assert late_result.applied is True
    assert late_result.application.application_index == 1
    assert (
        late_result.application.previous_application_key == first_result.application.application_key
    )
    assert store.read_financial_fill_application_chain(persisted.local_order_id)
    state = store.load_engine_state("trend")
    assert state is not None
    assert state["slots"]["slot"]["position"]["qty"] == pytest.approx(1.0)
    assert state["slots"]["slot"]["position"]["entry_price"] == pytest.approx(105.2)


def test_old_fill_replay_after_later_fill_is_noop(tmp_path: Path) -> None:
    store, persisted, first = _prepare(tmp_path, fill_quantity=0.4)
    second = _distinct_fill(
        persisted,
        quantity=0.6,
        price=102.0,
        fee=-0.02,
        event_at="2026-09-01T12:02:00Z",
        venue_fill_id="venue-fill-2",
        raw_hash="b" * 64,
    )
    store.append_external_fill(second)
    _append_fill_lookup(store, persisted, (second,), (None,))
    store.apply_financial_fill_atomically(
        local_order_id=persisted.local_order_id, fill_key=first.fill_key or ""
    )
    later = store.apply_financial_fill_atomically(
        local_order_id=persisted.local_order_id, fill_key=second.fill_key or ""
    )
    before = _counts(store, persisted.local_order_id)
    state_before = store.load_engine_state("trend")

    replay = store.apply_financial_fill_atomically(
        local_order_id=persisted.local_order_id, fill_key=first.fill_key or ""
    )

    assert replay.applied is False
    assert replay.already_applied is True
    assert replay.ledger_head_application_key == later.application.application_key
    assert replay.state_after_sha256 == later.application.state_after_sha256
    assert _counts(store, persisted.local_order_id) == before
    assert store.load_engine_state("trend") == state_before


def _exercise_failure_stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str) -> None:
    store, persisted, fill = _prepare(tmp_path)
    before_state = store.load_engine_state("trend")
    before_positions = _positions_snapshot(store)
    assert before_state is not None
    before_counts = _counts(store, persisted.local_order_id)

    def fail(*args, **kwargs):
        raise RuntimeError(f"injected {stage}")

    if stage == "after_ledger":
        monkeypatch.setattr(store, "_update_engine_state_cas_in_transaction", fail)
    elif stage == "after_engine":
        monkeypatch.setattr(store, "_sync_positions", fail)
    elif stage == "after_positions":
        monkeypatch.setattr(store, "_insert_financial_fill_trade_in_transaction", fail)
    else:
        original_insert_event = store._insert_event

        def fail_event(*args, **kwargs):
            if stage == "after_event":
                original_insert_event(*args, **kwargs)
            raise RuntimeError(f"injected {stage}")

        monkeypatch.setattr(store, "_insert_event", fail_event)

    with pytest.raises(RuntimeError, match=f"injected {stage}"):
        store.apply_financial_fill_atomically(
            local_order_id=persisted.local_order_id, fill_key=fill.fill_key or ""
        )
    _assert_failed_write_is_invisible(
        store, persisted.local_order_id, before_state, before_counts, before_positions
    )


@pytest.mark.parametrize(
    "stage",
    ["after_ledger", "after_engine", "after_positions", "after_trade", "after_event"],
)
def test_writer_failure_stages_are_all_rollback_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    _exercise_failure_stage(tmp_path, monkeypatch, stage)


def test_baseexception_after_event_rolls_back_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class InjectedPowerLoss(BaseException):
        pass

    store, persisted, fill = _prepare(tmp_path)
    before_state = store.load_engine_state("trend")
    before_positions = _positions_snapshot(store)
    assert before_state is not None
    before_counts = _counts(store, persisted.local_order_id)
    original_insert_event = store._insert_event

    def fail_after_event(*args, **kwargs):
        original_insert_event(*args, **kwargs)
        raise InjectedPowerLoss("simulated power loss")

    monkeypatch.setattr(store, "_insert_event", fail_after_event)
    with pytest.raises(InjectedPowerLoss, match="simulated power loss"):
        store.apply_financial_fill_atomically(
            local_order_id=persisted.local_order_id, fill_key=fill.fill_key or ""
        )
    _assert_failed_write_is_invisible(
        store, persisted.local_order_id, before_state, before_counts, before_positions
    )


def test_sqlite_constraint_failure_rolls_back_writer(tmp_path: Path) -> None:
    store, persisted, fill = _prepare(tmp_path)
    before_state = store.load_engine_state("trend")
    before_positions = _positions_snapshot(store)
    assert before_state is not None
    before_counts = _counts(store, persisted.local_order_id)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_financial_fill_audit
            BEFORE INSERT ON events
            WHEN NEW.event_type = "FINANCIAL_FILL_APPLIED"
            BEGIN
                SELECT RAISE(ABORT, "injected event constraint");
            END
            """
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected event constraint"):
        store.apply_financial_fill_atomically(
            local_order_id=persisted.local_order_id, fill_key=fill.fill_key or ""
        )
    _assert_failed_write_is_invisible(
        store, persisted.local_order_id, before_state, before_counts, before_positions
    )


def test_writer_rejects_stale_eligibility_after_new_ambiguous_fill(
    tmp_path: Path,
) -> None:
    store, persisted, first = _prepare(tmp_path)
    second = _distinct_fill(
        persisted,
        quantity=1.0,
        price=110.0,
        fee=-0.02,
        event_at="2026-09-01T12:03:00Z",
        venue_fill_id="venue-fill-2",
        raw_hash="b" * 64,
    )
    store.append_external_fill(second)
    _append_fill_lookup(store, persisted, (second,), ("tid-A",))
    before_state = store.load_engine_state("trend")
    before_positions = _positions_snapshot(store)
    before_counts = _counts(store, persisted.local_order_id)

    with pytest.raises(FinancialFillApplicationError, match="NOT_APPLICABLE"):
        store.apply_financial_fill_atomically(
            local_order_id=persisted.local_order_id, fill_key=first.fill_key or ""
        )
    _assert_failed_write_is_invisible(
        store, persisted.local_order_id, before_state, before_counts, before_positions
    )


def test_writer_rejects_missing_venue_fill_id(tmp_path: Path) -> None:
    store, persisted, fill = _prepare(tmp_path)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE external_fills SET venue_fill_id=NULL WHERE fill_key=?",
            (fill.fill_key,),
        )
    with pytest.raises(FinancialFillApplicationError, match="IRREVERSIBLE_FILL_IDENTITY_UNPROVEN"):
        store.apply_financial_fill_atomically(
            local_order_id=persisted.local_order_id, fill_key=fill.fill_key or ""
        )


def test_writer_rejects_missing_engine_state(tmp_path: Path) -> None:
    store, persisted, fill = _prepare(tmp_path)
    with sqlite3.connect(store.path) as connection:
        connection.execute("DELETE FROM engine_state WHERE engine=?", ("trend",))
    with pytest.raises(FinancialFillApplicationError, match="ENGINE_STATE_MISSING"):
        store.apply_financial_fill_atomically(
            local_order_id=persisted.local_order_id, fill_key=fill.fill_key or ""
        )


def test_writer_rejects_corrupted_ledger_before_new_application(tmp_path: Path) -> None:
    store, persisted, first = _prepare(tmp_path, fill_quantity=0.4)
    second = _distinct_fill(
        persisted,
        quantity=0.6,
        price=102.0,
        fee=-0.02,
        event_at="2026-09-01T12:02:00Z",
        venue_fill_id="venue-fill-2",
        raw_hash="b" * 64,
    )
    store.append_external_fill(second)
    _append_fill_lookup(store, persisted, (second,), (None,))
    store.apply_financial_fill_atomically(
        local_order_id=persisted.local_order_id, fill_key=first.fill_key or ""
    )
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE financial_fill_applications SET result_sha256=? WHERE application_index=0",
            ("0" * 64,),
        )
    before_state = store.load_engine_state("trend")
    before_positions = _positions_snapshot(store)
    before_counts = _counts(store, persisted.local_order_id)
    with pytest.raises(
        FinancialApplicationLedgerConflict, match="FINANCIAL_APPLICATION_LEDGER_CONFLICT"
    ):
        store.apply_financial_fill_atomically(
            local_order_id=persisted.local_order_id, fill_key=second.fill_key or ""
        )
    _assert_failed_write_is_invisible(
        store, persisted.local_order_id, before_state, before_counts, before_positions
    )


def test_two_writers_claim_same_fill_once(tmp_path: Path) -> None:
    store, persisted, fill = _prepare(tmp_path)
    barrier = Barrier(2)
    database = store.path
    fill_key = fill.fill_key or ""

    def worker() -> object:
        contender = StateStore(database)
        barrier.wait()
        return contender.apply_financial_fill_atomically(
            local_order_id=persisted.local_order_id, fill_key=fill_key
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(worker)
        second_future = executor.submit(worker)
        results = [first_future.result(timeout=20), second_future.result(timeout=20)]

    assert sorted(result.applied for result in results) == [False, True]
    assert sum(result.already_applied for result in results) == 1
    assert _counts(store, persisted.local_order_id) == (1, 0, 1, 1)
    assert store.read_financial_fill_application_chain(persisted.local_order_id)


def test_reader_accepts_writer_chain(tmp_path: Path) -> None:
    store, persisted, fill = _prepare(tmp_path)
    result = store.apply_financial_fill_atomically(
        local_order_id=persisted.local_order_id, fill_key=fill.fill_key or ""
    )
    chain = store.read_financial_fill_application_chain(persisted.local_order_id)
    assert chain[0].result == result.application.result
    assert chain[0].result.state_after_sha256 == result.state_after_sha256


def test_historical_replay_after_order_finalization_is_a_noop(tmp_path: Path) -> None:
    store, persisted, fill = _prepare(tmp_path)
    first = store.apply_financial_fill_atomically(
        local_order_id=persisted.local_order_id, fill_key=fill.fill_key or ""
    )
    before_counts = _counts(store, persisted.local_order_id)
    before_state = store.load_engine_state("trend")
    before_positions = _positions_snapshot(store)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            'UPDATE orders SET status="FILLED", local_state="TERMINAL", external_state="FILLED" WHERE id=?',
            (persisted.local_order_id,),
        )
    replay = store.apply_financial_fill_atomically(
        local_order_id=persisted.local_order_id, fill_key=fill.fill_key or ""
    )
    assert replay.applied is False
    assert replay.already_applied is True
    assert replay.application.application_key == first.application.application_key
    assert replay.state_after_sha256 == first.application.state_after_sha256
    assert replay.ledger_head_application_key == first.application.application_key
    assert _counts(store, persisted.local_order_id) == before_counts
    assert store.load_engine_state("trend") == before_state
    assert _positions_snapshot(store) == before_positions


def test_historical_replay_after_global_state_advanced_is_a_noop(tmp_path: Path) -> None:
    store, persisted, fill = _prepare(tmp_path)
    first = store.apply_financial_fill_atomically(
        local_order_id=persisted.local_order_id, fill_key=fill.fill_key or ""
    )
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            'UPDATE orders SET status="FILLED", local_state="TERMINAL", external_state="FILLED" WHERE id=?',
            (persisted.local_order_id,),
        )
    later_state = store.load_engine_state("trend")
    assert later_state is not None
    later_state["slots"]["slot"]["last_bar_ts"] = "2026-09-02T12:00:00+00:00"
    store.save_engine_state("trend", later_state, event_type="later_strategy_checkpoint")
    expected_state = store.load_engine_state("trend")
    expected_positions = _positions_snapshot(store)
    expected_counts = _counts(store, persisted.local_order_id)
    replay = store.apply_financial_fill_atomically(
        local_order_id=persisted.local_order_id, fill_key=fill.fill_key or ""
    )
    assert replay.applied is False
    assert replay.already_applied is True
    assert replay.state_after_sha256 == first.application.state_after_sha256
    assert replay.ledger_head_application_key == first.application.application_key
    assert store.load_engine_state("trend") == expected_state
    assert _positions_snapshot(store) == expected_positions
    assert _counts(store, persisted.local_order_id) == expected_counts


def test_terminal_order_with_new_fill_still_rejects(tmp_path: Path) -> None:
    store, persisted, first = _prepare(tmp_path, fill_quantity=0.4)
    second = _distinct_fill(
        persisted,
        quantity=0.6,
        price=102.0,
        fee=-0.02,
        event_at="2026-09-01T12:02:00Z",
        venue_fill_id="venue-fill-2",
        raw_hash="b" * 64,
    )
    store.append_external_fill(second)
    _append_fill_lookup(store, persisted, (second,), (None,))
    store.apply_financial_fill_atomically(
        local_order_id=persisted.local_order_id, fill_key=first.fill_key or ""
    )
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            'UPDATE orders SET status="FILLED", local_state="TERMINAL", external_state="FILLED" WHERE id=?',
            (persisted.local_order_id,),
        )
    before_state = store.load_engine_state("trend")
    before_positions = _positions_snapshot(store)
    before_counts = _counts(store, persisted.local_order_id)
    with pytest.raises(FinancialFillApplicationError, match="NOT_RECONCILIATION_READY"):
        store.apply_financial_fill_atomically(
            local_order_id=persisted.local_order_id, fill_key=second.fill_key or ""
        )
    assert store.load_engine_state("trend") == before_state
    assert _positions_snapshot(store) == before_positions
    assert _counts(store, persisted.local_order_id) == before_counts


def test_corrupted_historical_ledger_blocks_idempotent_replay(tmp_path: Path) -> None:
    store, persisted, fill = _prepare(tmp_path)
    store.apply_financial_fill_atomically(
        local_order_id=persisted.local_order_id, fill_key=fill.fill_key or ""
    )
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE financial_fill_applications SET result_sha256=? WHERE fill_key=?",
            ("0" * 64, fill.fill_key),
        )
    with pytest.raises(
        FinancialApplicationLedgerConflict, match="FINANCIAL_APPLICATION_LEDGER_CONFLICT"
    ):
        store.apply_financial_fill_atomically(
            local_order_id=persisted.local_order_id, fill_key=fill.fill_key or ""
        )


def _exit_position() -> dict[str, object]:
    return {
        "entry_time": "2026-08-31T12:00:00+00:00",
        "entry_price": 100.0,
        "qty": 1.0,
        "stop_price": 90.0,
        "direction": 1,
        "bars_held": 3,
        "best_close": 110.0,
        "initial_qty": 1.0,
        "last_add_price": 100.0,
        "pyramid_adds": 0,
    }


def test_actual_exit_trade_insert_then_failure_rolls_back_full_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, persisted, fill = _prepare(
        tmp_path,
        transition=FinancialTransitionType.EXIT,
        position=_exit_position(),
        side="SELL",
        reason="exit",
        reduce_only=True,
    )
    before_state = store.load_engine_state("trend")
    assert before_state is not None
    before_positions = _positions_snapshot(store)
    before_counts = _counts(store, persisted.local_order_id)
    real_insert = store._insert_financial_fill_trade_in_transaction

    def fail_after_real_trade(connection, payload):
        assert real_insert(connection, payload) is True
        assert connection.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1
        raise RuntimeError("after actual trade insert")

    monkeypatch.setattr(store, "_insert_financial_fill_trade_in_transaction", fail_after_real_trade)
    with pytest.raises(RuntimeError, match="after actual trade insert"):
        store.apply_financial_fill_atomically(
            local_order_id=persisted.local_order_id, fill_key=fill.fill_key or ""
        )
    _assert_failed_write_is_invisible(
        store, persisted.local_order_id, before_state, before_counts, before_positions
    )


def test_baseexception_after_actual_exit_trade_rolls_back_full_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class InjectedPowerLoss(BaseException):
        pass

    store, persisted, fill = _prepare(
        tmp_path,
        transition=FinancialTransitionType.EXIT,
        position=_exit_position(),
        side="SELL",
        reason="exit",
        reduce_only=True,
    )
    before_state = store.load_engine_state("trend")
    assert before_state is not None
    before_positions = _positions_snapshot(store)
    before_counts = _counts(store, persisted.local_order_id)
    real_insert = store._insert_financial_fill_trade_in_transaction

    def fail_after_real_trade(connection, payload):
        assert real_insert(connection, payload) is True
        assert connection.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1
        raise InjectedPowerLoss("power loss after actual trade")

    monkeypatch.setattr(store, "_insert_financial_fill_trade_in_transaction", fail_after_real_trade)
    with pytest.raises(InjectedPowerLoss, match="power loss after actual trade"):
        store.apply_financial_fill_atomically(
            local_order_id=persisted.local_order_id, fill_key=fill.fill_key or ""
        )
    _assert_failed_write_is_invisible(
        store, persisted.local_order_id, before_state, before_counts, before_positions
    )


def test_concurrent_exit_duplicate_inserts_one_trade(tmp_path: Path) -> None:
    store, persisted, fill = _prepare(
        tmp_path,
        transition=FinancialTransitionType.EXIT,
        position=_exit_position(),
        side="SELL",
        reason="exit",
        reduce_only=True,
    )
    barrier = Barrier(2)
    database = store.path
    fill_key = fill.fill_key or ""

    def worker() -> object:
        contender = StateStore(database)
        barrier.wait()
        return contender.apply_financial_fill_atomically(
            local_order_id=persisted.local_order_id, fill_key=fill_key
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [
            executor.submit(worker),
            executor.submit(worker),
        ]
        outcomes = [future.result(timeout=20) for future in results]
    assert sorted(result.applied for result in outcomes) == [False, True]
    assert sum(result.already_applied for result in outcomes) == 1
    assert _counts(store, persisted.local_order_id) == (1, 1, 1, 1)
    state = store.load_engine_state("trend")
    assert state is not None
    assert state["slots"]["slot"]["position"] is None
    assert len(store.read_trades()) == 1
    assert len(store.read_financial_fill_application_chain(persisted.local_order_id)) == 1


def test_engine_state_conflict_rejects_new_fill_without_writer_mutation(tmp_path: Path) -> None:
    store, persisted, fill = _prepare(tmp_path)
    changed_state = store.load_engine_state("trend")
    assert changed_state is not None
    changed_state["slots"]["slot"]["last_bar_ts"] = "2026-09-02T12:30:00+00:00"
    store.save_engine_state("trend", changed_state, event_type="unrelated_state_advance")
    before_state = store.load_engine_state("trend")
    before_positions = _positions_snapshot(store)
    before_counts = _counts(store, persisted.local_order_id)
    with pytest.raises(FinancialFillApplicationError, match="FINANCIAL_APPLICATION_STATE_CONFLICT"):
        store.apply_financial_fill_atomically(
            local_order_id=persisted.local_order_id, fill_key=fill.fill_key or ""
        )
    _assert_failed_write_is_invisible(
        store, persisted.local_order_id, before_state, before_counts, before_positions
    )


def test_position_projection_conflict_rejects_new_fill_without_repair(tmp_path: Path) -> None:
    store, persisted, fill = _prepare(tmp_path)
    with sqlite3.connect(store.path) as connection:
        connection.execute('UPDATE positions SET cash=cash+1 WHERE engine="trend" AND slot="slot"')
    before_state = store.load_engine_state("trend")
    before_positions = _positions_snapshot(store)
    before_counts = _counts(store, persisted.local_order_id)
    with pytest.raises(
        FinancialFillApplicationError, match="FINANCIAL_POSITION_PROJECTION_CONFLICT"
    ):
        store.apply_financial_fill_atomically(
            local_order_id=persisted.local_order_id, fill_key=fill.fill_key or ""
        )
    _assert_failed_write_is_invisible(
        store, persisted.local_order_id, before_state, before_counts, before_positions
    )


def test_resolution_snapshot_public_and_connection_scoped_reads_are_identical(
    tmp_path: Path,
) -> None:
    store, persisted, _fill_value = _prepare(tmp_path)
    public_snapshot = store.read_resolution_snapshot(persisted.local_order_id)
    with store._read_transaction() as connection:
        transactional_snapshot = store._read_resolution_snapshot_in_transaction(
            connection, persisted.local_order_id
        )
    assert public_snapshot == transactional_snapshot


def test_commit_result_rejects_impossible_typed_states(tmp_path: Path) -> None:
    store, persisted, fill = _prepare(tmp_path)
    result = store.apply_financial_fill_atomically(
        local_order_id=persisted.local_order_id, fill_key=fill.fill_key or ""
    )
    with pytest.raises(FinancialFillApplicationError, match="state_after_sha256"):
        replace(result, state_after_sha256="not-a-sha256")
    with pytest.raises(FinancialFillApplicationError, match="sans événement"):
        replace(result, event_id=None)
    replay = store.apply_financial_fill_atomically(
        local_order_id=persisted.local_order_id, fill_key=fill.fill_key or ""
    )
    with pytest.raises(FinancialFillApplicationError, match="rejeu idempotent"):
        replace(replay, trade_inserted=True)
