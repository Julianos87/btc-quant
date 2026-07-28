from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from btcquant.execution.shadow import (
    BookTop,
    ShadowCollector,
    ShadowConfig,
    ShadowStore,
)


class UnusedMarket:
    def top(self) -> BookTop:
        raise AssertionError("observe() reçoit directement le carnet dans ces tests")


def _book(at: datetime, bid: float, ask: float) -> BookTop:
    return BookTop(observed_at=at, bid=bid, ask=ask)


def test_shadow_pair_records_touch_and_fallback_without_orders(tmp_path):
    started = datetime(2026, 7, 28, 12, tzinfo=UTC)
    store = ShadowStore(tmp_path / "shadow.db")
    collector = ShadowCollector(UnusedMarket(), store)

    collector.observe(_book(started, 100.00, 100.01))
    collector.observe(_book(started + timedelta(seconds=10), 99.99, 100.00))
    collector.observe(_book(started + timedelta(seconds=31), 100.00, 100.02))

    rows = store.rows()
    assert [row["outcome"] for row in rows] == ["TOUCHED", "FALLBACK"]
    assert rows[0]["execution_fee_bps"] == pytest.approx(2.0)
    assert rows[1]["execution_fee_bps"] == pytest.approx(5.0)
    summary = store.summary()
    assert summary["status"] == "SHADOW_PROXY_ONLY"
    assert summary["evidence"]["eligible_intents"] == 2
    assert summary["touch_proxy_rate"] == pytest.approx(0.5)
    assert summary["fallback_rate"] == pytest.approx(0.5)
    assert summary["evidence"]["p95_fill_seconds"] == pytest.approx(10.0)
    assert not hasattr(collector, "create_order")


def test_restart_does_not_duplicate_a_pending_pair(tmp_path):
    started = datetime(2026, 7, 28, 12, tzinfo=UTC)
    database = tmp_path / "shadow.db"
    first = ShadowCollector(UnusedMarket(), ShadowStore(database))
    first.observe(_book(started, 100.00, 100.01))

    restarted = ShadowCollector(UnusedMarket(), ShadowStore(database))
    restarted.observe(_book(started + timedelta(seconds=5), 100.00, 100.01))

    assert len(restarted.store.rows()) == 2
    assert len(restarted.store.pending()) == 2


def test_restart_closes_expired_quotes_then_resumes_collection(tmp_path):
    started = datetime(2026, 7, 28, 12, tzinfo=UTC)
    database = tmp_path / "shadow.db"
    first = ShadowCollector(UnusedMarket(), ShadowStore(database))
    first.observe(_book(started, 100.00, 100.01))

    restarted = ShadowCollector(UnusedMarket(), ShadowStore(database))
    restarted.observe(_book(started + timedelta(seconds=31), 100.00, 100.01))

    rows = restarted.store.rows()
    assert [row["outcome"] for row in rows[:2]] == ["FALLBACK", "FALLBACK"]
    assert len(rows) == 4
    assert [row["outcome"] for row in rows[2:]] == ["PENDING", "PENDING"]


def test_empty_summary_and_invalid_timing(tmp_path):
    summary = ShadowStore(tmp_path / "shadow.db").summary()
    assert summary["evidence"]["eligible_intents"] == 0
    assert summary["proxy_qualification"]["passed"] is False

    with pytest.raises(ValueError, match="quote_interval_seconds"):
        ShadowConfig(quote_interval_seconds=20, maker_timeout_seconds=30)
