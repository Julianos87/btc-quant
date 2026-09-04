"""Contrats spécifiques au portail P1 Hyperliquid, sans accès réseau."""

from __future__ import annotations

from pathlib import Path

import pytest

from btcquant.execution.ccxt_broker import CcxtBroker

ROOT = Path(__file__).resolve().parents[1]


class FakeHyperliquid:
    last_config = None
    calls: list[object] = []

    def __init__(self, config):
        type(self).last_config = config
        type(self).calls = []
        self.urls = {
            "api": {
                "public": "https://api.hyperliquid-testnet.xyz",
                "private": "https://api.hyperliquid-testnet.xyz",
            }
        }

    def set_sandbox_mode(self, enabled):
        type(self).calls.append(("sandbox", enabled))

    def load_markets(self):
        type(self).calls.append("load_markets")

    def set_leverage(self, leverage, symbol):
        type(self).calls.append(("leverage", leverage, symbol))


def _open_safety(monkeypatch):
    monkeypatch.setattr(
        "btcquant.execution.ccxt_broker.require_live_execution_enabled",
        lambda **_kwargs: None,
    )


def test_hyperliquid_requires_public_account_and_dedicated_api_wallet_key(monkeypatch):
    _open_safety(monkeypatch)
    monkeypatch.delenv("HYPERLIQUID_WALLET_ADDRESS", raising=False)
    monkeypatch.delenv("HYPERLIQUID_PRIVATE_KEY", raising=False)

    with pytest.raises(RuntimeError, match="WALLET_ADDRESS"):
        CcxtBroker("hyperliquid", "BTC/USDC:USDC", testnet=True, market="perp")


def test_hyperliquid_credentials_and_sandbox_are_wired_before_market_load(monkeypatch):
    _open_safety(monkeypatch)
    monkeypatch.setenv("HYPERLIQUID_WALLET_ADDRESS", "0x" + "1" * 40)
    monkeypatch.setenv("HYPERLIQUID_PRIVATE_KEY", "0x" + "2" * 64)
    monkeypatch.setattr("btcquant.execution.ccxt_broker.ccxt.hyperliquid", FakeHyperliquid)

    broker = CcxtBroker(
        "hyperliquid",
        "BTC/USDC:USDC",
        testnet=True,
        market="perp",
        leverage=1,
    )

    assert broker.exchange_id == "hyperliquid"
    assert FakeHyperliquid.last_config is not None
    assert FakeHyperliquid.last_config["walletAddress"] == "0x" + "1" * 40
    assert FakeHyperliquid.last_config["privateKey"] == "0x" + "2" * 64
    assert "apiKey" not in FakeHyperliquid.last_config
    assert FakeHyperliquid.calls == [
        ("sandbox", True),
        "load_markets",
        ("leverage", 1, "BTC/USDC:USDC"),
    ]


def test_testnet_deployment_is_opt_in_hardened_and_mainnet_free():
    service = (ROOT / "deploy" / "btcquant-hyperliquid-testnet.service").read_text(encoding="utf-8")
    start = (ROOT / "deploy" / "start-hyperliquid-testnet.sh").read_text(encoding="utf-8")
    stop = (ROOT / "deploy" / "stop-hyperliquid-testnet.sh").read_text(encoding="utf-8")
    update = (ROOT / "deploy" / "update.sh").read_text(encoding="utf-8")

    assert "ConditionPathExists=/opt/btcquant/state/HYPERLIQUID_TESTNET_APPROVED" in service
    assert "Conflicts=btcquant-trend.service" in service
    assert "environments/testnet/config.yaml" in service
    assert "Restart=on-failure" in service
    assert "NoNewPrivileges=true" in service
    assert "--i-accept-hyperliquid-testnet-orders" in start
    assert "testnet-p1" in start
    assert "scripts/test_testnet.py" in start
    assert 'rm -- "${APPROVAL}"' in stop
    assert "restart_selected_engines" in update
    assert "btcquant-hyperliquid-testnet" in update
    assert "mainnet" not in service.lower()


def test_hyperliquid_rejects_mainnet_endpoint_after_sandbox(monkeypatch):
    _open_safety(monkeypatch)
    monkeypatch.setenv("HYPERLIQUID_WALLET_ADDRESS", "0x" + "1" * 40)
    monkeypatch.setenv("HYPERLIQUID_PRIVATE_KEY", "0x" + "2" * 64)

    class MainnetHyperliquid(FakeHyperliquid):
        def __init__(self, config):
            super().__init__(config)
            self.urls["api"] = {
                "public": "https://api.hyperliquid.xyz",
                "private": "https://api.hyperliquid.xyz",
            }

    monkeypatch.setattr(
        "btcquant.execution.ccxt_broker.ccxt.hyperliquid",
        MainnetHyperliquid,
    )

    with pytest.raises(RuntimeError, match="endpoint Hyperliquid testnet"):
        CcxtBroker("hyperliquid", "BTC/USDC:USDC", testnet=True, market="perp")
