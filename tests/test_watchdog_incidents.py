"""Le watchdog alerte une fois par incident, puis suit sa résolution."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from btcquant.execution.shadow import ShadowStore
from btcquant.execution.state_store import StateStore
from btcquant.entrypoints import watchdog


def test_watchdog_deduplicates_and_resolves_execution_alerts(tmp_path, monkeypatch):
    monkeypatch.setattr(watchdog, "STATE", tmp_path)
    monkeypatch.setattr(watchdog, "CHECKS", [("trend", 600, "btcquant-trend")])
    messages: list[str] = []
    monkeypatch.setattr(watchdog, "notify", messages.append)

    store = StateStore(tmp_path / "btcquant.db")
    store.save_engine_state("trend", {"slots": {}})
    order_id = store.begin_order(
        "trend",
        "strategy",
        "watchdog-unbalanced",
        "MARKET",
        "BUY",
        1.0,
        "entry",
        reference_price=100.0,
    )
    store.complete_order(
        order_id,
        status="UNBALANCED",
        filled_qty=0.5,
        price=100.0,
    )

    watchdog.main([])
    watchdog.main([])

    assert len(messages) == 1
    assert "UNBALANCED" in messages[0]
    incident = next(
        item for item in store.read_incidents(open_only=True) if item["kind"] == "unbalanced_orders"
    )
    assert incident["occurrences"] == 2

    store.complete_order(order_id, status="RECOVERED_ABORTED")
    watchdog.main([])

    assert messages == [messages[0]]
    assert not any(
        item["kind"] == "unbalanced_orders" for item in store.read_incidents(open_only=True)
    )


def test_watchdog_can_monitor_isolated_testnet_database(tmp_path, monkeypatch):
    messages: list[str] = []
    monkeypatch.setattr(watchdog, "notify", messages.append)
    database = tmp_path / "btcquant-testnet.db"
    StateStore(database).save_engine_state(
        "trend",
        {
            "slots": {
                "trend_ls_55": {
                    "position": {"qty": 0.01},
                    "stop_order_id": None,
                    "stop_transition": None,
                }
            }
        },
    )

    watchdog.main(
        [
            "--database",
            str(database),
            "--service",
            "btcquant-hyperliquid-testnet",
        ]
    )

    assert any("sans stop" in message for message in messages)


def test_watchdog_monitors_the_carry_engine(tmp_path, monkeypatch):
    """Le carry était absent de CHECKS : un moteur carry figé n'était jamais
    détecté, alors qu'il porte 40 % du portefeuille."""
    monkeypatch.setattr(watchdog, "STATE", tmp_path)
    monkeypatch.setenv("BTCQUANT_REQUIRED_ENGINES", "trend,carry")
    messages: list[str] = []
    monkeypatch.setattr(watchdog, "notify", messages.append)

    assert [engine for engine, _, _ in watchdog.CHECKS] == ["trend", "carry"]

    store = StateStore(tmp_path / "btcquant.db")
    store.save_engine_state("trend", {"slots": {}})
    # aucun checkpoint carry : le moteur n'a jamais démarré

    watchdog.main(["--database", str(tmp_path / "btcquant.db")])

    fingerprints = {item["fingerprint"] for item in store.read_incidents(open_only=True)}
    assert "engine:carry:stale" in fingerprints
    assert any("carry" in message for message in messages)


def test_watchdog_resolves_the_carry_alert_once_the_engine_writes(tmp_path, monkeypatch):
    monkeypatch.setattr(watchdog, "STATE", tmp_path)
    monkeypatch.setenv("BTCQUANT_REQUIRED_ENGINES", "trend,carry")
    monkeypatch.setattr(watchdog, "notify", lambda _message: None)

    store = StateStore(tmp_path / "btcquant.db")
    store.save_engine_state("trend", {"slots": {}})
    watchdog.main(["--database", str(tmp_path / "btcquant.db")])

    store.save_engine_state("carry", {"equity": 4000.0, "in_position": False})
    watchdog.main(["--database", str(tmp_path / "btcquant.db")])

    fingerprints = {item["fingerprint"] for item in store.read_incidents(open_only=True)}
    assert "engine:carry:stale" not in fingerprints


