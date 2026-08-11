from __future__ import annotations

from pathlib import Path

import ccxt
import pandas as pd
import pytest

from btcquant import data
from btcquant.data_integrity import GapPolicy


def _ohlcv(timestamp: int, close: float) -> list[float]:
    return [timestamp, close - 1, close + 1, close - 2, close, 10.0]


class PaginatedExchange:
    def __init__(self) -> None:
        self.calls = 0

    @staticmethod
    def parse_timeframe(_timeframe: str) -> int:
        return 60

    @staticmethod
    def milliseconds() -> int:
        return 180_000

    def fetch_ohlcv(self, _symbol, _timeframe, *, since, limit):
        assert limit == 1000
        self.calls += 1
        if self.calls == 1:
            raise ccxt.NetworkError("temporary")
        if since == 0:
            return [_ohlcv(0, 100), _ohlcv(60_000, 101)]
        return [_ohlcv(120_000, 102)]


def test_paginated_fetch_retries_and_advances_without_duplicates(monkeypatch):
    sleeps: list[int] = []
    monkeypatch.setattr(data.time, "sleep", sleeps.append)

    frame = data._fetch_paginated(PaginatedExchange(), "BTC/USDT", "1m", 0)

    assert sleeps == [1]
    assert frame["close"].tolist() == [100.0, 101.0, 102.0]
    assert str(frame.index.tz) == "UTC"


def test_paginated_fetch_fails_after_bounded_retries(monkeypatch):
    exchange = PaginatedExchange()
    monkeypatch.setattr(
        exchange,
        "fetch_ohlcv",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ccxt.ExchangeNotAvailable("down")),
    )
    monkeypatch.setattr(data.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="5 tentatives"):
        data._fetch_paginated(exchange, "BTC/USDT", "1m", 0)


def test_cached_load_without_refresh_is_utc_and_drops_open_candle(tmp_path: Path):
    path = tmp_path / "binance_BTC-USDT_1h.csv"
    path.write_text(
        "timestamp,open,high,low,close,volume\n"
        "2030-01-01T00:00:00Z,99,101,98,100,10\n"
        "2030-01-01T01:00:00Z,100,102,99,101,11\n"
        "2030-01-01T02:00:00Z,101,103,100,102,12\n",
        encoding="utf-8",
    )

    frame = data.load_ohlcv(
        "binance",
        "BTC/USDT",
        "1h",
        "2030-01-01",
        data_dir=tmp_path,
        refresh=False,
        gap_policy=GapPolicy.ALLOW_REPORTED,
    )

    assert frame["close"].tolist() == [100, 101]
    assert str(frame.index.tz) == "UTC"


def _write_gap_cache(tmp_path: Path) -> None:
    path = tmp_path / "binance_BTC-USDT_1h.csv"
    rows = [
        "timestamp,open,high,low,close,volume",
        "2030-01-01T00:00:00Z,99,101,98,100,10",
        "2030-01-01T01:00:00Z,100,102,99,101,11",
        "2030-01-01T03:00:00Z,102,104,101,103,12",
        "2030-01-01T04:00:00Z,103,105,102,104,13",
        "2030-01-01T05:00:00Z,104,106,103,105,14",
    ]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def test_cached_load_allows_reported_gap_without_repair(tmp_path: Path):
    _write_gap_cache(tmp_path)

    frame = data.load_ohlcv(
        "binance",
        "BTC/USDT",
        "1h",
        "2030-01-01",
        data_dir=tmp_path,
        refresh=False,
        gap_policy=GapPolicy.ALLOW_REPORTED,
    )

    assert len(frame) == 4
    assert frame.index.tolist() == list(
        pd.to_datetime(
            [
                "2030-01-01T00:00:00Z",
                "2030-01-01T01:00:00Z",
                "2030-01-01T03:00:00Z",
                "2030-01-01T04:00:00Z",
            ]
        )
    )
    assert frame.attrs["cadence_report"]["missing_rows"] == 1
    assert frame.attrs["cadence_report"]["missing_timestamps"] == ["2030-01-01T02:00:00+00:00"]


def test_cached_load_rejects_reported_gap_in_reject_mode(tmp_path: Path):
    _write_gap_cache(tmp_path)

    with pytest.raises(ValueError, match="GAP"):
        data.load_ohlcv(
            "binance",
            "BTC/USDT",
            "1h",
            "2030-01-01",
            data_dir=tmp_path,
            refresh=False,
            gap_policy=GapPolicy.REJECT,
        )


