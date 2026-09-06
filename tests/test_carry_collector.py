"""Tests for immutable, public Carry snapshot collection boundaries."""

from __future__ import annotations

import hashlib

from scripts.acquire_carry_v2_data import (
    END_MS,
    START_MS,
    _parse_iso_ms,
    _window_label,
)


def test_collector_window_labels_are_deterministic() -> None:
    assert _window_label(START_MS) == "20260114T000000Z"
    assert _window_label(END_MS) == "20260810T190000Z"


def test_collector_requires_timezone_aware_iso_timestamps() -> None:
    assert _parse_iso_ms("2026-09-07T00:00:00Z") > END_MS
    try:
        _parse_iso_ms("2026-09-07T00:00:00")
    except ValueError as exc:
        assert "timezone" in str(exc)
    else:
        raise AssertionError("naive collector timestamp was accepted")


def test_collector_gzip_bytes_are_reproducible(tmp_path) -> None:
    from scripts.acquire_carry_v2_data import _write_csv_gz

    first = tmp_path / "one.csv.gz"
    second = tmp_path / "two.csv.gz"
    rows = [{"timestamp": "2026-09-07T00:00:00Z", "value": "1"}]
    _write_csv_gz(first, ["timestamp", "value"], rows)
    _write_csv_gz(second, ["timestamp", "value"], rows)
    assert (
        hashlib.sha256(first.read_bytes()).digest() == hashlib.sha256(second.read_bytes()).digest()
    )


def test_collector_drops_incomplete_last_candle(monkeypatch) -> None:
    from scripts import acquire_carry_v2_data as collector

    start = 1_700_000_000_000
    complete = {
        "t": start,
        "T": start + 3_599_999,
        "o": "1",
        "h": "2",
        "l": "1",
        "c": "2",
        "v": "1",
        "n": 1,
    }
    incomplete = {**complete, "t": start + 3_600_000, "T": start + 7_199_999}
    monkeypatch.setattr(collector, "_post", lambda _payload: [complete, incomplete])
    rows = collector._candles(
        "BTC",
        start_ms=start,
        end_ms=start + 7_200_000,
        as_of_ms=start + 3_600_000,
    )
    assert len(rows) == 1