def test_watchdog_alerts_once_for_stale_shadow_then_resolves(tmp_path, monkeypatch):
    monkeypatch.setattr(watchdog, "STATE", tmp_path)
    messages: list[str] = []
    monkeypatch.setattr(watchdog, "notify", messages.append)
    main_database = tmp_path / "btcquant.db"
    store = StateStore(main_database)
    store.save_engine_state("trend", {"slots": {}})
    store.save_engine_state("carry", {"equity": 4000.0})
    shadow_database = tmp_path / "execution-shadow.db"
    shadow = ShadowStore(shadow_database)
    shadow.record_success(datetime.now(UTC) - timedelta(minutes=10))

    args = [
        "--database",
        str(main_database),
        "--shadow-database",
        str(shadow_database),
    ]
    watchdog.main(args)
    watchdog.main(args)

    assert len(messages) == 1
    assert "shadow" in messages[0].lower()
    assert any(
        item["fingerprint"] == "shadow:market_data_stale"
        for item in store.read_incidents(open_only=True)
    )

    shadow.record_success(datetime.now(UTC))
    watchdog.main(args)

    assert not any(
        item["fingerprint"] == "shadow:market_data_stale"
        for item in store.read_incidents(open_only=True)
    )


def test_watchdog_check_failed_resolves_and_reopens_after_recovery(tmp_path, monkeypatch):
    monkeypatch.setattr(watchdog, "notify", lambda _message: None)
    database = tmp_path / "btcquant.db"
    store = StateStore(database)
    store.save_engine_state("trend", {"slots": {}})
    real_health = watchdog.execution_health
    failing = {"value": True}

    def flaky_health(*args, **kwargs):
        if failing["value"]:
            raise ConnectionError("temporary read failure")
        return real_health(*args, **kwargs)

    monkeypatch.setattr(watchdog, "execution_health", flaky_health)
    args = ["--database", str(database), "--service", "test-engine"]
    watchdog.main(args)
    assert any(
        item["fingerprint"] == "watchdog:trend:check_failed"
        for item in store.read_incidents(open_only=True)
    )

    failing["value"] = False
    watchdog.main(args)
    assert not any(
        item["fingerprint"] == "watchdog:trend:check_failed"
        for item in store.read_incidents(open_only=True)
    )

    failing["value"] = True
    watchdog.main(args)
    assert any(
        item["fingerprint"] == "watchdog:trend:check_failed"
        for item in store.read_incidents(open_only=True)
    )


def test_shadow_check_failed_resolves_after_success(tmp_path, monkeypatch):
    monkeypatch.setattr(watchdog, "notify", lambda _message: None)
    database = tmp_path / "btcquant.db"
    StateStore(database).save_engine_state("trend", {"slots": {}})
    shadow_database = tmp_path / "shadow.db"
    ShadowStore(shadow_database).record_success(datetime.now(UTC))
    real_runtime_health = watchdog.ShadowStore.runtime_health
    failing = {"value": True}

    def flaky_runtime(self, *args, **kwargs):
        if failing["value"]:
            raise ConnectionError("temporary shadow read failure")
        return real_runtime_health(self, *args, **kwargs)

    monkeypatch.setattr(watchdog.ShadowStore, "runtime_health", flaky_runtime)
    args = [
        "--database",
        str(database),
        "--shadow-database",
        str(shadow_database),
    ]
    watchdog.main(args)
    incident = next(
        item
        for item in StateStore(database, initialize=False, read_only=True).read_incidents(
            open_only=True
        )
        if item["fingerprint"] == "watchdog:shadow:check_failed"
    )
    assert incident["severity"] == "WARNING"

    failing["value"] = False
    watchdog.main(args)
    assert not any(
        item["fingerprint"] == "watchdog:shadow:check_failed"
        for item in StateStore(database, initialize=False, read_only=True).read_incidents(
            open_only=True
        )
    )


