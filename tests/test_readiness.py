"""Preuves du verrou paper → testnet."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from btcquant.execution.readiness import (
    ReadinessPolicy,
    evaluate_readiness,
    finalize_campaign,
    require_passed_qualification,
    start_campaign,
)
from btcquant.execution.state_store import StateStore
from btcquant.execution.safety import require_live_execution_enabled


def _seed_healthy_campaign(store: StateStore, now: datetime) -> None:
    policy = replace(
        ReadinessPolicy(),
        min_observation_days=2,
        min_closed_trades=1,
        min_terminal_orders=2,
        min_terminal_orders_per_engine=1,
    )
    start_campaign(
        store,
        policy,
        started_at=(now - timedelta(days=2)).isoformat(),
    )
    for offset in (2, 1, 0):
        ts = (now - timedelta(days=offset)).isoformat()
        store.append_equity("trend", 6000.0 + offset, ts)
        store.append_equity("carry", 4000.0, ts)
    store.save_engine_state("trend", {"slots": {}, "halted": False})
    store.save_engine_state("carry", {"equity": 4000.0, "halted": False})
    order_id = store.begin_order(
        "trend",
        "strategy",
        "qualification-order",
        "MARKET",
        "BUY",
        1.0,
        "qualification",
        reference_price=100.0,
    )
    store.complete_order(order_id, status="FILLED", filled_qty=1.0, price=100.05)
    carry_order_id = store.begin_order(
        "carry",
        "carry",
        "qualification-carry-order",
        "MARKET",
        "SELL",
        1.0,
        "qualification",
        reference_price=100.0,
    )
    store.complete_order(
        carry_order_id,
        status="FILLED",
        filled_qty=1.0,
        price=99.95,
    )
    store.record_trade(
        {
            "exit_ts": now.isoformat(),
            "entry_ts": (now - timedelta(hours=4)).isoformat(),
            "strategy": "trend_ls_20",
            "direction": "LONG",
            "qty": 1.0,
            "entry_price": 100.0,
            "exit_price": 101.0,
            "pnl": 1.0,
            "bars_held": 1,
            "reason": "test",
        }
    )


def test_readiness_fails_closed_without_campaign(tmp_path):
    report = evaluate_readiness(StateStore(tmp_path / "state.db"))

    assert report["status"] == "FAIL"
    assert report["campaign_status"] == "NOT_STARTED"
    assert report["checks"][0]["key"] == "campaign"


def test_healthy_campaign_can_be_finalized_and_unlocks_qualification(tmp_path):
    store = StateStore(tmp_path / "state.db")
    now = datetime.now(UTC)
    _seed_healthy_campaign(store, now)

    report = evaluate_readiness(store, now=now, persist=True)
    assert report["status"] == "PASS"
    assert store.latest_readiness_report()["status"] == "PASS"

    final_report = finalize_campaign(store, now=now)
    assert final_report["status"] == "PASS"
    assert store.active_qualification_campaign() is None
    assert require_passed_qualification(store)["status"] == "PASS"
    with pytest.raises(RuntimeError, match="testnet non confirmé"):
        require_live_execution_enabled(testnet=True, state_path=store.path)
    with pytest.raises(RuntimeError, match="argent réel reste désactivée"):
        require_live_execution_enabled(testnet=False, state_path=store.path)

    expired = evaluate_readiness(store, now=now + timedelta(days=8))
    assert expired["status"] == "FAIL"
    assert expired["checks"][-1]["key"] == "qualification_age"

    start_campaign(store, replace(ReadinessPolicy(), min_observation_days=1))
    with pytest.raises(RuntimeError, match="nouvelle campagne est en cours"):
        require_passed_qualification(store)


def test_rejection_and_open_incident_block_finalization(tmp_path):
    store = StateStore(tmp_path / "state.db")
    now = datetime.now(UTC)
    _seed_healthy_campaign(store, now)
    rejected_id = store.begin_order(
        "trend",
        "strategy",
        "rejected-order",
        "MARKET",
        "BUY",
        1.0,
        "qualification",
        reference_price=100.0,
    )
    store.complete_order(rejected_id, status="REJECTED", error="exchange rejected")
    store.record_incident(
        "qualification:blocker",
        severity="CRITICAL",
        kind="test",
        message="blocker",
    )

    report = evaluate_readiness(store, now=now)
    failed = {item["key"] for item in report["checks"] if not item["passed"]}
    assert {"incidents", "rejections"}.issubset(failed)
    with pytest.raises(RuntimeError, match="Qualification refusée"):
        finalize_campaign(store, now=now)


def test_unresolved_order_blocks_campaign(tmp_path):
    store = StateStore(tmp_path / "state.db")
    now = datetime.now(UTC)
    _seed_healthy_campaign(store, now)
    store.begin_order(
        "carry",
        "carry",
        "pending-order",
        "MARKET",
        "SELL",
        1.0,
        "qualification",
        reference_price=100.0,
    )

    report = evaluate_readiness(store, now=now)
    unresolved = next(item for item in report["checks"] if item["key"] == "unresolved")
    assert not unresolved["passed"]
    assert unresolved["value"] == "1"


def test_campaign_policy_is_snapshotted(tmp_path):
    store = StateStore(tmp_path / "state.db")
    policy = replace(ReadinessPolicy(), min_observation_days=12)
    campaign = start_campaign(store, policy)

    assert campaign["policy"]["min_observation_days"] == 12
    with pytest.raises(RuntimeError, match="déjà active"):
        start_campaign(store, ReadinessPolicy())
