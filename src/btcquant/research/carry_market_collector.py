"""Isolated, public-only Hyperliquid market-data capture for Carry research.

This process has no account or trading credentials and never submits orders.
Its database and reports live under the experimental Carry root, separate from
the canonical PAPER state and Trend's database.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

API_URL = "https://api.hyperliquid.xyz/info"
SPOT_COIN = "@142"
PERP_COIN = "BTC"
EXPECTED_INTERVAL_SECONDS = 60
MAX_LEVELS = 10
MAX_SNAPSHOTS = 86_400  # 60 days at the normal one-minute cadence.
RETENTION = timedelta(days=60)
MAX_DATABASE_BYTES = 256 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 10
LOG = logging.getLogger("btcquant.carry_data")


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).astimezone(UTC).isoformat().replace("+00:00", "Z")


def _exchange_time(value: Any) -> tuple[int, str]:
    if isinstance(value, bool):
        raise ValueError("exchange timestamp must be milliseconds")
    timestamp_ms = int(value)
    if timestamp_ms <= 0:
        raise ValueError("exchange timestamp must be positive")
    timestamp = datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC)
    return timestamp_ms, _iso(timestamp)


def normalize_book(payload: Any, expected_coin: str) -> dict[str, Any]:
    """Validate and retain a bounded top-of-book sample without inventing depth."""

    if not isinstance(payload, dict) or payload.get("coin") != expected_coin:
        raise ValueError(f"unexpected order-book identity for {expected_coin}")
    exchange_ms, exchange_at = _exchange_time(payload.get("time"))
    levels = payload.get("levels")
    if not isinstance(levels, list) or len(levels) != 2:
        raise ValueError("order book must contain bid and ask level arrays")

    normalized: list[list[dict[str, float]]] = []
    for side in levels:
        if not isinstance(side, list) or not side:
            raise ValueError("order book side is empty")
        rows = []
        for level in side[:MAX_LEVELS]:
            if not isinstance(level, dict):
                raise ValueError("malformed order-book level")
            price = float(level["px"])
            size = float(level["sz"])
            if price <= 0 or size <= 0:
                raise ValueError("order-book prices and sizes must be positive")
            rows.append({"price": price, "size": size})
        normalized.append(rows)
    bids, asks = normalized
    if bids[0]["price"] >= asks[0]["price"]:
        raise ValueError("crossed or locked order book")
    return {
        "coin": expected_coin,
        "exchange_timestamp_ms": exchange_ms,
        "exchange_timestamp_utc": exchange_at,
        "best_bid": bids[0]["price"],
        "best_ask": asks[0]["price"],
        "levels": normalized,
    }


def extract_btc_funding(payload: Any) -> dict[str, Any]:
    """Preserve the venue's raw BTC funding field and its exact metadata identity."""

    if not isinstance(payload, list) or len(payload) != 2:
        raise ValueError("metaAndAssetCtxs response must contain metadata and contexts")
    meta, contexts = payload
    universe = meta.get("universe") if isinstance(meta, dict) else None
    if not isinstance(universe, list) or not isinstance(contexts, list):
        raise ValueError("malformed perp metadata/contexts")
    for index, asset in enumerate(universe):
        if isinstance(asset, dict) and asset.get("name") == PERP_COIN:
            if index >= len(contexts) or not isinstance(contexts[index], dict):
                raise ValueError("BTC funding context is missing")
            context = contexts[index]
            rate = context.get("funding")
            if rate is None:
                raise ValueError("BTC funding field is missing")
            return {
                "coin": PERP_COIN,
                "funding_rate_raw": str(rate),
                "mark_price_raw": context.get("markPx"),
                "oracle_price_raw": context.get("oraclePx"),
                "open_interest_raw": context.get("openInterest"),
            }
    raise ValueError("BTC perp missing from public metadata")


def market_quality_status(max_market_age_seconds: float, market_skew_ms: int) -> str:
    if max_market_age_seconds < -1.0:
        return "CLOCK_SKEW"
    if max_market_age_seconds <= 15 and market_skew_ms <= 5_000:
        return "FRESH"
    return "STALE_OR_SKEWED"


def _request(payload: dict[str, Any]) -> tuple[Any, datetime]:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = Request(
        API_URL,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "btcquant-carry-paper/1"},
        method="POST",
    )
    with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        value = json.loads(response.read())
    return value, _now()


