"""Regressions for audited filtering, reference scope, and network cache isolation."""

import threading
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

import dashboard.app as dashboard


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(dashboard, "AUTH_TOKEN", None)
    monkeypatch.setattr(
        dashboard,
        "_read_trades",
        lambda: pd.DataFrame(
            [
                {"exit_ts": "2026-10-30T23:59:59Z", "pnl": 1, "strategy": "a"},
                {"exit_ts": "2026-10-31T00:00:00Z", "pnl": 2, "strategy": "b"},
                {"exit_ts": "2026-10-31T00:00:01Z", "pnl": 3, "strategy": "a"},
            ]
        ),
    )
    return dashboard.app.test_client()


def test_calendar_end_is_exclusive_and_csv_matches(client):
    data = client.get("/api/trades?to=2026-10-30").get_json()
    assert data["stats"]["n"] == 1
    csv = client.get("/api/trades.csv?to=2026-10-30").text
    assert "23:59:59Z" in csv
    assert "00:00:00Z" not in csv


@pytest.mark.parametrize("endpoint", ["/api/trades", "/api/trades.csv"])
@pytest.mark.parametrize(
    "query", ["from=nope", "to=2026-02-30", "from=NaT", "from=2026-11-01&to=2026-10-30"]
)
def test_invalid_dates_are_400_even_without_trades(client, monkeypatch, endpoint, query):
    monkeypatch.setattr(dashboard, "_read_trades", lambda: pd.DataFrame())
    result = client.get(endpoint + "?" + query)
    assert result.status_code == 400
    assert result.get_json()["error"] == "INVALID_DATE_RANGE"


def test_timezone_instants_and_strategy_are_shared(client):
    query = {"from": "2026-10-31T01:00:00+01:00", "to": "2026-10-31T00:00:01Z", "strategy": "b"}
    assert client.get("/api/trades", query_string=query).get_json()["stats"]["n"] == 1
    csv = client.get("/api/trades.csv", query_string=query).text
    assert "00:00:00Z" in csv and "23:59:59Z" not in csv and "00:00:01Z" not in csv


def test_comparison_never_equates_trend_to_portfolio(client, monkeypatch):
    monkeypatch.setattr(dashboard, "_combined_equity", lambda **kw: pd.Series([100, 110, 102]))
    data = client.get("/api/conformity").get_json()
    assert data["comparison"]["comparable"] is False
    assert "SCOPE_MISMATCH" in data["comparison"]["reason_codes"]
    assert data["drawdown"]["current"] == pytest.approx(102 / 110 - 1)
    assert data["drawdown"]["backtest_time_at_least_as_deep"] is None
    yearly = client.get("/api/yearly").get_json()
    assert yearly["comparison"]["comparable"] is False
    assert yearly["generated_at"] == yearly["generated"]
    assert yearly["years"]


@pytest.fixture
def cache(monkeypatch):
    monkeypatch.setattr(dashboard, "_observation_cache", dashboard.BoundedReadCache())
    monkeypatch.setattr(dashboard, "_cache_snapshots", {})
    monkeypatch.setattr(dashboard, "_cache_key_locks", {})


def test_hot_price_does_not_wait_for_fx_network(cache):
    assert dashboard._cached("price", 30, lambda: 77000) == 77000
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def slow():
        entered.set()
        assert release.wait(3)
        return 1.1

    worker = threading.Thread(target=lambda: dashboard._cached("fx_eur", 30, slow))
    worker.start()
    assert entered.wait(1)
    results = []
    reader = threading.Thread(
        target=lambda: (
            results.append(dashboard._cached("price", 30, lambda: pytest.fail("hot read fetched"))),
            finished.set(),
        )
    )
    reader.start()
    try:
        assert finished.wait(1), "Hot cached price waited for unrelated network refresh"
        assert results == [77000]
    finally:
        release.set()
        worker.join(3)
        reader.join(3)


def test_same_key_singleflight_bounded_stale_and_cold(cache, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def slow():
        calls.append(1)
        entered.set()
        assert release.wait(3)
        return 80000

    worker = threading.Thread(target=lambda: dashboard._cached("price", 0, slow))
    worker.start()
    assert entered.wait(1)
    try:
        assert dashboard._cached("price", 30, slow) is None  # cold refresh in flight
        dashboard._cache_snapshots["price"] = dashboard.SourceSnapshot.success(
            77000, source="test", observed_at=datetime.now(UTC) - timedelta(seconds=10)
        )
        assert dashboard._cached("price", 30, slow) == 77000
        dashboard._cache_snapshots["price"] = dashboard.SourceSnapshot.success(
            77000, source="test", observed_at=datetime.now(UTC) - timedelta(days=1)
        )
        assert dashboard._cached("price", 30, slow) is None
        assert len(calls) == 1
    finally:
        release.set()
        worker.join(3)
