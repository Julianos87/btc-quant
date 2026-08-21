from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import dashboard.app as dashboard
from btcquant.entrypoints import watchdog
from btcquant.execution.health import execution_safety_health
from btcquant.execution.readiness import (
    ServiceComponentProfile,
    evaluate_service_readiness,
)
from btcquant.execution.shadow import ShadowStore
from btcquant.execution.state_store import StateStore
from btcquant.observability import (
    BoundedReadCache,
    CachePolicy,
    Freshness,
    SourceSnapshot,
    temporal_skew,
)


class FakeClock:
    def __init__(self) -> None:
        self.mono = 0.0
        self.now = datetime(2026, 8, 13, 12, tzinfo=UTC)

    def tick(self, seconds: float) -> None:
        self.mono += seconds
        self.now += timedelta(seconds=seconds)


def test_snapshot_separates_observation_and_transport_time() -> None:
    observed = datetime(2026, 8, 13, 12, tzinfo=UTC)
    received = observed + timedelta(seconds=3)
    snapshot = SourceSnapshot.success(
        123.0,
        source="TEST",
        observed_at=observed,
        received_at=received,
        max_age_seconds=10,
    )
    assert snapshot.observed_at == observed
    assert snapshot.received_at == received
    assert snapshot.age_seconds == pytest.approx(3.0)
    assert snapshot.freshness is Freshness.FRESH


def test_bounded_cache_returns_stale_then_unavailable_after_max_stale() -> None:
    clock = FakeClock()
    cache: BoundedReadCache[float] = BoundedReadCache(
        monotonic=lambda: clock.mono,
        now=lambda: clock.now,
    )
    calls = 0

    def loader() -> float:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise ConnectionError("offline")
        return 100.0

    policy = CachePolicy(ttl_seconds=10, max_stale_seconds=30, max_age_seconds=5)
    fresh = cache.get("price", policy, loader, source="TEST", observed_at=lambda _value: clock.now)
    assert fresh.freshness is Freshness.FRESH
    clock.tick(11)
    stale = cache.get("price", policy, loader, source="TEST", observed_at=lambda _value: clock.now)
    assert stale.value == 100.0
    assert stale.freshness is Freshness.STALE
    assert stale.age_seconds == pytest.approx(11.0)
    assert stale.last_success_at == fresh.last_success_at
    clock.tick(20)
    unavailable = cache.get(
        "price", policy, loader, source="TEST", observed_at=lambda _value: clock.now
    )
    assert unavailable.value is None
    assert unavailable.freshness is Freshness.UNAVAILABLE


def test_cache_refresh_ttl_uses_monotonic_clock() -> None:
    clock = FakeClock()
    cache: BoundedReadCache[int] = BoundedReadCache(
        monotonic=lambda: clock.mono,
        now=lambda: clock.now,
    )
    calls = 0

    def loader() -> int:
        nonlocal calls
        calls += 1
        return calls

    policy = CachePolicy(ttl_seconds=10, max_stale_seconds=30, max_age_seconds=30)
    assert (
        cache.get("x", policy, loader, source="TEST", observed_at=lambda _value: clock.now).value
        == 1
    )
    clock.tick(9)
    assert (
        cache.get("x", policy, loader, source="TEST", observed_at=lambda _value: clock.now).value
        == 1
    )
    clock.tick(1)
    assert (
        cache.get("x", policy, loader, source="TEST", observed_at=lambda _value: clock.now).value
        == 2
    )


def test_temporal_skew_is_explicit() -> None:
    start = datetime(2026, 8, 13, 12, tzinfo=UTC)
    result = temporal_skew(
        {"price": start, "state": start + timedelta(seconds=4)},
        max_skew_seconds=3,
        now=start + timedelta(seconds=4),
    )
    assert result["max_source_skew_seconds"] == pytest.approx(4.0)
    assert result["freshness_status"] == "STALE"


