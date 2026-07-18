"""Tests des indicateurs techniques.

Priorité donnée à deux propriétés dont dépend la validité de tout le système :

1. l'absence de look-ahead — un indicateur qui « voit » la barre courante rend
   les backtests non reproductibles en live ;
2. la convention de lissage de Wilder (RMA), utilisée par ATR/RSI/ADX, qui n'est
   pas la même qu'une EMA classique et fausserait les stops si elle dérivait.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btcquant.indicators import (
    adx,
    atr,
    bars_per_year,
    donchian_high,
    donchian_low,
    ema,
    realized_vol,
    rolling_median,
    rsi,
    sma,
    true_range,
)


def _ohlc(closes, highs=None, lows=None) -> pd.DataFrame:
    """OHLC minimal indexé en UTC 4 h, cohérent (high >= close >= low)."""
    closes = np.asarray(closes, dtype=float)
    highs = np.asarray(highs, dtype=float) if highs is not None else closes + 1.0
    lows = np.asarray(lows, dtype=float) if lows is not None else closes - 1.0
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="4h", tz="UTC")
    return pd.DataFrame(
        {"open": closes, "high": highs, "low": lows, "close": closes}, index=idx
    )


# ── moyennes ────────────────────────────────────────────────────────────────


def test_ema_matches_manual_recursion():
    """EMA(adjust=False) suit e_t = a*x_t + (1-a)*e_{t-1}, amorcée sur la SMA."""
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    out = ema(s, 3)
    alpha = 2.0 / (3 + 1)
    # min_periods=3 : les deux premières valeurs sont NaN, la 3e amorce
    assert out.iloc[:2].isna().all()
    expected = s.iloc[:3].mean()  # pandas amorce sur la moyenne des 3 premières
    # la valeur d'amorce dépend de l'implémentation ; on vérifie la récurrence
    # à partir de là, qui est ce qui compte pour la stabilité des signaux.
    prev = out.iloc[2]
    for i in (3, 4):
        expected = alpha * s.iloc[i] + (1 - alpha) * prev
        assert out.iloc[i] == pytest.approx(expected)
        prev = out.iloc[i]


def test_ema_requires_full_period_before_emitting():
    s = pd.Series(range(10), dtype=float)
    assert ema(s, 5).iloc[:4].isna().all()
    assert ema(s, 5).iloc[4:].notna().all()


def test_sma_is_plain_rolling_mean():
    s = pd.Series([1.0, 2.0, 3.0, 4.0])
    assert sma(s, 2).tolist()[1:] == [1.5, 2.5, 3.5]


# ── true range / ATR ────────────────────────────────────────────────────────


def test_true_range_uses_previous_close_on_gap():
    """Sur un gap haussier, le TR doit intégrer l'écart avec la clôture N-1,
    sinon les stops ATR sous-estiment le risque après un trou de cotation."""
    df = pd.DataFrame(
        {
            "open": [100.0, 120.0],
            "high": [101.0, 122.0],
            "low": [99.0, 119.0],
            "close": [100.0, 121.0],
        }
    )
    tr = true_range(df)
    # barre 1 : high-low = 3, |high - close_prev| = 22 -> le TR retient 22
    assert tr.iloc[1] == pytest.approx(22.0)


def test_atr_uses_wilder_smoothing_not_standard_ema():
    """Wilder = EMA d'alpha 1/N, pas 2/(N+1). Confondre les deux élargirait
    les stops d'environ 2x sur les petites périodes."""
    df = _ohlc(closes=[100.0] * 10, highs=[101.0] * 10, lows=[99.0] * 10)
    out = atr(df, period=3)
    # TR constant = 2 -> l'ATR converge vers 2 quelle que soit la convention,
    # on vérifie donc l'amorce sur une série à TR variable.
    df2 = _ohlc(closes=[100, 100, 100, 110], highs=[101, 101, 101, 115], lows=[99, 99, 99, 105])
    tr = true_range(df2)
    a = atr(df2, period=3)
    alpha = 1.0 / 3
    expected = alpha * tr.iloc[3] + (1 - alpha) * a.iloc[2]
    assert a.iloc[3] == pytest.approx(expected)
    assert out.iloc[-1] == pytest.approx(2.0)


def test_atr_is_nan_during_warmup():
    df = _ohlc(closes=[100.0] * 10)
    assert atr(df, period=5).iloc[:4].isna().all()


# ── RSI / ADX ───────────────────────────────────────────────────────────────


