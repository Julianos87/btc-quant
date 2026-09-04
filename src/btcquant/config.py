"""Chargement des profils YAML d'environnement vers les objets du système."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .carry import CarryPolicy
from .domain import ExecutionConfig
from .risk import RiskConfig
from .strategies import STRATEGY_REGISTRY, Strategy

TIMEFRAMES = {"1h", "2h", "4h", "6h", "12h", "1d"}
MARKETS = {"spot", "perp"}

HYPERLIQUID_MAINNET_API_URL = "https://api.hyperliquid.xyz"
HYPERLIQUID_TESTNET_API_URL = "https://api.hyperliquid-testnet.xyz"


@dataclass(frozen=True)
class PortfolioConfig:
    total_capital: float
    trend_fraction: float
    carry_fraction: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.total_capital) or self.total_capital <= 0:
            raise ValueError("portfolio.total_capital doit être strictement positif")
        for name in ("trend_fraction", "carry_fraction"):
            value = getattr(self, name)
            if not math.isfinite(value) or not 0 < value < 1:
                raise ValueError(f"portfolio.{name} doit être dans ]0, 1[")
        if not math.isclose(self.trend_fraction + self.carry_fraction, 1.0):
            raise ValueError("portfolio : trend_fraction + carry_fraction doit valoir 1")

    @property
    def trend_capital(self) -> float:
        return self.total_capital * self.trend_fraction

    @property
    def carry_capital(self) -> float:
        return self.total_capital * self.carry_fraction


def load_config(path: str | Path = "environments/dev/config.yaml") -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        payload = yaml.safe_load(fh)
    if not isinstance(payload, dict):
        raise TypeError("La configuration YAML doit être un mapping")
    _validate_config(payload)
    return payload


def _finite_non_negative(name: str, value: Any) -> None:
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)) or value < 0:
        raise ValueError(f"{name} doit être un nombre fini positif ou nul")


def _validate_required_profile(cfg: dict[str, Any]) -> str:
    environment = cfg.get("environment")
    if environment not in {"dev", "paper", "testnet"}:
        raise ValueError("environment doit valoir dev, paper ou testnet")
    for section in ("data", "costs", "risk", "strategies", "execution"):
        if not isinstance(cfg.get(section), dict):
            raise ValueError(f"Section obligatoire absente ou invalide : {section}")
    if not isinstance(cfg.get("symbol"), str) or "/" not in cfg["symbol"]:
        raise ValueError("symbol doit être une paire de type BASE/QUOTE")
    return environment


def _validate_costs(costs: dict[str, Any]) -> None:
    for key in ("fee_rate", "perp_fee_rate", "funding_rate_8h", "slippage_bps"):
        if key in costs:
            _finite_non_negative(f"costs.{key}", costs[key])


def _validate_execution_profile(environment: str, execution: dict[str, Any]) -> None:
    mode = execution.get("mode", "paper")
    if mode not in {"paper", "testnet"}:
        raise ValueError("Safety Baseline : execution.mode doit valoir 'paper' ou 'testnet'")
    if mode == "testnet":
        if environment != "testnet":
            raise ValueError("execution.mode testnet exige environment: testnet")
        if execution.get("testnet") is not True:
            raise ValueError("Safety Baseline : le mode testnet exige execution.testnet: true")
        if execution.get("live_exchange") != "hyperliquid":
            raise ValueError("Safety Baseline : seul le testnet Hyperliquid est autorisé")
        if execution.get("api_url") != HYPERLIQUID_TESTNET_API_URL:
            raise ValueError(
                "Safety Baseline : le testnet Hyperliquid exige son endpoint API testnet explicite"
            )
        live_symbol = execution.get("live_symbol")
        if not isinstance(live_symbol, str) or not live_symbol.endswith(":USDC"):
            raise ValueError("Le testnet Hyperliquid exige un perpétuel coté en USDC")
    elif environment == "testnet":
        raise ValueError("environment: testnet exige execution.mode: testnet")


def _validate_strategies(strategies: dict[str, Any]) -> None:
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
        if spec.get("enabled", False):
            _validate_strategy_params(name, spec)


def _validate_tandem_capital(cfg: dict[str, Any], risk: RiskConfig) -> None:
    if "portfolio" not in cfg and "carry" not in cfg:
        return
    portfolio = portfolio_from_config(cfg)
    carry_policy_from_config(cfg)
    if not math.isclose(risk.initial_capital, portfolio.trend_capital):
        raise ValueError(
            "risk.initial_capital doit égaler portfolio.total_capital × portfolio.trend_fraction"
        )


def _validate_config(cfg: dict[str, Any]) -> None:
    environment = _validate_required_profile(cfg)
    _validate_costs(cfg["costs"])
    _validate_execution_profile(environment, cfg["execution"])
    _validate_strategies(cfg["strategies"])
    risk = risk_from_config(cfg)
    execution_config_from_config(cfg, float(cfg["costs"].get("fee_rate", 0.0)))
    _validate_tandem_capital(cfg, risk)


def _validate_strategy_params(name: str, spec: dict[str, Any]) -> None:
    """Refuse au chargement une stratégie inconnue ou un paramètre mal orthographié.

    La validation se fait par instanciation réelle : c'est la seule façon de
    garantir que le contrôle suit la stratégie quand ses paramètres évoluent.
    """

    klass_name = spec.get("type", name)
    klass = STRATEGY_REGISTRY.get(klass_name)
    if klass is None:
        raise KeyError(
            f"strategies.{name} : stratégie inconnue {klass_name!r} ; "
            f"disponibles : {sorted(STRATEGY_REGISTRY)}"
        )
    params = spec.get("params") or {}
    if not isinstance(params, dict):
        raise TypeError(f"strategies.{name}.params doit être un mapping YAML")
    try:
        klass(**params)
    except ValueError as error:
        raise ValueError(f"strategies.{name} : {error}") from error


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


def portfolio_from_config(cfg: dict[str, Any]) -> PortfolioConfig:
    raw = cfg.get("portfolio")
    if not isinstance(raw, dict):
        raise ValueError("Section portfolio obligatoire pour le profil TANDEM")
    allowed = {"total_capital", "trend_fraction", "carry_fraction"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"portfolio : clé(s) inconnue(s) {unknown}")
    try:
        return PortfolioConfig(**raw)
    except TypeError as error:
        raise ValueError(f"portfolio incomplet : {error}") from error


def carry_policy_from_config(cfg: dict[str, Any]) -> CarryPolicy:
    portfolio = portfolio_from_config(cfg)
    raw = cfg.get("carry")
    if not isinstance(raw, dict):
        raise ValueError("Section carry obligatoire pour le profil TANDEM")
    allowed = {"leverage", "enter_ann", "exit_ann", "smooth_days", "borrow_rate_ann"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"carry : clé(s) inconnue(s) {unknown}")
    try:
        return CarryPolicy(
            capital=portfolio.carry_capital,
            fee_rate=float(cfg["costs"]["perp_fee_rate"]),
            slippage_bps=float(cfg["costs"]["slippage_bps"]),
            **raw,
        )
    except TypeError as error:
        raise ValueError(f"carry incomplet : {error}") from error


def execution_config_from_config(
    cfg: dict[str, Any],
    fee_rate: float,
) -> ExecutionConfig:
    """Construit le modèle commun depuis ``execution.simulation``.

    Les frais et le slippage restent dans ``costs`` pour conserver une source
    unique ; les autres paramètres sont optionnels et désactivés par défaut.
    """

    raw_simulation = cfg.get("execution", {}).get("simulation") or {}
    if not isinstance(raw_simulation, dict):
        raise TypeError("execution.simulation doit être un mapping YAML")
    simulation = dict(raw_simulation)
    profile = simulation.pop("profile", None)
    profiles = simulation.pop("profiles", None)
    if profile is not None or profiles is not None:
        if not isinstance(profile, str) or not isinstance(profiles, dict):
            raise ValueError("execution.simulation exige profile et profiles")
        selected = profiles.get(profile)
        if not isinstance(selected, dict):
            raise ValueError(f"Profil de simulation inconnu : {profile!r}")
        simulation.update(selected)
    forbidden = {"fee_rate", "slippage_bps"} & simulation.keys()
    if forbidden:
        raise ValueError("Configurer fee_rate/slippage_bps dans costs, pas execution.simulation")
    try:
        return ExecutionConfig(
            fee_rate=fee_rate,
            slippage_bps=cfg["costs"]["slippage_bps"],
            **simulation,
        )
    except TypeError as error:
        raise ValueError(f"execution.simulation invalide : {error}") from error


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
            raise KeyError(f"Stratégie inconnue dans la configuration : {klass_name!r}")
        strategy = STRATEGY_REGISTRY[klass_name](**(spec.get("params") or {}))
        strategy.name = name
        strategy.timeframe = spec.get("timeframe", strategy.timeframe)
        out.append((strategy, float(spec.get("capital_fraction", 1.0)), spec.get("market", "spot")))
    if not out:
        raise ValueError("Aucune stratégie activée dans la configuration")
    total = sum(f for _, f, _ in out)
    if total > 1.0 + 1e-9:
        raise ValueError(f"Somme des capital_fraction = {total} > 1")
    return out
