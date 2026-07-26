"""Acquisition et déduplication temporelle des paiements de funding."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from .ports import ClockPort, MarketDataPort


def _as_utc(value: Any) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


@dataclass(frozen=True)
class FundingPayment:
    timestamp: pd.Timestamp
    rate: float


@dataclass(frozen=True)
class FundingPoll:
    checkpoint: pd.Timestamp
    payments: tuple[FundingPayment, ...]
    initialized: bool = False


class FundingService:
    def __init__(
        self,
        venue: MarketDataPort,
        clock: ClockPort,
        *,
        poll_seconds: float = 300,
    ) -> None:
        self.venue = venue
        self.clock = clock
        self.poll_seconds = poll_seconds
        self.last_poll_monotonic = float("-inf")

    def poll(self, since: pd.Timestamp | None) -> FundingPoll | None:
        monotonic_now = self.clock.monotonic()
        if monotonic_now - self.last_poll_monotonic < self.poll_seconds:
            return None
        self.last_poll_monotonic = monotonic_now
        current = _as_utc(self.clock.utc_now())
        if since is None:
            return FundingPoll(current, (), initialized=True)

        since = _as_utc(since)
        raw = self.venue.funding_history_since(since).copy()
        raw.index = pd.to_datetime(raw.index, utc=True)
        raw = raw[(raw.index > since) & (raw.index <= current)].sort_index()
        payments = tuple(
            FundingPayment(_as_utc(timestamp), float(rate)) for timestamp, rate in raw.items()
        )
        checkpoint = payments[-1].timestamp if payments else since
        return FundingPoll(checkpoint, payments)
