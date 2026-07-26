"""Stratégie intraday momentum/breakout (timeframe 1h par défaut).

Fondements empiriques :
- prédictibilité intraday documentée sur BTC (time-series momentum :
  les sessions à fort volume prédisent la suite de la journée) ;
- les breakouts confirmés par le volume ont un taux de réussite supérieur
  aux breakouts à faible volume ;
- la mean reversion pure est perdante sur BTC — on ne trade que dans le
  sens de la tendance de fond.

Règles :
- Filtre de tendance : clôture > EMA(ema_trend) sur 1h.
- Entrée : clôture > plus haut des `lookback_high` heures précédentes
  ET volume > volume_mult × médiane du volume (30 jours).
- Stop initial : entrée − atr_mult·ATR.
- Stop suiveur : plus haut close depuis l'entrée − atr_mult·ATR.
- Stop temporel : sortie après `max_bars_held` barres si toujours en position
  (un breakout qui ne paie pas vite est probablement un faux signal).
"""

from __future__ import annotations

import pandas as pd

from ...indicators import atr, donchian_high, ema, rolling_median
from ...strategies.base import Position, Strategy


class IntradayBreakout(Strategy):
    name = "intraday_breakout"
    timeframe = "1h"

    @staticmethod
    def default_params() -> dict:
        return {
            "lookback_high": 24,
            "ema_trend": 200,
            "atr_period": 24,
            "atr_mult": 2.0,
            "volume_mult": 1.3,
            "volume_median_period": 720,  # 30 jours en barres 1h
            "max_bars_held": 48,
        }

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        out = df.copy()
        out["ema_trend"] = ema(out["close"], p["ema_trend"])
        out["hh"] = donchian_high(out, p["lookback_high"])
        out["atr"] = atr(out, p["atr_period"])
        out["vol_median"] = rolling_median(out["volume"], p["volume_median_period"])
        return out

    def entry_signal(self, row: pd.Series) -> int:
        return int(
            pd.notna(row["ema_trend"])
            and pd.notna(row["hh"])
            and pd.notna(row["atr"])
            and pd.notna(row["vol_median"])
            and row["close"] > row["ema_trend"]
            and row["close"] > row["hh"]
            and row["volume"] > self.params["volume_mult"] * row["vol_median"]
        )

    def initial_stop(self, row: pd.Series, entry_price: float, direction: int = 1) -> float:
        return entry_price - self.params["atr_mult"] * row["atr"]

    def trailing_stop(self, row: pd.Series, position: Position) -> float | None:
        if pd.isna(row["atr"]):
            return None
        return position.best_close - self.params["atr_mult"] * row["atr"]

    def exit_signal(self, row: pd.Series, position: Position) -> bool:
        return position.bars_held >= self.params["max_bars_held"]

    def warmup_bars(self) -> int:
        return max(self.params["ema_trend"], self.params["volume_median_period"]) + 20
