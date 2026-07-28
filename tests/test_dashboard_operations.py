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


def test_shadow_endpoint_and_prometheus_metrics(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard_app, "STATE", tmp_path)
    started = datetime(2026, 7, 28, 12, tzinfo=UTC)
    store = ShadowStore(tmp_path / "execution-shadow.db")
    collector = ShadowCollector(None, store)
    collector.observe(BookTop(started, bid=100.00, ask=100.01))
    collector.observe(
        BookTop(started + timedelta(seconds=31), bid=100.00, ask=100.01)
    )

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
    assert "btcquant_shadow_eligible_intents 2" in metrics.get_data(as_text=True)
