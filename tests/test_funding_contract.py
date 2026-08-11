from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from btcquant import carry


ROOT = Path(__file__).resolve().parents[1]


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


def _write_binance_funding_cache(
    tmp_path: Path, timestamps: list[str], rates: list[float] | None = None
) -> Path:
    path = carry.funding_cache_path("BTC/USDT:USDT", tmp_path)
    index = pd.to_datetime(timestamps, utc=True, format="mixed")
    values = rates if rates is not None else [0.001] * len(timestamps)
    pd.DataFrame({"rate": values}, index=index).to_csv(path, index_label="ts")
    return path


def test_binance_usdm_funding_cache_path_is_canonical(tmp_path: Path) -> None:
    expected = tmp_path / "binanceusdm_BTCUSDT_USDT_funding.csv"
    assert carry.funding_cache_path("BTC/USDT:USDT", tmp_path) == expected

    from scripts import make_yearly_reference

    assert make_yearly_reference.funding_cache_path("BTC/USDT:USDT", tmp_path) == expected


@pytest.mark.parametrize(
    "jittered_timestamp",
    [
        "2030-01-01T00:00:00.047Z",
        "2030-01-01T07:59:59.953Z",
        "2030-01-01T16:00:00.999Z",
    ],
)
def test_load_funding_accepts_binance_millisecond_jitter(
    tmp_path: Path, jittered_timestamp: str
) -> None:
    timestamps = [
        jittered_timestamp if "00:00:00" in jittered_timestamp else "2030-01-01T00:00:00Z",
        jittered_timestamp if "07:59:59" in jittered_timestamp else "2030-01-01T08:00:00Z",
        jittered_timestamp if "16:00:00" in jittered_timestamp else "2030-01-01T16:00:00Z",
    ]
    _write_binance_funding_cache(tmp_path, timestamps)

    result = carry.load_funding("BTC/USDT:USDT", data_dir=tmp_path, refresh=False)

    assert result.index.equals(pd.to_datetime(timestamps, utc=True, format="mixed"))


def test_load_funding_rejects_jitter_over_one_second(tmp_path: Path) -> None:
    _write_binance_funding_cache(
        tmp_path,
        [
            "2030-01-01T00:00:00Z",
            "2030-01-01T08:00:01.001Z",
            "2030-01-01T16:00:00Z",
        ],
    )

    with pytest.raises(ValueError, match="Jitter"):
        carry.load_funding("BTC/USDT:USDT", data_dir=tmp_path, refresh=False)


def test_load_funding_rejects_missing_binance_slot(tmp_path: Path) -> None:
    _write_binance_funding_cache(
        tmp_path,
        ["2030-01-01T00:00:00Z", "2030-01-01T16:00:00Z"],
    )

    with pytest.raises(ValueError, match="GAP"):
        carry.load_funding("BTC/USDT:USDT", data_dir=tmp_path, refresh=False)


def test_load_funding_rejects_duplicate_binance_slot(tmp_path: Path) -> None:
    _write_binance_funding_cache(
        tmp_path,
        [
            "2030-01-01T00:00:00.100Z",
            "2030-01-01T00:00:00.200Z",
            "2030-01-01T08:00:00Z",
        ],
    )

    with pytest.raises(ValueError, match="DUPLICATE"):
        carry.load_funding("BTC/USDT:USDT", data_dir=tmp_path, refresh=False)


def test_load_funding_rejects_out_of_order_binance_rows(tmp_path: Path) -> None:
    _write_binance_funding_cache(
        tmp_path,
        [
            "2030-01-01T08:00:00Z",
            "2030-01-01T00:00:00Z",
            "2030-01-01T16:00:00Z",
        ],
    )

    with pytest.raises(ValueError, match="OUT_OF_ORDER"):
        carry.load_funding("BTC/USDT:USDT", data_dir=tmp_path, refresh=False)


def test_load_funding_rejects_non_finite_binance_rate(tmp_path: Path) -> None:
    _write_binance_funding_cache(
        tmp_path,
        [
            "2030-01-01T00:00:00Z",
            "2030-01-01T08:00:00Z",
            "2030-01-01T16:00:00Z",
        ],
        rates=[0.001, float("nan"), 0.001],
    )

    with pytest.raises(ValueError, match="NaN"):
        carry.load_funding("BTC/USDT:USDT", data_dir=tmp_path, refresh=False)


@pytest.mark.parametrize(
    ("script", "script_args"),
    [
        ("scripts/run_backtest.py", []),
        ("scripts/run_walkforward.py", ["trend_ls"]),
        ("scripts/make_yearly_reference.py", []),
    ],
)
@pytest.mark.parametrize(
    "funding_args",
    [
        ["--funding-mode", "synthetic"],
        ["--funding-mode", "real", "--synthetic-funding-rate", "0.001"],
    ],
)
def test_funding_cli_contract_fails_before_loading_config(
    tmp_path: Path, script: str, script_args: list[str], funding_args: list[str]
) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / script),
            *script_args,
            *funding_args,
            "--config",
            str(tmp_path / "missing.yaml"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "--synthetic-funding-rate" in result.stderr
