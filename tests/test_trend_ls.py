"""Tests de TrendLS — la stratégie effectivement exécutée en paper (config_4x).

Couvre les règles d'entrée (cassure + régime EMA), les filtres ADX et funding,
la symétrie long/short des stops, et la sortie sur retournement de régime.

Les lignes sont construites à la main plutôt que dérivées d'un OHLC : on teste
la LOGIQUE DE DÉCISION isolément, les indicateurs étant couverts par
test_indicators.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btcquant.strategies.base import Position
from btcquant.strategies.trend_ls import TrendLS


def _row(**over) -> pd.Series:
    """Ligne neutre : régime haussier, pas de cassure, indicateurs valides."""
    base = {
        "close": 100.0,
        "atr": 2.0,
        "donchian_high": 110.0,
        "donchian_low": 90.0,
        "ema_fast": 105.0,
        "ema_slow": 100.0,
        "regime_up": True,
        "adx": 30.0,
    }
    base.update(over)
    return pd.Series(base)


# ── entrées ─────────────────────────────────────────────────────────────────


def test_long_on_breakout_in_uptrend():
    s = TrendLS()
    assert s.entry_signal(_row(close=111.0)) == 1


def test_short_on_breakdown_in_downtrend():
    s = TrendLS()
    assert s.entry_signal(_row(close=89.0, regime_up=False)) == -1


def test_no_entry_without_breakout():
    s = TrendLS()
    assert s.entry_signal(_row(close=100.0)) == 0


def test_breakout_must_be_strict():
    """close == donchian_high n'est pas une cassure."""
    s = TrendLS()
    assert s.entry_signal(_row(close=110.0)) == 0


def test_regime_blocks_counter_trend_entry():
    """Cassure haussière mais régime baissier : pas de long.

    C'est le filtre qui empêche d'acheter les rebonds dans un marché baissier.
    """
    s = TrendLS()
    assert s.entry_signal(_row(close=111.0, regime_up=False)) == 0
    # symétrique : cassure basse en régime haussier
    assert s.entry_signal(_row(close=89.0, regime_up=True)) == 0


def test_nan_indicators_produce_no_signal():
    """Pendant la chauffe, aucun trade ne doit être pris."""
    s = TrendLS()
    for col in ("atr", "donchian_high", "ema_slow"):
        assert s.entry_signal(_row(close=111.0, **{col: np.nan})) == 0


# ── filtre ADX ──────────────────────────────────────────────────────────────


def test_adx_filter_blocks_weak_trend():
    s = TrendLS(adx_min=25)
    assert s.entry_signal(_row(close=111.0, adx=30.0)) == 1
    assert s.entry_signal(_row(close=111.0, adx=20.0)) == 0


def test_adx_filter_blocks_when_adx_is_nan():
    """ADX indisponible + filtre actif : on s'abstient plutôt que de passer."""
    s = TrendLS(adx_min=25)
    assert s.entry_signal(_row(close=111.0, adx=np.nan)) == 0


def test_adx_ignored_when_disabled():
    s = TrendLS(adx_min=None)
    assert s.entry_signal(_row(close=111.0, adx=1.0)) == 1


# ── filtre funding (contrarien) ─────────────────────────────────────────────
#
# Convention : `funding` est un taux en ÉQUIVALENT 8 H, comme le seuil.
# Le runner live le renseigne via Venue.funding_rate_8h().


def test_funding_filter_blocks_crowded_long():
    """Funding très positif = longs surpeuplés : on ne rejoint pas la foule."""
    s = TrendLS(funding_long_max=0.0008)
    assert s.entry_signal(_row(close=111.0, funding=0.0005)) == 1
    assert s.entry_signal(_row(close=111.0, funding=0.0010)) == 0


def test_funding_filter_blocks_late_short():
    """Funding très négatif = capitulation : on ne shorte pas le bas."""
    s = TrendLS(funding_short_min=-0.0008)
    row = {"close": 89.0, "regime_up": False}
    assert s.entry_signal(_row(**row, funding=-0.0005)) == -1
    assert s.entry_signal(_row(**row, funding=-0.0010)) == 0


def test_funding_filter_is_neutral_when_value_missing():
    """Funding indisponible (NaN ou colonne absente) : le filtre ne doit pas
    bloquer, sinon une panne d'API arrêterait tout le trading."""
    s = TrendLS(funding_long_max=0.0008)
    assert s.entry_signal(_row(close=111.0, funding=np.nan)) == 1
    assert s.entry_signal(_row(close=111.0)) == 1  # colonne absente


