"""Tests for immutable, public Carry snapshot collection boundaries."""

from __future__ import annotations

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
