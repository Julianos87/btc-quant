from __future__ import annotations

import sqlite3

import pytest

from btcquant.execution.historical_state_reader import HistoricalStateReader
from btcquant.execution.paper_trade_reversal import apply_plan, build_plan
from btcquant.execution.state_store import StateStore


def _slot(*, cash: float, position: dict | None, timeline: list, last_bar: str) -> dict:
    return {
        "cash": cash,
        "position": position,
        "stop_order_id": None,
        "stop_order_local_id": None,
        "stop_intent_id": None,
        "stop_transition": None,
        "entry_fee": 0.1 if position else 0.0,
        "last_bar_ts": last_bar,
        "financial_transition_seq": 0,
        "position_timeline": timeline,
    }


def _state(*, open_position: bool, cash: float, last_funding: str, last_bar: str) -> dict:
    entry_ts = "2026-01-01T00:00:00+00:00"
    intent_id = "paper-stop-observed-1"
    position = (
        {
            "entry_time": entry_ts,
            "entry_price": 100.0,
            "qty": 0.1,
            "stop_price": 110.0,
            "direction": -1,
            "bars_held": 2,
            "best_close": 98.0,
            "initial_qty": 0.1,
            "last_add_price": 100.0,
            "pyramid_adds": 0,
        }
        if open_position
        else None
    )
    timeline = [
        {
            "effective_at": entry_ts,
            "intent_id": "entry-intent",
            "qty": 0.1,
            "direction": -1,
            "generation": f"entry={entry_ts}|initial_qty=0.1",
        }
    ]
    if not open_position:
        timeline.append(
            {
                "effective_at": "2026-01-02T00:00:00+00:00",
                "intent_id": intent_id,
                "qty": 0.0,
                "direction": -1,
                "generation": f"entry={entry_ts}|initial_qty=0.1",
            }
        )
    return {
        "slots": {
            "trend_ls_20": _slot(
                cash=cash, position=position, timeline=timeline, last_bar=last_bar
            ),
            "trend_ls_55": _slot(cash=100.0, position=None, timeline=[], last_bar=last_bar),
            "trend_ls_100": _slot(cash=100.0, position=None, timeline=[], last_bar=last_bar),
        },
        "last_funding_ts": last_funding,
        "reconciliation_required": False,
    }


def _insert_ledger(store: StateStore, **values) -> None:
    defaults = {
        "event_key": "source-key",
        "venue": "paper",
        "instrument": "BTC/USDC:USDC",
        "funding_timestamp": "2026-01-02T10:00:00+00:00",
        "native_funding_rate": 0.0,
        "position_generation": "FLAT",
        "funding_notional": 0.0,
        "funding_notional_price": None,
        "funding_notional_price_source": None,
        "funding_notional_price_timestamp": None,
        "funding_pnl": 0.0,
        "borrow_principal": 0.0,
        "borrow_rate_ann": 0.0,
        "borrow_dt_seconds": 0.0,
        "borrow_cost": 0.0,
        "applied_at": "2026-01-02T11:00:00+00:00",
    }
    defaults.update(values)
    with store._transaction() as connection:
        store._insert_funding_ledger(connection, defaults)