def test_cached_load_rejects_timezone_naive_timestamps(tmp_path: Path):
    path = tmp_path / "binance_BTC-USDT_1h.csv"
    path.write_text(
        "timestamp,open,high,low,close,volume\n"
        "2030-01-01 00:00:00,99,101,98,100,10\n"
        "2030-01-01 01:00:00,100,102,99,101,11\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="sans fuseau UTC"):
        data.load_ohlcv("binance", "BTC/USDT", "1h", "2030-01-01", data_dir=tmp_path, refresh=False)


def test_refresh_merges_cache_and_replaces_duplicate_timestamp(tmp_path: Path, monkeypatch):
    path = tmp_path / "binance_BTC-USDT_1h.csv"
    path.write_text(
        "timestamp,open,high,low,close,volume\n2030-01-01T00:00:00Z,99,101,98,100,10\n",
        encoding="utf-8",
    )
    fresh_index = pd.to_datetime(
        ["2030-01-01T00:00:00Z", "2030-01-01T01:00:00Z", "2030-01-01T02:00:00Z"]
    )
    fresh = pd.DataFrame(
        {
            "open": [100.0, 101.0, 102.0],
            "high": [102.0, 103.0, 104.0],
            "low": [99.0, 100.0, 101.0],
            "close": [101.0, 102.0, 103.0],
            "volume": [10.0, 11.0, 12.0],
        },
        index=fresh_index,
    )
    monkeypatch.setattr(data, "_make_exchange", lambda _exchange_id: object())
    monkeypatch.setattr(data, "_fetch_paginated", lambda *_args: fresh)

    frame = data.load_ohlcv(
        "binance",
        "BTC/USDT",
        "1h",
        "2030-01-01",
        data_dir=tmp_path,
    )

    assert frame["close"].tolist() == [101.0, 102.0]
    persisted = pd.read_csv(path)
    assert len(persisted) == 3


def test_missing_cache_and_resampling_edge_cases(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="Aucun cache"):
        data.load_ohlcv(
            "binance",
            "BTC/USDT",
            "1h",
            "2030-01-01",
            data_dir=tmp_path,
            refresh=False,
        )

    index = pd.date_range("2030-01-01", periods=4, freq="1h", tz="UTC")
    source = pd.DataFrame(
        {
            "open": [1.0, 2.0, 3.0, 4.0],
            "high": [2.0, 3.0, 4.0, 5.0],
            "low": [0.0, 1.0, 2.0, 3.0],
            "close": [1.5, 2.5, 3.5, 4.5],
            "volume": [1.0, 2.0, 3.0, 4.0],
        },
        index=index,
    )
    result = data.resample(source, "2h")
    assert result["open"].tolist() == [1.0, 3.0]
    assert result["high"].tolist() == [3.0, 5.0]
    assert result["volume"].tolist() == [3.0, 7.0]


# ── agrégat de fin d'historique ─────────────────────────────────────────────
# `load_ohlcv` écarte la bougie de base en cours, mais `resample` construisait
# encore une fenêtre 4 h « close » à partir de 1 à 3 barres horaires seulement.
# Le moteur décidait donc sur une bougie non close en fin d'historique —
# exactement ce que `validate_closed_ohlcv` refuse côté live.


def _hourly(n: int, start: str = "2030-01-01") -> pd.DataFrame:
    index = pd.date_range(start, periods=n, freq="1h", tz="UTC")
    values = [float(i) for i in range(n)]
    return pd.DataFrame(
        {
            "open": values,
            "high": [v + 1 for v in values],
            "low": values,
            "close": values,
            "volume": [1.0] * n,
        },
        index=index,
    )


@pytest.mark.parametrize("hours,expected_bars", [(8, 2), (9, 2), (11, 2), (12, 3)])
def test_resample_drops_the_incomplete_trailing_window(hours: int, expected_bars: int):
    result = data.resample(_hourly(hours), "4h")
    assert len(result) == expected_bars
    covered_hours = expected_bars * 4
    assert result.index[-1] == _hourly(hours).index[0] + pd.Timedelta(hours=covered_hours - 4)


def test_resample_drops_a_window_with_a_gap_in_the_source():
    """Une fenêtre agrégée ne peut être valide que si toutes ses sources sont présentes."""
    source = _hourly(12).drop(_hourly(12).index[5])
    assert len(data.resample(source, "4h")) == 2


def test_resample_to_daily_requires_a_full_day():
    assert len(data.resample(_hourly(48), "1D")) == 2
    assert len(data.resample(_hourly(47), "1D")) == 1
