"""Export Prometheus local et sans dépendance externe."""

from __future__ import annotations

import math

import dashboard.app as dashboard
from btcquant.execution.state_store import StateStore
from btcquant.reporting.prometheus import render_prometheus


def test_prometheus_renderer_skips_invalid_and_non_finite_values():
    rendered = render_prometheus(
        {
            "valid_metric": 1.25,
            "missing_metric": None,
            "nan_metric": math.nan,
            "invalid-name": 4,
        }
    )

    assert rendered == "valid_metric 1.25\n"


def test_prometheus_endpoint_is_local_and_does_not_require_dashboard_session(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(dashboard, "STATE", tmp_path)
    store = StateStore(tmp_path / "btcquant.db")
    order_id = store.begin_order(
        "trend",
        "strategy",
        "prometheus-order",
        "MARKET",
        "BUY",
        2.0,
        "entry",
        reference_price=100.0,
    )
    store.complete_order(order_id, status="FILLED", filled_qty=2.0, price=100.05)
    monkeypatch.setattr(dashboard, "AUTH_TOKEN", "very-secret-token")
    monkeypatch.setattr(
        dashboard,
        "_combined_equity",
        lambda net_of_flows=False: dashboard.pd.Series(
            [12_345.0],
            index=dashboard.pd.to_datetime(["2026-01-01"], utc=True),
        ),
    )
    monkeypatch.setattr(
        dashboard,
        "_live_metrics",
        lambda: {"sharpe": 1.2, "sortino": None, "days": 42},
    )
    monkeypatch.setattr(dashboard, "_engine_age_seconds", lambda *_args: 5.0)

    response = dashboard.app.test_client().get("/metrics/prometheus")

    assert response.status_code == 200
    assert response.mimetype == "text/plain"
    assert b"btcquant_dashboard_up 1" in response.data
    assert b"btcquant_portfolio_equity 12345" in response.data
    assert b"btcquant_portfolio_sharpe 1.2" in response.data
    assert b"btcquant_execution_trend_orders_analyzed 1" in response.data
    assert b"btcquant_execution_trend_fill_ratio 1" in response.data
    assert b"btcquant_execution_trend_p95_slippage_bps " in response.data
    assert b"sortino" not in response.data


def test_prometheus_endpoint_rejects_remote_clients(monkeypatch):
    monkeypatch.setattr(dashboard, "AUTH_TOKEN", "very-secret-token")

    response = dashboard.app.test_client().get(
        "/metrics/prometheus",
        headers={"X-Forwarded-For": "203.0.113.10"},
    )

    assert response.status_code == 403