def _seed_false_stop(database) -> StateStore:
    store = StateStore(database)
    entry_ts = "2026-01-01T00:00:00+00:00"
    exit_ts = "2026-01-02T00:00:00+00:00"
    open_state = _state(
        open_position=True,
        cash=90.0,
        last_funding="2026-01-02T10:00:00+00:00",
        last_bar="2026-01-02T04:00:00+00:00",
    )
    store.save_engine_state("trend", open_state, event_type="pre_stop_checkpoint")
    trade = {
        "exit_ts": exit_ts,
        "entry_ts": entry_ts,
        "strategy": "trend_ls_20",
        "direction": "SHORT",
        "qty": 0.1,
        "entry_price": 100.0,
        "exit_price": 110.0,
        "pnl": -1.2,
        "bars_held": 8,
        "reason": "stop_exchange",
        "execution_provenance": "PAPER_SIMULATED_STOP",
    }
    correlation = "paper-stop-observed-1"
    closed_state = _state(
        open_position=False,
        cash=88.8,
        last_funding="2026-01-02T11:00:00+00:00",
        last_bar="2026-01-02T08:00:00+00:00",
    )
    with store._transaction() as connection:
        connection.execute(
            """INSERT INTO trades(exit_ts,entry_ts,strategy,direction,qty,entry_price,
               exit_price,pnl,bars_held,reason) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                exit_ts,
                entry_ts,
                "trend_ls_20",
                "SHORT",
                0.1,
                100.0,
                110.0,
                -1.2,
                8,
                "stop_exchange",
            ),
        )
        trade_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        store._insert_event(
            connection,
            "trend",
            "external_stop_fill_observed",
            store._state_event(closed_state, {"order_id": 7, "filled_qty": 0.1}),
            "order",
            "7",
            correlation,
            ts="2026-01-02T11:00:01+00:00",
        )
        stop_event_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        store._insert_event(
            connection,
            "trend",
            "trade_closed",
            trade,
            "strategy",
            "trend_ls_20",
            correlation,
            ts="2026-01-02T11:00:02+00:00",
        )
        store._insert_event(
            connection,
            "trend",
            "state_checkpoint",
            store._state_event(closed_state),
            "engine",
            "trend",
            ts="2026-01-02T11:00:03+00:00",
        )
    store.save_engine_state("trend", closed_state, event_type="after_stop")

    _insert_ledger(
        store,
        event_key="trend|original-funding",
        funding_pnl=0.2,
        funding_timestamp="2026-01-02T10:00:00+00:00",
    )
    _insert_ledger(
        store,
        event_key="trend-stop-repair-v1|trend|original-funding",
        funding_pnl=-0.2,
        position_generation="CORRECTION:trend|original-funding",
        funding_timestamp="2026-01-02T10:00:00+00:00",
    )
    _insert_ledger(store, event_key="trend|post-stop-funding", funding_pnl=0.0)
    correction = {
        "original_event_id": 1,
        "original_event_key": "trend|original-funding",
        "event_key": "trend-stop-repair-v1|trend|original-funding",
        "funding_timestamp": "2026-01-02T10:00:00+00:00",
        "venue": "paper",
        "instrument": "BTC/USDC:USDC",
        "amounts": {"trend_ls_20": -0.2},
        "funding_pnl": -0.2,
    }
    post_funding = {
        "reason": "funding_payment",
        "event_key": "trend|post-stop-funding",
        "payment_ts": "2026-01-02T11:00:00+00:00",
        "native_funding_rate": 0.00001,
        "reference_price": 1000.0,
        "reference_price_source": "observed_test_price",
        "reference_price_timestamp": "2026-01-02T10:00:00+00:00",
        "amounts": {"trend_ls_20": {"amount": 0.0, "qty": 0.0}},
        "funding_pnl": 0.0,
    }
    with store._transaction() as connection:
        store._insert_event(
            connection,
            "trend",
            "funding_payment",
            {"reason": "historical_stop_funding_reconciliation", "correction": correction},
            ts="2026-01-02T11:01:00+00:00",
        )
        store._insert_event(
            connection,
            "trend",
            "funding_payment",
            post_funding,
            ts="2026-01-02T12:01:00+00:00",
        )
    store.append_equity("trend", 195.0, "2026-01-02T11:00:30+00:00")
    store.append_equity("trend", 188.8, "2026-01-02T11:02:00+00:00")
    assert trade_id == 1
    assert stop_event_id > 0
    return store


def test_false_paper_stop_reversal_restores_cash_position_and_read_views(tmp_path):
    database = tmp_path / "state.db"
    store = _seed_false_stop(database)

    plan = build_plan(database, 1)
    assert plan["cash_restored"] == pytest.approx(90.001)
    assert plan["cash_delta"] == pytest.approx(1.201)
    assert plan["funding_pnl_restored"] == pytest.approx(0.201)
    assert plan["equity_rows_to_remove"] == 1
    assert plan["state_after"]["slots"]["trend_ls_20"]["position"]["bars_held"] == 8

    result = apply_plan(database, plan)
    assert result["already_applied"] is False
    assert HistoricalStateReader(database).read_trades() == []

    state = store.load_engine_state("trend")
    restored = state["slots"]["trend_ls_20"]
    assert restored["position"]["qty"] == pytest.approx(0.1)
    assert restored["position_timeline"][-1]["qty"] == pytest.approx(0.1)
    assert restored["cash"] == pytest.approx(90.001)
    assert restored["stop_order_id"] is None
    assert store.read_funding_ledger()[-1]["event_key"].startswith(
        "paper-trade-reversal-v1|funding|"
    )
    assert [r["ts"] for r in HistoricalStateReader(database).read_equity("trend")] == [
        "2026-01-02T11:00:30+00:00"
    ]

    with sqlite3.connect(database) as connection:
        event_types = [r[0] for r in connection.execute("SELECT event_type FROM events")]
    assert "trade_closed" in event_types
    assert "paper_trade_reversed" in event_types
    assert "external_stop_fill_observed" in event_types

    already = build_plan(database, 1)
    assert already["already_applied"] is True
    assert apply_plan(database, already)["already_applied"] is True
