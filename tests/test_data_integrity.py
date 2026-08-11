from __future__ import annotations

import pandas as pd
import pytest

from btcquant.data import cadence_report, resample
from btcquant.data_integrity import (
    DataIntegrityError,
    GapPolicy,
    frame_provenance,
    require_cadence,
    validate_cadence,
)


def _bars(index: pd.DatetimeIndex) -> pd.DataFrame:
    values = [float(i + 1) for i in range(len(index))]
    return pd.DataFrame(
        {
            "open": values,
            "high": [value + 1.0 for value in values],
            "low": [value - 1.0 for value in values],
            "close": values,
            "volume": [1.0] * len(values),
        },
        index=index,
    )


def test_perfect_hourly_series_has_no_cadence_anomaly() -> None:
    index = pd.date_range("2030-01-01", periods=5, freq="1h", tz="UTC")

    report = cadence_report(index, "1h")

    assert report.is_valid
    assert report.is_structurally_valid
    assert report.rows == 5
    assert report.expected_bars == 5
    assert report.present_bars == 5
    assert report.missing_bars == 0
    assert report.gap_count == 0
    assert report.availability_ratio == pytest.approx(1.0)


def _gap_index() -> pd.DatetimeIndex:
    return pd.DatetimeIndex(
        [
            pd.Timestamp("2030-01-01T00:00:00Z"),
            pd.Timestamp("2030-01-01T01:00:00Z"),
            pd.Timestamp("2030-01-01T03:00:00Z"),
            pd.Timestamp("2030-01-01T04:00:00Z"),
        ]
    )


def test_hourly_gap_is_reported_as_missing_bar() -> None:
    report = cadence_report(_gap_index(), "1h")

    assert not report.is_valid
    assert report.is_structurally_valid
    assert "GAP" in report.anomalies
    assert report.missing_bars == 1
    assert report.gap_count == 1
    assert report.missing_timestamps == (pd.Timestamp("2030-01-01T02:00:00Z"),)


def test_allow_reported_accepts_gap_but_keeps_full_inventory() -> None:
    report = validate_cadence(
        _gap_index(),
        "1h",
        gap_policy=GapPolicy.ALLOW_REPORTED,
    )

    payload = report.to_dict()
    assert payload["expected_rows_if_continuous"] == 5
    assert payload["actual_rows"] == 4
    assert payload["missing_rows"] == 1
    assert payload["gap_count"] == 1
    assert payload["missing_timestamps"] == ["2030-01-01T02:00:00+00:00"]


def test_reject_policy_fails_on_an_explicit_gap() -> None:
    with pytest.raises(DataIntegrityError, match="GAP"):
        validate_cadence(_gap_index(), "1h", gap_policy=GapPolicy.REJECT)


def test_duplicate_timestamp_is_not_silently_normalized() -> None:
    index = pd.DatetimeIndex(
        [
            pd.Timestamp("2030-01-01T00:00:00Z"),
            pd.Timestamp("2030-01-01T01:00:00Z"),
            pd.Timestamp("2030-01-01T01:00:00Z"),
            pd.Timestamp("2030-01-01T02:00:00Z"),
        ]
    )

    report = cadence_report(index, "1h")

    assert not report.is_valid
    assert "DUPLICATE" in report.anomalies
    assert report.duplicates == (pd.Timestamp("2030-01-01T01:00:00Z"),)
    with pytest.raises(DataIntegrityError, match="DUPLICATE"):
        validate_cadence(index, "1h", gap_policy=GapPolicy.ALLOW_REPORTED)


def test_out_of_order_timestamp_is_reported_before_normalization() -> None:
    index = pd.DatetimeIndex(
        [
            pd.Timestamp("2030-01-01T00:00:00Z"),
            pd.Timestamp("2030-01-01T02:00:00Z"),
            pd.Timestamp("2030-01-01T01:00:00Z"),
            pd.Timestamp("2030-01-01T03:00:00Z"),
        ]
    )

    report = cadence_report(index, "1h")

    assert not report.is_valid
    assert "OUT_OF_ORDER" in report.anomalies
    with pytest.raises(DataIntegrityError, match="OUT_OF_ORDER"):
        validate_cadence(index, "1h", gap_policy=GapPolicy.ALLOW_REPORTED)


def test_unexpected_59_minute_interval_is_structural() -> None:
    index = pd.date_range("2030-01-01", periods=3, freq="59min", tz="UTC")

    report = cadence_report(index, "1h")

    assert not report.is_valid
    assert "UNEXPECTED_INTERVAL" in report.anomalies
    with pytest.raises(DataIntegrityError, match="UNEXPECTED_INTERVAL"):
        validate_cadence(index, "1h", gap_policy=GapPolicy.ALLOW_REPORTED)


def test_unaligned_timestamp_is_structural() -> None:
    index = pd.DatetimeIndex(
        [
            pd.Timestamp("2030-01-01T00:30:00Z"),
            pd.Timestamp("2030-01-01T01:30:00Z"),
        ]
    )

    report = cadence_report(index, "1h")

    assert "UNALIGNED_TIMESTAMP" in report.anomalies
    with pytest.raises(DataIntegrityError, match="UNALIGNED_TIMESTAMP"):
        validate_cadence(index, "1h", gap_policy=GapPolicy.ALLOW_REPORTED)


def test_historical_contract_rejects_a_dataset_without_duration() -> None:
    index = pd.DatetimeIndex([pd.Timestamp("2030-01-01T00:00:00Z")])

    with pytest.raises(DataIntegrityError, match="Durée historique insuffisante"):
        require_cadence(index, "1h")


def test_dst_series_converted_to_utc_has_no_fictional_hour() -> None:
    local = pd.date_range("2030-03-30 23:00", periods=6, freq="1h", tz="Europe/Paris").tz_convert(
        "UTC"
    )

    report = cadence_report(local, "1h")

    assert report.is_valid
    assert report.rows == 6
    assert report.calendar_duration == pd.Timedelta(hours=5)


def test_frame_provenance_exposes_source_span_and_missing_bars() -> None:
    index = pd.DatetimeIndex(
        [
            pd.Timestamp("2030-01-01T00:00:00Z"),
            pd.Timestamp("2030-01-01T02:00:00Z"),
        ]
    )
    provenance = frame_provenance(_bars(index), source="fixture", expected_frequency="1h")

    assert provenance["source"] == "fixture"
    assert provenance["rows"] == 2
    assert provenance["expected_rows_if_continuous"] == 3
    assert provenance["span"] == [index[0].isoformat(), index[-1].isoformat()]
    assert provenance["missing_rows"] == 1
    assert provenance["gap_count"] == 1


def test_resample_drops_a_4h_window_with_a_missing_hour() -> None:
    index = pd.DatetimeIndex(
        [
            pd.Timestamp("2030-01-01T00:00:00Z"),
            pd.Timestamp("2030-01-01T01:00:00Z"),
            pd.Timestamp("2030-01-01T03:00:00Z"),
        ]
    )

    result = resample(_bars(index), "4h", source_frequency="1h")

    assert result.empty
    assert "PARTIAL_WINDOW" in result.attrs["cadence_report"]["anomalies"]


def test_resample_keeps_exactly_one_complete_4h_window() -> None:
    index = pd.date_range("2030-01-01", periods=4, freq="1h", tz="UTC")

    result = resample(_bars(index), "4h", source_frequency="1h")

    assert len(result) == 1
    assert result.index[0] == index[0]
    assert result.iloc[0]["open"] == 1.0
    assert result.iloc[0]["close"] == 4.0
