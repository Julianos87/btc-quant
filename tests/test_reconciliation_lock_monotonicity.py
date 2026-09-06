"""Regression tests for the durable reconciliation safety latch."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from btcquant.execution.broker import PaperBroker
from btcquant.execution.errors import ReconciliationRequired
from btcquant.execution.financial_application_plan import (
    FinancialApplicationPlan,
    sha256_json,
)
from btcquant.execution.order_state import FinancialTransitionType, LogicalOrderIdentity
from btcquant.execution.runner import LiveRunner
from btcquant.execution.state_store import StateStore
from btcquant.risk import RiskConfig


def _state(*, reconciliation_required: bool, marker: str = "state") -> dict:
    return {
        "slots": {},
        "marker": marker,
        "peak_equity": 1_000.0,
        "halted": False,
        "day": None,
        "day_start_equity": 1_000.0,
        "daily_lockout": False,
        "reconciliation_required": reconciliation_required,
        "last_funding_ts": None,
    }


def _modern_state(*, reconciliation_required: bool) -> dict:
    return {
        "slots": {
            "slot": {
                "cash": 1_000.0,
                "position": None,
                "stop_order_id": None,
                "stop_order_local_id": None,
                "stop_intent_id": None,
                "stop_transition": None,
                "entry_fee": 0.0,
                "last_bar_ts": None,
                "financial_transition_seq": 0,
            }
        },
        "peak_equity": 1_000.0,
        "halted": False,
        "day": None,
        "day_start_equity": 1_000.0,
        "daily_lockout": False,
        "reconciliation_required": reconciliation_required,
        "last_funding_ts": None,
        "stop_protection_mode": "SOFTWARE",
    }


def _store(tmp_path) -> StateStore:
    return StateStore(tmp_path / "btcquant.db")


def _runner(state_path) -> LiveRunner:
    risk = RiskConfig(
        initial_capital=1_000.0,
        risk_per_trade=0.01,
        max_position_pct=0.95,
        vol_target_annual=None,
        max_drawdown_halt=0.5,
        daily_loss_limit=None,
    )
    runner = LiveRunner(
        [],
        PaperBroker(),
        risk,
        "binance",
        "BTC/USDT",
        state_path,
    )
    runner.notifier = lambda _message: None
    runner.peak_equity = 1_000.0
    runner.day_start_equity = 1_000.0
    return runner


def test_false_to_false_checkpoint_remains_unlocked(tmp_path):
    store = _store(tmp_path)

    store.save_engine_state("trend", _state(reconciliation_required=False, marker="first"))
    store.save_engine_state("trend", _state(reconciliation_required=False, marker="second"))

    persisted = store.load_engine_state("trend")
    assert persisted is not None
    assert persisted["reconciliation_required"] is False
    assert persisted["marker"] == "second"


def test_false_to_true_uses_the_modern_manual_reconciliation_path(tmp_path):
    runner = _runner(tmp_path / "state.json")

    with pytest.raises(ReconciliationRequired, match="monotonicity"):
        runner._require_manual_reconciliation("monotonicity")

    persisted = runner.store.load_engine_state("trend")
    assert persisted is not None
    assert persisted["reconciliation_required"] is True


def test_stale_generic_false_checkpoint_cannot_clear_durable_lock(tmp_path):
    store = _store(tmp_path)
    store.save_engine_state("trend", _state(reconciliation_required=True, marker="locked"))

    store.save_engine_state("trend", _state(reconciliation_required=False, marker="stale"))

    persisted = store.load_engine_state("trend")
    assert persisted is not None
    assert persisted["reconciliation_required"] is True
    assert persisted["marker"] == "stale"


def test_unrelated_checkpoint_cannot_clear_durable_lock(tmp_path):
    store = _store(tmp_path)
    store.save_engine_state("trend", _state(reconciliation_required=True, marker="locked"))

    store.save_engine_state("trend", {"slots": {}, "marker": "unrelated"})

    persisted = store.load_engine_state("trend")
    assert persisted is not None
    assert persisted["reconciliation_required"] is True
    assert persisted["marker"] == "unrelated"


def test_only_hash_checked_qualified_resolution_can_clear_lock(tmp_path):
    store = _store(tmp_path)
    locked = _state(reconciliation_required=True, marker="locked")
    store.save_engine_state("trend", locked)
    durable = store.load_engine_state("trend")
    assert durable is not None

    store.save_engine_state_after_reconciliation(
        "trend",
        _state(reconciliation_required=False, marker="resolved"),
        expected_state_sha256=sha256_json(durable),
        resolution="operator_position_reconciled",
    )

    persisted = store.load_engine_state("trend")
    assert persisted is not None
    assert persisted["reconciliation_required"] is False
    assert persisted["marker"] == "resolved"
    assert store.read_events("trend")[-1]["event_type"] == "reconciliation_resolved"


def test_stale_qualified_resolution_cannot_clear_newer_state(tmp_path):
    store = _store(tmp_path)
    store.save_engine_state("trend", _state(reconciliation_required=True, marker="locked"))
    durable = store.load_engine_state("trend")
    assert durable is not None
    store.save_engine_state("trend", _state(reconciliation_required=True, marker="newer"))

    with pytest.raises(ReconciliationRequired, match="modifié"):
        store.save_engine_state_after_reconciliation(
            "trend",
            _state(reconciliation_required=False, marker="resolved"),
            expected_state_sha256=sha256_json(durable),
            resolution="stale_operator_proof",
        )

    persisted = store.load_engine_state("trend")
    assert persisted is not None
    assert persisted["reconciliation_required"] is True
    assert persisted["marker"] == "newer"


def test_restart_preserves_lock_and_blocks_runner_startup(tmp_path):
    state_path = tmp_path / "state.json"
    runner = _runner(state_path)
    with pytest.raises(ReconciliationRequired):
        runner._require_manual_reconciliation("restart lock")

    with pytest.raises(ReconciliationRequired, match="démarrage interdit"):
        _runner(state_path)


def test_concurrent_true_and_stale_false_checkpoints_converge_to_locked(tmp_path):
    database = tmp_path / "btcquant.db"
    first = StateStore(database)
    first.save_engine_state("trend", _state(reconciliation_required=False, marker="initial"))
    barrier = Barrier(2)

    def checkpoint(required: bool, marker: str) -> None:
        store = StateStore(database)
        barrier.wait()
        store.save_engine_state(
            "trend",
            _state(reconciliation_required=required, marker=marker),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (
            executor.submit(checkpoint, True, "reconciliation"),
            executor.submit(checkpoint, False, "stale"),
        )
        for future in futures:
            future.result()

    persisted = first.load_engine_state("trend")
    assert persisted is not None
    assert persisted["reconciliation_required"] is True


def test_concurrent_qualified_resolution_and_stale_checkpoint_never_unlocks_unqualified(
    tmp_path,
):
    store = _store(tmp_path)
    locked = _state(reconciliation_required=True, marker="locked")
    store.save_engine_state("trend", locked)
    durable = store.load_engine_state("trend")
    assert durable is not None
    barrier = Barrier(2)

    def stale_checkpoint() -> None:
        barrier.wait()
        store.save_engine_state(
            "trend",
            _state(reconciliation_required=False, marker="stale"),
        )

    def qualified_resolution() -> str:
        barrier.wait()
        try:
            store.save_engine_state_after_reconciliation(
                "trend",
                _state(reconciliation_required=False, marker="resolved"),
                expected_state_sha256=sha256_json(durable),
                resolution="concurrent_operator_proof",
            )
        except ReconciliationRequired:
            return "rejected_as_stale"
        return "resolved"

    with ThreadPoolExecutor(max_workers=2) as executor:
        stale_future = executor.submit(stale_checkpoint)
        resolution_future = executor.submit(qualified_resolution)
        stale_future.result()
        resolution = resolution_future.result()

    persisted = store.load_engine_state("trend")
    assert persisted is not None
    if resolution == "resolved":
        assert persisted["reconciliation_required"] is False
    else:
        assert resolution == "rejected_as_stale"
        assert persisted["reconciliation_required"] is True


def test_checkpoint_rollback_preserves_previous_lock(tmp_path, monkeypatch):
    store = _store(tmp_path)
    original = _state(reconciliation_required=True, marker="locked")
    store.save_engine_state("trend", original)
    event_count = len(store.read_events("trend"))

    def fail_event(*_args, **_kwargs):
        raise RuntimeError("event write failed")

    monkeypatch.setattr(store, "_insert_event", fail_event)
    with pytest.raises(RuntimeError, match="event write failed"):
        store.save_engine_state(
            "trend",
            _state(reconciliation_required=False, marker="stale"),
        )

    assert store.load_engine_state("trend") == original
    assert len(store.read_events("trend")) == event_count


def test_base_exception_rollback_preserves_previous_lock(tmp_path, monkeypatch):
    class PowerLoss(BaseException):
        pass

    store = _store(tmp_path)
    original = _state(reconciliation_required=True, marker="locked")
    store.save_engine_state("trend", original)

    def fail_event(*_args, **_kwargs):
        raise PowerLoss()

    monkeypatch.setattr(store, "_insert_event", fail_event)
    with pytest.raises(PowerLoss):
        store.save_engine_state(
            "trend",
            _state(reconciliation_required=False, marker="stale"),
        )

    assert store.load_engine_state("trend") == original


def test_reconciliation_lock_blocks_new_modern_intent(tmp_path):
    store = _store(tmp_path)
    store.save_engine_state("trend", _modern_state(reconciliation_required=True))
    identity = LogicalOrderIdentity(
        "trend",
        "slot",
        "2026-09-01T00:00:00Z",
        FinancialTransitionType.ENTER_LONG,
    )
    plan = FinancialApplicationPlan(
        identity=identity,
        side="BUY",
        requested_qty=1.0,
        reference_price=100.0,
        reason="entry",
        reduce_only=False,
        planned_effect_at="2026-09-01T00:00:00Z",
        pre_state_payload=_modern_state(reconciliation_required=False),
        protection_mode="SOFTWARE",
        entry_direction=1,
        entry_stop_price=90.0,
    )

    with pytest.raises(ReconciliationRequired, match="marqué"):
        store.reserve_market_order_with_application_plan(identity, plan=plan)

    assert store.read_orders("trend") == []