def collect_public_cycle() -> dict[str, Any]:
    """Fetch the two books and native perp funding once, recording source/receive clocks."""

    spot_raw, spot_received = _request({"type": "l2Book", "coin": SPOT_COIN})
    perp_raw, perp_received = _request({"type": "l2Book", "coin": PERP_COIN})
    funding_raw, funding_received = _request({"type": "metaAndAssetCtxs"})
    spot = normalize_book(spot_raw, SPOT_COIN)
    perp = normalize_book(perp_raw, PERP_COIN)
    funding = extract_btc_funding(funding_raw)
    received = _now()
    market_skew_ms = abs(spot["exchange_timestamp_ms"] - perp["exchange_timestamp_ms"])
    max_market_age_seconds = max(
        (received.timestamp() * 1000 - spot["exchange_timestamp_ms"]) / 1000,
        (received.timestamp() * 1000 - perp["exchange_timestamp_ms"]) / 1000,
    )
    status = market_quality_status(max_market_age_seconds, market_skew_ms)
    return {
        "collected_at_utc": _iso(received),
        "status": status,
        "spot_received_at_utc": _iso(spot_received),
        "perp_received_at_utc": _iso(perp_received),
        "funding_received_at_utc": _iso(funding_received),
        "spot": spot,
        "perp": perp,
        "funding": funding,
        "market_skew_ms": market_skew_ms,
        "max_market_age_seconds": max_market_age_seconds,
        "source": API_URL,
        "source_method": "Hyperliquid public info API l2Book + metaAndAssetCtxs",
        "funding_semantics": "native BTC perp funding field preserved as returned; no annualization",
    }


def open_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=15.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA busy_timeout=15000")
    connection.execute("PRAGMA journal_size_limit=16777216")
    connection.execute(f"PRAGMA max_page_count={MAX_DATABASE_BYTES // 4096}")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS market_snapshots (
            snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
            collected_at_utc TEXT NOT NULL,
            status TEXT NOT NULL,
            spot_exchange_ms INTEGER,
            perp_exchange_ms INTEGER,
            spot_received_at_utc TEXT,
            perp_received_at_utc TEXT,
            funding_received_at_utc TEXT,
            market_skew_ms INTEGER,
            max_market_age_seconds REAL,
            spot_json TEXT,
            perp_json TEXT,
            funding_json TEXT,
            failure_reason TEXT,
            payload_sha256 TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_market_snapshots_collected
            ON market_snapshots(collected_at_utc);
        CREATE TABLE IF NOT EXISTS collector_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        INSERT OR REPLACE INTO collector_meta(key, value)
            VALUES ('schema_version', '1');
        """
    )
    return connection


def _prune(connection: sqlite3.Connection, now: datetime) -> None:
    cutoff = _iso(now - RETENTION)
    with connection:
        connection.execute("DELETE FROM market_snapshots WHERE collected_at_utc < ?", (cutoff,))
        connection.execute(
            """
            DELETE FROM market_snapshots
            WHERE snapshot_id <= COALESCE(
                (SELECT MAX(snapshot_id) - ? FROM market_snapshots), 0
            )
            """,
            (MAX_SNAPSHOTS,),
        )


def persist_cycle(connection: sqlite3.Connection, cycle: dict[str, Any]) -> None:
    spot = cycle.get("spot")
    perp = cycle.get("perp")
    funding = cycle.get("funding")
    encoded = json.dumps(cycle, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    with connection:
        connection.execute(
            """
            INSERT INTO market_snapshots (
                collected_at_utc, status, spot_exchange_ms, perp_exchange_ms,
                spot_received_at_utc, perp_received_at_utc, funding_received_at_utc,
                market_skew_ms, max_market_age_seconds, spot_json, perp_json,
                funding_json, failure_reason, payload_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cycle["collected_at_utc"],
                cycle["status"],
                spot.get("exchange_timestamp_ms") if isinstance(spot, dict) else None,
                perp.get("exchange_timestamp_ms") if isinstance(perp, dict) else None,
                cycle.get("spot_received_at_utc"),
                cycle.get("perp_received_at_utc"),
                cycle.get("funding_received_at_utc"),
                cycle.get("market_skew_ms"),
                cycle.get("max_market_age_seconds"),
                json.dumps(spot, sort_keys=True) if spot is not None else None,
                json.dumps(perp, sort_keys=True) if perp is not None else None,
                json.dumps(funding, sort_keys=True) if funding is not None else None,
                cycle.get("failure_reason"),
                digest,
            ),
        )
    _prune(connection, _now())


