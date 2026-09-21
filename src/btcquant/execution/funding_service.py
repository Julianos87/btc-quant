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
    # A funding rate is not enough to settle a linear contract. The venue's
    # reference price must be carried with the event; None is deliberate
    # and means that accounting cannot safely be completed for an open
    # position.
    reference_price: float | None = None
    reference_price_timestamp: pd.Timestamp | None = None
    reference_price_source: str | None = None


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
            self._payment(_as_utc(timestamp), float(rate)) for timestamp, rate in raw.items()
        )
        checkpoint = payments[-1].timestamp if payments else since
        return FundingPoll(checkpoint, payments)

    def _payment(self, timestamp: pd.Timestamp, rate: float) -> FundingPayment:
        """Attach an explicit venue price when the data port can provide one.

        The resolver is intentionally optional for compatibility with data
        ports that only expose rates. The runner treats an unresolved price
        as an accounting uncertainty when a position was active at the event;
        it never substitutes the current mark price.
        """

        resolver = getattr(self.venue, "funding_reference_price", None)
        if not callable(resolver):
            return FundingPayment(timestamp, rate)
        resolved = resolver(timestamp)
        if isinstance(resolved, dict):
            price = resolved.get("price")
            price_timestamp = resolved.get("timestamp", timestamp)
            source = resolved.get("source")
        elif isinstance(resolved, tuple) and len(resolved) == 3:
            price, price_timestamp, source = resolved
        else:
            # A bare price has no auditable provenance and is therefore not
            # accepted as a qualified accounting input.
            price, price_timestamp, source = resolved, timestamp, None
        return FundingPayment(
            timestamp,
            rate,
            float(price) if price is not None else None,
            _as_utc(price_timestamp) if price_timestamp is not None else None,
            str(source) if source is not None else None,
        )
