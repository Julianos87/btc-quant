from __future__ import annotations

from btcquant.operations.alerts import FAIL, PASS, WATCH, evaluate_alerts


def _report(**overrides: object) -> dict[str, object]:
    domains: dict[str, object] = {
        "release": {"status": PASS},
        "qualification": {"status": PASS},
        "maturity": {"status": WATCH, "binding_status": "PASS"},
        "database": {
            "status": PASS,
            "unresolved_orders": 0,
            "reconciliation_required": 0,
            "open_critical_incidents": 0,
        },
        "health": {
            "status": PASS,
            "healthz": {"status": PASS},
            "readyz": {"status": PASS},
        },
        "services": {"status": PASS, "components": {"trend": {"status": PASS}}},
        "backup": {"status": PASS},
        "capacity": {"status": PASS},
        "testnet_safety": {"status": PASS},
    }
    domains.update(overrides)
    return {"domains": domains}


def test_healthy_snapshot_has_no_alerts() -> None:
    result = evaluate_alerts(_report())

    assert result["status"] == PASS
    assert result["active"] == []
    assert result["real_notifications_sent"] == 0


def test_critical_conditions_have_stable_dedup_keys() -> None:
    report = _report(database={"status": FAIL, "unresolved_orders": 2})
    first = evaluate_alerts(report)
    second = evaluate_alerts(
        report,
        previous_keys=[item["dedup_key"] for item in first["active"]],
    )

    assert first["status"] == FAIL
    assert {item["dedup_key"] for item in first["active"]} == {
        "DB_SAFETY_FAILED:paper",
        "UNRESOLVED_ORDERS:paper",
    }
    assert all(item["state"] == "ongoing" for item in second["active"])


def test_capacity_watch_is_warning_not_critical() -> None:
    result = evaluate_alerts(_report(capacity={"status": WATCH}))

    assert result["status"] == WATCH
    assert result["active"][0]["severity"] == "WARNING"


def test_previous_condition_is_reported_as_resolved() -> None:
    result = evaluate_alerts(_report(), previous_keys=["DISK_CAPACITY_WARNING:root"])

    assert result["resolved"] == [
        {
            "dedup_key": "DISK_CAPACITY_WARNING:root",
            "severity": "INFO",
            "state": "resolved",
            "message": "Previously observed operational condition is no longer active",
        }
    ]