def test_funding_filter_ignored_when_disabled():
    s = TrendLS(funding_long_max=None)
    assert s.entry_signal(_row(close=111.0, funding=0.05)) == 1


def test_funding_threshold_is_exclusive():
    """Exactement au seuil : on passe encore (comparaison stricte)."""
    s = TrendLS(funding_long_max=0.0008)
    assert s.entry_signal(_row(close=111.0, funding=0.0008)) == 1


# ── stops ───────────────────────────────────────────────────────────────────


def test_initial_stop_is_below_entry_for_long():
    s = TrendLS(atr_mult=3.0)
    assert s.initial_stop(_row(atr=2.0), entry_price=100.0, direction=1) == pytest.approx(94.0)


def test_initial_stop_is_above_entry_for_short():
    s = TrendLS(atr_mult=3.0)
    assert s.initial_stop(_row(atr=2.0), entry_price=100.0, direction=-1) == pytest.approx(106.0)


def test_trailing_stop_follows_best_close():
    s = TrendLS(atr_mult=3.0)
    pos = Position(
        entry_time=pd.Timestamp("2026-01-01", tz="UTC"),
        entry_price=100.0, qty=1.0, stop_price=94.0, direction=1, best_close=120.0,
    )
    assert s.trailing_stop(_row(atr=2.0), pos) == pytest.approx(114.0)


def test_trailing_stop_is_mirrored_for_short():
    s = TrendLS(atr_mult=3.0)
    pos = Position(
        entry_time=pd.Timestamp("2026-01-01", tz="UTC"),
        entry_price=100.0, qty=1.0, stop_price=106.0, direction=-1, best_close=80.0,
    )
    assert s.trailing_stop(_row(atr=2.0), pos) == pytest.approx(86.0)


def test_trailing_stop_none_when_atr_missing():
    s = TrendLS()
    pos = Position(
        entry_time=pd.Timestamp("2026-01-01", tz="UTC"),
        entry_price=100.0, qty=1.0, stop_price=94.0, direction=1, best_close=100.0,
    )
    assert s.trailing_stop(_row(atr=np.nan), pos) is None


# ── sortie ──────────────────────────────────────────────────────────────────


def test_exit_on_regime_reversal():
    s = TrendLS()
    long_pos = Position(
        entry_time=pd.Timestamp("2026-01-01", tz="UTC"),
        entry_price=100.0, qty=1.0, stop_price=94.0, direction=1,
    )
    assert s.exit_signal(_row(regime_up=False), long_pos) is True
    assert s.exit_signal(_row(regime_up=True), long_pos) is False


def test_exit_on_regime_reversal_for_short():
    s = TrendLS()
    short_pos = Position(
        entry_time=pd.Timestamp("2026-01-01", tz="UTC"),
        entry_price=100.0, qty=1.0, stop_price=106.0, direction=-1,
    )
    assert s.exit_signal(_row(regime_up=True), short_pos) is True
    assert s.exit_signal(_row(regime_up=False), short_pos) is False


# ── prepare / warmup ────────────────────────────────────────────────────────


def _ohlc(n: int = 400) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    closes = 100 + rng.normal(0, 1, n).cumsum()
    idx = pd.date_range("2026-01-01", periods=n, freq="4h", tz="UTC")
    return pd.DataFrame(
        {"open": closes, "high": closes + 1.0, "low": closes - 1.0, "close": closes},
        index=idx,
    )


def test_prepare_adds_expected_columns_without_mutating_input():
    df = _ohlc()
    before = df.columns.tolist()
    out = TrendLS().prepare(df)
    assert df.columns.tolist() == before, "prepare() ne doit pas modifier l'entrée"
    for col in ("ema_fast", "ema_slow", "donchian_high", "donchian_low", "atr", "regime_up"):
        assert col in out.columns


def test_prepare_omits_adx_when_filter_disabled():
    out = TrendLS(adx_min=None).prepare(_ohlc())
    assert "adx" not in out.columns
    out2 = TrendLS(adx_min=20).prepare(_ohlc())
    assert "adx" in out2.columns


def test_indicators_are_valid_after_warmup():
    s = TrendLS()
    out = s.prepare(_ohlc(600))
    tail = out.iloc[s.warmup_bars():]
    for col in ("ema_fast", "ema_slow", "donchian_high", "donchian_low", "atr"):
        assert tail[col].notna().all(), f"{col} encore NaN après la chauffe"


def test_warmup_covers_slowest_indicator():
    s = TrendLS(ema_slow=200, donchian=100)
    assert s.warmup_bars() >= 200
    s2 = TrendLS(ema_slow=50, donchian=300)
    assert s2.warmup_bars() >= 300
