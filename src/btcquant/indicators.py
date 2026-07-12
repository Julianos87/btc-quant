"""Indicateurs techniques vectorisés (pandas), sans dépendance TA-Lib.

Convention anti look-ahead : chaque valeur à l'index t n'utilise que des
données <= t. Les indicateurs destinés à détecter un franchissement
(donchian_high/low) sont décalés d'une barre : la valeur à t est le plus
haut/bas des N barres PRÉCÉDENTES, si bien que `close > donchian_high`
est un vrai breakout et non une tautologie.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period, min_periods=period).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """ATR lissé à la Wilder (RMA)."""
    tr = true_range(df)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """RSI de Wilder."""
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    return out.fillna(100.0).where(avg_loss.notna(), np.nan)


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """ADX de Wilder : force de la tendance (0-100), direction ignorée.
    > 25 : tendance nette ; < 20 : marché haché où le trend following subit
    ses séquences de stops."""
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = ((up > down) & (up > 0)) * up
    minus_dm = ((down > up) & (down > 0)) * down
    alpha = 1.0 / period
    tr_s = true_range(df).ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    plus_di = 100 * plus_dm.ewm(alpha=alpha, adjust=False, min_periods=period).mean() / tr_s
    minus_di = 100 * minus_dm.ewm(alpha=alpha, adjust=False, min_periods=period).mean() / tr_s
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    return dx.ewm(alpha=alpha, adjust=False, min_periods=period).mean()


def donchian_high(df: pd.DataFrame, period: int) -> pd.Series:
    """Plus haut des `period` barres précédentes (barre courante exclue)."""
    return df["high"].rolling(period, min_periods=period).max().shift(1)


def donchian_low(df: pd.DataFrame, period: int) -> pd.Series:
    """Plus bas des `period` barres précédentes (barre courante exclue)."""
    return df["low"].rolling(period, min_periods=period).min().shift(1)


def realized_vol(series: pd.Series, period: int, bars_per_year: float) -> pd.Series:
    """Volatilité annualisée des rendements log sur `period` barres."""
    log_ret = np.log(series / series.shift(1))
    return log_ret.rolling(period, min_periods=period).std() * np.sqrt(bars_per_year)


def rolling_median(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period, min_periods=period).median()


BARS_PER_YEAR = {
    "1m": 525_600,
    "5m": 105_120,
    "15m": 35_040,
    "30m": 17_520,
    "1h": 8_760,
    "2h": 4_380,
    "4h": 2_190,
    "6h": 1_460,
    "12h": 730,
    "1d": 365,
}


def bars_per_year(timeframe: str) -> float:
    try:
        return float(BARS_PER_YEAR[timeframe])
    except KeyError:
        raise ValueError(f"Timeframe inconnu : {timeframe!r}") from None
