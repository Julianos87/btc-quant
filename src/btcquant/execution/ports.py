"""Ports applicatifs consommés par les runners, sans dépendre de CCXT."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

import pandas as pd

Notifier = Callable[[str], bool]


class MarketDataPort(Protocol):
    payments_per_day: int

    @property
    def payments_per_year(self) -> int: ...

    def last_price(self) -> float: ...

    def fetch_ohlcv(self, timeframe: str, limit: int = 1000) -> list[list]: ...

    def funding_rate_8h(self) -> float: ...

    def funding_history(self, days: float) -> pd.Series: ...

    def funding_history_since(self, since: pd.Timestamp) -> pd.Series: ...


class ClockPort(Protocol):
    def utc_now(self) -> pd.Timestamp: ...

    def time(self) -> float: ...

    def monotonic(self) -> float: ...
