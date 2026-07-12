"""Chargement de config.yaml vers les objets du système."""

from __future__ import annotations

from pathlib import Path

import yaml

from .risk import RiskConfig
from .strategies import STRATEGY_REGISTRY, Strategy


def load_config(path: str | Path = "config.yaml") -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def risk_from_config(cfg: dict) -> RiskConfig:
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


def build_strategies(cfg: dict) -> list[tuple[Strategy, float, str]]:
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
