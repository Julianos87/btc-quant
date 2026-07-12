"""Stratégie swing trend-following (timeframe 4h par défaut).

Fondements empiriques :
- le trend following sur BTC bat le buy-and-hold en risque ajusté
  (croisements de moyennes ~10-30 jours, Sharpe ~1.6-1.7 dans plusieurs
  études 2012-2025) ;
- les breakouts de canal (Donchian) capturent les départs de tendance ;
- le stop chandelier (plus haut close depuis l'entrée − k·ATR) laisse
  courir les gains tout en coupant les retournements.

Règles :
- Régime haussier : EMA(fast) > EMA(slow).
- Entrée : clôture > plus haut des `donchian` barres précédentes, en régime haussier.
- Stop initial : entrée − atr_mult·ATR.
- Stop suiveur : plus haut close depuis l'entrée − atr_mult·ATR (ratchet).
- Sortie de régime : EMA(fast) repasse sous EMA(slow).
"""

from __future__ import annotations

import pandas as pd

from ..indicators import atr, donchian_high, ema
from .base import Position, Strategy


class TrendSwing(Strategy):
    name = "trend_swing"
    timeframe = "4h"

    @staticmethod
    def default_params() -> dict:
        return {
            "ema_fast": 50,
            "ema_slow": 200,
            "donchian": 55,
            "atr_period": 14,
            "atr_mult": 3.0,
        }

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        out = df.copy()
        out["ema_fast"] = ema(out["close"], p["ema_fast"])
        out["ema_slow"] = ema(out["close"], p["ema_slow"])
        out["donchian_high"] = donchian_high(out, p["donchian"])
        out["atr"] = atr(out, p["atr_period"])
        out["regime_up"] = out["ema_fast"] > out["ema_slow"]
        return out

    def entry_signal(self, row: pd.Series) -> int:
        return int(
            bool(row["regime_up"])
            and pd.notna(row["donchian_high"])
            and pd.notna(row["atr"])
            and row["close"] > row["donchian_high"]
        )

    def initial_stop(self, row: pd.Series, entry_price: float, direction: int = 1) -> float:
        return entry_price - self.params["atr_mult"] * row["atr"]

    def trailing_stop(self, row: pd.Series, position: Position) -> float | None:
        if pd.isna(row["atr"]):
            return None
        return position.best_close - self.params["atr_mult"] * row["atr"]

    def exit_signal(self, row: pd.Series, position: Position) -> bool:
        return not bool(row["regime_up"])

    def warmup_bars(self) -> int:
        return max(self.params["ema_slow"], self.params["donchian"]) + 20