def test_required_shadow_read_failure_is_critical(tmp_path, monkeypatch):
    monkeypatch.setenv("BTCQUANT_REQUIRED_ENGINES", "trend,shadow")
    monkeypatch.setattr(watchdog, "notify", lambda _message: None)
    database = tmp_path / "btcquant.db"
    StateStore(database).save_engine_state("trend", {"slots": {}})
    shadow_database = tmp_path / "shadow.db"
    ShadowStore(shadow_database).record_success(datetime.now(UTC))
    monkeypatch.setattr(
        watchdog.ShadowStore,
        "runtime_health",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ConnectionError("shadow read failed")),
    )
    watchdog.main(
        [
            "--database",
            str(database),
            "--shadow-database",
            str(shadow_database),
        ]
    )
    incidents = StateStore(database, initialize=False, read_only=True).read_incidents(
        open_only=True
    )
    incident = next(
        item for item in incidents if item["fingerprint"] == "watchdog:shadow:check_failed"
    )
    assert incident["severity"] == "CRITICAL"


def test_profile_required_to_optional_does_not_leave_stale_incident_blocking(tmp_path, monkeypatch):
    from btcquant.execution.readiness import ServiceComponentProfile, evaluate_service_readiness

    monkeypatch.setattr(watchdog, "STATE", tmp_path)
    monkeypatch.setattr(watchdog, "notify", lambda _message: None)
    monkeypatch.setenv("BTCQUANT_REQUIRED_ENGINES", "trend,carry")
    database = tmp_path / "btcquant.db"
    store = StateStore(database)
    store.save_engine_state("trend", {"slots": {}})
    store.save_engine_state("carry", {"slots": {}, "reconciliation_required": False})
    store.record_incident(
        "engine:carry:stale",
        engine="carry",
        severity="CRITICAL",
        kind="engine_stale",
        message="carry stale while required",
    )
    assert any(
        item["fingerprint"] == "engine:carry:stale" for item in store.read_incidents(open_only=True)
    )

    monkeypatch.setenv("BTCQUANT_REQUIRED_ENGINES", "trend")
    watchdog.main(["--database", str(database)])

    assert any(
        item["fingerprint"] == "engine:carry:stale" for item in store.read_incidents(open_only=True)
    )
    readiness = evaluate_service_readiness(
        database,
        profile=ServiceComponentProfile(required=("trend",), optional=("carry", "shadow")),
    )
    assert readiness["ready"] is True


def test_profile_optional_does_not_resolve_execution_safety_incident(tmp_path, monkeypatch):
    from btcquant.execution.readiness import ServiceComponentProfile, evaluate_service_readiness

    monkeypatch.setattr(watchdog, "STATE", tmp_path)
    monkeypatch.setattr(watchdog, "notify", lambda _message: None)
    monkeypatch.setenv("BTCQUANT_REQUIRED_ENGINES", "trend")
    database = tmp_path / "btcquant.db"
    store = StateStore(database)
    store.save_engine_state("trend", {"slots": {}})
    store.save_engine_state(
        "carry",
        {"slots": {"carry": {"position": {"qty": 1.0}, "stop_order_id": None}}},
    )
    store.record_incident(
        "execution:carry:unprotected",
        engine="carry",
        severity="CRITICAL",
        kind="unprotected_position",
        message="carry position has no stop",
    )

    watchdog.main(["--database", str(database)])

    assert any(
        item["fingerprint"] == "execution:carry:unprotected"
        for item in store.read_incidents(open_only=True)
    )
    readiness = evaluate_service_readiness(
        database,
        profile=ServiceComponentProfile(required=("trend",), optional=("carry", "shadow")),
    )
    assert readiness["ready"] is False
    assert readiness["details"]["execution_safety"]["status"] == "FAIL"
