"""Trend following long-short sur canal de Donchian (perpétuels).

Fondements documentés :
- système de cassure de canal « turtle » classique (Donchian 20/55), étudié
  depuis les années 1980 et validé sur crypto (arXiv 2009.12155 : une décennie
  de trend following crypto rentable ; Concretum 2018-2025 : benchmark trend
  long-short Sharpe ~1.6 vs ~0.8 pour le buy-and-hold à vol égale) ;
- la version SHORT capture les tendances baissières que le long/flat ne fait
  qu'éviter ;
- les paramètres (20/55/100) sont des standards de la littérature, PAS
  optimisés sur nos données — l'ensemble des trois horizons diversifie le
  risque de paramètre (pratique CTA courante).

Règles (symétriques long/short) :
- Long  : clôture > plus haut des N barres précédentes ET EMA50 > EMA200.
- Short : clôture < plus bas des N barres précédentes ET EMA50 < EMA200.
- Stop initial : entrée ∓ atr_mult·ATR ; stop suiveur chandelier depuis le
  meilleur close ; sortie sur retournement de régime EMA.
"""

from __future__ import annotations

import pandas as pd

from ..indicators import adx, atr, donchian_high, donchian_low, ema
from .base import Position, Strategy


class TrendLS(Strategy):
    name = "trend_ls"
    timeframe = "4h"

    @staticmethod
    def default_params() -> dict:
        return {
            "ema_fast": 50,
            "ema_slow": 200,
            "donchian": 55,
            "atr_period": 14,
            "atr_mult": 3.0,
            # filtre de force de tendance (None = désactivé) : bloque les
            # entrées quand ADX < seuil, i.e. en marché haché — réduction
            # documentée de 30-40 % des faux signaux
            "adx_period": 14,
            "adx_min": None,
            # filtre de funding contrarien (None = désactivé) : pas de
            # nouveau long quand le funding est extrême positif (longs
            # surpeuplés → risque de cascade de liquidations), pas de
            # nouveau short quand extrême négatif (capitulation).
            # Nécessite une colonne "funding" dans le DataFrame.
            "funding_long_max": None,
            "funding_short_min": None,
        }

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        out = df.copy()
        out["ema_fast"] = ema(out["close"], p["ema_fast"])
        out["ema_slow"] = ema(out["close"], p["ema_slow"])
        out["donchian_high"] = donchian_high(out, p["donchian"])
        out["donchian_low"] = donchian_low(out, p["donchian"])
        out["atr"] = atr(out, p["atr_period"])
        out["regime_up"] = out["ema_fast"] > out["ema_slow"]
        if p["adx_min"] is not None:
            out["adx"] = adx(out, p["adx_period"])
        return out

    def entry_signal(self, row: pd.Series) -> int:
        p = self.params
        if pd.isna(row["atr"]) or pd.isna(row["donchian_high"]) or pd.isna(row["ema_slow"]):
            return 0
        if p["adx_min"] is not None and not (pd.notna(row["adx"]) and row["adx"] >= p["adx_min"]):
            return 0
        funding = row.get("funding")
        if row["regime_up"] and row["close"] > row["donchian_high"]:
            if (
                p["funding_long_max"] is not None
                and pd.notna(funding)
                and funding > p["funding_long_max"]
            ):
                return 0  # longs surpeuplés
            return 1
        if not row["regime_up"] and row["close"] < row["donchian_low"]:
            if (
                p["funding_short_min"] is not None
                and pd.notna(funding)
                and funding < p["funding_short_min"]
            ):
                return 0  # capitulation, pas de short tardif
            return -1
        return 0

    def initial_stop(self, row: pd.Series, entry_price: float, direction: int = 1) -> float:
        return entry_price - direction * self.params["atr_mult"] * row["atr"]

    def trailing_stop(self, row: pd.Series, position: Position) -> float | None:
        if pd.isna(row["atr"]):
            return None
        return position.best_close - position.direction * self.params["atr_mult"] * row["atr"]

    def exit_signal(self, row: pd.Series, position: Position) -> bool:
        # retournement de régime contre la position
        return bool(row["regime_up"]) != (position.direction == 1)

    def warmup_bars(self) -> int:
        return max(self.params["ema_slow"], self.params["donchian"]) + 20
