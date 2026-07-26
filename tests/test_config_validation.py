"""Validation fail-fast des configurations financières."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from btcquant.config import load_config
from btcquant.risk import RiskConfig

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "filename",
    ["config.yaml", "research/configs/config_3x.yaml", "config_4x.yaml"],
)
def test_repository_configs_are_valid(filename):
    config = load_config(ROOT / filename)
    assert config["execution"]["mode"] == "paper"


def test_hyperliquid_testnet_config_is_valid_and_isolated():
    config = load_config(ROOT / "config_testnet.yaml")

    assert config["execution"]["mode"] == "testnet"
    assert config["execution"]["live_exchange"] == "hyperliquid"
    assert config["execution"]["state_file"] == "state/btcquant-testnet.db"


def _base_config():
    return {
        "symbol": "BTC/USDT",
        "data": {"base_timeframe": "1h"},
        "costs": {"fee_rate": 0.001, "slippage_bps": 5.0},
        "risk": {"initial_capital": 10_000.0},
        "strategies": {
            "trend_ls_20": {
                "enabled": True,
                "type": "trend_ls",
                "market": "perp",
                "timeframe": "4h",
                "capital_fraction": 1.0,
            }
        },
        "execution": {"mode": "paper"},
    }


def _write(tmp_path, payload):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda cfg: cfg["execution"].update(mode="live"), "Safety Baseline"),
        (lambda cfg: cfg["costs"].update(fee_rate=-0.1), "costs.fee_rate"),
        (
            lambda cfg: cfg["strategies"]["trend_ls_20"].update(timeframe="13m"),
            "Timeframe invalide",
        ),
        (
            lambda cfg: cfg["strategies"]["trend_ls_20"].update(capital_fraction=0),
            "capital_fraction",
        ),
        (lambda cfg: cfg["risk"].update(initial_capital=-1), "initial_capital"),
    ],
)
def test_invalid_configs_fail_at_load_time(tmp_path, mutate, message):
    config = deepcopy(_base_config())
    mutate(config)

    with pytest.raises((TypeError, ValueError), match=message):
        load_config(_write(tmp_path, config))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"risk_per_trade": 0},
        {"max_position_pct": 1.1},
        {"vol_target_annual": -1},
        {"max_drawdown_halt": 1},
        {"daily_loss_limit": 0},
        {"max_leverage": 0},
    ],
)
def test_risk_config_rejects_impossible_values(kwargs):
    with pytest.raises(ValueError):
        RiskConfig(**kwargs)


def test_testnet_rejects_any_exchange_other_than_hyperliquid(tmp_path):
    config = deepcopy(_base_config())
    config["execution"].update(
        mode="testnet",
        testnet=True,
        live_exchange="binance",
        live_symbol="BTC/USDT:USDT",
    )

    with pytest.raises(ValueError, match="Hyperliquid"):
        load_config(_write(tmp_path, config))
