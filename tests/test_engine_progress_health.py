"""Fresh process heartbeats cannot certify stalled strategy execution."""

from datetime import UTC, datetime, timedelta

import pytest

from btcquant.execution.health import execution_health, trend_progress_health
from btcquant.execution.operational_state_reader import OperationalStateReader
from btcquant.execution.readiness import evaluate_service_readiness
from btcquant.execution.state_store import StateStore

NAMES = ("trend_ls_20", "trend_ls_55", "trend_ls_100")


def checkpoint(store, bar):
    store.save_engine_state(
        "trend", {"slots": {name: {"last_bar_ts": bar, "position": None} for name in NAMES}}
    )


def test_fresh_heartbeat_does_not_hide_days_of_stalled_bars(tmp_path):
    store = StateStore(tmp_path / "btcquant.db")
    checkpoint(store, "2026-10-28T04:00:00+00:00")
    now = datetime.now(UTC)
    progress = trend_progress_health(store, now=now)
    assert progress["heartbeat_age_seconds"] < 2
    assert progress["status"] == "STALE"
    assert all(row["lag_seconds"] > 86400 for row in progress["slots"])
    readiness = evaluate_service_readiness(str(store.path), now=now)
    assert readiness["checks"]["trend_fresh"]
    assert not readiness["ready"]
    assert not readiness["checks"]["trend_progression"]


@pytest.mark.parametrize("second,status", [(19, "FRESH"), (20, "STALE"), (21, "STALE")])
def test_bar_close_deadline_respects_configured_buffer(tmp_path, second, status):
    store = StateStore(tmp_path / "btcquant.db")
    checkpoint(store, "2026-11-01T00:00:00+00:00")
    progress = trend_progress_health(store, now=datetime(2026, 11, 1, 8, 0, second, tzinfo=UTC))
    assert progress["status"] == status
    assert progress["last_available_bar_ts"] is None


def test_checkpoint_from_bar_still_inside_poll_buffer_is_not_fresh(tmp_path):
    store = StateStore(tmp_path / "btcquant.db")
    checkpoint(store, "2026-11-01T04:00:00+00:00")
    progress = trend_progress_health(store, now=datetime(2026, 11, 1, 8, 0, 19, tzinfo=UTC))
    assert progress["status"] == "UNKNOWN"


def test_missing_future_or_naive_checkpoint_is_not_fresh(tmp_path):
    store = StateStore(tmp_path / "btcquant.db")
    for bar in (None, "broken", "2026-11-02T00:00:00+00:00", "2026-11-01T04:00:00"):
        checkpoint(store, bar)
        assert (
            trend_progress_health(store, now=datetime(2026, 11, 1, 8, 1, tzinfo=UTC))["status"]
            == "UNKNOWN"
        )


def test_configured_timeframe_and_tolerance_are_used(tmp_path):
    store = StateStore(tmp_path / "btcquant.db")
    checkpoint(store, "2026-11-01T06:00:00+00:00")
    config = {
        "strategies": {NAMES[0]: {"timeframe": "1h"}},
        "execution": {"poll_buffer_seconds": 45},
    }
    assert (
        trend_progress_health(
            store, config=config, now=datetime(2026, 11, 1, 8, 0, 44, tzinfo=UTC)
        )["status"]
        == "FRESH"
    )
    assert (
        trend_progress_health(
            store, config=config, now=datetime(2026, 11, 1, 8, 0, 45, tzinfo=UTC)
        )["status"]
        == "STALE"
    )


def protected_state(store, broker_id="paper-stop-1"):
    order_id = store.begin_order(
        "trend", NAMES[0], "current-stop-intent", "STOP", "SELL", 1, "protect", reference_price=90
    )
    store.complete_order(order_id, status="OPEN", broker_order_id=broker_id)
    slot = {
        "position": {
            "entry_time": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
            "qty": 1,
            "direction": 1,
            "stop_price": 90,
        },
        "stop_order_id": broker_id,
        "stop_order_local_id": order_id,
        "stop_intent_id": "current-stop-intent",
    }
    store.save_engine_state(
        "trend", {"stop_protection_mode": "EXCHANGE", "slots": {NAMES[0]: slot}}
    )
    return slot


def test_current_durable_open_stop_proves_protection(tmp_path):
    store = StateStore(tmp_path / "btcquant.db")
    protected_state(store)
    health = execution_health(store, "trend")
    assert not health.unprotected_slots
    assert dict(health.slot_protection)[NAMES[0]] == "EXCHANGE_STOP_CONFIRMED"


def test_old_filled_order_collision_invalidates_current_stop(tmp_path):
    store = StateStore(tmp_path / "btcquant.db")
    old = store.begin_order(
        "trend", NAMES[0], "old-fill-intent", "STOP", "SELL", 1, "old", reference_price=90
    )
    store.complete_order(
        old, status="FILLED", broker_order_id="paper-stop-1", filled_qty=1, price=90
    )
    protected_state(store)
    health = execution_health(store, "trend")
    assert health.unprotected_slots == (NAMES[0],)
    assert dict(health.slot_protection)[NAMES[0]] == "EXCHANGE_STOP_ID_COLLISION"


@pytest.mark.parametrize(
    "field,value",
    [("stop_intent_id", "wrong"), ("stop_order_local_id", 1234), ("stop_order_local_id", None)],
)
def test_id_without_position_order_binding_is_unverified(tmp_path, field, value):
    store = StateStore(tmp_path / "btcquant.db")
    slot = protected_state(store)
    slot[field] = value
    store.save_engine_state(
        "trend", {"stop_protection_mode": "EXCHANGE", "slots": {NAMES[0]: slot}}
    )
    assert execution_health(store, "trend").unprotected_slots == (NAMES[0],)


def test_watchdog_records_deduplicated_progress_incident_without_real_notification(
    tmp_path, monkeypatch
):
    from btcquant.entrypoints import watchdog

    store = StateStore(tmp_path / "btcquant.db")
    checkpoint(store, "2026-10-28T04:00:00+00:00")
    notifications = []
    monkeypatch.setattr(watchdog, "notify", notifications.append)
    for _ in range(2):
        watchdog.main(["--database", str(store.path), "--service", "test-trend"])
    incidents = OperationalStateReader(store.path).read_incidents(open_only=True)
    assert any(item["kind"] == "engine_progression_stalled" for item in incidents)
    assert sum("Progression" in message for message in notifications) == 1


def test_readiness_is_separate_from_process_liveness(tmp_path, monkeypatch):
    from dashboard import app as dashboard

    store = StateStore(tmp_path / "btcquant.db")
    checkpoint(store, "2026-10-28T04:00:00+00:00")
    monkeypatch.setattr(dashboard, "STATE", tmp_path)
    client = dashboard.app.test_client()
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 503
