"""Durable, public-only Deribit market-data collector for dated Carry research.

The collector stores public metadata and official settlements. In production,
the PAPER runner is the single source of exact spot/future books; this service
archives those snapshots from the shared evidence database. It has no account
client and no trading methods. Old observations are archived to immutable,
hash-manifested gzip JSONL segments before they are removed from the live DB.
"""

from __future__ import annotations

import argparse
import fcntl
import gzip
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import time
import uuid
from contextlib import contextmanager
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from btcquant.domain.carry_dated import (
    canonical_market_book_snapshot,
    canonical_payload_sha256,
)
from btcquant.execution.carry_dated_market import DeribitPublicClient

LOG = logging.getLogger("btcquant.carry_dated.collector")
SCHEMA_VERSION = 1
DEFAULT_DATABASE = Path("/opt/btcquant/state/carry-dated-experiment/market.sqlite3")
DEFAULT_ARCHIVE_DIR = Path("/opt/btcquant/data/carry-dated-experiment/archive")
DEFAULT_STATUS = Path("/opt/btcquant/state/carry-dated-experiment/collector_status.json")
DEFAULT_INTERVAL_SECONDS = 60
DEFAULT_SETTLEMENT_INTERVAL_SECONDS = 1800
DEFAULT_ARCHIVE_AGE_HOURS = 24
DEFAULT_RETENTION_DAYS = 700
DEFAULT_MAX_TOTAL_BYTES = 5 * 1024 * 1024 * 1024
DEFAULT_MIN_FREE_BYTES = 2 * 1024 * 1024 * 1024
MAX_BOOK_INSTRUMENTS = 16
DEFAULT_BOOK_DEPTH = 20
DEFAULT_INTERVAL_SECONDS = 30
ALLOWED_BOOK_DEPTHS = frozenset({10, 20, 50, 100})
MIN_EXPIRY_DAYS = 14
MAX_EXPIRY_DAYS = 90
POST_EXPIRY_SETTLEMENT_DAYS = 30
RUNNER_BOOK_FRESHNESS_SECONDS = 45


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("collector timestamps must be timezone-aware UTC")
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _reject_symlink_components(path: Path) -> None:
    current = path.expanduser().absolute()
    while True:
        if current.is_symlink():
            raise ValueError("collector paths cannot traverse a symlink")
        if current == current.parent:
            return
        current = current.parent


def _runtime_paths(runtime_root: Path) -> tuple[Path, Path, Path]:
    _reject_symlink_components(runtime_root)
    root = runtime_root.resolve()
    return (
        root / "state/carry-dated-experiment/market.sqlite3",
        root / "data/carry-dated-experiment/archive",
        root / "state/carry-dated-experiment/collector_status.json",
    )


def _validate_cli_paths(args: argparse.Namespace, *, runtime_root: Path) -> None:
    root = runtime_root.resolve()
    expected_database, expected_archive, expected_status = _runtime_paths(runtime_root)
    for field, expected in (
        ("database", expected_database),
        ("archive_dir", expected_archive),
        ("status_path", expected_status),
    ):
        path = Path(getattr(args, field))
        _reject_symlink_components(path)
        if not path.is_absolute() or path.resolve() != expected:
            raise ValueError(f"collector {field} must use its isolated runtime path")
    if args.restore_manifest:
        manifest = Path(args.restore_manifest)
        restore_database = Path(args.restore_database)
        _reject_symlink_components(manifest)
        _reject_symlink_components(restore_database)
        archive_root = (root / "data/carry-dated-experiment/archive").resolve()
        restore_root = (root / "state/carry-dated-experiment/restore-test").resolve()
        if (
            not manifest.is_absolute()
            or not manifest.resolve().is_relative_to(archive_root)
            or not manifest.name.endswith(".manifest.json")
            or not restore_database.is_absolute()
            or not restore_database.resolve().is_relative_to(restore_root)
            or restore_database.resolve().suffix != ".sqlite3"
        ):
            raise ValueError("archive restoration must stay inside its isolated clean-room paths")


def _instrument_payload(instrument: Any) -> dict[str, Any]:
    return {
        "venue": instrument.venue,
        "instrument_id": instrument.instrument_id,
        "kind": instrument.kind,
        "base": instrument.base,
        "quote": instrument.quote,
        "settlement_currency": instrument.settlement_currency,
        "contract_size": format(instrument.contract_size, "f"),
        "amount_step": format(instrument.amount_step, "f"),
        "min_amount": format(instrument.min_amount, "f"),
        "tick_size": format(instrument.tick_size, "f"),
        "expiration_utc": _iso(instrument.expiration_utc) if instrument.expiration_utc else None,
        "active": instrument.active,
        "maker_commission": (
            format(instrument.maker_commission, "f")
            if instrument.maker_commission is not None
            else None
        ),
        "taker_commission": (
            format(instrument.taker_commission, "f")
            if instrument.taker_commission is not None
            else None
        ),
        "fee_currency": instrument.fee_currency,
        "fee_provenance": instrument.fee_provenance,
        "metadata_timestamp": _iso(instrument.metadata_timestamp),
        "metadata_hash": instrument.metadata_hash,
        "instrument_state": instrument.instrument_state,
        "source": instrument.source,
    }


def _book_payload(book: Any, *, quality_reasons: tuple[str, ...]) -> dict[str, Any]:
    return {
        "instrument_id": book.instrument_id,
        "bids": [[format(level.price, "f"), format(level.quantity, "f")] for level in book.bids],
        "asks": [[format(level.price, "f"), format(level.quantity, "f")] for level in book.asks],
        "market_timestamp": _iso(book.market_timestamp),
        "received_timestamp": _iso(book.received_timestamp),
        "source": book.source,
        "instrument_state": book.instrument_state,
        "mark_price": format(book.mark_price, "f") if book.mark_price is not None else None,
        "index_price": format(book.index_price, "f") if book.index_price is not None else None,
        "sequence": book.sequence,
        "content_hash": book.content_hash,
        "quality_reasons": list(quality_reasons),
        "price_unit": "USDC per BTC",
        "quantity_unit": "BTC",
    }


