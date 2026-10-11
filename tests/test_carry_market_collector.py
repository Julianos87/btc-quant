from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

import pytest

from btcquant.research.carry_market_collector import (
    extract_btc_funding,
    normalize_book,
    open_database,
    persist_cycle,
    market_quality_status,
    write_quality_report,
)


def book(coin: str, *, timestamp: int = 1_800_000_000_000) -> dict:
    return {
        "coin": coin,
        "time": timestamp,
        "levels": [
            [{"px": "83000", "sz": "0.4", "n": 2}],
            [{"px": "83001", "sz": "0.5", "n": 3}],
        ],
    }


def test_book_keeps_explicit_identity_and_exchange_timestamp() -> None:
    normalized = normalize_book(book("@142"), "@142")
    assert normalized["coin"] == "@142"
    assert normalized["best_bid"] == 83000
    assert normalized["exchange_timestamp_ms"] == 1_800_000_000_000
    assert normalized["exchange_timestamp_utc"].endswith("Z")


@pytest.mark.parametrize(
    "payload",
    [
        book("BTC"),
        {**book("@142"), "levels": [[], [{"px": "1", "sz": "1"}]]},
        {
            **book("@142"),
            "levels": [[{"px": "83001", "sz": "1"}], [{"px": "83001", "sz": "1"}]],
        },
    ],
)
def test_book_rejects_bad_identity_or_unusable_levels(payload: dict) -> None:
    with pytest.raises(ValueError):
        normalize_book(payload, "@142")


def test_funding_is_selected_by_btc_identity_and_left_unannualized() -> None:
    raw = [
        {"universe": [{"name": "ETH"}, {"name": "BTC"}]},
        [{"funding": "0.0001"}, {"funding": "-0.0002", "markPx": "83000"}],
    ]
    funding = extract_btc_funding(raw)
    assert funding == {
        "coin": "BTC",
        "funding_rate_raw": "-0.0002",
        "mark_price_raw": "83000",
        "oracle_price_raw": None,
        "open_interest_raw": None,
    }


def test_market_quality_never_labels_future_source_times_fresh() -> None:
    assert market_quality_status(-2.0, 0) == "CLOCK_SKEW"
    assert market_quality_status(16.0, 0) == "STALE_OR_SKEWED"
    assert market_quality_status(1.0, 5_001) == "STALE_OR_SKEWED"
    assert market_quality_status(1.0, 5_000) == "FRESH"


def test_persisted_cycles_have_hashes_and_quality_manifest(tmp_path) -> None:
    database = tmp_path / "data" / "carry_market.sqlite3"
    connection = open_database(database)
    cycle = {
        "collected_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "status": "FRESH",
        "spot": normalize_book(book("@142"), "@142"),
        "perp": normalize_book(book("BTC"), "BTC"),
        "funding": {"coin": "BTC", "funding_rate_raw": "0.0001"},
        "market_skew_ms": 0,
        "max_market_age_seconds": 0.1,
    }
    persist_cycle(connection, cycle)
    connection.close()

    report = write_quality_report(database, tmp_path / "reports")
    assert report["quality"]["rows"] == 1
    assert report["quality"]["fresh_rows"] == 1
    assert report["database"]["sha256"]
    assert report["instruments"]["spot"]["canonical"] is False
    saved = json.loads((tmp_path / "reports" / "carry_market_quality_latest.json").read_text())
    assert saved["quality"]["payload_hashes_present"] == 1

    check = sqlite3.connect(database)
    assert check.execute("SELECT status FROM market_snapshots").fetchone()[0] == "FRESH"
    check.close()
