"""Horloge système, adaptateur du port temporel des runners."""

from __future__ import annotations

import time

import pandas as pd


class SystemClock:
    def utc_now(self) -> pd.Timestamp:
        return pd.Timestamp.now(tz="UTC")

    def time(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()
