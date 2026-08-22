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
    candles_1h = [[open_ts + hour * 3_600_000, 120.0, 121.0, 119.0, 120.0] for hour in range(2)]

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
    assert "checks" not in response.get_json()

    store.record_incident(
        "test:critical",
        engine="trend",
        severity="CRITICAL",
        kind="test",
        message="critical test incident",
    )
    response = client.get("/api/operational-health")

    assert response.status_code == 503
    assert response.get_json()["checks"]["no_critical_incident"] is False


def test_readyz_reports_missing_operational_state_without_business_data(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard_app, "STATE", tmp_path)

    response = dashboard_app.app.test_client().get("/readyz")

    assert response.status_code == 503
    payload = response.get_json()
    assert payload["status"] == "not_ready"
    assert payload["kind"] == "SERVICE_READINESS"
    assert payload["ready"] is False
    assert "checks" not in payload


def test_summary_exposes_position_detail_without_changing_execution_semantics(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(dashboard_app, "STATE", tmp_path)
    now = datetime.now(UTC)
    observed = now - timedelta(seconds=1)
    snapshots = {
        "price": dashboard_app.SourceSnapshot.success(
            105.0, source="test-price", observed_at=observed
        ),
        "ohlcv1h": dashboard_app.SourceSnapshot.success(
            [], source="test-candles", observed_at=observed
        ),
        "funding": dashboard_app.SourceSnapshot.unavailable(source="test-funding"),
        "fx_eur": dashboard_app.SourceSnapshot.success(1.1, source="test-fx", observed_at=observed),
    }
    monkeypatch.setattr(dashboard_app, "_cache_snapshots", snapshots)
    monkeypatch.setattr(
        dashboard_app,
        "_cached",
        lambda key, _ttl, _fn: {
            "price": 105.0,
            "ohlcv1h": [],
            "funding": None,
            "fx_eur": 1.1,
        }[key],
    )

    store = StateStore(tmp_path / "btcquant.db")
    entry_time = (now - timedelta(hours=5)).isoformat()
    funding_time = (now - timedelta(minutes=12)).isoformat()
    store.save_engine_state(
        "trend",
        {
            "slots": {
                "trend_ls_20": {
                    "cash": 5_800.0,
                    "last_bar_ts": (now - timedelta(hours=1)).isoformat(),
                    "entry_fee": 1.25,
                    "stop_order_id": None,
                    "stop_transition": None,
                    "position": {
                        "direction": 1,
                        "qty": 2.0,
                        "initial_qty": 2.5,
                        "entry_price": 100.0,
                        "stop_price": 102.0,
                        "entry_time": entry_time,
                        "bars_held": 12,
                        "pyramid_adds": 2,
                        "best_close": 106.0,
                        "last_add_price": 104.0,
                    },
                },
                "trend_ls_55": {
                    "cash": 200.0,
                    "last_bar_ts": None,
                    "position": None,
                },
            },
            "peak_equity": 6_000.0,
            "day_start_equity": 6_000.0,
        },
    )
    store.save_engine_state(
        "carry",
        {
            "equity": 4_000.0,
            "in_position": True,
            "execution_state": "OPEN",
            "qty": 1.0,
            "spot_qty": 1.0,
            "perp_qty": -1.0,
            "entry_timestamp": entry_time,
            "last_funding_ts": funding_time,
            "peak_equity": 4_000.0,
            "day_start_equity": 4_000.0,
        },
    )
    store.append_equity("trend", 6_000.0, (now - timedelta(minutes=1)).isoformat())
    store.append_equity("trend", 6_000.0, now.isoformat())
    store.append_equity("carry", 4_000.0, (now - timedelta(minutes=1)).isoformat())
    store.append_equity("carry", 4_000.0, now.isoformat())

    response = dashboard_app.app.test_client().get(
        "/api/summary",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    slot = next(item for item in payload["trend"]["slots"] if item["name"] == "trend_ls_20")
    assert slot["state"] == "LONG"
    assert slot["market_price"] == pytest.approx(105.0)
    assert slot["notional"] == pytest.approx(210.0)
    assert slot["upnl"] == pytest.approx(10.0)
    assert slot["upnl_pct"] == pytest.approx(0.05)
    assert slot["stop_distance"] == pytest.approx(3.0)
    assert slot["stop_pnl"] == pytest.approx(4.0)
    assert slot["stop_distance_pct"] == pytest.approx(3.0 / 105.0)
    assert slot["protection_mode"] == "SOFTWARE"
    assert slot["initial_qty"] == pytest.approx(2.5)
    assert slot["entry_fee"] == pytest.approx(1.25)
    assert slot["protection_status"] == "ACTIVE"
    assert slot["pyramid_adds"] == 2
    assert slot["best_close"] == pytest.approx(106.0)
    assert slot["position_age_s"] >= 5 * 3600 - 5

    trend = payload["trend"]
    assert trend["open_slots"] == 1
    assert trend["total_slots"] == 2
    assert trend["total_notional"] == pytest.approx(210.0)
    assert trend["total_upnl"] == pytest.approx(10.0)
    assert trend["total_upnl_pct"] == pytest.approx(0.05)
    assert trend["protection_status"] == "ACTIVE"

    carry = payload["carry"]
    assert carry["mode"] == "PAPER_SYNTHETIC"
    assert carry["position_status"] == "OPEN"
    assert carry["spot_notional_derived"] == pytest.approx(105.0)
    assert carry["perp_notional_derived"] == pytest.approx(105.0)
    assert carry["gross_notional"] == pytest.approx(210.0)
    assert carry["net_notional"] == pytest.approx(0.0)
    assert carry["notional_source"] == "MODELLED_FROM_QTY_AND_MARK"
    assert carry["costs"] is None
    assert carry["position_age_s"] >= 5 * 3600 - 5
    assert carry["last_funding_age_s"] >= 12 * 60 - 5

    assert payload["totals"]["trend_notional"] == pytest.approx(210.0)
    assert payload["totals"]["trend_upnl"] == pytest.approx(10.0)
    assert payload["totals"]["trend_open_slots"] == 1
    assert payload["btc"]["price_semantics"] == "HYPERLIQUID_1M_CLOSE"
    assert payload["health"]["valuation_status"] == "MARK_TO_MARKET_ESTIMATE"


def test_summary_keeps_mark_to_market_fields_unknown_when_price_is_unavailable(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(dashboard_app, "STATE", tmp_path)
    monkeypatch.setattr(dashboard_app, "_cached", lambda *_args: None)
    store = StateStore(tmp_path / "btcquant.db")
    store.save_engine_state(
        "trend",
        {
            "slots": {
                "trend_ls_20": {
                    "cash": 6_000.0,
                    "position": {
                        "direction": 1,
                        "qty": 2.0,
                        "entry_price": 100.0,
                        "stop_price": 95.0,
                    },
                }
            }
        },
    )
    store.save_engine_state(
        "carry",
        {
            "equity": 4_000.0,
            "in_position": True,
            "spot_qty": 1.0,
            "perp_qty": -1.0,
        },
    )

    response = dashboard_app.app.test_client().get(
        "/api/summary",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    slot = payload["trend"]["slots"][0]
    assert slot["market_price"] is None
    assert slot["upnl"] is None
    assert slot["notional"] is None
    assert payload["trend"]["total_notional"] is None
    assert payload["carry"]["spot_notional_derived"] is None
    assert payload["carry"]["perp_notional_derived"] is None
    assert payload["carry"]["notional_source"] is None


def test_summary_distinguishes_carry_two_leg_gross_and_funding_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard_app, "STATE", tmp_path)
    now = datetime.now(UTC)
    snapshots = {
        "price": dashboard_app.SourceSnapshot.success(
            105.0, source="hyperliquid-1m-close", observed_at=now
        ),
        "ohlcv1h": dashboard_app.SourceSnapshot.success([], source="test-candles", observed_at=now),
        "funding": dashboard_app.SourceSnapshot.unavailable(source="test-funding"),
        "fx_eur": dashboard_app.SourceSnapshot.success(1.1, source="test-fx", observed_at=now),
    }
    monkeypatch.setattr(dashboard_app, "_cache_snapshots", snapshots)
    monkeypatch.setattr(
        dashboard_app,
        "_cached",
        lambda key, _ttl, _fn: {
            "price": 105.0,
            "ohlcv1h": [],
            "funding": None,
            "fx_eur": 1.1,
        }[key],
    )
    monkeypatch.setattr(
        StateStore,
        "read_funding_ledger",
        lambda _self: [
            {
                "funding_timestamp": now.isoformat(),
                "funding_pnl": 100.0,
                "borrow_cost": 2.5,
            }
        ],
    )
    store = StateStore(tmp_path / "btcquant.db")
    store.save_engine_state(
        "trend",
        {
            "slots": {
                "trend_ls_20": {
                    "cash": 6_000.0,
                    "position": {
                        "direction": 1,
                        "qty": 2.0,
                        "entry_price": 100.0,
                        "stop_price": 95.0,
                    },
                }
            }
        },
    )
    store.save_engine_state(
        "carry",
        {
            "equity": 4_000.0,
            "in_position": True,
            "spot_qty": 1.0,
            "perp_qty": -1.0,
            "spot_notional": 300.0,
            "perp_notional": 280.0,
            "entry_price": 100.0,
            "borrow_principal": 200.0,
            "position_generation": "carry-generation-1",
        },
    )
    store.append_equity("trend", 6_000.0, now.isoformat())
    store.append_equity("carry", 4_000.0, now.isoformat())

    payload = (
        dashboard_app.app.test_client()
        .get(
            "/api/summary",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
        )
        .get_json()
    )

    carry = payload["carry"]
    assert carry["notional_source"] == "PERSISTED"
    assert carry["gross_notional"] == pytest.approx(580.0)
    assert carry["net_notional"] == pytest.approx(20.0)
    assert carry["funding_ledger_status"] == "AVAILABLE"
    assert carry["funding_gross_total"] == pytest.approx(100.0)
    assert carry["borrow_cost_total"] == pytest.approx(2.5)
    totals = payload["totals"]
    assert totals["portfolio_gross_notional"] == pytest.approx(790.0)
    assert totals["portfolio_directional_net_notional"] == pytest.approx(230.0)
    assert totals["risk_engine_notional"] == pytest.approx(12_210.0)
    assert totals["risk_engine_notional"] != totals["portfolio_gross_notional"]


def test_summary_keeps_flat_and_stale_values_explicitly_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard_app, "STATE", tmp_path)
    now = datetime.now(UTC)
    # Keep the snapshot beyond the fresh window but inside the unavailable
    # cutoff so the summary reports STALE rather than UNKNOWN.
    stale = now - timedelta(minutes=2, seconds=45)
    snapshots = {
        "price": dashboard_app.SourceSnapshot.success(
            105.0,
            source="hyperliquid-1m-close",
            observed_at=stale,
            max_age_seconds=60.0,
        ),
        "ohlcv1h": dashboard_app.SourceSnapshot.success([], source="test-candles", observed_at=now),
        "funding": dashboard_app.SourceSnapshot.success(
            {"rate": 0.0}, source="test-funding", observed_at=now
        ),
        "fx_eur": dashboard_app.SourceSnapshot.success(1.1, source="test-fx", observed_at=now),
    }
    monkeypatch.setattr(dashboard_app, "_cache_snapshots", snapshots)
    monkeypatch.setattr(
        dashboard_app,
        "_cached",
        lambda key, _ttl, _fn: {
            "price": 105.0,
            "ohlcv1h": [],
            "funding": None,
            "fx_eur": 1.1,
        }[key],
    )
    store = StateStore(tmp_path / "btcquant.db")
    store.save_engine_state(
        "trend",
        {
            "slots": {
                "trend_ls_20": {
                    "cash": 6_000.0,
                    "position": None,
                }
            }
        },
    )
    store.save_engine_state(
        "carry",
        {
            "equity": 4_000.0,
            "in_position": False,
            "spot_qty": 1.0,
            "perp_qty": -1.0,
            "spot_notional": 300.0,
            "perp_notional": 280.0,
        },
    )
    store.append_equity("trend", 6_000.0, now.isoformat())
    store.append_equity("carry", 4_000.0, now.isoformat())

    response = dashboard_app.app.test_client().get(
        "/api/summary",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["health"]["valuation_status"] == "STALE_MARK_TO_MARKET_ESTIMATE"
    assert payload["trend"]["slots"][0]["state"] == "FLAT"
    assert payload["trend"]["slots"][0].get("notional") is None
    assert payload["trend"]["total_notional"] == pytest.approx(0.0)
    assert payload["carry"]["position_status"] == "FLAT"
    assert payload["carry"]["gross_notional"] == pytest.approx(0.0)
    assert payload["carry"]["net_notional"] == pytest.approx(0.0)
    assert payload["totals"]["portfolio_gross_notional"] == pytest.approx(0.0)
