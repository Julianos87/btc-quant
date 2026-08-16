"""Preuves du verrou paper → testnet."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from btcquant.execution.readiness import (
    ReadinessPolicy,
    _format_duration,
    _freshness_check,
    evaluate_readiness,
    finalize_campaign,
    require_passed_qualification,
    start_campaign,
    testnet_p1_policy as p1_policy,
)
from btcquant.execution.state_store import StateStore
from btcquant.execution.safety import require_live_execution_enabled


def _seed_healthy_campaign(store: StateStore, now: datetime) -> None:
    policy = replace(
        ReadinessPolicy(),
        min_observation_days=2,
        min_closed_trades=1,
        min_terminal_orders=1,
        min_terminal_orders_per_engine=1,
    )
    started = now - timedelta(days=2)
    start_campaign(
        store,
        policy,
        started_at=started.isoformat(),
    )
    sample = started
    while sample <= now:
        store.append_equity("trend", 6000.0, sample.isoformat())
        sample += timedelta(minutes=5)
    store.save_engine_state("trend", {"slots": {}, "halted": False})
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
        "trend",
        "strategy",
        "pending-order",
        "MARKET",
        "BUY",
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
    assert campaign["policy"]["required_engines"] == ["trend"]
    with pytest.raises(RuntimeError, match="déjà active"):
        start_campaign(store, ReadinessPolicy())


def test_uptime_uses_elapsed_time_not_daily_presence(tmp_path):
    store = StateStore(tmp_path / "state.db")
    now = datetime.now(UTC)
    policy = replace(
        ReadinessPolicy(),
        min_observation_days=1,
        min_closed_trades=0,
        min_terminal_orders=0,
        min_terminal_orders_per_engine=0,
    )
    started = now - timedelta(days=1)
    start_campaign(store, policy, started_at=started.isoformat())
    # Un seul point par date aurait satisfait l'ancien calcul.
    store.append_equity("trend", 1000.0, started.isoformat())
    store.append_equity("trend", 1000.0, now.isoformat())
    store.save_engine_state("trend", {"slots": {}, "halted": False})

    report = evaluate_readiness(store, now=now)
    uptime = next(item for item in report["checks"] if item["key"] == "trend_uptime")

    assert not uptime["passed"]
    assert float(uptime["value"].rstrip("%")) < 2.0


def test_intraday_drawdown_is_not_hidden_by_daily_close(tmp_path):
    store = StateStore(tmp_path / "state.db")
    now = datetime.now(UTC)
    policy = replace(
        ReadinessPolicy(),
        min_observation_days=0,
        min_engine_uptime=0.0,
        min_daily_sample_coverage=0.0,
        min_equity_coverage=0.0,
        min_closed_trades=0,
        min_terminal_orders=0,
        min_terminal_orders_per_engine=0,
        max_drawdown=-0.20,
    )
    started = now - timedelta(minutes=10)
    start_campaign(store, policy, started_at=started.isoformat())
    store.append_equity("trend", 1000.0, started.isoformat())
    store.append_equity("trend", 700.0, (started + timedelta(minutes=5)).isoformat())
    store.append_equity("trend", 1000.0, now.isoformat())
    store.save_engine_state("trend", {"slots": {}, "halted": False})

    report = evaluate_readiness(store, now=now)
    drawdown = next(item for item in report["checks"] if item["key"] == "drawdown")

    assert not drawdown["passed"]
    assert drawdown["value"] == "-30.0%"


def test_previous_protocol_pass_is_reported_as_expired(tmp_path):
    store = StateStore(tmp_path / "state.db")
    now = datetime.now(UTC)
    campaign = store.start_qualification_campaign(
        protocol_version=1,
        policy=ReadinessPolicy().to_dict(),
        started_at=(now - timedelta(days=1)).isoformat(),
    )
    store.finish_qualification_campaign(
        int(campaign["id"]),
        status="PASSED",
        ended_at=now.isoformat(),
        final_report={
            "status": "PASS",
            "ready": True,
            "checks": [],
            "n_ok": 0,
            "n_total": 0,
        },
    )

    report = evaluate_readiness(store, now=now)

    assert report["status"] == "FAIL"
    assert report["ready"] is False
    assert report["checks"][-1]["key"] == "protocol_version"


def test_active_previous_protocol_cannot_be_finalized(tmp_path):
    store = StateStore(tmp_path / "state.db")
    now = datetime.now(UTC)
    store.start_qualification_campaign(
        protocol_version=1,
        policy=replace(
            ReadinessPolicy(),
            min_observation_days=0,
            min_engine_uptime=0.0,
            min_daily_sample_coverage=0.0,
            min_equity_coverage=0.0,
            min_closed_trades=0,
            min_terminal_orders=0,
            min_terminal_orders_per_engine=0,
        ).to_dict(),
        started_at=now.isoformat(),
    )

    report = evaluate_readiness(store, now=now + timedelta(seconds=1))

    campaign = next(item for item in report["checks"] if item["key"] == "campaign")
    assert not campaign["passed"]
    with pytest.raises(RuntimeError, match="Qualification refusée"):
        finalize_campaign(store, now=now + timedelta(seconds=1))


def test_freshness_is_shown_in_seconds_or_minutes_not_zero_hours():
    fresh = _freshness_check("trend_freshness", "Fraîcheur moteur trend", 23.0, 600.0)
    stale = _freshness_check("trend_freshness", "Fraîcheur moteur trend", 900.0, 600.0)

    assert fresh.passed
    assert fresh.value == "23 s"
    assert fresh.target == "max 10 min"
    assert stale.passed is False
    assert stale.value == "15 min"
    assert _format_duration(0.4) == "0 s"


def test_testnet_p1_profile_requires_30_days_and_two_smoke_orders():
    policy = p1_policy()

    assert policy.required_engines == ("trend",)
    assert policy.min_observation_days == 30
    assert policy.min_engine_uptime == pytest.approx(0.995)
    assert policy.min_terminal_orders == 2
    assert policy.min_terminal_orders_per_engine == 2
    assert policy.min_closed_trades == 0
