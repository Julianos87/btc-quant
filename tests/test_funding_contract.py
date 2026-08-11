from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from btcquant import carry


def test_real_funding_missing_file_fails_without_fallback(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        carry.resolve_funding(
            "BTC/USDT:USDT",
            data_dir=tmp_path,
            refresh=False,
            mode="REAL",
            synthetic_rate=0.0001,
        )


def test_real_funding_corrupt_file_fails_without_fallback(tmp_path: Path) -> None:
    path = tmp_path / "binanceusdm_BTCUSDT_USDT_funding.csv"
    path.write_text("ts,wrong_column\n2030-01-01T00:00:00Z,0.01\n", encoding="utf-8")

    with pytest.raises((KeyError, ValueError)):
        carry.resolve_funding(
            "BTC/USDT:USDT",
            data_dir=tmp_path,
            refresh=False,
            mode="REAL",
            synthetic_rate=0.0001,
        )


def test_synthetic_funding_requires_explicit_mode_and_exposes_source(tmp_path: Path) -> None:
    resolved = carry.resolve_funding(
        "BTC/USDT:USDT",
        data_dir=tmp_path,
        refresh=False,
        mode="SYNTHETIC_EXPLICIT",
        synthetic_rate=0.0002,
    )

    assert resolved.source == "synthetic_constant"
    assert resolved.rate == pytest.approx(0.0002)
    assert resolved.series is None


def test_funding_after_ohlcv_span_is_rejected() -> None:
    bars = pd.DataFrame(
        {"open": [100.0], "high": [101.0], "low": [99.0], "close": [100.0], "volume": [1.0]},
        index=pd.DatetimeIndex([pd.Timestamp("2030-01-01T00:00:00Z")]),
    )
    funding = pd.Series(
        [0.001],
        index=pd.DatetimeIndex([pd.Timestamp("2030-01-01T08:00:00Z")]),
        name="rate",
    )

    with pytest.raises(ValueError, match="après la dernière bougie"):
        carry.add_funding_columns(bars, funding, "4h")


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        (
            "ts,rate\n2030-01-01T00:00:00Z,0.01\n2030-01-01T00:00:00Z,0.02\n",
            "DUPLICATE",
        ),
        (
            "ts,rate\n2030-01-01T00:00:00Z,0.01\n",
            "Durée",
        ),
        (
            "ts,rate\n2030-01-01T08:00:00Z,0.01\n2030-01-01T00:00:00Z,0.02\n",
            "OUT_OF_ORDER",
        ),
    ],
)
def test_real_funding_duplicate_or_out_of_order_fails(
    tmp_path: Path, rows: str, message: str
) -> None:
    path = tmp_path / "binanceusdm_BTCUSDT_USDT_funding.csv"
    path.write_text(rows, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        carry.resolve_funding(
            "BTC/USDT:USDT",
            data_dir=tmp_path,
            refresh=False,
            mode="REAL",
        )