def test_rsi_saturates_at_100_when_only_gains():
    s = pd.Series(np.arange(1, 30, dtype=float))
    out = rsi(s, 14)
    assert out.dropna().iloc[-1] == pytest.approx(100.0)


def test_rsi_stays_within_bounds():
    rng = np.random.default_rng(0)
    s = pd.Series(100 + rng.normal(0, 2, 200).cumsum())
    out = rsi(s, 14).dropna()
    assert out.between(0.0, 100.0).all()


def test_adx_stays_within_bounds_and_warms_up():
    rng = np.random.default_rng(1)
    closes = 100 + rng.normal(0, 2, 300).cumsum()
    df = _ohlc(closes, highs=closes + 1.5, lows=closes - 1.5)
    out = adx(df, 14)
    assert out.iloc[:13].isna().all()
    assert out.dropna().between(0.0, 100.0).all()


def test_adx_is_high_on_a_clean_trend():
    """Une tendance monotone doit produire un ADX nettement au-dessus du seuil
    de 20/25 utilisé comme filtre d'entrée."""
    closes = np.arange(100, 200, dtype=float)
    df = _ohlc(closes, highs=closes + 0.5, lows=closes - 0.5)
    assert adx(df, 14).dropna().iloc[-1] > 25.0


# ── Donchian : la propriété anti look-ahead ─────────────────────────────────


def test_donchian_excludes_current_bar():
    """LE test qui compte : donchian_high(t) ne doit voir que t-N..t-1.

    Sans le décalage, `close > donchian_high` serait toujours faux (le plus
    haut inclurait la barre courante) et la stratégie ne prendrait aucun trade
    — ou pire, en prendrait sur une information du futur.
    """
    highs = [10.0, 11.0, 12.0, 100.0, 13.0]
    df = _ohlc(closes=[9.0, 10.0, 11.0, 99.0, 12.0], highs=highs, lows=[1.0] * 5)
    dh = donchian_high(df, 3)
    # à t=3, le plus haut des 3 barres précédentes (10, 11, 12) = 12
    assert dh.iloc[3] == pytest.approx(12.0)
    # la barre 3 (high=100) n'apparaît qu'à t=4
    assert dh.iloc[4] == pytest.approx(100.0)


def test_donchian_low_excludes_current_bar():
    lows = [10.0, 9.0, 8.0, 1.0, 7.0]
    df = _ohlc(closes=[10.0] * 5, highs=[20.0] * 5, lows=lows)
    dl = donchian_low(df, 3)
    assert dl.iloc[3] == pytest.approx(8.0)
    assert dl.iloc[4] == pytest.approx(1.0)


def test_donchian_breakout_is_detectable():
    """Une clôture au-dessus du canal doit être strictement supérieure."""
    df = _ohlc(closes=[10, 10, 10, 15.0], highs=[10, 10, 10, 15.0], lows=[10] * 4)
    dh = donchian_high(df, 3)
    assert df["close"].iloc[3] > dh.iloc[3]


# ── volatilité ──────────────────────────────────────────────────────────────


def test_realized_vol_scales_with_annualisation():
    rng = np.random.default_rng(2)
    s = pd.Series(100 * np.exp(rng.normal(0, 0.01, 500).cumsum()))
    v_daily = realized_vol(s, 30, 365).dropna().iloc[-1]
    v_hourly = realized_vol(s, 30, 8760).dropna().iloc[-1]
    assert v_hourly == pytest.approx(v_daily * np.sqrt(8760 / 365))


def test_realized_vol_is_zero_on_flat_series():
    s = pd.Series([100.0] * 50)
    assert realized_vol(s, 10, 365).dropna().iloc[-1] == pytest.approx(0.0)


def test_rolling_median_ignores_outlier():
    s = pd.Series([1.0, 2.0, 3.0, 1000.0, 4.0])
    assert rolling_median(s, 3).iloc[3] == pytest.approx(3.0)


# ── barres par an ───────────────────────────────────────────────────────────


def test_bars_per_year_known_timeframes():
    assert bars_per_year("4h") == 2190
    assert bars_per_year("1d") == 365
    # cohérence : 4h = 6 barres/jour
    assert bars_per_year("4h") == pytest.approx(365 * 6)


def test_bars_per_year_rejects_unknown_timeframe():
    with pytest.raises(ValueError, match="Timeframe inconnu"):
        bars_per_year("3h")
