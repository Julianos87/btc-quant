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
import io
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


def _candles(
    coin: str,
    *,
    start_ms: int = START_MS,
    end_ms: int = END_MS,
    as_of_ms: int | None = None,
) -> list[dict[str, Any]]:
    if as_of_ms is None:
        as_of_ms = int(datetime.now(UTC).timestamp() * 1000)
    response = _post(
        {
            "type": "candleSnapshot",
            "req": {
                "coin": coin,
                "interval": INTERVAL,
                "startTime": start_ms - 3_600_000,
                "endTime": end_ms,
            },
        }
    )
    if not isinstance(response, list):
        raise ValueError(f"unexpected candle response for {coin}")
    rows = [
        row
        for row in response
        if start_ms - 3_600_000 <= int(row["t"]) <= end_ms and int(row["T"]) <= as_of_ms
    ]
    rows.sort(key=lambda row: int(row["t"]))
    timestamps = [int(row["t"]) for row in rows]
    if len(timestamps) != len(set(timestamps)):
        raise ValueError(f"duplicate {coin} candle timestamps")
    if any(right <= left for left, right in zip(timestamps, timestamps[1:], strict=False)):
        raise ValueError(f"unordered {coin} candle timestamps")
    return rows


def _funding(
    *,
    start_ms: int = START_MS,
    end_ms: int = END_MS,
    as_of_ms: int | None = None,
) -> list[dict[str, Any]]:
    if as_of_ms is None:
        as_of_ms = int(datetime.now(UTC).timestamp() * 1000)
    rows: list[dict[str, Any]] = []
    cursor = start_ms
    while cursor <= end_ms:
        response = _post(
            {
                "type": "fundingHistory",
                "coin": PERP_COIN,
                "startTime": cursor,
                "endTime": end_ms,
            }
        )
        if not isinstance(response, list):
            raise ValueError("unexpected funding response")
        batch = [
            row
            for row in response
            if start_ms <= int(row["time"]) <= end_ms and int(row["time"]) <= as_of_ms
        ]
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
    # ``gzip.open`` embeds the current wall-clock mtime, making an otherwise
    # identical snapshot acquire a different SHA-256 on every run. A research
    # manifest must identify bytes, so freeze the gzip timestamp and omit the
    # source filename from the header.
    with path.open("wb") as raw:
        with gzip.GzipFile(
            fileobj=raw, mode="wb", filename="", compresslevel=9, mtime=0
        ) as compressed:
            with io.TextIOWrapper(compressed, encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
                writer.writeheader()
                writer.writerows(rows)


def _normalize_candles(
    rows: list[dict[str, Any]], *, start_ms: int = START_MS, end_ms: int = END_MS
) -> list[dict[str, Any]]:
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
        if start_ms <= int(row["t"]) <= end_ms
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
        "path": str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path),
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


def _window_label(milliseconds: int) -> str:
    return datetime.fromtimestamp(milliseconds / 1000, tz=UTC).strftime("%Y%m%dT%H%M%SZ")


def _parse_iso_ms(value: str) -> int:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid ISO-8601 timestamp: {value}") from exc
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include an explicit timezone")
    return int(parsed.timestamp() * 1000)


def acquire(
    *,
    start_ms: int = START_MS,
    end_ms: int = END_MS,
    output_dir: Path = OUTPUT_DIR,
) -> dict[str, Any]:
    if start_ms <= 0 or end_ms <= start_ms:
        raise ValueError("Carry window must have positive start < end")
    retrieval_cutoff_ms = int(datetime.now(UTC).timestamp() * 1000)
    spot_raw = _candles(SPOT_COIN, start_ms=start_ms, end_ms=end_ms, as_of_ms=retrieval_cutoff_ms)
    perp_raw = _candles(PERP_COIN, start_ms=start_ms, end_ms=end_ms, as_of_ms=retrieval_cutoff_ms)
    funding_raw = _funding(start_ms=start_ms, end_ms=end_ms, as_of_ms=retrieval_cutoff_ms)
    funding_raw = [
        row for row in funding_raw if int(row["time"]) >= int(perp_raw[0]["t"]) + 3_600_000
    ]
    spot = _normalize_candles(spot_raw, start_ms=start_ms, end_ms=end_ms)
    perp = _normalize_candles(perp_raw, start_ms=start_ms, end_ms=end_ms)
    funding = _normalize_funding(funding_raw, perp_raw)
    stem = f"{_window_label(start_ms)}_{_window_label(end_ms)}"
    spot_path = output_dir / f"hyperliquid_ubtc_usdc_spot_1h_{stem}_v2.csv.gz"
    perp_path = output_dir / f"hyperliquid_btc_perp_1h_{stem}_v2.csv.gz"
    funding_path = output_dir / f"hyperliquid_btc_funding_1h_{stem}_v2.csv.gz"
    _write_csv_gz(spot_path, list(spot[0]), spot)
    _write_csv_gz(perp_path, list(perp[0]), perp)
    _write_csv_gz(funding_path, list(funding[0]), funding)
    metadata = {
        "venue": "Hyperliquid",
        "endpoint": API_URL,
        "downloaded_at": datetime.now(UTC).isoformat(),
        "retrieval_cutoff": _iso(retrieval_cutoff_ms),
        "schema_version": 2,
        "temporal_semantics": {
            "old_baseline_status": "TEMPORAL_SEMANTICS_INVALID_FOR_CAUSAL_CLOSE_REPLAY",
            "new_baseline_status": "CAUSAL_CLOSE_AVAILABILITY",
            "open_timestamp": "row.t / candle begin",
            "close_timestamp": "row.T / candle end",
            "available_at": "close_timestamp",
            "generic_timestamp": "close_timestamp / available_at",
        },
        "supersedes": (
            [
                "hyperliquid_ubtc_usdc_spot_1h_20260114_20260810.csv.gz",
                "hyperliquid_btc_perp_1h_20260114_20260810.csv.gz",
                "hyperliquid_btc_funding_1h_20260114_20260810.csv.gz",
                "hyperliquid_carry_v2_20260114_20260810.metadata.json",
            ]
            if start_ms == START_MS and end_ms == END_MS
            else []
        ),
        "snapshot_identity": f"hyperliquid_carry_v2_{_window_label(start_ms)}_{_window_label(end_ms)}_v2",
        "window": {"start": _iso(start_ms), "end": _iso(end_ms)},
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
    metadata_path = output_dir / f"hyperliquid_carry_v2_{stem}_v2.metadata.json"
    metadata_path.write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return metadata


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh", action="store_true", help="re-download this public window")
    parser.add_argument("--start", default=_iso(START_MS), help="UTC ISO-8601 window start")
    parser.add_argument("--end", default=_iso(END_MS), help="UTC ISO-8601 window end")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()
    start_ms = _parse_iso_ms(args.start)
    end_ms = _parse_iso_ms(args.end)
    stem = f"{_window_label(start_ms)}_{_window_label(end_ms)}"
    existing = args.output_dir / f"hyperliquid_carry_v2_{stem}_v2.metadata.json"
    if not args.refresh and existing.exists():
        raise SystemExit("snapshot exists; pass --refresh only for an explicit research refresh")
    print(
        json.dumps(
            acquire(start_ms=start_ms, end_ms=end_ms, output_dir=args.output_dir),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
