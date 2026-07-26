from __future__ import annotations

import pandas as pd

from btcquant.execution.funding_service import FundingService


class Clock:
    now = pd.Timestamp("2030-01-01T12:00:00Z")
    mono = 100.0

    def utc_now(self):
        return self.now

    def monotonic(self):
        return self.mono

    def time(self):
        return self.now.timestamp()


class Venue:
    payments_per_day = 24
    payments_per_year = 24 * 365

    def funding_history_since(self, _since):
        return pd.Series(
            [0.1, 0.2, 0.3],
            index=pd.DatetimeIndex(
                [
                    pd.Timestamp("2030-01-01T09:00:00Z"),
                    pd.Timestamp("2030-01-01T11:00:00Z"),
                    pd.Timestamp("2030-01-01T13:00:00Z"),
                ]
            ),
        )


class NaiveVenue(Venue):
    def funding_history_since(self, _since):
        return pd.Series(
            [0.1, 0.2, 0.3],
            index=pd.DatetimeIndex(
                [
                    pd.Timestamp("2030-01-01T09:00:00"),
                    pd.Timestamp("2030-01-01T11:00:00"),
                    pd.Timestamp("2030-01-01T13:00:00"),
                ]
            ),
        )


def test_first_poll_initializes_without_loading_historical_payments():
    service = FundingService(Venue(), Clock())

    result = service.poll(None)

    assert result is not None
    assert result.initialized
    assert result.checkpoint == Clock.now
    assert result.payments == ()


def test_poll_filters_future_and_already_applied_payments_and_is_rate_limited():
    clock = Clock()
    service = FundingService(Venue(), clock, poll_seconds=300)
    since = pd.Timestamp("2030-01-01T10:00:00Z")

    result = service.poll(since)

    assert result is not None
    assert [payment.rate for payment in result.payments] == [0.2]
    assert result.checkpoint == pd.Timestamp("2030-01-01T11:00:00Z")
    assert service.poll(result.checkpoint) is None


def test_poll_normalizes_naive_venue_index_and_restored_checkpoint_to_utc():
    service = FundingService(NaiveVenue(), Clock())

    result = service.poll(pd.Timestamp("2030-01-01T10:00:00"))

    assert result is not None
    assert [payment.rate for payment in result.payments] == [0.2]
    assert result.payments[0].timestamp == pd.Timestamp("2030-01-01T11:00:00Z")
    assert result.payments[0].timestamp.tzinfo is not None
    assert result.checkpoint == pd.Timestamp("2030-01-01T11:00:00Z")