def _runner_book_snapshot(book: Any) -> dict[str, Any]:
    """Canonical snapshot representation shared with the engine fill ledger."""

    return canonical_market_book_snapshot(book)


def _settlement_payload(settlement: Any) -> dict[str, Any]:
    return {
        "venue": settlement.venue,
        "instrument_id": settlement.instrument_id,
        "settlement_key": settlement.settlement_key,
        "kind": settlement.kind,
        "period_start": _iso(settlement.period_start),
        "period_end": _iso(settlement.period_end),
        "event_timestamp": _iso(settlement.event_timestamp),
        "official_index_price": format(settlement.official_index_price, "f"),
        "source": settlement.source,
        "received_at": _iso(settlement.received_at),
        "content_hash": settlement.content_hash,
        "correction_version": settlement.correction_version,
    }


def open_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=15.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA busy_timeout=15000")
    connection.execute("PRAGMA journal_size_limit=16777216")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            record_key TEXT NOT NULL,
            market_timestamp TEXT,
            received_timestamp TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            UNIQUE(kind, record_key, payload_sha256)
        );
        CREATE INDEX IF NOT EXISTS idx_observations_received
            ON observations(received_timestamp, id);
        CREATE TABLE IF NOT EXISTS instruments (
            instrument_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            expiration_utc TEXT,
            last_seen_at TEXT NOT NULL,
            last_settlement_poll_at TEXT,
            last_settlement_key TEXT,
            last_settlement_event_at TEXT
        );
        CREATE TABLE IF NOT EXISTS collector_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS archive_segments (
            manifest_name TEXT PRIMARY KEY,
            archive_sha256 TEXT,
            last_verified_at TEXT NOT NULL,
            verification_state TEXT NOT NULL,
            verification_reason TEXT
        );
        INSERT INTO collector_meta(key, value) VALUES ('schema_version', '1')
            ON CONFLICT(key) DO UPDATE SET value=excluded.value;
        """
    )
    return connection


def _record(
    connection: sqlite3.Connection,
    *,
    kind: str,
    key: str,
    payload: dict[str, Any],
    received_at: datetime,
    market_at: datetime | None = None,
) -> tuple[int | None, bool]:
    encoded = _canonical_json(payload)
    digest = _sha256(encoded.encode("utf-8"))
    cursor = connection.execute(
        """INSERT OR IGNORE INTO observations(
            kind, record_key, market_timestamp, received_timestamp, payload_sha256, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?)""",
        (kind, key, _iso(market_at) if market_at else None, _iso(received_at), digest, encoded),
    )
    if cursor.rowcount == 0:
        row = connection.execute(
            "SELECT id FROM observations WHERE kind=? AND record_key=? AND payload_sha256=?",
            (kind, key, digest),
        ).fetchone()
        return (int(row["id"]) if row else None), False
    inserted_id = cursor.lastrowid
    if inserted_id is None:
        raise RuntimeError("inserted observation has no durable database identity")
    return int(inserted_id), True


class DatedMarketEvidenceRecorder:
    """Persist the runner's exact consumed books in the collector's archive stream."""

    def __init__(
        self,
        *,
        database: Path,
        archive_dir: Path,
        max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
        min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
    ) -> None:
        if max_total_bytes < 1024 * 1024 or min_free_bytes < 0:
            raise ValueError("market evidence storage budget is invalid")
        self.database = database
        self.archive_dir = archive_dir
        self.max_total_bytes = max_total_bytes
        self.min_free_bytes = min_free_bytes

    def persist(
        self,
        book: Any,
        *,
        purpose: str,
        requested_depth: int,
        protocol_version: str,
        now: datetime,
    ) -> str:
        current = _utc(now)
        if not purpose or requested_depth != DEFAULT_BOOK_DEPTH:
            raise ValueError("runner evidence purpose/depth does not match the frozen protocol")
        if protocol_version != "carry-dated-market-evidence-v2":
            raise ValueError("unsupported runner market evidence protocol")
        if len(book.bids) > requested_depth or len(book.asks) > requested_depth:
            raise ValueError("runner book exceeds the frozen evidence depth")
        _reject_symlink_components(self.database)
        _reject_symlink_components(self.archive_dir)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)

        snapshot = _runner_book_snapshot(book)
        snapshot_hash = canonical_payload_sha256(snapshot)
        evidence_id = "dated-book-v2:" + canonical_payload_sha256(
            {
                "protocol_version": protocol_version,
                "instrument_id": book.instrument_id,
                "snapshot_sha256": snapshot_hash,
            }
        )
        evidence_payload = {
            "evidence_id": evidence_id,
            "protocol_version": protocol_version,
            "requested_depth": requested_depth,
            "observed_bid_levels": len(book.bids),
            "observed_ask_levels": len(book.asks),
            "snapshot_sha256": snapshot_hash,
            "book_snapshot": snapshot,
        }
        encoded_bytes = len(_canonical_json(evidence_payload).encode("utf-8"))
        reserve_bytes = max(65_536, encoded_bytes * 4)

        connection = open_database(self.database)
        try:
            connection.execute("BEGIN IMMEDIATE")
            total_bytes = sum(
                path.stat().st_size
                for path in self.database.parent.glob(self.database.name + "*")
                if path.is_file()
            ) + sum(
                path.stat().st_size for path in self.archive_dir.rglob("*") if path.is_file()
            )
            free_bytes = shutil.disk_usage(self.database.parent).free
            if (
                total_bytes + reserve_bytes > self.max_total_bytes
                or free_bytes < self.min_free_bytes + reserve_bytes
            ):
                connection.rollback()
                raise RuntimeError(
                    "MARKET_EVIDENCE_QUOTA_EXCEEDED: runner evidence was refused before the storage limit"
                )

            existing = connection.execute(
                "SELECT payload_sha256 FROM observations WHERE kind='runner_book' AND record_key=?",
                (evidence_id,),
            ).fetchone()
            evidence_hash = _sha256(_canonical_json(evidence_payload).encode("utf-8"))
            if existing is not None and existing["payload_sha256"] != evidence_hash:
                raise RuntimeError("RUNNER_BOOK_EVIDENCE_IDENTITY_COLLISION")
            _record(
                connection,
                kind="runner_book",
                key=evidence_id,
                payload=evidence_payload,
                received_at=current,
                market_at=book.market_timestamp,
            )
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()
        return evidence_id


@contextmanager
def exclusive_lock(path: Path, *, blocking: bool = False) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o640)
    try:
        try:
            flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
            fcntl.flock(descriptor, flags)
        except BlockingIOError as error:
            raise RuntimeError("CARRY_DATED_COLLECTOR_ALREADY_RUNNING") from error
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


class DatedMarketCollector:
    """Collect and retain observed Deribit data; never submit trades."""

    def __init__(
        self,
        *,
        database: Path,
        archive_dir: Path,
        status_path: Path,
        market: Any | None = None,
        clock: Any = lambda: datetime.now(UTC),
        interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
        book_depth: int = DEFAULT_BOOK_DEPTH,
        settlement_interval_seconds: int = DEFAULT_SETTLEMENT_INTERVAL_SECONDS,
        archive_age_hours: int = DEFAULT_ARCHIVE_AGE_HOURS,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
        min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
        books_from_runner: bool = False,
    ) -> None:
        if (
            interval_seconds < 5
            or book_depth not in ALLOWED_BOOK_DEPTHS
            or settlement_interval_seconds < 60
            or archive_age_hours < 1
            or retention_days < 560
        ):
            raise ValueError("collector cadence/archive/retention configuration is too small")
        if max_total_bytes < 1024 * 1024 or min_free_bytes < 0:
            raise ValueError("collector storage budget is invalid")
        self.database = database
        self.archive_dir = archive_dir
        self.status_path = status_path
        self.market = market if market is not None else DeribitPublicClient()
        self.clock = clock
        self.interval_seconds = interval_seconds
        self.book_depth = book_depth
        self.settlement_interval_seconds = settlement_interval_seconds
        self.archive_age = timedelta(hours=archive_age_hours)
        self.retention_days = retention_days
        self.max_total_bytes = max_total_bytes
        self.min_free_bytes = min_free_bytes
        self.books_from_runner = books_from_runner
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)

    def _storage(self) -> tuple[int, int]:
        db_bytes = sum(
            path.stat().st_size
            for path in self.database.parent.glob(self.database.name + "*")
            if path.is_file()
        )
        archive_bytes = sum(
            path.stat().st_size for path in self.archive_dir.rglob("*") if path.is_file()
        )
        return db_bytes + archive_bytes, shutil.disk_usage(self.database.parent).free

    def _coverage(self) -> dict[str, Any]:
        observed_start: str | None = None
        observed_end: str | None = None
        live_record_count = 0
        database_status = "ABSENT"
        if self.database.is_file():
            connection: sqlite3.Connection | None = None
            try:
                uri = self.database.resolve().as_uri() + "?mode=ro"
                connection = sqlite3.connect(uri, timeout=1.0, uri=True)
                connection.execute("PRAGMA query_only=ON")
                row = connection.execute(
                    """SELECT MIN(received_timestamp), MAX(received_timestamp), COUNT(*)
                       FROM observations"""
                ).fetchone()
                observed_start, observed_end = row[0], row[1]
                live_record_count = int(row[2])
                database_status = "READABLE"
            except sqlite3.Error:
                database_status = "UNREADABLE"
            finally:
                if connection is not None:
                    connection.close()
        archived_starts: list[str] = []
        archived_ends: list[str] = []
        archive_records = 0
        invalid_manifests = 0
        verified_manifests = 0
        failed_manifest_names: set[str] = set()
        unverified_manifests = 0
        latest_verification: str | None = None
        manifests = list(self.archive_dir.glob("*.manifest.json"))
        verification_rows: dict[str, sqlite3.Row] = {}
        if self.database.is_file():
            verification_connection: sqlite3.Connection | None = None
            try:
                uri = self.database.resolve().as_uri() + "?mode=ro"
                verification_connection = sqlite3.connect(uri, timeout=1.0, uri=True)
                verification_connection.row_factory = sqlite3.Row
                verification_rows = {
                    str(row["manifest_name"]): row
                    for row in verification_connection.execute(
                        "SELECT manifest_name, last_verified_at, verification_state FROM archive_segments"
                    )
                }
            except sqlite3.Error:
                database_status = "UNREADABLE"
            finally:
                if verification_connection is not None:
                    verification_connection.close()
        for path in manifests:
            try:
                _reject_symlink_components(path)
                manifest = json.loads(path.read_text(encoding="utf-8"))
                archived_starts.append(str(manifest["received_from"]))
                archived_ends.append(str(manifest["received_through"]))
                archive_records += int(manifest["record_count"])
                segment = verification_rows.get(path.name)
                if segment is None:
                    unverified_manifests += 1
                elif segment["verification_state"] == "PASS":
                    verified_manifests += 1
                    last_verified = str(segment["last_verified_at"])
                    latest_verification = max(latest_verification or last_verified, last_verified)
                else:
                    failed_manifest_names.add(path.name)
                    last_verified = str(segment["last_verified_at"])
                    latest_verification = max(latest_verification or last_verified, last_verified)
            except (OSError, ValueError, TypeError, KeyError):
                invalid_manifests += 1
                failed_manifest_names.add(path.name)
        starts = [value for value in (observed_start, *archived_starts) if value is not None]
        ends = [value for value in (observed_end, *archived_ends) if value is not None]
        return {
            "observed_coverage_start_utc": min(starts) if starts else None,
            "observed_coverage_end_utc": max(ends) if ends else None,
            "database_read_status": database_status,
            "live_record_count": live_record_count,
            "archive_segment_count": len(manifests),
            "archive_record_count_from_manifests": archive_records,
            "invalid_archive_manifest_count": invalid_manifests,
            "archive_verified_segment_count": verified_manifests,
            "archive_failed_segment_count": len(failed_manifest_names),
            "archive_unverified_segment_count": unverified_manifests,
            "archive_last_verification_at": latest_verification,
            "archive_integrity_status": (
                "FAILED"
                if failed_manifest_names or invalid_manifests
                else "VERIFIED"
                if manifests and verified_manifests == len(manifests)
                else "PENDING"
                if manifests
                else "NO_ARCHIVES"
            ),
        }

    def _write_status(self, payload: dict[str, Any]) -> None:
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.status_path.with_name(
            f".{self.status_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        encoded = (
            json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False)
            + "\n"
        )
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.status_path)
            directory_fd = os.open(
                self.status_path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)

    def collect_once(self, *, now: datetime | None = None) -> dict[str, Any]:
        current = _utc(now or self.clock())
        total_bytes, free_bytes = self._storage()
        # Compact the live database into verified immutable segments before
        # deciding that the collector quota is exhausted. Never start an
        # archive write below the reserved free-space floor.
        if self.database.exists() and free_bytes >= self.min_free_bytes:
            try:
                archive_connection = open_database(self.database)
                try:
                    with archive_connection:
                        archived = self._archive_old(archive_connection, now=current)
                    if archived:
                        self._compact_database(archive_connection)
                finally:
                    archive_connection.close()
            except Exception as error:
                LOG.exception("could not archive old observations safely")
                status = self._status(
                    state="FAILED",
                    now=current,
                    reason=f"ARCHIVE_VERIFICATION_FAILED:{type(error).__name__}",
                )
                self._write_status(status)
                return status
            total_bytes, free_bytes = self._storage()
        if total_bytes >= self.max_total_bytes or free_bytes < self.min_free_bytes:
            status = self._status(
                state="SUSPENDED_STORAGE_BUDGET",
                now=current,
                reason="STORAGE_BUDGET_OR_FREE_SPACE_LIMIT",
                total_bytes=total_bytes,
                free_bytes=free_bytes,
            )
            self._write_status(status)
            return status

        # Hash at most one existing segment per cycle. This catches later
        # storage corruption without re-reading the full archive collection on
        # every 60-second market poll. Unchecked segments remain visible.
        try:
            integrity_connection = open_database(self.database)
            try:
                with integrity_connection:
                    self._verify_archive_once(integrity_connection, now=current)
            finally:
                integrity_connection.close()
        except Exception as error:
            status = self._status(
                state="FAILED",
                now=current,
                reason=f"ARCHIVE_VERIFICATION_FAILED:{type(error).__name__}",
                total_bytes=total_bytes,
                free_bytes=free_bytes,
            )
            self._write_status(status)
            return status
        integrity = self._coverage()["archive_integrity_status"]
        if integrity == "FAILED":
            status = self._status(
                state="FAILED",
                now=current,
                reason="ARCHIVE_INTEGRITY_FAILURE",
                total_bytes=total_bytes,
                free_bytes=free_bytes,
            )
            self._write_status(status)
            return status

        received_at = _utc(self.clock())
        inserted = 0
        freshness: list[datetime] = []
        try:
            instruments = tuple(self.market.list_active_instruments())
            spots = [
                item
                for item in instruments
                if item.kind == "spot" and item.base == "BTC" and item.quote == "USDC"
            ]
            if len(spots) != 1:
                raise ValueError(f"ACTIVE_SPOT_IDENTITY_COUNT_{len(spots)}")
            tracked_futures = [
                item
                for item in instruments
                if item.kind == "dated_future"
                and item.base == "BTC"
                and item.quote == "USDC"
                and item.settlement_currency == "USDC"
                and item.active
                and item.expiration_utc is not None
                and current < item.expiration_utc <= current + timedelta(days=MAX_EXPIRY_DAYS)
            ]
            if len(tracked_futures) + 1 > MAX_BOOK_INSTRUMENTS:
                raise ValueError("ACTIVE_FUTURE_COUNT_EXCEEDS_COLLECTOR_LIMIT")
            entry_candidates = [
                item
                for item in tracked_futures
                if item.expiration_utc is not None
                and current + timedelta(days=MIN_EXPIRY_DAYS) <= item.expiration_utc
            ]
            selected_future = (
                min(entry_candidates, key=lambda item: (item.expiration_utc, item.instrument_id))
                if entry_candidates
                else None
            )

            connection = open_database(self.database)
            try:
                with connection:
                    for instrument in (*spots, *tracked_futures):
                        payload = _instrument_payload(instrument)
                        _, was_inserted = _record(
                            connection,
                            kind="instrument",
                            key=f"{instrument.instrument_id}:{instrument.metadata_hash}",
                            payload=payload,
                            received_at=received_at,
                            market_at=instrument.metadata_timestamp,
                        )
                        inserted += int(was_inserted)
                        connection.execute(
                            """INSERT INTO instruments(
                                instrument_id, kind, expiration_utc, last_seen_at
                            ) VALUES (?, ?, ?, ?) ON CONFLICT(instrument_id) DO UPDATE SET
                                kind=excluded.kind, expiration_utc=excluded.expiration_utc,
                                last_seen_at=excluded.last_seen_at""",
                            (
                                instrument.instrument_id,
                                instrument.kind,
                                _iso(instrument.expiration_utc)
                                if instrument.expiration_utc
                                else None,
                                _iso(current),
                            ),
                        )

                books: list[dict[str, Any]] = []
                data_errors: list[dict[str, str]] = []
                book_quality: dict[str, list[str]] = {}
                book_instruments = (spots[0], *((selected_future,) if selected_future else ()))
                if self.books_from_runner:
                    books, data_errors, book_quality, freshness = self._latest_runner_books(
                        connection,
                        expected_instruments=book_instruments,
                        now=current,
                    )
                else:
                    for instrument in book_instruments:
                        try:
                            book = self.market.get_book(
                                instrument.instrument_id, depth=self.book_depth
                            )
                            reasons = list(book.quality_reasons(now=current))
                            if instrument.kind == "dated_future" and (
                                book.mark_price is None or book.index_price is None
                            ):
                                reasons.append("FUTURE_MARK_OR_INDEX_MISSING")
                            book_quality[instrument.instrument_id] = reasons
                            freshness.append(book.market_timestamp)
                            payload = _book_payload(book, quality_reasons=tuple(reasons))
                            books.append(
                                {
                                    "instrument_id": instrument.instrument_id,
                                    "market_timestamp": _iso(book.market_timestamp),
                                    "received_timestamp": _iso(book.received_timestamp),
                                    "content_hash": book.content_hash,
                                    "quality_reasons": reasons,
                                }
                            )
                            with connection:
                                _, was_inserted = _record(
                                    connection,
                                    kind="book",
                                    key=f"{instrument.instrument_id}:{book.market_timestamp.isoformat()}:{book.sequence}",
                                    payload=payload,
                                    received_at=book.received_timestamp,
                                    market_at=book.market_timestamp,
                                )
                            inserted += int(was_inserted)
                        except Exception as error:
                            data_errors.append(
                                {
                                    "instrument_id": instrument.instrument_id,
                                    "reason": type(error).__name__,
                                }
                            )
                            LOG.warning(
                                "order book unavailable for %s (%s)",
                                instrument.instrument_id,
                                type(error).__name__,
                            )

                settlement_count, settlement_errors = self._collect_settlements(
                    connection, now=current
                )
                inserted += settlement_count
                cycle_payload = {
                    "state": (
                        "OK"
                        if not settlement_errors
                        and not data_errors
                        and not any(book_quality.values())
                        else "PARTIAL"
                    ),
                    "collector_version": "carry_dated_collector_v2",
                    "book_source": (
                        "runner_market_evidence" if self.books_from_runner else "collector_public_api"
                    ),
                    "source": "Deribit public market-data API; unauthenticated GET only",
                    "active_spot": spots[0].instrument_id,
                    "entry_candidate_futures": [item.instrument_id for item in entry_candidates],
                    "selected_entry_future": (
                        selected_future.instrument_id if selected_future is not None else None
                    ),
                    "tracked_futures": [item.instrument_id for item in tracked_futures],
                    "book_depth_requested": self.book_depth,
                    "books": books,
                    "settlement_errors": settlement_errors,
                    "book_errors": data_errors,
                    "records_inserted": inserted,
                }
                archived = 0
                with connection:
                    _record(
                        connection,
                        kind="cycle",
                        key=_iso(current),
                        payload=cycle_payload,
                        received_at=received_at,
                    )
                    archived = self._archive_old(connection, now=current)
                if archived:
                    self._compact_database(connection)
            finally:
                connection.close()
            total_bytes, free_bytes = self._storage()
            quality_errors = {
                instrument_id: reasons
                for instrument_id, reasons in book_quality.items()
                if reasons
            }
            result = self._status(
                state=(
                    "OK"
                    if not settlement_errors and not data_errors and not quality_errors
                    else "PARTIAL"
                ),
                now=current,
                total_bytes=total_bytes,
                free_bytes=free_bytes,
                records_inserted=inserted,
                freshest_market_timestamp=(_iso(max(freshness)) if freshness else None),
                settlement_errors=settlement_errors,
                book_errors=data_errors,
                book_quality=book_quality,
                book_source=(
                    "runner_market_evidence" if self.books_from_runner else "collector_public_api"
                ),
            )
        except Exception as error:
            LOG.exception("public dated-carry collection failed (%s)", type(error).__name__)
            result = self._status(
                state="FAILED",
                now=current,
                reason=f"{type(error).__name__}: {str(error)[:180]}",
                total_bytes=total_bytes,
                free_bytes=free_bytes,
            )
        self._write_status(result)
        return result

    def _latest_runner_books(
        self,
        connection: sqlite3.Connection,
        *,
        expected_instruments: tuple[Any, ...],
        now: datetime,
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]], dict[str, list[str]], list[datetime]]:
        """Read recent exact runner books without making a duplicate market poll."""

        expected_ids = {item.instrument_id: item for item in expected_instruments}
        latest: dict[str, dict[str, Any]] = {}
        rows = connection.execute(
            "SELECT payload_json FROM observations WHERE kind='runner_book' ORDER BY id DESC LIMIT 4096"
        )
        for (payload_json,) in rows:
            try:
                evidence = json.loads(payload_json)
                snapshot = evidence["book_snapshot"]
                instrument_id = str(snapshot["instrument_id"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if instrument_id in expected_ids and instrument_id not in latest:
                latest[instrument_id] = snapshot
            if len(latest) == len(expected_ids):
                break

        books: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        quality: dict[str, list[str]] = {}
        freshness: list[datetime] = []
        for instrument_id, instrument in expected_ids.items():
            snapshot = latest.get(instrument_id)
            if snapshot is None:
                errors.append({"instrument_id": instrument_id, "reason": "RUNNER_BOOK_NOT_FOUND"})
                continue
            market_timestamp = datetime.fromisoformat(
                str(snapshot["market_timestamp"]).replace("Z", "+00:00")
            )
            received_timestamp = datetime.fromisoformat(
                str(snapshot["received_timestamp"]).replace("Z", "+00:00")
            )
            age_seconds = max(
                (now - market_timestamp).total_seconds(),
                (now - received_timestamp).total_seconds(),
            )
            reasons: list[str] = []
            if age_seconds < -5:
                reasons.append("RUNNER_BOOK_CLOCK_SKEW")
            elif age_seconds > RUNNER_BOOK_FRESHNESS_SECONDS:
                reasons.append("RUNNER_BOOK_STALE")
            if instrument.kind == "dated_future" and (
                snapshot.get("mark_price") is None or snapshot.get("index_price") is None
            ):
                reasons.append("FUTURE_MARK_OR_INDEX_MISSING")
            quality[instrument_id] = reasons
            freshness.append(market_timestamp)
            books.append(
                {
                    "instrument_id": instrument_id,
                    "market_timestamp": _iso(market_timestamp),
                    "received_timestamp": _iso(received_timestamp),
                    "content_hash": snapshot.get("content_hash"),
                    "quality_reasons": reasons,
                    "evidence_source": "carry-dated-market-evidence-v2",
                }
            )
        return books, errors, quality, freshness

    def _collect_settlements(
        self, connection: sqlite3.Connection, *, now: datetime
    ) -> tuple[int, list[dict[str, str]]]:
        inserted = 0
        errors: list[dict[str, str]] = []
        tracked = connection.execute(
            "SELECT * FROM instruments WHERE kind='dated_future' ORDER BY instrument_id"
        ).fetchall()
        for row in tracked:
            instrument_id = str(row["instrument_id"])
            expiry_raw = row["expiration_utc"]
            if expiry_raw is None:
                continue
            expiry = datetime.fromisoformat(str(expiry_raw).replace("Z", "+00:00"))
            if now > expiry + timedelta(days=POST_EXPIRY_SETTLEMENT_DAYS):
                continue
            last_poll_raw = row["last_settlement_poll_at"]
            if last_poll_raw:
                last_poll = datetime.fromisoformat(str(last_poll_raw).replace("Z", "+00:00"))
                if (now - last_poll).total_seconds() < self.settlement_interval_seconds:
                    continue
            # Re-read from the first observed event rather than the newest
            # cursor. The public venue can correct an older settlement after
            # later daily events have already arrived. Versioned payload hashes
            # preserve both observations and make this bounded per-contract
            # history scan idempotent.
            settlement_prefix = f"deribit:{instrument_id}:"
            earliest = connection.execute(
                """SELECT record_key FROM observations
                   WHERE kind='settlement'
                     AND substr(record_key, 1, length(?)) = ?
                   ORDER BY market_timestamp ASC, id ASC LIMIT 1""",
                (settlement_prefix, settlement_prefix),
            ).fetchone()
            cursor = str(earliest["record_key"]) if earliest is not None else None
            try:
                settlements = self.market.get_settlements(instrument_id, after_key=cursor)
                with connection:
                    for settlement in settlements:
                        payload = _settlement_payload(settlement)
                        _, was_inserted = _record(
                            connection,
                            kind="settlement",
                            key=settlement.settlement_key,
                            payload=payload,
                            received_at=settlement.received_at,
                            market_at=settlement.event_timestamp,
                        )
                        inserted += int(was_inserted)
                    latest = (
                        max(settlements, key=lambda item: item.event_timestamp)
                        if settlements
                        else None
                    )
                    connection.execute(
                        """UPDATE instruments SET last_settlement_poll_at=?,
                            last_settlement_key=COALESCE(?, last_settlement_key),
                            last_settlement_event_at=COALESCE(?, last_settlement_event_at)
                            WHERE instrument_id=?""",
                        (
                            _iso(now),
                            latest.settlement_key if latest else None,
                            _iso(latest.event_timestamp) if latest else None,
                            instrument_id,
                        ),
                    )
            except Exception as error:
                errors.append({"instrument_id": instrument_id, "reason": type(error).__name__})
                LOG.warning(
                    "settlement history unavailable for %s (%s)",
                    instrument_id,
                    type(error).__name__,
                )
        return inserted, errors

    def _archive_old(self, connection: sqlite3.Connection, *, now: datetime) -> int:
        cutoff = _iso(now - self.archive_age)
        rows = connection.execute(
            "SELECT * FROM observations WHERE received_timestamp < ? ORDER BY id", (cutoff,)
        ).fetchall()
        if not rows:
            return 0
        groups: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            day = str(row["received_timestamp"])[:10].replace("-", "")
            groups.setdefault(day, []).append(row)
        archived = 0
        for day, group in sorted(groups.items()):
            first_id, last_id = int(group[0]["id"]), int(group[-1]["id"])
            archive_path = self.archive_dir / f"observations-{day}-{first_id}-{last_id}.jsonl.gz"
            manifest_path = archive_path.with_suffix(archive_path.suffix + ".manifest.json")
            _reject_symlink_components(archive_path)
            _reject_symlink_components(manifest_path)
            body = b"".join((_canonical_json(dict(row)) + "\n").encode("utf-8") for row in group)
            compressed = gzip.compress(body, compresslevel=9, mtime=0)
            archive_hash = _sha256(compressed)
            manifest = {
                "schema_version": SCHEMA_VERSION,
                "format": "gzip-jsonl",
                "kind": "btcquant_carry_dated_public_observations",
                "archive_file": archive_path.name,
                "archive_sha256": archive_hash,
                "record_count": len(group),
                "first_observation_id": first_id,
                "last_observation_id": last_id,
                "received_from": str(group[0]["received_timestamp"]),
                "received_through": str(group[-1]["received_timestamp"]),
                "created_at": _iso(now),
            }
            if archive_path.exists():
                if not archive_path.is_file() or _sha256(archive_path.read_bytes()) != archive_hash:
                    raise RuntimeError(f"ARCHIVE_IMMUTABILITY_CONFLICT:{archive_path.name}")
            else:
                _atomic_bytes(archive_path, compressed)
            if manifest_path.exists():
                if not manifest_path.is_file():
                    raise RuntimeError(f"ARCHIVE_IMMUTABILITY_CONFLICT:{manifest_path.name}")
                existing = json.loads(manifest_path.read_text(encoding="utf-8"))
                if any(
                    existing.get(key) != value
                    for key, value in manifest.items()
                    if key != "created_at"
                ):
                    raise RuntimeError(f"ARCHIVE_IMMUTABILITY_CONFLICT:{manifest_path.name}")
            else:
                _atomic_bytes(
                    manifest_path,
                    (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode("utf-8"),
                )
            # Verify the file and all row identities before deleting live rows.
            verify_archive(manifest_path)
            connection.execute(
                """INSERT INTO archive_segments(
                    manifest_name, archive_sha256, last_verified_at,
                    verification_state, verification_reason
                ) VALUES (?, ?, ?, 'PASS', NULL)
                ON CONFLICT(manifest_name) DO UPDATE SET
                    archive_sha256=excluded.archive_sha256,
                    last_verified_at=excluded.last_verified_at,
                    verification_state='PASS', verification_reason=NULL""",
                (manifest_path.name, archive_hash, _iso(now)),
            )
            ids = [int(row["id"]) for row in group]
            connection.executemany("DELETE FROM observations WHERE id=?", ((item,) for item in ids))
            archived += len(ids)
        return archived

    def _verify_archive_once(self, connection: sqlite3.Connection, *, now: datetime) -> None:
        """Hash-check at most one due immutable segment and persist the result."""

        due: list[tuple[datetime, Path]] = []
        rescan_after = timedelta(days=1)
        for manifest_path in self.archive_dir.glob("*.manifest.json"):
            _reject_symlink_components(manifest_path)
            row = connection.execute(
                "SELECT last_verified_at FROM archive_segments WHERE manifest_name=?",
                (manifest_path.name,),
            ).fetchone()
            if row is None:
                due.append((datetime.min.replace(tzinfo=UTC), manifest_path))
                continue
            last_verified = datetime.fromisoformat(
                str(row["last_verified_at"]).replace("Z", "+00:00")
            )
            if now - last_verified >= rescan_after:
                due.append((last_verified, manifest_path))
        if not due:
            return
        _, manifest_path = min(due, key=lambda item: (item[0], item[1].name))
        archive_sha256: str | None = None
        try:
            result = verify_archive(manifest_path)
            archive_sha256 = str(result["archive_sha256"])
            state = "PASS"
            reason = None
        except Exception as error:
            state = "FAILED"
            reason = type(error).__name__
            LOG.error(
                "dated-carry archive verification failed for %s (%s)",
                manifest_path.name,
                reason,
            )
        connection.execute(
            """INSERT INTO archive_segments(
                manifest_name, archive_sha256, last_verified_at,
                verification_state, verification_reason
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(manifest_name) DO UPDATE SET
                archive_sha256=excluded.archive_sha256,
                last_verified_at=excluded.last_verified_at,
                verification_state=excluded.verification_state,
                verification_reason=excluded.verification_reason""",
            (manifest_path.name, archive_sha256, _iso(now), state, reason),
        )

    @staticmethod
    def _compact_database(connection: sqlite3.Connection) -> None:
        """Reclaim pages only after each archived segment has verified and committed."""

        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("VACUUM")

    def _status(self, *, state: str, now: datetime, **details: Any) -> dict[str, Any]:
        coverage = self._coverage()
        return {
            "schema_version": SCHEMA_VERSION,
            "service": "btcquant-carry-dated-collector",
            "paper_only": True,
            "state": state,
            "attempted_at": _iso(now),
            "database": str(self.database),
            "archive_dir": str(self.archive_dir),
            "retention_goal_days": self.retention_days,
            "poll_interval_seconds": self.interval_seconds,
            "book_depth_requested": self.book_depth,
            "book_source": (
                "runner_market_evidence" if self.books_from_runner else "collector_public_api"
            ),
            "retention_policy": (
                "goal_only; no automatic archive purge; suspend at the configured byte/free-space limits"
            ),
            "max_total_bytes": self.max_total_bytes,
            "min_free_bytes": self.min_free_bytes,
            "details": {**coverage, **details},
        }


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def verify_archive(manifest_path: Path, *, restore_database: Path | None = None) -> dict[str, Any]:
    """Verify an archive and optionally restore it into a clean/compatible DB."""

    _reject_symlink_components(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        manifest.get("schema_version") != SCHEMA_VERSION
        or manifest.get("format") != "gzip-jsonl"
        or manifest.get("kind") != "btcquant_carry_dated_public_observations"
    ):
        raise ValueError("ARCHIVE_MANIFEST_UNSUPPORTED")
    archive_name = manifest.get("archive_file")
    if (
        not isinstance(archive_name, str)
        or Path(archive_name).name != archive_name
        or not archive_name.endswith(".jsonl.gz")
    ):
        raise ValueError("ARCHIVE_FILENAME_INVALID")
    archive_path = manifest_path.parent / archive_name
    _reject_symlink_components(archive_path)
    if not archive_path.is_file() or archive_path.is_symlink():
        raise ValueError("ARCHIVE_FILE_MISSING_OR_NOT_REGULAR")
    expected_hash = manifest.get("archive_sha256")
    if (
        not isinstance(expected_hash, str)
        or len(expected_hash) != 64
        or any(char not in "0123456789abcdef" for char in expected_hash)
    ):
        raise ValueError("ARCHIVE_HASH_INVALID")
    compressed = archive_path.read_bytes()
    if _sha256(compressed) != expected_hash:
        raise ValueError("ARCHIVE_HASH_MISMATCH")
    raw = gzip.decompress(compressed)
    records = [json.loads(line) for line in raw.splitlines() if line]
    if not isinstance(manifest.get("record_count"), int) or len(records) != manifest.get("record_count"):
        raise ValueError("ARCHIVE_RECORD_COUNT_MISMATCH")
    if len({record.get("id") for record in records if isinstance(record, dict)}) != len(records):
        raise ValueError("ARCHIVE_DUPLICATE_RECORD_ID")
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("payload_json"), str):
            raise ValueError("ARCHIVE_RECORD_INVALID")
        if _sha256(record["payload_json"].encode("utf-8")) != record.get("payload_sha256"):
            raise ValueError("ARCHIVE_ROW_PAYLOAD_HASH_MISMATCH")
    if records:
        if (
            int(records[0]["id"]) != manifest.get("first_observation_id")
            or int(records[-1]["id"]) != manifest.get("last_observation_id")
            or records[0]["received_timestamp"] != manifest.get("received_from")
            or records[-1]["received_timestamp"] != manifest.get("received_through")
        ):
            raise ValueError("ARCHIVE_COVERAGE_MISMATCH")
    if restore_database is not None:
        connection = open_database(restore_database)
        try:
            with connection:
                for record in records:
                    existing = connection.execute(
                        "SELECT * FROM observations WHERE id=?", (record["id"],)
                    ).fetchone()
                    if existing is not None and dict(existing) != record:
                        raise ValueError("RESTORE_IDENTITY_CONFLICT")
                    if (
                        _sha256(str(record["payload_json"]).encode("utf-8"))
                        != record["payload_sha256"]
                    ):
                        raise ValueError("ARCHIVE_ROW_PAYLOAD_HASH_MISMATCH")
                    connection.execute(
                        """INSERT OR IGNORE INTO observations(
                            id, kind, record_key, market_timestamp, received_timestamp,
                            payload_sha256, payload_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (
                            record["id"],
                            record["kind"],
                            record["record_key"],
                            record["market_timestamp"],
                            record["received_timestamp"],
                            record["payload_sha256"],
                            record["payload_json"],
                        ),
                    )
                _rebuild_instrument_cursors(connection)
        finally:
            connection.close()
    return {
        "status": "PASS",
        "archive_sha256": manifest["archive_sha256"],
        "record_count": len(records),
        "first_observation_id": manifest.get("first_observation_id"),
        "last_observation_id": manifest.get("last_observation_id"),
    }


def _rebuild_instrument_cursors(connection: sqlite3.Connection) -> None:
    """Recreate the collector's small resume index from restored immutable rows."""

    metadata_rows = connection.execute(
        """SELECT payload_json, received_timestamp FROM observations
           WHERE kind='instrument' ORDER BY received_timestamp, id"""
    ).fetchall()
    for row in metadata_rows:
        payload = json.loads(str(row["payload_json"]))
        instrument_id = payload.get("instrument_id")
        kind = payload.get("kind")
        if not isinstance(instrument_id, str) or kind not in {"spot", "dated_future"}:
            continue
        connection.execute(
            """INSERT INTO instruments(instrument_id, kind, expiration_utc, last_seen_at)
               VALUES (?, ?, ?, ?) ON CONFLICT(instrument_id) DO UPDATE SET
               kind=excluded.kind, expiration_utc=excluded.expiration_utc,
               last_seen_at=MAX(instruments.last_seen_at, excluded.last_seen_at)""",
            (
                instrument_id,
                kind,
                payload.get("expiration_utc"),
                str(row["received_timestamp"]),
            ),
        )
    instruments = connection.execute(
        "SELECT instrument_id FROM instruments WHERE kind='dated_future'"
    ).fetchall()
    for row in instruments:
        instrument_id = str(row["instrument_id"])
        settlements = connection.execute(
            """SELECT payload_json, market_timestamp, received_timestamp FROM observations
               WHERE kind='settlement' AND record_key LIKE ?
               ORDER BY market_timestamp DESC, id DESC LIMIT 1""",
            (f"deribit:{instrument_id}:%",),
        ).fetchone()
        if settlements is None:
            continue
        payload = json.loads(str(settlements["payload_json"]))
        connection.execute(
            """UPDATE instruments SET last_settlement_key=?, last_settlement_event_at=?
               WHERE instrument_id=?""",
            (
                payload["settlement_key"],
                str(settlements["market_timestamp"]),
                instrument_id,
            ),
        )


def _run(args: argparse.Namespace) -> int:
    if args.restore_manifest:
        result = verify_archive(
            Path(args.restore_manifest), restore_database=Path(args.restore_database)
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    collector = DatedMarketCollector(
        database=Path(args.database),
        archive_dir=Path(args.archive_dir),
        status_path=Path(args.status_path),
        interval_seconds=args.interval,
        book_depth=args.book_depth,
        settlement_interval_seconds=args.settlement_interval,
        archive_age_hours=args.archive_age_hours,
        retention_days=args.retention_days,
        max_total_bytes=args.max_total_bytes,
        min_free_bytes=args.min_free_bytes,
        books_from_runner=args.books_from_runner,
    )
    lock = collector.database.with_suffix(collector.database.suffix + ".lock")
    snapshot_lock = collector.database.with_suffix(collector.database.suffix + ".snapshot.lock")
    with exclusive_lock(lock):
        if args.once:
            with exclusive_lock(snapshot_lock, blocking=True):
                result = collector.collect_once()
            print(json.dumps(result, sort_keys=True))
            return 0 if result["state"] in {"OK", "PARTIAL"} else 1
        previous_state: str | None = None
        while True:
            started = time.monotonic()
            with exclusive_lock(snapshot_lock, blocking=True):
                result = collector.collect_once()
            state = str(result.get("state", "UNKNOWN"))
            if state != previous_state:
                if state in {"FAILED", "SUSPENDED_STORAGE_BUDGET", "PARTIAL"}:
                    LOG.error(
                        "dated-carry collector state changed to %s; details=%s",
                        state,
                        result.get("reason") or result.get("details", {}),
                    )
                else:
                    LOG.info("dated-carry collector recovered to %s", state)
                previous_state = state
            if result["state"] in {"FAILED", "SUSPENDED_STORAGE_BUDGET"}:
                time.sleep(min(args.interval, 300))
                continue
            time.sleep(max(0.0, args.interval - (time.monotonic() - started)))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    runtime_root = Path(os.environ.get("BTCQUANT_ROOT", "/opt/btcquant")).absolute()
    database, archive_dir, status_path = _runtime_paths(runtime_root)
    parser.add_argument("--database", default=str(database))
    parser.add_argument("--archive-dir", default=str(archive_dir))
    parser.add_argument("--status-path", default=str(status_path))
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument(
        "--book-depth",
        type=int,
        choices=sorted(ALLOWED_BOOK_DEPTHS),
        default=DEFAULT_BOOK_DEPTH,
    )
    parser.add_argument(
        "--settlement-interval", type=int, default=DEFAULT_SETTLEMENT_INTERVAL_SECONDS
    )
    parser.add_argument("--archive-age-hours", type=int, default=DEFAULT_ARCHIVE_AGE_HOURS)
    parser.add_argument("--retention-days", type=int, default=DEFAULT_RETENTION_DAYS)
    parser.add_argument("--max-total-bytes", type=int, default=DEFAULT_MAX_TOTAL_BYTES)
    parser.add_argument("--min-free-bytes", type=int, default=DEFAULT_MIN_FREE_BYTES)
    parser.add_argument(
        "--books-from-runner",
        action="store_true",
        help="archive the runner's exact books instead of polling duplicate books",
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--restore-manifest")
    parser.add_argument("--restore-database")
    args = parser.parse_args()
    if args.interval < 5 or args.settlement_interval < 60:
        parser.error("interval must be >= 5s and settlement-interval >= 60s")
    if bool(args.restore_manifest) != bool(args.restore_database):
        parser.error("restore-manifest and restore-database must be supplied together")
    try:
        _validate_cli_paths(args, runtime_root=runtime_root)
    except ValueError as error:
        parser.error(str(error))
    logging.basicConfig(
        level=os.environ.get("BTCQUANT_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    raise SystemExit(_run(args))


if __name__ == "__main__":
    main()
