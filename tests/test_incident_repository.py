"""Focused contract tests for the incident persistence boundary."""

from __future__ import annotations

import pytest

from btcquant.execution.state_store import StateStore
from btcquant.execution.operational_state_reader import OperationalStateReader


def test_incident_repository_preserves_reopen_and_resolve_semantics(tmp_path) -> None:
    store = StateStore(tmp_path / "btcquant.db")

    first = store.record_incident(
        "engine:trend:stale",
        severity="CRITICAL",
        kind="engine_stale",
        message="stale",
        engine="trend",
        context={"source": "test"},
    )
    assert first["is_new_or_reopened"] is True
    assert first["occurrences"] == 1

    second = store.record_incident(
        "engine:trend:stale",
        severity="CRITICAL",
        kind="engine_stale",
        message="still stale",
        engine="trend",
    )
    assert second["is_new_or_reopened"] is False
    assert second["occurrences"] == 2
    assert store.resolve_incident("engine:trend:stale") is True
    assert store.resolve_incident("engine:trend:stale") is False


def test_incident_repository_rejects_invalid_severity_before_write(tmp_path) -> None:
    store = StateStore(tmp_path / "btcquant.db")

    with pytest.raises(ValueError, match="severity"):
        store.record_incident(
            "invalid",
            severity="INFO",
            kind="test",
            message="not allowed",
        )

    assert OperationalStateReader(store.path).read_incidents() == []
