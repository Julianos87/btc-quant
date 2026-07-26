"""Chargement de config.yaml vers les objets du système."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import yaml

from .domain import ExecutionConfig
from .risk import RiskConfig
from .strategies import STRATEGY_REGISTRY, Strategy


TIMEFRAMES = {"1h", "2h", "4h", "6h", "12h", "1d"}
MARKETS = {"spot", "perp"}


def load_config(path: str | Path = "config.yaml") -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        payload = yaml.safe_load(fh)
    if not isinstance(payload, dict):
        raise TypeError("La configuration YAML doit être un mapping")
    _validate_config(payload)
    return payload


def _finite_non_negative(name: str, value: Any) -> None:
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)) or value < 0:
        raise ValueError(f"{name} doit être un nombre fini positif ou nul")


def _validate_config(cfg: dict[str, Any]) -> None:
    for section in ("data", "costs", "risk", "strategies", "execution"):
        if not isinstance(cfg.get(section), dict):
            raise ValueError(f"Section obligatoire absente ou invalide : {section}")
    if not isinstance(cfg.get("symbol"), str) or "/" not in cfg["symbol"]:
        raise ValueError("symbol doit être une paire de type BASE/QUOTE")
    for key in ("fee_rate", "perp_fee_rate", "funding_rate_8h", "slippage_bps"):
        if key in cfg["costs"]:
            _finite_non_negative(f"costs.{key}", cfg["costs"][key])
    if cfg["execution"].get("mode", "paper") != "paper":
        raise ValueError("Safety Baseline : execution.mode doit rester 'paper'")
    strategies = cfg["strategies"]
    if not strategies:
        raise ValueError("strategies ne peut pas être vide")
    for name, spec in strategies.items():
        if not isinstance(spec, dict):
            raise TypeError(f"strategies.{name} doit être un mapping")
        timeframe = spec.get("timeframe", "1d")
        if timeframe not in TIMEFRAMES:
            raise ValueError(f"Timeframe invalide pour {name} : {timeframe!r}")
        market = spec.get("market", "spot")
        if market not in MARKETS:
            raise ValueError(f"Marché invalide pour {name} : {market!r}")
        fraction = spec.get("capital_fraction", 1.0)
        if not isinstance(fraction, (int, float)) or not 0 < float(fraction) <= 1:
            raise ValueError(f"capital_fraction invalide pour {name}")
    risk_from_config(cfg)


def risk_from_config(cfg: dict[str, Any]) -> RiskConfig:
    r = cfg.get("risk", {})
    return RiskConfig(
        initial_capital=r.get("initial_capital", 10_000.0),
        risk_per_trade=r.get("risk_per_trade", 0.0075),
        max_position_pct=r.get("max_position_pct", 0.95),
        vol_target_annual=r.get("vol_target_annual"),
        max_drawdown_halt=r.get("max_drawdown_halt", 0.30),
        daily_loss_limit=r.get("daily_loss_limit"),
        max_leverage=r.get("max_leverage", 1.0),
    )


def execution_config_from_config(
    cfg: dict[str, Any],
    fee_rate: float,
) -> ExecutionConfig:
    """Construit le modèle commun depuis ``execution.simulation``.

    Les frais et le slippage restent dans ``costs`` pour conserver une source
    unique ; les autres paramètres sont optionnels et désactivés par défaut.
    """

    simulation = cfg.get("execution", {}).get("simulation") or {}
    if not isinstance(simulation, dict):
        raise TypeError("execution.simulation doit être un mapping YAML")
    forbidden = {"fee_rate", "slippage_bps"} & simulation.keys()
    if forbidden:
        raise ValueError("Configurer fee_rate/slippage_bps dans costs, pas execution.simulation")
    return ExecutionConfig(
        fee_rate=fee_rate,
        slippage_bps=cfg["costs"]["slippage_bps"],
        **simulation,
    )


def build_strategies(cfg: dict[str, Any]) -> list[tuple[Strategy, float, str]]:
    """Instancie les stratégies activées.

    Retourne [(stratégie, fraction de capital, marché "spot"|"perp")].
    La clé YAML est le nom d'instance ; `type` désigne la classe (défaut :
    la clé elle-même), ce qui permet plusieurs instances de la même classe
    avec des paramètres différents (ensemble d'horizons).
    """
    out: list[tuple[Strategy, float, str]] = []
    for name, spec in cfg.get("strategies", {}).items():
        if not spec.get("enabled", False):
            continue
        klass_name = spec.get("type", name)
        if klass_name not in STRATEGY_REGISTRY:
            raise KeyError(f"Stratégie inconnue dans config.yaml : {klass_name!r}")
        strategy = STRATEGY_REGISTRY[klass_name](**(spec.get("params") or {}))
        strategy.name = name
        strategy.timeframe = spec.get("timeframe", strategy.timeframe)
        out.append((strategy, float(spec.get("capital_fraction", 1.0)), spec.get("market", "spot")))
    if not out:
        raise ValueError("Aucune stratégie activée dans config.yaml")
    total = sum(f for _, f, _ in out)
    if total > 1.0 + 1e-9:
        raise ValueError(f"Somme des capital_fraction = {total} > 1")
    return out
