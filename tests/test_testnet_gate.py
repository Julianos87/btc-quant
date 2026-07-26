from __future__ import annotations

import pytest

from btcquant.execution import safety


def test_testnet_requires_explicit_per_session_confirmation(monkeypatch):
    monkeypatch.setattr(safety, "require_passed_qualification", lambda _store: {})
    monkeypatch.delenv("BTCQUANT_ENABLE_TESTNET", raising=False)
    with pytest.raises(RuntimeError, match="testnet non confirmé"):
        safety.require_live_execution_enabled(testnet=True)


def test_testnet_gate_opens_only_after_qualification_and_confirmation(monkeypatch):
    monkeypatch.setattr(safety, "require_passed_qualification", lambda _store: {})
    monkeypatch.setenv("BTCQUANT_ENABLE_TESTNET", safety.TESTNET_CONFIRMATION)
    safety.require_live_execution_enabled(testnet=True)


def test_real_money_remains_unconditionally_disabled(monkeypatch):
    monkeypatch.setattr(safety, "require_passed_qualification", lambda _store: {})
    monkeypatch.setenv("BTCQUANT_ENABLE_TESTNET", safety.TESTNET_CONFIRMATION)
    with pytest.raises(RuntimeError, match="argent réel"):
        safety.require_live_execution_enabled(testnet=False)
