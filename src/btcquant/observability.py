"""Contracts for truthful operational observations.

This module deliberately contains no exchange client and no trading-store
writer.  It is shared by the dashboard, readiness and metrics paths so that a
cached observation cannot silently be presented as a live one.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Generic, TypeVar, cast

T = TypeVar("T")
MAX_FUTURE_SKEW_SECONDS = 5.0


class Freshness(StrEnum):
    FRESH = "FRESH"
    STALE = "STALE"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


class SafetyStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


def utc_now() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


@dataclass(frozen=True)
class SourceSnapshot(Generic[T]):
    """A value plus its observation and transport provenance."""

    value: T | None
    source: str
    observed_at: datetime | None
    received_at: datetime
    age_seconds: float | None
    freshness: Freshness
    error: str | None = None
    last_success_at: datetime | None = None
    expected_interval_seconds: float | None = None
    freshness_lateness_seconds: float | None = None

    @classmethod
    def success(
        cls,
        value: T,
        *,
        source: str,
        observed_at: datetime | None,
        received_at: datetime | None = None,
        max_age_seconds: float | None = None,
        expected_interval_seconds: float | None = None,
    ) -> SourceSnapshot[T]:
        received = as_utc(received_at) or utc_now()
        observed = as_utc(observed_at)
        delta = (received - observed).total_seconds() if observed is not None else None
        future = delta is not None and delta < -MAX_FUTURE_SKEW_SECONDS
        missing_timestamp = observed is None
        age = None if missing_timestamp or future else max(0.0, delta or 0.0)
        expected = expected_interval_seconds
        lateness = (
            None if age is None else max(0.0, age - expected) if expected is not None else age
        )
        freshness = Freshness.UNKNOWN if missing_timestamp or future else Freshness.FRESH
        error = (
            "OBSERVATION_TIMESTAMP_UNAVAILABLE"
            if missing_timestamp
            else "CLOCK_SKEW"
            if future
            else None
        )
        if (
            freshness is Freshness.FRESH
            and lateness is not None
            and max_age_seconds is not None
            and lateness > max_age_seconds
        ):
            freshness = Freshness.STALE
        return cls(
            value,
            source,
            observed,
            received,
            age,
            freshness,
            error=error,
            last_success_at=received,
            expected_interval_seconds=expected,
            freshness_lateness_seconds=lateness,
        )

    @classmethod
    def unavailable(
        cls,
        *,
        source: str,
        received_at: datetime | None = None,
        error: str | None = None,
        last_success_at: datetime | None = None,
        age_seconds: float | None = None,
    ) -> SourceSnapshot[None]:
        return cast(
            SourceSnapshot[None],
            cls(
                None,
                source,
                None,
                as_utc(received_at) or utc_now(),
                age_seconds,
                Freshness.UNAVAILABLE,
                error=error,
                last_success_at=as_utc(last_success_at),
            ),
        )

    def at(
        self,
        *,
        now: datetime | None = None,
        max_age_seconds: float | None = None,
        max_stale_seconds: float | None = None,
        error: str | None = None,
        expected_interval_seconds: float | None = None,
    ) -> SourceSnapshot[T]:
        current = as_utc(now) or utc_now()
        expected = (
            self.expected_interval_seconds
            if expected_interval_seconds is None
            else expected_interval_seconds
        )
        reference = self.observed_at or self.received_at
        delta = (current - reference).total_seconds()
        future = self.observed_at is not None and delta < -MAX_FUTURE_SKEW_SECONDS
        age = None if future else max(0.0, delta)
        lateness = (
            None if age is None else max(0.0, age - expected) if expected is not None else age
        )
        if self.value is None:
            status = Freshness.UNAVAILABLE
        elif future:
            status = Freshness.UNKNOWN
        elif self.freshness is Freshness.UNKNOWN:
            status = Freshness.UNKNOWN
        elif (
            lateness is not None and max_stale_seconds is not None and lateness > max_stale_seconds
        ):
            status = Freshness.UNAVAILABLE
        elif lateness is not None and max_age_seconds is not None and lateness > max_age_seconds:
            status = Freshness.STALE
        else:
            status = self.freshness
        return replace(
            self,
            age_seconds=age,
            freshness=status,
            error=error if error is not None else ("CLOCK_SKEW" if future else self.error),
            expected_interval_seconds=expected,
            freshness_lateness_seconds=lateness,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "received_at": self.received_at.isoformat(),
            "age_seconds": self.age_seconds,
            "observation_age_seconds": self.age_seconds,
            "lateness_seconds": self.freshness_lateness_seconds,
            "expected_interval_seconds": self.expected_interval_seconds,
            "freshness": self.freshness.value,
            "error": self.error,
            "last_success_at": self.last_success_at.isoformat() if self.last_success_at else None,
        }


@dataclass(frozen=True)
class CachePolicy:
    """Refresh TTL and acceptable fallback age are intentionally separate."""

    ttl_seconds: float
    max_stale_seconds: float
    max_age_seconds: float
    expected_interval_seconds: float | None = None


@dataclass
class _CacheEntry(Generic[T]):
    snapshot: SourceSnapshot[T]
    refreshed_monotonic: float


class BoundedReadCache(Generic[T]):
    """Small single-process cache for idempotent reads only.

    A failed refresh may return a bounded STALE value, never an unbounded one.
    The monotonic clock controls refresh TTL; UTC timestamps control operator
    age and provenance.
    """

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._monotonic = monotonic
        self._now = now
        self._entries: dict[str, _CacheEntry[T]] = {}

    def get(
        self,
        key: str,
        policy: CachePolicy,
        loader: Callable[[], T],
        *,
        source: str,
        observed_at: Callable[[T], datetime | None] | None = None,
    ) -> SourceSnapshot[T]:
        current_mono = self._monotonic()
        current = self._now()
        entry = self._entries.get(key)
        if entry is not None and current_mono - entry.refreshed_monotonic < policy.ttl_seconds:
            return entry.snapshot.at(
                now=current,
                max_age_seconds=policy.max_age_seconds,
                max_stale_seconds=policy.max_stale_seconds,
                expected_interval_seconds=policy.expected_interval_seconds,
            )
        try:
            value = loader()
            if value is None:
                snapshot = cast(
                    SourceSnapshot[T],
                    SourceSnapshot.unavailable(
                        source=source,
                        received_at=current,
                        error="SOURCE_UNAVAILABLE",
                    ),
                )
                self._entries[key] = _CacheEntry(snapshot, current_mono)
                return snapshot
            observed = observed_at(value) if observed_at is not None else None
            snapshot = SourceSnapshot.success(
                value,
                source=source,
                observed_at=observed,
                received_at=current,
                max_age_seconds=policy.max_age_seconds,
                expected_interval_seconds=policy.expected_interval_seconds,
            ).at(
                now=current,
                max_age_seconds=policy.max_age_seconds,
                max_stale_seconds=policy.max_stale_seconds,
                expected_interval_seconds=policy.expected_interval_seconds,
            )
            self._entries[key] = _CacheEntry(snapshot, current_mono)
            return snapshot
        except Exception as exc:
            message = type(exc).__name__
            if entry is None:
                return cast(
                    SourceSnapshot[T],
                    SourceSnapshot.unavailable(source=source, received_at=current, error=message),
                )
            fallback = entry.snapshot.at(
                now=current,
                max_age_seconds=policy.max_age_seconds,
                max_stale_seconds=policy.max_stale_seconds,
                error=message,
                expected_interval_seconds=policy.expected_interval_seconds,
            )
            if fallback.freshness is Freshness.UNAVAILABLE:
                return cast(
                    SourceSnapshot[T],
                    SourceSnapshot.unavailable(
                        source=source,
                        received_at=current,
                        error=message,
                        last_success_at=entry.snapshot.last_success_at,
                        age_seconds=fallback.age_seconds,
                    ),
                )
            if fallback.freshness is Freshness.UNKNOWN:
                return replace(fallback, freshness=Freshness.UNKNOWN, error=message)
            return replace(fallback, freshness=Freshness.STALE)

    def clear(self) -> None:
        self._entries.clear()


def temporal_skew(
    observations: dict[str, datetime | None],
    *,
    max_skew_seconds: float,
    now: datetime | None = None,
) -> dict[str, object]:
    known = {
        name: normalized
        for name, value in observations.items()
        if (normalized := as_utc(value)) is not None
    }
    if not known:
        return {
            "oldest_source_at": None,
            "newest_source_at": None,
            "max_source_skew_seconds": None,
            "freshness_status": Freshness.UNKNOWN.value,
        }
    oldest = min(known.values())
    newest = max(known.values())
    skew = max(0.0, (newest - oldest).total_seconds())
    current = as_utc(now) or utc_now()
    has_future = any(
        (current - timestamp).total_seconds() < -MAX_FUTURE_SKEW_SECONDS
        for timestamp in known.values()
    )
    return {
        "oldest_source_at": oldest.isoformat(),
        "newest_source_at": newest.isoformat(),
        "max_source_skew_seconds": skew,
        "freshness_status": (
            Freshness.UNKNOWN.value
            if has_future
            else Freshness.FRESH.value
            if skew <= max_skew_seconds
            else Freshness.STALE.value
        ),
    }
