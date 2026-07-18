"""Parité du filtre funding entre backtest et paper.

Contexte : jusqu'en juillet 2026, le backtest ne créait que la colonne
`funding_rate` (P&L). `TrendLS` lisant une colonne distincte `funding` pour
filtrer les entrées, le filtre était **silencieusement inactif en backtest**
alors qu'il l'était en paper — les configs le déclarant pourtant actif
(`funding_long_max: 0.0008`). Les deux chemins ne prenaient donc pas les mêmes
décisions d'entrée.

Ces tests figent la correction : les deux colonnes existent, portent la bonne
unité, et le filtre agit réellement côté backtest.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btcquant.carry import add_funding_columns
from btcquant.strategies.trend_ls import TrendLS


def _funding_8h(n: int = 30, rate: float = 0.0001) -> pd.Series:
    """Taux 8 h natifs, aux heures réelles de paiement (00/08/16 UTC)."""
    idx = pd.date_range("2026-01-01", periods=n, freq="8h", tz="UTC")
    return pd.Series([rate] * n, index=idx, name="rate")


def _bars_4h(n: int = 60) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=n, freq="4h", tz="UTC")
    closes = np.linspace(100, 120, n)
    return pd.DataFrame(
        {"open": closes, "high": closes + 1, "low": closes - 1, "close": closes},
        index=idx,
    )


# ── les deux colonnes, deux unités ──────────────────────────────────────────


def test_both_columns_are_created():
    out = add_funding_columns(_bars_4h(), _funding_8h(), "4h")
    assert "funding_rate" in out.columns, "colonne du P&L manquante"
    assert "funding" in out.columns, "colonne du filtre d'entrée manquante"


def test_funding_rate_is_zero_on_half_the_4h_bars():
    """Les paiements tombent à 00/08/16 : une barre 4 h sur deux n'en reçoit
    aucun. C'est correct pour le P&L, et c'est précisément pourquoi cette
    colonne ne peut pas servir de valeur au filtre."""
    out = add_funding_columns(_bars_4h(), _funding_8h(), "4h")
    zero_share = (out["funding_rate"] == 0.0).mean()
    assert zero_share == pytest.approx(0.5, abs=0.05)


def test_funding_column_is_never_zeroed_between_payments():
    """Le filtre doit voir le dernier taux CONNU sur toutes les barres, pas un
    zéro artificiel une barre sur deux."""
    out = add_funding_columns(_bars_4h(), _funding_8h(rate=0.0009), "4h")
    active = out["funding"].dropna()
    assert len(active) > 0
    assert np.allclose(active.to_numpy(), 0.0009)


def test_funding_column_keeps_8h_unit():
    """`funding` doit rester en équivalent 8 h — même unité que le seuil des
    configs et que Venue.funding_rate_8h() côté live. Le confondre avec le
    cumul par barre sous-estimerait le taux d'un facteur 2 sur du 4 h."""
    rate = 0.0006
    out = add_funding_columns(_bars_4h(), _funding_8h(rate=rate), "4h")
    payment_bars = out.loc[out["funding_rate"] != 0.0]
    # sur une barre qui reçoit un paiement, les deux colonnes coïncident…
    assert payment_bars["funding_rate"].iloc[0] == pytest.approx(rate)
    # …et la colonne du filtre vaut ce même taux 8 h partout
    assert out["funding"].dropna().iloc[-1] == pytest.approx(rate)


def test_no_lookahead_before_first_payment():
    """Avant le premier paiement connu, le filtre ne doit rien voir."""
    bars = _bars_4h()
    late = _funding_8h(n=5)
    late.index = late.index + pd.Timedelta(days=3)
    out = add_funding_columns(bars, late, "4h")
    assert out["funding"].iloc[0] != out["funding"].iloc[0] or pd.isna(out["funding"].iloc[0])


def test_input_dataframe_is_not_mutated():
    df = _bars_4h()
    before = df.columns.tolist()
    add_funding_columns(df, _funding_8h(), "4h")
    assert df.columns.tolist() == before


# ── le filtre agit réellement, des deux côtés ───────────────────────────────


def test_filter_blocks_entry_with_backtest_columns():
    """LE test de parité : une ligne produite par add_funding_columns doit
    déclencher le filtre exactement comme une ligne du runner live."""
    strat = TrendLS(funding_long_max=0.0008)
    out = add_funding_columns(_bars_4h(), _funding_8h(rate=0.0010), "4h")

    row = out.iloc[-1].copy()
    # conditions d'entrée long réunies
    row["atr"] = 2.0
    row["donchian_high"] = 100.0
    row["donchian_low"] = 90.0
    row["ema_slow"] = 100.0
    row["regime_up"] = True
    row["close"] = 110.0
    row["adx"] = 30.0

    assert strat.entry_signal(row) == 0, "funding extrême : l'entrée doit être bloquée"

    # même ligne, funding sous le seuil -> l'entrée passe
    out_ok = add_funding_columns(_bars_4h(), _funding_8h(rate=0.0002), "4h")
    row_ok = out_ok.iloc[-1].copy()
    for k, v in row.items():
        if k != "funding":
            row_ok[k] = v
    assert strat.entry_signal(row_ok) == 1


def test_missing_funding_column_leaves_filter_neutral():
    """Si le funding n'a pas pu être chargé, le backtest tourne sans la colonne
    et le filtre doit rester neutre plutôt que de tout bloquer."""
    strat = TrendLS(funding_long_max=0.0008)
    row = pd.Series({
        "close": 110.0, "atr": 2.0, "donchian_high": 100.0, "donchian_low": 90.0,
        "ema_slow": 100.0, "regime_up": True, "adx": 30.0,
    })
    assert strat.entry_signal(row) == 1
