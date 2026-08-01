"""Gouverneur de risque causal, auto-calibre sur le regime recent.

Le controleur ne predit pas le prochain rendement et ne choisit pas une
strategie gagnante a posteriori. Il mesure seulement trois proprietes
observables a la cloture courante : efficacite directionnelle, force ADX et
choc de volatilite. Les references statistiques sont calculees sur les barres
strictement anterieures, puis le multiplicateur est lisse et borne.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .indicators import realized_vol


@dataclass(frozen=True)
class AdaptiveRegimeConfig:
    efficiency_bars: int = 30
    volatility_bars: int = 30
    reference_bars: int = 540
    smoothing_span: int = 12
    minimum_multiplier: float = 0.50
    maximum_multiplier: float = 1.00
    volatility_shock_ratio: float = 2.00

    def __post_init__(self) -> None:
        for name in (
            "efficiency_bars",
            "volatility_bars",
            "reference_bars",
            "smoothing_span",
        ):
            if getattr(self, name) < 2:
                raise ValueError(f"{name} doit etre >= 2")
        if self.reference_bars <= max(self.efficiency_bars, self.volatility_bars):
            raise ValueError("reference_bars doit depasser les fenetres de mesure")
        if not 0 < self.minimum_multiplier <= self.maximum_multiplier <= 1:
            raise ValueError("les multiplicateurs doivent verifier 0 < minimum <= maximum <= 1")
        if self.volatility_shock_ratio <= 1:
            raise ValueError("volatility_shock_ratio doit etre > 1")


def _safe_ratio(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    return numerator / denominator.replace(0.0, np.nan)


def adaptive_regime_frame(
    close: pd.Series,
    adx_values: pd.Series,
    *,
    bars_per_year: float,
    config: AdaptiveRegimeConfig | None = None,
) -> pd.DataFrame:
    """Retourne scores, etiquette de regime et multiplicateur de risque.

    Les medianes de reference sont decalees d'une barre. Une modification des
    donnees futures ne peut donc jamais changer une decision deja produite.
    """

    cfg = config or AdaptiveRegimeConfig()
    movement = close.diff().abs()
    directional = (close - close.shift(cfg.efficiency_bars)).abs()
    travelled = movement.rolling(
        cfg.efficiency_bars,
        min_periods=cfg.efficiency_bars,
    ).sum()
    efficiency = _safe_ratio(directional, travelled).clip(0.0, 1.0)
    efficiency_reference = (
        efficiency.shift(1)
        .rolling(cfg.reference_bars, min_periods=cfg.reference_bars)
        .quantile(0.75)
    )
    efficiency_score = _safe_ratio(efficiency, efficiency_reference).clip(0.0, 1.0)

    volatility = realized_vol(close, cfg.volatility_bars, bars_per_year)
    volatility_reference = (
        volatility.shift(1)
        .rolling(cfg.reference_bars, min_periods=cfg.reference_bars)
        .median()
    )
    volatility_ratio = _safe_ratio(volatility, volatility_reference)
    shock_progress = (
        (volatility_ratio - 1.0) / (cfg.volatility_shock_ratio - 1.0)
    ).clip(0.0, 1.0)
    volatility_score = 1.0 - shock_progress

    adx_score = ((adx_values - 15.0) / 20.0).clip(0.0, 1.0)
    trend_quality = (0.55 * efficiency_score + 0.45 * adx_score).clip(0.0, 1.0)
    raw_score = (trend_quality * volatility_score).clip(0.0, 1.0)
    raw_multiplier = cfg.minimum_multiplier + (
        cfg.maximum_multiplier - cfg.minimum_multiplier
    ) * raw_score
    multiplier = raw_multiplier.ewm(
        span=cfg.smoothing_span,
        adjust=False,
        min_periods=cfg.smoothing_span,
    ).mean()
    multiplier = multiplier.clip(cfg.minimum_multiplier, cfg.maximum_multiplier)

    regime = pd.Series("WARMUP", index=close.index, dtype="object")
    ready = multiplier.notna()
    regime.loc[ready] = "TRANSITION"
    regime.loc[ready & (raw_score <= 0.33)] = "CHOP"
    regime.loc[ready & (raw_score >= 0.67)] = "TREND"
    regime.loc[ready & (volatility_score < 0.50)] = "STRESS"

    return pd.DataFrame(
        {
            "adaptive_efficiency": efficiency,
            "adaptive_efficiency_score": efficiency_score,
            "adaptive_volatility": volatility,
            "adaptive_volatility_ratio": volatility_ratio,
            "adaptive_volatility_score": volatility_score,
            "adaptive_trend_quality": trend_quality,
            "adaptive_regime_score": raw_score,
            "adaptive_risk_multiplier": multiplier,
            "adaptive_regime": regime,
        },
        index=close.index,
    )
