"""Contracts for historical market-data cadence and provenance."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from pathlib import Path

import pandas as pd


class GapPolicy(StrEnum):
    """Policy selected by a consumer for explicitly detected cadence gaps."""

    REJECT = "reject"
    ALLOW_REPORTED = "allow_reported"


@dataclass(frozen=True)
class CadenceReport:
    """Serializable observation report for a timestamped dataset."""

    expected_frequency: str
    expected_delta: pd.Timedelta
    rows: int
    start: pd.Timestamp | None
    end: pd.Timestamp | None
    calendar_duration: pd.Timedelta
    expected_bars: int
    present_bars: int
    missing_bars: int
    missing_timestamps: tuple[pd.Timestamp, ...]
    duplicates: tuple[pd.Timestamp, ...]
    out_of_order: bool
    unexpected_intervals: tuple[pd.Timedelta, ...]
    anomalies: tuple[str, ...]

    @property
    def availability_ratio(self) -> float:
        return self.present_bars / self.expected_bars if self.expected_bars else 0.0

    @property
    def gap_groups(self) -> tuple[tuple[pd.Timestamp, ...], ...]:
        groups: list[list[pd.Timestamp]] = []
        for timestamp in self.missing_timestamps:
            if not groups or timestamp - groups[-1][-1] != self.expected_delta:
                groups.append([])
            groups[-1].append(timestamp)
        return tuple(tuple(group) for group in groups)

    @property
    def gap_count(self) -> int:
        return len(self.gap_groups)

    @property
    def is_valid(self) -> bool:
        return not self.anomalies

    @property
    def is_structurally_valid(self) -> bool:
        return not any(anomaly != "GAP" for anomaly in self.anomalies)

    def to_dict(self) -> dict[str, object]:
        return {
            "expected_frequency": self.expected_frequency,
            "expected_delta": self.expected_delta.total_seconds(),
            "rows": self.rows,
            "actual_rows": self.rows,
            "start": self.start.isoformat() if self.start is not None else None,
            "end": self.end.isoformat() if self.end is not None else None,
            "calendar_duration_seconds": self.calendar_duration.total_seconds(),
            "elapsed_span": self.calendar_duration.total_seconds(),
            "expected_bars": self.expected_bars,
            "expected_rows_if_continuous": self.expected_bars,
            "present_bars": self.present_bars,
            "missing_bars": self.missing_bars,
            "missing_rows": self.missing_bars,
            "missing_timestamps": [value.isoformat() for value in self.missing_timestamps],
            "gap_count": self.gap_count,
            "gap_groups": [
                {
                    "start": group[0].isoformat(),
                    "end": group[-1].isoformat(),
                    "rows": len(group),
                }
                for group in self.gap_groups
            ],
            "duplicates": [value.isoformat() for value in self.duplicates],
            "out_of_order": self.out_of_order,
            "unexpected_intervals_seconds": [
                value.total_seconds() for value in self.unexpected_intervals
            ],
            "anomalies": list(self.anomalies),
            "availability_ratio": self.availability_ratio,
        }


class DataIntegrityError(ValueError):
    """Historical data cannot satisfy the requested temporal contract."""


def _utc_index(index: pd.Index) -> pd.DatetimeIndex:
    if not isinstance(index, pd.DatetimeIndex):
        raise TypeError("L'index doit être un DatetimeIndex")
    if index.tz is None:
        raise DataIntegrityError("Index temporel sans fuseau : convertir en UTC explicitement")
    return index.tz_convert("UTC")


def cadence_report(index: pd.Index, expected_frequency: str) -> CadenceReport:
    """Classify cadence anomalies without sorting or deduplicating observations."""

    expected_delta = pd.Timedelta(expected_frequency)
    if expected_delta <= pd.Timedelta(0):
        raise ValueError("La fréquence attendue doit être strictement positive")
    timestamps = _utc_index(index)
    rows = len(timestamps)
    if not rows:
        return CadenceReport(
            expected_frequency=expected_frequency,
            expected_delta=expected_delta,
            rows=0,
            start=None,
            end=None,
            calendar_duration=pd.Timedelta(0),
            expected_bars=0,
            present_bars=0,
            missing_bars=0,
            missing_timestamps=(),
            duplicates=(),
            out_of_order=False,
            unexpected_intervals=(),
            anomalies=(),
        )

    unique_sorted = pd.DatetimeIndex(timestamps.unique()).sort_values()
    start = unique_sorted[0]
    end = unique_sorted[-1]
    calendar_duration = end - start
    expected_bars = int(calendar_duration // expected_delta) + 1
    expected_index = pd.date_range(start, end, freq=expected_delta, tz="UTC")
    missing = tuple(expected_index.difference(unique_sorted))
    duplicates = tuple(pd.DatetimeIndex(timestamps[timestamps.duplicated(keep=False)]).unique())
    deltas = timestamps.to_series().diff().dropna()
    out_of_order = bool((deltas < pd.Timedelta(0)).any())
    unexpected = tuple(
        sorted(
            {
                delta
                for delta in deltas
                if delta > pd.Timedelta(0) and delta.value % expected_delta.value != 0
            }
        )
    )
    epoch_ns = pd.Timestamp("1970-01-01T00:00:00Z").value
    unaligned = any(
        (timestamp.value - epoch_ns) % expected_delta.value != 0 for timestamp in timestamps
    )
    anomalies: list[str] = []
    if duplicates:
        anomalies.append("DUPLICATE")
    if out_of_order:
        anomalies.append("OUT_OF_ORDER")
    if missing:
        anomalies.append("GAP")
    if unexpected:
        anomalies.append("UNEXPECTED_INTERVAL")
    if unaligned:
        anomalies.append("UNALIGNED_TIMESTAMP")
    return CadenceReport(
        expected_frequency=expected_frequency,
        expected_delta=expected_delta,
        rows=rows,
        start=start,
        end=end,
        calendar_duration=calendar_duration,
        expected_bars=expected_bars,
        present_bars=len(unique_sorted),
        missing_bars=len(missing),
        missing_timestamps=missing,
        duplicates=duplicates,
        out_of_order=out_of_order,
        unexpected_intervals=unexpected,
        anomalies=tuple(anomalies),
    )


def portable_sha256(path: str | Path) -> str:
    """Hash a text/binary source with stable line-ending semantics."""
    return sha256(Path(path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def frame_provenance(
    frame: pd.DataFrame | pd.Series,
    *,
    source: str,
    expected_frequency: str,
    path: str | Path | None = None,
) -> dict[str, object]:
    """Describe the exact observations used by a quantitative result."""
    report = cadence_report(frame.index, expected_frequency)
    result = report.to_dict()
    result.update(
        {
            "source": source,
            "path": str(path) if path is not None else None,
            "sha256": portable_sha256(path) if path is not None else None,
            "span": [report.start.isoformat(), report.end.isoformat()]
            if report.start is not None and report.end is not None
            else None,
        }
    )
    return result


def validate_cadence(
    index: pd.Index,
    expected_frequency: str,
    *,
    gap_policy: GapPolicy | str = GapPolicy.REJECT,
) -> CadenceReport:
    """Validate structural integrity and apply an explicit gap policy."""
    report = cadence_report(index, expected_frequency)
    policy = GapPolicy(gap_policy)
    if not report.is_structurally_valid:
        structural = [anomaly for anomaly in report.anomalies if anomaly != "GAP"]
        raise DataIntegrityError(
            f"Cadence {expected_frequency} structurelle invalide : {', '.join(structural)}"
        )
    if report.missing_bars and policy is GapPolicy.REJECT:
        raise DataIntegrityError(f"Cadence {expected_frequency} contient {report.missing_bars} GAP")
    if report.rows < 2:
        raise DataIntegrityError(
            "Durée historique insuffisante : au moins deux observations sont requises"
        )
    return report


def require_cadence(index: pd.Index, expected_frequency: str) -> CadenceReport:
    """Backward-compatible strict validation for callers needing complete data."""
    return validate_cadence(index, expected_frequency, gap_policy=GapPolicy.REJECT)
