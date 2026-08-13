from __future__ import annotations

import pytest

from btcquant.execution import safety
from btcquant.execution.state_store import StateStore


def test_testnet_requires_explicit_per_session_confirmation(tmp_path, monkeypatch):
    monkeypatch.setattr(safety, "require_passed_qualification", lambda _store: {})
    monkeypatch.delenv("BTCQUANT_ENABLE_TESTNET", raising=False)
    database = tmp_path / "btcquant.db"
    StateStore(database)
    with pytest.raises(RuntimeError, match="testnet non confirmé"):
        safety.require_live_execution_enabled(testnet=True, state_path=database)


def test_testnet_gate_opens_only_after_qualification_and_confirmation(tmp_path, monkeypatch):
    monkeypatch.setattr(safety, "require_passed_qualification", lambda _store: {})
    monkeypatch.setenv("BTCQUANT_ENABLE_TESTNET", safety.TESTNET_CONFIRMATION)
    database = tmp_path / "btcquant.db"
    StateStore(database)
    safety.require_live_execution_enabled(testnet=True, state_path=database)


def test_missing_qualification_db_fails_closed_without_creation(tmp_path, monkeypatch):
    monkeypatch.setenv("BTCQUANT_ENABLE_TESTNET", safety.TESTNET_CONFIRMATION)
    database = tmp_path / "missing" / "btcquant.db"

    assert not database.exists()
    assert not database.parent.exists()
    with pytest.raises(RuntimeError, match="Safety Baseline"):
        safety.require_live_execution_enabled(testnet=True, state_path=database)

    assert not database.exists()
    assert not database.parent.exists()


def test_corrupt_qualification_db_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("BTCQUANT_ENABLE_TESTNET", safety.TESTNET_CONFIRMATION)
    database = tmp_path / "corrupt.db"
    database.write_bytes(b"not a SQLite database")

    with pytest.raises(RuntimeError, match="Safety Baseline"):
        safety.require_live_execution_enabled(testnet=True, state_path=database)


def test_real_money_remains_unconditionally_disabled(monkeypatch):
    monkeypatch.setattr(safety, "require_passed_qualification", lambda _store: {})
    monkeypatch.setenv("BTCQUANT_ENABLE_TESTNET", safety.TESTNET_CONFIRMATION)
    with pytest.raises(RuntimeError, match="argent réel"):
        safety.require_live_execution_enabled(testnet=False)
