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

    def __init__(self, **params) -> None:
        super().__init__(**params)
        threshold = self.params["funding_sizing_threshold"]
        floor = float(self.params["funding_sizing_floor"])
        if threshold is not None and float(threshold) <= 0:
            raise ValueError("funding_sizing_threshold doit être strictement positif")
        if not 0.0 <= floor <= 1.0:
            raise ValueError("funding_sizing_floor doit être dans [0, 1]")
        strong_adx = self.params["strong_trend_adx"]
        strong_atr = self.params["strong_trend_atr_mult"]
        if (strong_adx is None) != (strong_atr is None):
            raise ValueError(
                "strong_trend_adx et strong_trend_atr_mult doivent être activés ensemble"
            )
        if strong_adx is not None and (float(strong_adx) <= 0 or float(strong_atr) <= 0):
            raise ValueError("Les paramètres de tendance forte doivent être positifs")
        pyramid_step = self.params["pyramid_atr_step"]
        pyramid_fraction = float(self.params["pyramid_add_fraction"])
        pyramid_adds = int(self.params["pyramid_max_adds"])
        if pyramid_step is not None and float(pyramid_step) <= 0:
            raise ValueError("pyramid_atr_step doit être strictement positif")
        if not 0 < pyramid_fraction <= 1:
            raise ValueError("pyramid_add_fraction doit être dans ]0, 1]")
        if pyramid_adds < 0:
            raise ValueError("pyramid_max_adds doit être positif ou nul")

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
            # réduction progressive de la taille lorsque le funding indique
            # que le côté demandé est surpeuplé. None désactive ce sizing.
            "funding_sizing_threshold": None,
            "funding_sizing_floor": 0.25,
            # marge minimale au-delà du canal avant l'entrée. Elle permet de
            # refuser les cassures trop petites pour couvrir raisonnablement
            # les coûts aller-retour. 0 conserve strictement la règle historique.
            "entry_buffer_bps": 0.0,
            # Confirmation exprimée en ATR au-delà du canal. Utilisée par les
            # recherches de renforcement; 0 préserve l'entrée historique.
            "entry_buffer_atr": 0.0,
            # Laisse respirer une tendance déjà confirmée sans éloigner le stop
            # initial. Désactivé par défaut pour préserver la stratégie validée.
            "strong_trend_adx": None,
            "strong_trend_atr_mult": None,
            # Un renfort au plus, exprimé comme fraction de la tranche initiale,
            # après une progression favorable en ATR. None désactive le mécanisme.
            "pyramid_atr_step": None,
            "pyramid_add_fraction": 0.30,
            "pyramid_max_adds": 1,
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
        if p["adx_min"] is not None or p["strong_trend_adx"] is not None:
            out["adx"] = adx(out, p["adx_period"])
        return out

    def entry_signal(self, row: pd.Series) -> int:
        p = self.params
        if pd.isna(row["atr"]) or pd.isna(row["donchian_high"]) or pd.isna(row["ema_slow"]):
            return 0
        if p["adx_min"] is not None and not (pd.notna(row["adx"]) and row["adx"] >= p["adx_min"]):
            return 0
        funding = row.get("funding")
        entry_buffer = float(p["entry_buffer_bps"]) / 10_000.0
        atr_buffer = float(p["entry_buffer_atr"]) * float(row["atr"])
        long_trigger = row["donchian_high"] * (1.0 + entry_buffer) + atr_buffer
        short_trigger = row["donchian_low"] * (1.0 - entry_buffer) - atr_buffer
        if row["regime_up"] and row["close"] > long_trigger:
            if (
                p["funding_long_max"] is not None
                and pd.notna(funding)
                and funding > p["funding_long_max"]
            ):
                return 0  # longs surpeuplés
            return 1
        if not row["regime_up"] and row["close"] < short_trigger:
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

    def position_size_multiplier(self, row: pd.Series, direction: int) -> float:
        threshold = self.params["funding_sizing_threshold"]
        funding = row.get("funding")
        if threshold is None or funding is None or pd.isna(funding):
            return 1.0
        crowding = direction * float(funding)
        if crowding <= 0:
            return 1.0
        floor = float(self.params["funding_sizing_floor"])
        progress = min(1.0, crowding / float(threshold))
        return max(floor, 1.0 - (1.0 - floor) * progress)

    def trailing_stop(self, row: pd.Series, position: Position) -> float | None:
        if pd.isna(row["atr"]):
            return None
        atr_mult = float(self.params["atr_mult"])
        strong_adx = self.params["strong_trend_adx"]
        if strong_adx is not None and pd.notna(row.get("adx")) and row["adx"] >= strong_adx:
            atr_mult = float(self.params["strong_trend_atr_mult"])
        return position.best_close - position.direction * atr_mult * row["atr"]

    def pyramid_fraction(self, row: pd.Series, position: Position) -> float:
        step = self.params["pyramid_atr_step"]
        if (
            step is None
            or position.pyramid_adds >= int(self.params["pyramid_max_adds"])
            or pd.isna(row.get("atr"))
            or bool(row["regime_up"]) != (position.direction == 1)
        ):
            return 0.0
        favorable_move = position.direction * (float(row["close"]) - position.last_add_price)
        if favorable_move < float(step) * float(row["atr"]):
            return 0.0
        return float(self.params["pyramid_add_fraction"])

    def exit_signal(self, row: pd.Series, position: Position) -> bool:
        # retournement de régime contre la position
        return bool(row["regime_up"]) != (position.direction == 1)

    def warmup_bars(self) -> int:
        return max(self.params["ema_slow"], self.params["donchian"]) + 20
