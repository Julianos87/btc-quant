"""Exposition en lecture seule de la santé d'exécution au dashboard."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dashboard.app as dashboard_app

from btcquant.execution.state_store import StateStore
from btcquant.execution.shadow import BookTop, ShadowCollector, ShadowStore


def test_donchian_display_uses_only_closed_4h_bars_for_regime_and_thresholds():
    four_h = dashboard_app.FOUR_HOURS_MS
    start = datetime(2026, 1, 1, tzinfo=UTC).timestamp() * 1000
    closed_4h = []
    for index in range(220):
        close = 100.0 + index * 0.1
        closed_4h.append(
            [int(start + index * four_h), close - 0.2, close + 1.0, close - 1.0, close]
        )

    open_ts = int(start + len(closed_4h) * four_h)
    # Valeurs volontairement absurdes : si la bougie ouverte fuit dans le
    # calcul, elle retourne le régime et pollue immédiatement les canaux.
    open_4h = [open_ts, 1.0, 1_000_000.0, 0.01, 1.0]
    candles_1h = [
        [open_ts + hour * 3_600_000, 120.0, 121.0, 119.0, 120.0] for hour in range(2)
    ]

    channels, regime_up = dashboard_app._donchian_channels(
        candles_1h,
        [*closed_4h, open_4h],
        now_ms=open_ts + 2 * 3_600_000,
    )

    assert regime_up is True
    assert [channel["name"] for channel in channels] == ["D20", "D55", "D100"]
    for period, channel in zip((20, 55, 100), channels, strict=True):
        expected_high = max(row[2] for row in closed_4h[-period:])
        expected_low = min(row[3] for row in closed_4h[-period:])
        assert channel["high"] == [expected_high, expected_high]
        assert channel["low"] == [expected_low, expected_low]


def test_operations_endpoint_exposes_metrics_and_incidents(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard_app, "STATE", tmp_path)
    store = StateStore(tmp_path / "btcquant.db")
    order_id = store.begin_order(
        "trend",
        "strategy",
        "dashboard-order",
        "MARKET",
        "BUY",
        2.0,
        "entry",
        reference_price=100.0,
    )
    store.complete_order(
        order_id,
        status="FILLED",
        filled_qty=2.0,
        price=100.05,
    )
    store.record_incident(
        "test:dashboard",
        engine="trend",
        severity="WARNING",
        kind="test",
        message="Incident visible",
        context={"order_id": order_id},
    )
    store.register_deposit("monthly:2026-08", 250.0)

    response = dashboard_app.app.test_client().get(
        "/api/operations",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["execution"]["trend"]["orders_analyzed"] == 1
    assert payload["execution"]["trend"]["p95_slippage_bps"] == pytest.approx(5.0)
    assert payload["incidents"][0]["message"] == "Incident visible"
    assert payload["incidents"][0]["context"] == {"order_id": order_id}
    assert payload["deposits"][0]["deposit_id"] == "monthly:2026-08"
    assert payload["deposits"][0]["amount"] == pytest.approx(250.0)
    assert payload["deposits"][0]["status"] == "PENDING"


def test_summary_exposes_pending_deposit_amount(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard_app, "STATE", tmp_path)
    monkeypatch.setattr(dashboard_app, "_cached", lambda *_args: None)
    store = StateStore(tmp_path / "btcquant.db")
    store.save_engine_state(
        "trend",
        {
            "slots": {
                "trend_ls_20": {
                    "cash": 6_000.0,
                    "position": None,
                }
            },
            "peak_equity": 6_000.0,
            "day_start_equity": 6_000.0,
        },
    )
    store.save_engine_state(
        "carry",
        {
            "equity": 4_000.0,
            "in_position": False,
            "peak_equity": 4_000.0,
            "day_start_equity": 4_000.0,
        },
    )
    now = datetime.now(UTC).isoformat()
    store.append_equity("trend", 6_000.0, now)
    store.append_equity("carry", 4_000.0, now)
    store.register_deposit("monthly:2026-08", 250.0)

    response = dashboard_app.app.test_client().get(
        "/api/summary",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 200
    totals = response.get_json()["totals"]
    assert totals["pending_deposits"] == pytest.approx(250.0)
    assert totals["pending_deposit_count"] == 1
    metrics = dashboard_app.app.test_client().get(
        "/metrics/prometheus",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )
    metrics_text = metrics.get_data(as_text=True)
    assert "btcquant_pending_deposit_amount 250" in metrics_text
    assert "btcquant_pending_deposit_count 1" in metrics_text


def test_shadow_endpoint_and_prometheus_metrics(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard_app, "STATE", tmp_path)
    started = datetime(2026, 7, 28, 12, tzinfo=UTC)
    store = ShadowStore(tmp_path / "execution-shadow.db")
    collector = ShadowCollector(None, store)
    collector.observe(BookTop(started, bid=100.00, ask=100.01))
    collector.observe(BookTop(started + timedelta(seconds=31), bid=100.00, ask=100.01))

    client = dashboard_app.app.test_client()
    response = client.get(
        "/api/execution-shadow",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )
    assert response.status_code == 200
    assert response.get_json()["status"] == "SHADOW_PROXY_ONLY"

    metrics = client.get(
        "/metrics/prometheus",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )
    assert metrics.status_code == 200
    metrics_text = metrics.get_data(as_text=True)
    assert "btcquant_shadow_eligible_intents 2" in metrics_text
    assert "btcquant_shadow_consecutive_failures 0" in metrics_text
    assert "btcquant_shadow_last_success_age_seconds" in metrics_text


def test_readyz_checks_engines_incidents_and_shadow_freshness(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard_app, "STATE", tmp_path)
    store = StateStore(tmp_path / "btcquant.db")
    store.save_engine_state("trend", {"slots": {}})
    store.save_engine_state("carry", {"equity": 4000.0})
    ShadowStore(tmp_path / "execution-shadow.db").record_success(datetime.now(UTC))
    client = dashboard_app.app.test_client()

    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.get_json()["status"] == "ready"

    store.record_incident(
        "test:critical",
        engine="trend",
        severity="CRITICAL",
        kind="test",
        message="critical test incident",
    )
    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.get_json()["checks"]["no_critical_incident"] is False


def test_readyz_reports_missing_operational_state_without_business_data(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard_app, "STATE", tmp_path)

    response = dashboard_app.app.test_client().get("/readyz")

    assert response.status_code == 503
    payload = response.get_json()
    assert payload["status"] == "not_ready"
    assert payload["checks"] == {"database": False, "shadow_database": False}
