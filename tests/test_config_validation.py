"""Validation fail-fast des configurations financières."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError
import math
from pathlib import Path

import pytest
import yaml

from btcquant.config import (
    CostsConfig,
    HYPERLIQUID_TESTNET_API_URL,
    carry_policy_from_config,
    costs_from_config,
    load_config,
    portfolio_from_config,
    risk_from_config,
    runtime_execution_from_config,
)
from btcquant.risk import RiskConfig
from btcquant.strategies.trend_ls import TrendLS

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "filename",
    [
        "environments/dev/config.yaml",
        "research/configs/config_3x.yaml",
        "environments/paper/config.yaml",
    ],
)
def test_repository_configs_are_valid(filename):
    config = load_config(ROOT / filename)
    assert config["execution"]["mode"] == "paper"


def test_tandem_portfolio_and_carry_have_one_typed_source():
    config = load_config(ROOT / "environments" / "paper" / "config.yaml")
    portfolio = portfolio_from_config(config)
    carry = carry_policy_from_config(config)

    assert portfolio.total_capital == 10_000
    assert portfolio.trend_capital == 6_000
    assert portfolio.carry_capital == 4_000
    assert carry.capital == portfolio.carry_capital
    assert carry.leverage == config["carry"]["leverage"]
    assert carry.fee_rate == config["costs"]["perp_fee_rate"]
    assert carry.slippage_bps == config["costs"]["slippage_bps"]


def test_tandem_profile_rejects_capital_drift(tmp_path):
    config = load_config(ROOT / "environments" / "paper" / "config.yaml")
    config["risk"]["initial_capital"] = 5_999

    with pytest.raises(ValueError, match="risk.initial_capital"):
        load_config(_write(tmp_path, config))


def test_tandem_profile_rejects_unknown_portfolio_key(tmp_path):
    config = load_config(ROOT / "environments" / "paper" / "config.yaml")
    config["portfolio"]["typo_fraction"] = 0.1

    with pytest.raises(ValueError, match="typo_fraction"):
        load_config(_write(tmp_path, config))


def test_hyperliquid_testnet_config_is_valid_and_isolated():
    config = load_config(ROOT / "environments" / "testnet" / "config.yaml")

    assert config["execution"]["mode"] == "testnet"
    assert config["environment"] == "testnet"
    assert config["execution"]["live_exchange"] == "hyperliquid"
    assert config["execution"]["api_url"] == HYPERLIQUID_TESTNET_API_URL
    assert config["execution"]["state_file"] == "state/btcquant-testnet.db"


def test_environment_profiles_use_distinct_state_databases():
    profiles = {
        name: load_config(ROOT / "environments" / name / "config.yaml")
        for name in ("dev", "paper", "testnet")
    }

    assert {profile["environment"] for profile in profiles.values()} == {
        "dev",
        "paper",
        "testnet",
    }
    assert len({profile["execution"]["state_file"] for profile in profiles.values()}) == 3


@pytest.mark.parametrize(
    "filename",
    [
        "environments/dev/config.yaml",
        "environments/paper/config.yaml",
        "environments/testnet/config.yaml",
    ],
)
def test_critical_typed_config_values_match_valid_yaml(filename):
    config = load_config(ROOT / filename)
    costs = costs_from_config(config)
    risk = risk_from_config(config)
    execution = runtime_execution_from_config(config)

    assert costs.fee_rate == config["costs"].get("fee_rate", 0.0)
    assert costs.slippage_bps == config["costs"].get("slippage_bps", 0.0)
    assert costs.perp_fee_rate == config["costs"].get("perp_fee_rate")
    assert costs.funding_rate_8h == config["costs"].get("funding_rate_8h")
    for field in (
        "initial_capital",
        "risk_per_trade",
        "max_position_pct",
        "vol_target_annual",
        "max_drawdown_halt",
        "daily_loss_limit",
        "max_leverage",
    ):
        assert getattr(risk, field) == config["risk"].get(field, getattr(RiskConfig(), field))
    assert execution.mode == config["execution"].get("mode", "paper")
    assert execution.testnet == config["execution"].get("testnet", False)
    assert execution.state_file == config["execution"].get("state_file")
    assert execution.poll_buffer_seconds == config["execution"].get("poll_buffer_seconds", 20)


def test_critical_config_contracts_are_immutable():
    costs = CostsConfig(fee_rate=0.001, slippage_bps=5.0)
    execution = runtime_execution_from_config(_base_config())

    with pytest.raises(FrozenInstanceError):
        setattr(costs, "fee" + "_rate", 0.002)
    with pytest.raises(FrozenInstanceError):
        setattr(execution, "mo" + "de", "testnet")


@pytest.mark.parametrize(
    ("section", "key", "value", "message"),
    [
        ("risk", "max_daily_los", 0.01, "max_daily_los"),
        ("costs", "fee_rte", 0.001, "fee_rte"),
        ("execution", "poll_buffer_second", 20, "poll_buffer_second"),
    ],
)
def test_unknown_critical_config_keys_fail_closed(tmp_path, section, key, value, message):
    config = deepcopy(_base_config())
    config[section][key] = value

    with pytest.raises(ValueError, match=message):
        load_config(_write(tmp_path, config))


@pytest.mark.parametrize(
    ("section", "key", "value", "message"),
    [
        ("risk", "risk_per_trade", True, "risk.risk_per_trade"),
        ("risk", "risk_per_trade", math.nan, "risk.risk_per_trade"),
        ("risk", "risk_per_trade", math.inf, "risk.risk_per_trade"),
        ("risk", "initial_capital", None, "risk.initial_capital"),
        ("costs", "fee_rate", "0.001", "costs.fee_rate"),
        ("costs", "fee_rate", math.nan, "costs.fee_rate"),
        ("execution", "testnet", "false", "execution.testnet"),
        ("execution", "testnet", 1, "execution.testnet"),
        ("execution", "poll_buffer_seconds", 2.7, "poll_buffer_seconds"),
    ],
)
def test_invalid_critical_yaml_types_fail_closed(tmp_path, section, key, value, message):
    config = deepcopy(_base_config())
    config[section][key] = value

    with pytest.raises((TypeError, ValueError), match=message):
        load_config(_write(tmp_path, config))


@pytest.mark.parametrize("simulation", [[], ""])
def test_invalid_simulation_container_no_longer_becomes_implicit_defaults(tmp_path, simulation):
    config = deepcopy(_base_config())
    config["execution"]["simulation"] = simulation

    with pytest.raises(TypeError, match="execution.simulation"):
        load_config(_write(tmp_path, config))


def _base_config():
    return {
        "environment": "dev",
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
    config["environment"] = "testnet"
    config["execution"].update(
        mode="testnet",
        testnet=True,
        live_exchange="binance",
        live_symbol="BTC/USDT:USDT",
    )

    with pytest.raises(ValueError, match="Hyperliquid"):
        load_config(_write(tmp_path, config))


# ── paramètres de stratégie : une faute de frappe doit être fatale ───────────
# Contexte : `Strategy.__init__` fusionnait silencieusement tout kwarg dans
# `params`. Écrire `donchain: 20` au lieu de `donchian: 20` laissait tourner la
# stratégie sur sa valeur par défaut, sans trace — un ensemble de trois horizons
# devenait trois copies du même horizon, à levier 4, sans que rien ne l'indique.


def test_strategy_rejects_unknown_parameter():
    with pytest.raises(ValueError, match="donchain"):
        TrendLS(donchain=20)


def test_strategy_still_accepts_every_documented_parameter():
    known = TrendLS.default_params()
    assert TrendLS(**known).params == known


def test_config_rejects_misspelled_strategy_parameter(tmp_path):
    config = deepcopy(_base_config())
    strategy = next(iter(config["strategies"].values()))
    strategy["enabled"] = True
    strategy["params"] = {"donchain": 20}

    with pytest.raises(ValueError, match="donchain"):
        load_config(_write(tmp_path, config))


def test_config_rejects_unknown_strategy_class_at_load_time(tmp_path):
    config = deepcopy(_base_config())
    strategy = next(iter(config["strategies"].values()))
    strategy.update(enabled=True, type="stratégie_inexistante")

    with pytest.raises(KeyError, match="inexistante"):
        load_config(_write(tmp_path, config))


def test_disabled_strategy_params_are_not_enforced(tmp_path):
    """Une section désactivée ne doit pas bloquer le chargement : on ne valide
    que ce qui va réellement tourner."""
    config = deepcopy(_base_config())
    for strategy in config["strategies"].values():
        strategy["enabled"] = False
    first = next(iter(config["strategies"].values()))
    first.update(enabled=True, params={})
    config["strategies"]["archivée"] = {
        "enabled": False,
        "type": "stratégie_inexistante",
        "params": {"n_importe_quoi": 1},
    }

    load_config(_write(tmp_path, config))


def test_hyperliquid_testnet_rejects_mainnet_api_url(tmp_path):
    config = deepcopy(load_config(ROOT / "environments" / "testnet" / "config.yaml"))
    config["execution"]["api_url"] = "https://api.hyperliquid.xyz"

    with pytest.raises(ValueError, match="endpoint API testnet"):
        load_config(_write(tmp_path, config))
