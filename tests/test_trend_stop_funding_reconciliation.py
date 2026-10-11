"""Historical funding corrections must be proven, additive and restart safe."""

import copy
from unittest.mock import patch

import pandas as pd
import pytest

from btcquant.execution.broker import PaperBroker, ProtectiveOrderSnapshot
from btcquant.execution.protective_stops import StopDecision, StopDecisionKind
from btcquant.execution.state_store import StateStore
from scripts.reconcile_trend_stop_funding import build_plan, apply_plan
from test_protective_stop_saga import _runner, _position


def legacy_database(tmp_path, filled=1.0):
    path = tmp_path / "trend.db"
    runner, slot = _runner(path, PaperBroker(simulate_exchange_stops=True))
    runner.notifier = lambda _: True
    slot.position = _position()
    slot.position_timeline = [
        {
            "effective_at": slot.position.entry_time.isoformat(),
            "intent_id": "entry",
            "qty": 1.0,
            "direction": 1,
            "generation": runner._position_generation(slot.position),
        }
    ]
    runner._save_state()
    record = runner.store.record_observed_fill_and_checkpoint

    def legacy_stop(**kwargs):
        kwargs["state"] = copy.deepcopy(kwargs["state"])
        kwargs["state"]["slots"][slot.strategy.name]["position_timeline"] = slot.position_timeline[
            :1
        ]
        return record(**kwargs)

    runner.store.record_observed_fill_and_checkpoint = legacy_stop
    runner._materialize_filled_stop(
        slot,
        StopDecision(
            StopDecisionKind.FILLED if filled == 1 else StopDecisionKind.PARTIAL_FILLED,
            previous_stop_id="old-stop",
            snapshot=ProtectiveOrderSnapshot(
                "old-stop", "FILLED" if filled == 1 else "PARTIAL", 1, filled, 0, 90
            ),
        ),
    )
    state = runner.store.load_engine_state("trend")
    # The repair is intentionally offline/flat-only; the partial scenario is
    # tested by the runner transition suite, not by inventing a flat checkpoint.
    timestamp = (pd.Timestamp.now(tz="UTC") + pd.Timedelta(hours=1)).isoformat()
    key = "trend|fixture|BTC/USDT|" + timestamp
    ledger = {
        "event_key": key,
        "venue": "fixture",
        "instrument": "BTC/USDT",
        "funding_timestamp": timestamp,
        "native_funding_rate": 0.01,
        "position_generation": "legacy",
        "funding_notional": 100.0,
        "funding_notional_price": 100.0,
        "funding_notional_price_source": "fixture",
        "funding_notional_price_timestamp": timestamp,
        "funding_pnl": -1.0,
        "borrow_principal": 0.0,
        "borrow_rate_ann": 0.0,
        "borrow_dt_seconds": 0.0,
        "borrow_cost": 0.0,
        "applied_at": timestamp,
    }
    state["slots"][slot.strategy.name]["cash"] -= 1
    runner.store.apply_carry_accounting_event_and_checkpoint(
        ledger,
        state,
        engine="trend",
        event_payload={
            "event_key": key,
            "payment_ts": timestamp,
            "native_funding_rate": 0.01,
            "reference_price": 100.0,
            "funding_pnl": -1.0,
            "amounts": {slot.strategy.name: {"amount": 1.0, "qty": 1.0, "direction": 1}},
        },
    )
    return path, slot.strategy.name, key


def test_historical_stop_refund_is_additive_and_idempotent(tmp_path):
    path, name, key = legacy_database(tmp_path)
    store = StateStore(path, initialize=False)
    before = store.load_engine_state("trend")["slots"][name]["cash"]
    plan = build_plan(path)
    assert plan["cash_adjustments"][name] == pytest.approx(1)
    assert len(plan["stop_evidence"]) == 1
    after = apply_plan(path, plan)
    assert not after["adjustments"] and not after["stop_evidence"]
    assert store.load_engine_state("trend")["slots"][name]["cash"] == pytest.approx(before + 1)
    assert sum(r["funding_pnl"] for r in store.read_funding_ledger()) == pytest.approx(0)
    original = [r for r in store.read_funding_ledger() if r["event_key"] == key][0]
    assert original["funding_pnl"] == -1
    apply_plan(path, build_plan(path))
    assert store.load_engine_state("trend")["slots"][name]["cash"] == pytest.approx(before + 1)


def test_crash_after_timeline_repair_leaves_refund_replayable_once(tmp_path):
    path, name, _ = legacy_database(tmp_path)
    before = StateStore(path, initialize=False).load_engine_state("trend")["slots"][name]["cash"]
    with patch.object(
        StateStore, "apply_carry_accounting_event_and_checkpoint", side_effect=OSError("disk error")
    ):
        with pytest.raises(OSError):
            apply_plan(path, build_plan(path))
    plan = build_plan(path)
    assert not plan["stop_evidence"] and len(plan["adjustments"]) == 1
    apply_plan(path, plan)
    assert StateStore(path, initialize=False).load_engine_state("trend")["slots"][name][
        "cash"
    ] == pytest.approx(before + 1)


def test_stale_reconciliation_plan_cannot_overwrite_newer_checkpoint(tmp_path):
    path, name, _ = legacy_database(tmp_path)
    plan = build_plan(path)
    store = StateStore(path, initialize=False)
    state = store.load_engine_state("trend")
    state["slots"][name]["cash"] += 1
    store.save_engine_state("trend", state)
    with pytest.raises(ValueError, match="changed"):
        apply_plan(path, plan)