def test_read_only_state_store_cannot_initialize_or_write(tmp_path: Path) -> None:
    database = tmp_path / "btcquant.db"
    store = StateStore(database)
    store.save_engine_state("trend", {"slots": {}})
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    read_store = StateStore(database, initialize=False, read_only=True)
    assert read_store.load_engine_state("trend") == {"slots": {}}
    assert read_store.integrity_check()
    with pytest.raises(sqlite3.OperationalError):
        with read_store._transaction() as connection:
            connection.execute("UPDATE metadata SET value = value WHERE key = 'schema_version'")
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


def test_read_only_shadow_store_does_not_create_or_write(tmp_path: Path) -> None:
    missing = tmp_path / "missing-shadow.db"
    with pytest.raises(FileNotFoundError):
        ShadowStore(missing, read_only=True)
    database = tmp_path / "execution-shadow.db"
    ShadowStore(database)
    # Initialize uses WAL. Hashing the main file before checkpoint races with
    # SQLite merging the WAL, especially when pytest runs as root inside
    # create-release. Checkpoint first so the byte comparison is about
    # read_only summary(), not a pending WAL flush.
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    assert ShadowStore(database, read_only=True).summary()["status"] == "SHADOW_PROXY_ONLY"
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


def test_service_readiness_defaults_to_required_trend_only(tmp_path: Path) -> None:
    database = tmp_path / "btcquant.db"
    store = StateStore(database)
    store.save_engine_state("trend", {"slots": {}})
    now = datetime.now(UTC)
    payload = evaluate_service_readiness(database, tmp_path / "missing-shadow.db", now=now)
    assert payload["kind"] == "SERVICE_READINESS"
    assert payload["ready"] is True
    assert "carry" in payload["optional_components"]
    assert "shadow" in payload["optional_components"]


def test_service_readiness_optional_and_required_stale_components(tmp_path: Path) -> None:
    database = tmp_path / "btcquant.db"
    store = StateStore(database)
    store.save_engine_state("trend", {"slots": {}})
    store.save_engine_state("carry", {"equity": 4000.0})
    stale_at = (datetime.now(UTC) - timedelta(seconds=1300)).isoformat()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE engine_state SET updated_at=? WHERE engine IN ('carry', 'trend')",
            (stale_at,),
        )
        connection.execute(
            "UPDATE engine_state SET updated_at=? WHERE engine='trend'",
            ((datetime.now(UTC) - timedelta(seconds=601)).isoformat(),),
        )
    optional = evaluate_service_readiness(database)
    assert optional["ready"] is False
    assert any(reason.startswith("REQUIRED_TREND_STALE") for reason in optional["reason_codes"])

    store.save_engine_state("trend", {"slots": {}})
    optional = evaluate_service_readiness(database)
    assert optional["ready"] is True
    carry = next(item for item in optional["components"] if item["name"] == "carry")
    assert carry["status"] == "STALE"

    shadow_path = tmp_path / "execution-shadow.db"
    ShadowStore(shadow_path).record_success(datetime.now(UTC) - timedelta(seconds=301))
    required_shadow = evaluate_service_readiness(
        database,
        shadow_path,
        profile=ServiceComponentProfile(required=("trend", "shadow"), optional=("carry",)),
    )
    assert required_shadow["ready"] is False
    assert any(
        reason.startswith("REQUIRED_SHADOW_STALE") for reason in required_shadow["reason_codes"]
    )


def test_service_readiness_explicit_required_carry_is_fail_closed(tmp_path: Path) -> None:
    database = tmp_path / "btcquant.db"
    store = StateStore(database)
    store.save_engine_state("trend", {"slots": {}})
    payload = evaluate_service_readiness(
        database,
        profile=ServiceComponentProfile(required=("trend", "carry"), optional=("shadow",)),
    )
    assert payload["ready"] is False
    assert any(reason.startswith("REQUIRED_CARRY_") for reason in payload["reason_codes"])


