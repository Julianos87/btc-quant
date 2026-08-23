"""Acquire bounded public Hyperliquid Carry V2 research inputs.

The command is deliberately research-only.  It calls only public ``info``
endpoints, uses a fixed UTC window, and writes immutable, normalized files
under ``audit/baselines/data/carry_v2``.  It never reads a user address and
never submits an exchange action.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import sys
from bisect import bisect_right
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "audit" / "baselines" / "data" / "carry_v2"
API_URL = "https://api.hyperliquid.xyz/info"
INTERVAL = "1h"
START_MS = 1_768_348_800_000  # 2026-01-14T00:00:00Z
END_MS = 1_786_388_400_000  # 2026-08-10T19:00:00Z
SPOT_COIN = "@142"
PERP_COIN = "BTC"


def _iso(milliseconds: int) -> str:
    return datetime.fromtimestamp(milliseconds / 1000, tz=UTC).isoformat().replace("+00:00", "Z")


def _post(payload: dict[str, Any]) -> Any:
    body = json.dumps(payload).encode("utf-8")
    request = Request(
        API_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=30) as response:  # noqa: S310 - fixed HTTPS endpoint
        return json.load(response)


def _candles(coin: str) -> list[dict[str, Any]]:
    response = _post(
        {
            "type": "candleSnapshot",
            "req": {
                "coin": coin,
                "interval": INTERVAL,
                "startTime": START_MS - 3_600_000,
                "endTime": END_MS,
            },
        }
    )
    if not isinstance(response, list):
        raise ValueError(f"unexpected candle response for {coin}")
    rows = [row for row in response if START_MS - 3_600_000 <= int(row["t"]) <= END_MS]
    rows.sort(key=lambda row: int(row["t"]))
    timestamps = [int(row["t"]) for row in rows]
    if len(timestamps) != len(set(timestamps)):
        raise ValueError(f"duplicate {coin} candle timestamps")
    if any(right <= left for left, right in zip(timestamps, timestamps[1:], strict=False)):
        raise ValueError(f"unordered {coin} candle timestamps")
    return rows


def _funding() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cursor = START_MS
    while cursor <= END_MS:
        response = _post(
            {
                "type": "fundingHistory",
                "coin": PERP_COIN,
                "startTime": cursor,
                "endTime": END_MS,
            }
        )
        if not isinstance(response, list):
            raise ValueError("unexpected funding response")
        batch = [row for row in response if START_MS <= int(row["time"]) <= END_MS]
        if not batch:
            break
        rows.extend(batch)
        last = max(int(row["time"]) for row in batch)
        if last < cursor or len(batch) < 500:
            break
        cursor = last + 1
    rows.sort(key=lambda row: int(row["time"]))
    timestamps = [int(row["time"]) for row in rows]
    if len(timestamps) != len(set(timestamps)):
        raise ValueError("duplicate funding timestamps")
    if any(right <= left for left, right in zip(timestamps, timestamps[1:], strict=False)):
        raise ValueError("unordered funding timestamps")
    return rows


def _write_csv_gz(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8", newline="", compresslevel=9) as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _normalize_candles(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "open_timestamp": _iso(int(row["t"])),
            "close_timestamp": _iso(int(row["T"])),
            # The generic replay timestamp is an availability timestamp.
            "timestamp": _iso(int(row["T"])),
            "open": row["o"],
            "high": row["h"],
            "low": row["l"],
            "close": row["c"],
            "volume": row["v"],
            "trades": row["n"],
        }
        for row in rows
        if START_MS <= int(row["t"]) <= END_MS
    ]


def _normalize_funding(
    rows: list[dict[str, Any]], perp_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    candle_times = [int(row["T"]) for row in perp_rows]
    candle_prices = [row["c"] for row in perp_rows]
    normalized: list[dict[str, Any]] = []
    for row in rows:
        timestamp = int(row["time"])
        index = bisect_right(candle_times, timestamp) - 1
        if index < 0:
            raise ValueError("no previous completed perp candle for funding event")
        normalized.append(
            {
                "timestamp": _iso(timestamp),
                "native_rate": row["fundingRate"],
                "premium": row["premium"],
                "reference_price": candle_prices[index],
                "reference_price_timestamp": _iso(candle_times[index]),
                "price_source": "Hyperliquid BTC 1h close at previous completed candle (T <= funding time)",
            }
        )
    return normalized


def _inventory(path: Path, timestamp_column: str) -> dict[str, Any]:
    import pandas as pd

    frame = pd.read_csv(path, compression="gzip")
    timestamps = pd.to_datetime(frame[timestamp_column], utc=True, format="mixed")
    deltas = timestamps.diff().dropna()
    return {
        "path": str(path.relative_to(ROOT)),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "rows": int(len(frame)),
        "coverage_start": timestamps.iloc[0].isoformat() if len(frame) else None,
        "coverage_end": timestamps.iloc[-1].isoformat() if len(frame) else None,
        "duplicates": int(timestamps.duplicated().sum()),
        "out_of_order": bool((deltas < pd.Timedelta(0)).any()),
        "frequency": "1h",
        "availability_semantics": "timestamp is close_timestamp / available_at",
        "timezone": "UTC",
    }


def acquire() -> dict[str, Any]:
    spot_raw = _candles(SPOT_COIN)
    perp_raw = _candles(PERP_COIN)
    funding_raw = _funding()
    funding_raw = [
        row for row in funding_raw if int(row["time"]) >= int(perp_raw[0]["t"]) + 3_600_000
    ]
    spot = _normalize_candles(spot_raw)
    perp = _normalize_candles(perp_raw)
    funding = _normalize_funding(funding_raw, perp_raw)
    spot_path = OUTPUT_DIR / "hyperliquid_ubtc_usdc_spot_1h_20260114_20260810_v2.csv.gz"
    perp_path = OUTPUT_DIR / "hyperliquid_btc_perp_1h_20260114_20260810_v2.csv.gz"
    funding_path = OUTPUT_DIR / "hyperliquid_btc_funding_1h_20260114_20260810_v2.csv.gz"
    _write_csv_gz(spot_path, list(spot[0]), spot)
    _write_csv_gz(perp_path, list(perp[0]), perp)
    _write_csv_gz(funding_path, list(funding[0]), funding)
    metadata = {
        "venue": "Hyperliquid",
        "endpoint": API_URL,
        "downloaded_at": datetime.now(UTC).isoformat(),
        "schema_version": 2,
        "temporal_semantics": {
            "old_baseline_status": "TEMPORAL_SEMANTICS_INVALID_FOR_CAUSAL_CLOSE_REPLAY",
            "new_baseline_status": "CAUSAL_CLOSE_AVAILABILITY",
            "open_timestamp": "row.t / candle begin",
            "close_timestamp": "row.T / candle end",
            "available_at": "close_timestamp",
            "generic_timestamp": "close_timestamp / available_at",
        },
        "supersedes": [
            "hyperliquid_ubtc_usdc_spot_1h_20260114_20260810.csv.gz",
            "hyperliquid_btc_perp_1h_20260114_20260810.csv.gz",
            "hyperliquid_btc_funding_1h_20260114_20260810.csv.gz",
            "hyperliquid_carry_v2_20260114_20260810.metadata.json",
        ],
        "window": {"start": _iso(START_MS), "end": _iso(END_MS)},
        "spot": {
            "coin": SPOT_COIN,
            "instrument": "@142 / UBTC/USDC",
            "representation": "WRAPPED_TOKENIZED_BTC",
            "request": {"type": "candleSnapshot", "interval": INTERVAL},
            "inventory": _inventory(spot_path, "timestamp"),
        },
        "perp": {
            "coin": PERP_COIN,
            "instrument": "BTC linear perpetual",
            "request": {"type": "candleSnapshot", "interval": INTERVAL},
            "inventory": _inventory(perp_path, "timestamp"),
        },
        "funding": {
            "coin": PERP_COIN,
            "request": {"type": "fundingHistory"},
            "inventory": _inventory(funding_path, "timestamp"),
            "rate_semantics": "native hourly funding rate returned by Hyperliquid",
            "reference_price_semantics": "previous completed BTC 1h close approximation; selected by close_timestamp <= funding timestamp; not oracle/mark",
        },
        "spot_perp_synchronization": {
            "contract": "causal timestamp pairing, UTC, max skew 1 minute, no forward fill",
            "status": "TO_BE_VALIDATED_BY_REPLAY",
        },
    }
    metadata_path = OUTPUT_DIR / "hyperliquid_carry_v2_20260114_20260810_v2.metadata.json"
    metadata_path.write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return metadata


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--refresh", action="store_true", help="re-download the fixed public window"
    )
    args = parser.parse_args()
    if not args.refresh:
        existing = OUTPUT_DIR / "hyperliquid_carry_v2_20260114_20260810_v2.metadata.json"
        if existing.exists():
            raise SystemExit(
                "baseline exists; pass --refresh only for an explicit research refresh"
            )
    print(json.dumps(acquire(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
