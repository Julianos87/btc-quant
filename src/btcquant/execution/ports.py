"""Ports applicatifs consommés par les runners, sans dépendre de CCXT."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

import pandas as pd

from .financial_fill_application import FinancialFillCommitResult

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


@runtime_checkable
class AtomicFinancialWriter(Protocol):
    """Narrow port for the existing atomic financial-fill transaction.

    Implementations must preserve the caller-visible E3 contract: one
    idempotent application attempt owns the ledger, financial projections and
    audit event in one transaction.  The port deliberately exposes no SQL or
    unrelated ``StateStore`` operations.
    """

    def apply_financial_fill_atomically(
        self, *, local_order_id: int, fill_key: str
    ) -> FinancialFillCommitResult: ...