def write_quality_report(database: Path, report_dir: Path) -> dict[str, Any]:
    """Write a daily, replaceable UTC manifest with hashes and gap/duplicate counts."""

    report_dir.mkdir(parents=True, exist_ok=True)
    expiration = time.time() - RETENTION.total_seconds()
    for old_report in report_dir.glob("carry_market_quality_????????.json"):
        if old_report.stat().st_mtime < expiration:
            old_report.unlink()
    connection = sqlite3.connect(database, timeout=15.0)
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        rows = connection.execute(
            """
            SELECT collected_at_utc, status, market_skew_ms,
                max_market_age_seconds, payload_sha256
            FROM market_snapshots ORDER BY collected_at_utc
            """
        ).fetchall()
    finally:
        connection.close()
    times = [datetime.fromisoformat(row[0].replace("Z", "+00:00")) for row in rows]
    gaps = [
        (right - left).total_seconds()
        for left, right in zip(times, times[1:], strict=False)
        if (right - left).total_seconds() > EXPECTED_INTERVAL_SECONDS * 1.5
    ]
    timestamp_values = [row[0] for row in rows]
    report = {
        "schema_version": 1,
        "generated_at_utc": _iso(),
        "source": API_URL,
        "instruments": {
            "spot": {
                "coin": SPOT_COIN,
                "identity": "Unit Bitcoin (UBTC) / USDC",
                "canonical": False,
            },
            "perp": {"coin": PERP_COIN, "identity": "BTC linear perpetual / USDC"},
        },
        "cadence_seconds": EXPECTED_INTERVAL_SECONDS,
        "retention_days": int(RETENTION.total_seconds() / 86_400),
        "database": {
            "path": str(database),
            "sha256": hashlib.sha256(database.read_bytes()).hexdigest(),
            "bytes": database.stat().st_size,
            "max_bytes": MAX_DATABASE_BYTES,
        },
        "quality": {
            "rows": len(rows),
            "fresh_rows": sum(row[1] == "FRESH" for row in rows),
            "stale_or_skewed_rows": sum(
                row[1] in {"STALE_OR_SKEWED", "CLOCK_SKEW"} for row in rows
            ),
            "failed_rows": sum(row[1] == "FAILED" for row in rows),
            "duplicate_collection_timestamps": len(timestamp_values) - len(set(timestamp_values)),
            "gap_count_over_90_seconds": len(gaps),
            "max_gap_seconds": max(gaps) if gaps else None,
            "max_market_skew_ms": max((row[2] or 0) for row in rows) if rows else None,
            "max_market_age_seconds": max((row[3] or 0) for row in rows) if rows else None,
            "coverage_start_utc": timestamp_values[0] if rows else None,
            "coverage_end_utc": timestamp_values[-1] if rows else None,
            "payload_hashes_present": sum(bool(row[4]) for row in rows),
        },
        "limitations": [
            "Public data only; no user account fees, balances, margin, or fills are observed.",
            "Order-book snapshots do not reconstruct historical fills or queue position.",
            "UBTC/USDC is non-canonical Unit Bitcoin and is not native BTC.",
            "A snapshot is not an independent closed Carry trade or profitability evidence.",
        ],
    }
    destination = report_dir / f"carry_market_quality_{_now():%Y%m%d}.json"
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, destination)
    latest = report_dir / "carry_market_quality_latest.json"
    latest_tmp = latest.with_suffix(".json.tmp")
    latest_tmp.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(latest_tmp, latest)
    return report


def run(
    data_dir: Path, *, interval_seconds: int = EXPECTED_INTERVAL_SECONDS, once: bool = False
) -> int:
    if interval_seconds < 30 or interval_seconds > 300:
        raise ValueError("collector interval must be between 30 and 300 seconds")
    data_dir.mkdir(parents=True, exist_ok=True)
    lock_path = data_dir / "collector.lock"
    lock_file = lock_path.open("a", encoding="utf-8")
    try:
        import fcntl

        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another Carry collector instance owns the data directory") from exc
        database = data_dir / "carry_market.sqlite3"
        report_dir = data_dir.parent / "reports"
        connection = open_database(database)
        previous_status: str | None = None
        next_report = 0.0
        try:
            while True:
                started = time.monotonic()
                try:
                    cycle = collect_public_cycle()
                except Exception as exc:  # keep collecting after transient public API failures
                    cycle = {
                        "collected_at_utc": _iso(),
                        "status": "FAILED",
                        "failure_reason": f"{type(exc).__name__}: {exc}"[:500],
                        "source": API_URL,
                    }
                persist_cycle(connection, cycle)
                if cycle["status"] != previous_status:
                    if cycle["status"] == "FAILED":
                        LOG.warning("public Carry data capture entered FAILED state")
                    else:
                        LOG.info("public Carry data capture state=%s", cycle["status"])
                    previous_status = cycle["status"]
                if time.monotonic() >= next_report:
                    write_quality_report(database, report_dir)
                    next_report = time.monotonic() + 3600
                if once:
                    return 0 if cycle["status"] != "FAILED" else 1
                elapsed = time.monotonic() - started
                time.sleep(max(1.0, interval_seconds - elapsed))
        finally:
            connection.close()
    finally:
        lock_file.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(os.environ.get("BTCQUANT_CARRY_DATA_DIR", "data/carry-experiment")),
    )
    parser.add_argument("--interval-seconds", type=int, default=EXPECTED_INTERVAL_SECONDS)
    parser.add_argument("--once", action="store_true", help="collect one public snapshot and exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        return run(args.data_dir, interval_seconds=args.interval_seconds, once=args.once)
    except KeyboardInterrupt:
        return 0
    except Exception:
        LOG.exception("public Carry data collector stopped")
        return 1


if __name__ == "__main__":
    sys.exit(main())