def test_healthz_is_process_liveness_without_exchange_access(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(dashboard, "STATE", tmp_path)

    def forbidden_exchange():
        raise AssertionError("healthz must not use exchange")

    monkeypatch.setattr(dashboard, "_hl", forbidden_exchange)
    response = dashboard.app.test_client().get(
        "/healthz",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )
    assert response.status_code == 200
    assert response.get_json() == {
        "api_schema_version": 2,
        "kind": "PROCESS_LIVENESS",
        "status": "ok",
    }


def test_dashboard_read_paths_do_not_modify_trading_db(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(dashboard, "STATE", tmp_path)
    monkeypatch.setattr(dashboard, "_cached", lambda *_args: None)
    store = StateStore(tmp_path / "btcquant.db")
    store.save_engine_state("trend", {"slots": {}})
    store.save_engine_state("carry", {"equity": 4000.0})
    before = hashlib.sha256((tmp_path / "btcquant.db").read_bytes()).hexdigest()
    client = dashboard.app.test_client()
    assert client.get("/api/summary").status_code == 200
    assert client.get("/api/operations").status_code == 200
    assert client.get("/metrics/prometheus").status_code == 200
    assert hashlib.sha256((tmp_path / "btcquant.db").read_bytes()).hexdigest() == before


def test_metrics_expose_unknown_safety_when_trading_db_is_unavailable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(dashboard, "STATE", tmp_path)
    (tmp_path / "btcquant.db").write_bytes(b"not sqlite")
    response = dashboard.app.test_client().get(
        "/metrics/prometheus",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert "btcquant_trading_db_available 0" in text
    assert "btcquant_execution_safety_unknown 1" in text
    assert "btcquant_open_critical_incidents 0" not in text


def test_watchdog_read_failure_records_unknown_incident(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "btcquant.db"
    StateStore(database).save_engine_state("trend", {"slots": {}})
    monkeypatch.setattr(
        watchdog,
        "execution_health",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ConnectionError("read failed")),
    )
    monkeypatch.setattr(watchdog, "notify", lambda _message: None)
    watchdog.main(["--database", str(database), "--service", "test-engine"])
    incidents = StateStore(database, initialize=False, read_only=True).read_incidents(
        open_only=True
    )
    assert any(item["kind"] == "watchdog_check_failed" for item in incidents)


def test_missing_reporting_equity_is_explicit(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(dashboard, "STATE", tmp_path)
    response = dashboard.app.test_client().get("/api/equity")
    assert response.status_code == 503
    assert response.get_json() == {"status": "SOURCE_UNAVAILABLE", "source": "reporting"}


def test_fx_failure_does_not_break_usd_summary_or_service_readiness(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(dashboard, "STATE", tmp_path)
    monkeypatch.setattr(dashboard, "_cache_snapshots", {})
    monkeypatch.setattr(dashboard, "_cached", lambda *_args: None)
    store = StateStore(tmp_path / "btcquant.db")
    store.save_engine_state("trend", {"slots": {}})
    store.save_engine_state("carry", {"equity": 4000.0})
    client = dashboard.app.test_client()
    summary = client.get("/api/summary")
    assert summary.status_code == 200
    payload = summary.get_json()
    assert payload["fx"]["eur_usd"] is None
    assert payload["fx"]["display_only"] is True
    assert payload["btc"]["freshness"] == "UNAVAILABLE"
    assert client.get("/readyz").status_code == 200


def test_future_source_timestamp_is_unknown_not_fresh() -> None:
    now = datetime(2026, 8, 13, 12, tzinfo=UTC)
    snapshot = SourceSnapshot.success(
        123.0,
        source="TEST",
        observed_at=now + timedelta(hours=1),
        received_at=now,
    )
    assert snapshot.freshness is Freshness.UNKNOWN
    assert snapshot.age_seconds is None
    assert snapshot.error == "CLOCK_SKEW"
    skew = temporal_skew(
        {"price": now + timedelta(hours=1), "state": now},
        max_skew_seconds=30,
        now=now,
    )
    assert skew["freshness_status"] == "UNKNOWN"


def test_read_only_missing_state_store_does_not_create_database(tmp_path: Path) -> None:
    database = tmp_path / "missing.db"
    with pytest.raises(FileNotFoundError):
        StateStore(database, read_only=True)
    assert not database.exists()


def test_read_only_wal_connection_refuses_all_sqlite_writes(tmp_path: Path) -> None:
    database = tmp_path / "btcquant.db"
    StateStore(database).save_engine_state("trend", {"slots": {}})
    read_store = StateStore(database, read_only=True)
    connection = read_store._connect()
    try:
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        statements = (
            "INSERT INTO metadata(key, value) VALUES ('x', 'y')",
            "UPDATE metadata SET value = 'changed' WHERE key = 'schema_version'",
            "DELETE FROM metadata WHERE key = 'schema_version'",
            "CREATE TABLE forbidden_write(id INTEGER)",
            "PRAGMA user_version = 123",
        )
        for statement in statements:
            with pytest.raises(sqlite3.OperationalError):
                connection.execute(statement)
    finally:
        connection.close()


def test_read_only_wal_reader_sees_later_writer_commit(tmp_path: Path) -> None:
    database = tmp_path / "btcquant.db"
    StateStore(database).save_engine_state("trend", {"marker": "A"})
    writer = sqlite3.connect(database)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=100000")
    try:
        assert StateStore(database, initialize=False, read_only=True).load_engine_state(
            "trend"
        ) == {"marker": "A"}
        writer.execute(
            "UPDATE engine_state SET payload=json(?) WHERE engine='trend'",
            ('{"marker":"B"}',),
        )
        writer.commit()
        wal_path = database.with_name(database.name + "-wal")
        assert wal_path.exists()
        assert StateStore(database, initialize=False, read_only=True).load_engine_state(
            "trend"
        ) == {"marker": "B"}
    finally:
        writer.close()


def _sqlite_semantic_snapshot(database: Path) -> dict[str, tuple[tuple[object, ...], ...]]:
    with sqlite3.connect(database) as connection:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
        return {
            table: tuple(
                tuple(row) for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid')
            )
            for table in tables
        }


def test_all_dashboard_get_routes_are_semantically_zero_write(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(dashboard, "STATE", tmp_path)
    monkeypatch.setattr(dashboard, "_cached", lambda *_args: None)
    database = tmp_path / "btcquant.db"
    store = StateStore(database)
    store.save_engine_state("trend", {"slots": {}})
    store.save_engine_state("carry", {"equity": 4000.0})
    shadow_database = tmp_path / "execution-shadow.db"
    ShadowStore(shadow_database).record_success(datetime.now(UTC))
    before_db = _sqlite_semantic_snapshot(database)
    before_shadow = _sqlite_semantic_snapshot(shadow_database)
    client = dashboard.app.test_client()
    environ = {"REMOTE_ADDR": "127.0.0.1"}
    paths = (
        "/healthz",
        "/readyz",
        "/api/operational-health",
        "/api/readiness",
        "/api/summary",
        "/metrics/prometheus",
    )
    for path in paths:
        response = client.get(path, environ_base=environ)
        assert response.status_code in (200, 503), path
    assert _sqlite_semantic_snapshot(database) == before_db
    assert _sqlite_semantic_snapshot(shadow_database) == before_shadow


def test_required_components_are_server_config_not_client_input(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(dashboard, "STATE", tmp_path)
    store = StateStore(tmp_path / "btcquant.db")
    store.save_engine_state("trend", {"slots": {}})
    monkeypatch.setenv("BTCQUANT_REQUIRED_ENGINES", "trend")
    client = dashboard.app.test_client()
    environ = {"REMOTE_ADDR": "127.0.0.1"}
    headers = {"X-BTCQuant-Required-Engines": "carry"}
    first = client.get(
        "/api/operational-health?carry_required=false&shadow_required=false",
        headers=headers,
        environ_base=environ,
    )
    second = client.get("/api/operational-health", environ_base=environ)
    assert first.get_json()["required_components"] == ["trend"]
    assert second.get_json()["required_components"] == ["trend"]


def test_invalid_required_component_config_fails_closed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(dashboard, "STATE", tmp_path)
    StateStore(tmp_path / "btcquant.db").save_engine_state("trend", {"slots": {}})
    monkeypatch.setenv("BTCQUANT_REQUIRED_ENGINES", "trend,false")
    response = dashboard.app.test_client().get(
        "/api/operational-health",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )
    assert response.status_code == 503
    assert "INVALID_REQUIRED_ENGINE_PROFILE" in response.get_json()["reason_codes"]


def test_prometheus_never_calls_network(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(dashboard, "STATE", tmp_path)
    StateStore(tmp_path / "btcquant.db").save_engine_state("trend", {"slots": {}})

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Prometheus must not call network")

    monkeypatch.setattr(dashboard, "_hl", forbidden)
    monkeypatch.setattr(dashboard, "_fx_ex", forbidden)
    response = dashboard.app.test_client().get(
        "/metrics/prometheus",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )
    assert response.status_code == 200


def test_shadow_read_only_uses_query_only_without_journal_mutation(tmp_path: Path) -> None:
    database = tmp_path / "shadow.db"
    ShadowStore(database).record_success(datetime.now(UTC))
    connection = ShadowStore(database, read_only=True)._connect()
    try:
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM shadow_runtime")
    finally:
        connection.close()


def test_watchdog_unknown_read_does_not_resolve_active_incident(
    tmp_path: Path, monkeypatch
) -> None:
    database = tmp_path / "btcquant.db"
    store = StateStore(database)
    store.save_engine_state("trend", {"slots": {}})
    store.record_incident(
        "engine:trend:stale",
        engine="trend",
        severity="CRITICAL",
        kind="engine_stale",
        message="stale",
    )
    monkeypatch.setattr(
        watchdog,
        "execution_health",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ConnectionError("read failed")),
    )
    monkeypatch.setattr(watchdog, "notify", lambda _message: None)
    watchdog.main(["--database", str(database), "--service", "test-engine"])
    incidents = StateStore(database, initialize=False, read_only=True).read_incidents(
        open_only=True
    )
    assert any(item["fingerprint"] == "engine:trend:stale" for item in incidents)


def test_watchdog_unavailable_database_is_explicit_nonzero(tmp_path: Path, monkeypatch) -> None:
    messages: list[str] = []
    monkeypatch.setattr(watchdog, "notify", messages.append)
    with pytest.raises(SystemExit) as stopped:
        watchdog.main(["--database", str(tmp_path / "missing.db")])
    assert stopped.value.code == 2
    assert messages and "UNKNOWN" in messages[0]


def test_optional_carry_safety_does_not_gate_service_readiness(tmp_path: Path) -> None:
    database = tmp_path / "btcquant.db"
    store = StateStore(database)
    store.save_engine_state("trend", {"slots": {}})
    store.save_engine_state(
        "carry",
        {"slots": {"carry": {"position": {"qty": 1.0}, "stop_order_id": None}}},
    )
    payload = evaluate_service_readiness(database)
    safety = payload["details"]["execution_safety"]
    assert safety["status"] == "FAIL"
    assert "CARRY_UNPROTECTED_POSITION" in safety["reasons"]
    assert payload["ready"] is False


def test_missing_source_timestamp_is_unknown_not_fresh() -> None:
    snapshot = SourceSnapshot.success(
        123.0,
        source="TEST",
        observed_at=None,
        received_at=datetime(2026, 8, 13, 12, tzinfo=UTC),
    )
    assert snapshot.freshness is Freshness.UNKNOWN
    assert snapshot.age_seconds is None
    assert snapshot.error == "OBSERVATION_TIMESTAMP_UNAVAILABLE"


def test_optional_carry_is_not_a_watchdog_critical_failure(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(watchdog, "STATE", tmp_path)
    monkeypatch.delenv("BTCQUANT_REQUIRED_ENGINES", raising=False)
    messages: list[str] = []
    monkeypatch.setattr(watchdog, "notify", messages.append)
    database = tmp_path / "btcquant.db"
    StateStore(database).save_engine_state("trend", {"slots": {}})
    watchdog.main(["--database", str(database)])
    assert not any("carry" in message.lower() for message in messages)
    incidents = StateStore(database, initialize=False, read_only=True).read_incidents(
        open_only=True
    )
    assert not any(item["engine"] == "carry" for item in incidents)


def test_future_market_timestamp_never_produces_live_valuation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(dashboard, "STATE", tmp_path)
    now = datetime.now(UTC)
    dashboard._cache_snapshots = {
        "price": SourceSnapshot.success(
            100.0,
            source="HYPERLIQUID",
            observed_at=now + timedelta(hours=1),
            received_at=now,
        ),
        "ohlcv1h": SourceSnapshot.unavailable(source="HYPERLIQUID_OHLCV_1H"),
        "funding": SourceSnapshot.unavailable(source="HYPERLIQUID_FUNDING"),
        "fx_eur": SourceSnapshot.unavailable(source="BINANCE_FX_DISPLAY_ONLY"),
    }
    monkeypatch.setattr(
        dashboard,
        "_cached",
        lambda key, _ttl, _fn: {"price": 100.0}.get(key),
    )
    store = StateStore(tmp_path / "btcquant.db")
    store.save_engine_state("trend", {"slots": {}})
    store.save_engine_state("carry", {"equity": 4000.0})
    payload = dashboard.app.test_client().get("/api/summary").get_json()
    assert payload["btc"]["freshness"] == "UNKNOWN"
    assert payload["btc"]["valuation_status"] == "UNKNOWN"
    assert payload["health"]["safety_status"] == "PASS"


def _clean_execution_store(path: Path) -> StateStore:
    store = StateStore(path)
    store.save_engine_state("trend", {"slots": {}})
    store.save_engine_state("carry", {"slots": {}, "reconciliation_required": False})
    return store


def test_execution_safety_contract_clean_state_is_pass(tmp_path: Path) -> None:
    store = _clean_execution_store(tmp_path / "btcquant.db")
    safety = execution_safety_health(store)
    assert safety.status.value == "PASS"
    assert safety.reasons == ()


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        (
            {"slots": {"carry": {"position": {"qty": 1.0}, "stop_order_id": None}}},
            "CARRY_UNPROTECTED_POSITION",
        ),
        ({"reconciliation_required": True}, "CARRY_RECONCILIATION_REQUIRED"),
    ],
)
def test_execution_safety_known_unsafe_state_is_fail(
    tmp_path: Path, state: dict, reason: str
) -> None:
    store = _clean_execution_store(tmp_path / "btcquant.db")
    store.save_engine_state("carry", state)
    safety = execution_safety_health(store)
    assert safety.status.value == "FAIL"
    assert reason in safety.reasons


def test_execution_safety_unbalanced_order_is_fail(tmp_path: Path) -> None:
    store = _clean_execution_store(tmp_path / "btcquant.db")
    order_id = store.begin_order(
        "carry",
        "carry",
        "safety-unbalanced",
        "MARKET",
        "BUY",
        1.0,
        "entry",
        reference_price=100.0,
    )
    store.complete_order(order_id, status="UNBALANCED", filled_qty=0.5, price=100.0)
    safety = execution_safety_health(store)
    assert safety.status.value == "FAIL"
    assert "CARRY_UNBALANCED" in safety.reasons


def test_execution_safety_read_failure_is_unknown(tmp_path: Path, monkeypatch) -> None:
    store = _clean_execution_store(tmp_path / "btcquant.db")
    import btcquant.execution.health as health_module

    monkeypatch.setattr(
        health_module,
        "execution_health",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ConnectionError("read failed")),
    )
    safety = execution_safety_health(store)
    assert safety.status.value == "UNKNOWN"
    assert "EXECUTION_SAFETY_UNKNOWN" in safety.reasons


def test_summary_safety_never_ignores_unprotected_position(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(dashboard, "STATE", tmp_path)
    monkeypatch.setattr(dashboard, "_cached", lambda *_args: None)
    store = _clean_execution_store(tmp_path / "btcquant.db")
    store.save_engine_state(
        "carry",
        {"slots": {"carry": {"position": {"qty": 1.0}, "stop_order_id": None}}},
    )
    payload = dashboard.app.test_client().get("/api/summary").get_json()
    assert payload["health"]["safety_status"] == "FAIL"
    assert payload["health"]["execution_safety"]["status"] == "FAIL"


def test_required_shadow_fresh_is_ready(tmp_path: Path) -> None:
    database = tmp_path / "btcquant.db"
    store = StateStore(database)
    store.save_engine_state("trend", {"slots": {}})
    shadow = tmp_path / "execution-shadow.db"
    ShadowStore(shadow).record_success(datetime(2026, 8, 13, 12, tzinfo=UTC))
    payload = evaluate_service_readiness(
        database,
        shadow,
        now=datetime(2026, 8, 13, 12, tzinfo=UTC),
        profile=ServiceComponentProfile(required=("trend", "shadow"), optional=("carry",)),
    )
    assert payload["checks"]["shadow_fresh"] is True
    assert payload["ready"] is True


def test_required_shadow_unavailable_is_not_ready(tmp_path: Path) -> None:
    database = tmp_path / "btcquant.db"
    StateStore(database).save_engine_state("trend", {"slots": {}})
    payload = evaluate_service_readiness(
        database,
        tmp_path / "missing-shadow.db",
        profile=ServiceComponentProfile(required=("trend", "shadow"), optional=("carry",)),
    )
    assert payload["checks"]["shadow_fresh"] is False
    assert payload["ready"] is False


def test_periodic_freshness_uses_lateness_after_expected_interval() -> None:
    start = datetime(2026, 8, 13, 12, tzinfo=UTC)
    for expected, age in ((3600.0, 50 * 60), (3600.0, 65 * 60), (14400.0, 3 * 3600)):
        now = start + timedelta(seconds=age)
        snapshot = SourceSnapshot.success(
            1.0,
            source="PERIODIC",
            observed_at=start,
            received_at=now,
            expected_interval_seconds=expected,
            max_age_seconds=600,
        ).at(
            now=now,
            expected_interval_seconds=expected,
            max_age_seconds=600,
            max_stale_seconds=900,
        )
        assert snapshot.freshness is Freshness.FRESH
        assert snapshot.age_seconds == pytest.approx(float(age))
        assert snapshot.freshness_lateness_seconds == pytest.approx(max(0.0, age - expected))

    late = start + timedelta(hours=1, seconds=601)
    stale = SourceSnapshot.success(
        1.0,
        source="HOURLY",
        observed_at=start,
        received_at=late,
        expected_interval_seconds=3600,
    ).at(
        now=late,
        expected_interval_seconds=3600,
        max_age_seconds=600,
        max_stale_seconds=900,
    )
    assert stale.freshness is Freshness.STALE
    unavailable_time = start + timedelta(hours=1, seconds=1501)
    unavailable = stale.at(
        now=unavailable_time,
        expected_interval_seconds=3600,
        max_age_seconds=600,
        max_stale_seconds=900,
    )
    assert unavailable.freshness is Freshness.UNAVAILABLE


def test_prometheus_recomputes_snapshot_age_without_network(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(dashboard, "STATE", tmp_path)
    _clean_execution_store(tmp_path / "btcquant.db")
    now = datetime.now(UTC)
    old = SourceSnapshot.success(
        1.0,
        source="HYPERLIQUID_OHLCV_1H",
        observed_at=now - timedelta(hours=1, seconds=601),
        received_at=now,
        expected_interval_seconds=3600,
    )
    dashboard._cache_snapshots = {"ohlcv1h": old}
    original = dashboard._cache_snapshot
    called: list[str] = []

    def recompute(key: str):
        called.append(key)
        return original(key)

    monkeypatch.setattr(dashboard, "_cache_snapshot", recompute)
    monkeypatch.setattr(dashboard, "_hl", lambda: (_ for _ in ()).throw(AssertionError("network")))
    monkeypatch.setattr(
        dashboard, "_fx_ex", lambda: (_ for _ in ()).throw(AssertionError("network"))
    )
    response = dashboard.app.test_client().get(
        "/metrics/prometheus",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )
    text = response.get_data(as_text=True)
    assert response.status_code == 200
    assert called == list(dashboard._CACHE_SOURCES)
    assert "btcquant_source_ohlcv1h_fresh 0" in text
    assert "btcquant_source_ohlcv1h_stale 1" in text


def test_frontend_safety_and_notification_contracts_are_explicit() -> None:
    javascript = (Path(__file__).resolve().parents[1] / "dashboard/static/dashboard.js").read_text(
        encoding="utf-8"
    )
    assert 'safety === "FAIL"' in javascript
    assert 'safety === "UNKNOWN"' in javascript
    assert "trendConfirmed" in javascript
    assert "carryConfirmed" in javascript
    assert "trendAlive" not in javascript
    assert "carryAlive" not in javascript
