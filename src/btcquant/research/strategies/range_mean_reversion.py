"""Retour à la moyenne expérimental, actif uniquement en régime peu directionnel."""

from __future__ import annotations

import pandas as pd

from ...indicators import adx, atr, bars_per_year, ema, realized_vol
from ...strategies.base import Position, Strategy


class RangeMeanReversion(Strategy):
    name = "range_mean_reversion"
    timeframe = "4h"

    @staticmethod
    def default_params() -> dict:
        return {
            "lookback": 40,
            "z_entry": 2.0,
            "adx_period": 14,
            "adx_max": 20.0,
            "exit_adx_min": 25.0,
            "atr_period": 14,
            "atr_mult": 2.0,
            "ema_fast": 50,
            "ema_slow": 200,
            "max_ema_gap_atr": 1.0,
            "exit_ema_gap_atr": 1.5,
            "vol_lookback": 30,
            "max_annual_vol": 1.2,
            "max_bars_held": 30,
        }

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        params = self.params
        out = df.copy()
        prior = out["close"].shift(1)
        mean = prior.rolling(params["lookback"], min_periods=params["lookback"]).mean()
        std = prior.rolling(params["lookback"], min_periods=params["lookback"]).std()
        out["zscore"] = (out["close"] - mean) / std.replace(0.0, float("nan"))
        out["adx"] = adx(out, params["adx_period"])
        out["atr"] = atr(out, params["atr_period"])
        out["ema_fast"] = ema(out["close"], params["ema_fast"])
        out["ema_slow"] = ema(out["close"], params["ema_slow"])
        out["ema_gap_atr"] = (
            (out["ema_fast"] - out["ema_slow"]).abs()
            / out["atr"].replace(0.0, float("nan"))
        )
        out["annual_vol"] = realized_vol(
            out["close"],
            params["vol_lookback"],
            bars_per_year(self.timeframe),
        )
        return out

    def entry_signal(self, row: pd.Series) -> int:
        if (
            pd.isna(row["zscore"])
            or pd.isna(row["adx"])
            or pd.isna(row["atr"])
            or pd.isna(row["ema_gap_atr"])
            or pd.isna(row["annual_vol"])
            or row["adx"] > self.params["adx_max"]
            or row["ema_gap_atr"] > self.params["max_ema_gap_atr"]
            or row["annual_vol"] > self.params["max_annual_vol"]
        ):
            return 0
        if row["zscore"] <= -self.params["z_entry"]:
            return 1
        if row["zscore"] >= self.params["z_entry"]:
            return -1
        return 0

    def initial_stop(self, row: pd.Series, entry_price: float, direction: int = 1) -> float:
        return entry_price - direction * self.params["atr_mult"] * row["atr"]

    def exit_signal(self, row: pd.Series, position: Position) -> bool:
        reverted = (position.direction == 1 and row["zscore"] >= 0) or (
            position.direction == -1 and row["zscore"] <= 0
        )
        regime_invalid = (
            pd.isna(row["adx"])
            or pd.isna(row["ema_gap_atr"])
            or pd.isna(row["annual_vol"])
            or row["adx"] >= self.params["exit_adx_min"]
            or row["ema_gap_atr"] >= self.params["exit_ema_gap_atr"]
            or row["annual_vol"] > self.params["max_annual_vol"]
        )
        return bool(
            reverted
            or regime_invalid
            or position.bars_held >= self.params["max_bars_held"]
        )

    def warmup_bars(self) -> int:
        return (
            max(
                self.params["lookback"],
                2 * self.params["adx_period"],
                self.params["ema_slow"],
                self.params["vol_lookback"],
            )
            + 10
        )
