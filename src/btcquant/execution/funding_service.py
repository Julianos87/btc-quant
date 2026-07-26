"""Acquisition et déduplication temporelle des paiements de funding."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .ports import ClockPort, MarketDataPort


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
        current = self.clock.utc_now()
        if since is None:
            return FundingPoll(current, (), initialized=True)

        raw = self.venue.funding_history_since(since)
        raw = raw[(raw.index > since) & (raw.index <= current)].sort_index()
        payments = tuple(
            FundingPayment(pd.Timestamp(str(timestamp)), float(rate))
            for timestamp, rate in raw.items()
        )
        checkpoint = payments[-1].timestamp if payments else since
        return FundingPoll(checkpoint, payments)
