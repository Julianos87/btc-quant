from __future__ import annotations

import json
import gzip
import hashlib
import multiprocessing
import sqlite3
from datetime import UTC, datetime, timedelta
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from random import Random

import pytest

from btcquant.domain.carry_dated import BookLevel, DatedBook, DatedInstrument, DatedSettlement
from btcquant.research.carry_dated_collector import (
    DatedMarketCollector,
    DatedMarketEvidenceRecorder,
    _record,
    exclusive_lock,
    open_database,
    verify_archive,
)


NOW = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
SPOT_ID = "BTC_USDC"
FUTURE_ID = "BTC_USDC-11NOV26"
SOURCE = "https://www.deribit.com/api/v2/public/get_order_book"


def _instrument(instrument_id: str, *, expiry: datetime | None = None) -> DatedInstrument:
    kind = "spot" if expiry is None else "dated_future"
    return DatedInstrument(
        venue="deribit",
        instrument_id=instrument_id,
        kind=kind,  # type: ignore[arg-type]
        base="BTC",
        quote="USDC",
        settlement_currency="USDC" if expiry else None,
        contract_size=Decimal("1"),
        amount_step=Decimal("0.0001"),
        min_amount=Decimal("0.0001"),
        tick_size=Decimal("0.1"),
        expiration_utc=expiry,
        active=True,
        maker_commission=None,
        taker_commission=None,
        fee_currency=None,
        fee_provenance="public metadata; routed spot fee currency unknown",
        metadata_timestamp=NOW - timedelta(seconds=2),
        metadata_hash=("a" if kind == "spot" else "b") * 64,
        instrument_state="open",
        source="Deribit public get_instruments",
    )


def _book(instrument_id: str, *, now: datetime = NOW) -> DatedBook:
    return DatedBook(
        instrument_id=instrument_id,
        bids=(BookLevel(Decimal("99999"), Decimal("0.025")),),
        asks=(BookLevel(Decimal("100001"), Decimal("0.03")),),
        market_timestamp=now - timedelta(seconds=1),
        received_timestamp=now,
        source=SOURCE,
        instrument_state="open",
        mark_price=Decimal("100000") if instrument_id != SPOT_ID else None,
        index_price=Decimal("100000") if instrument_id != SPOT_ID else None,
        sequence=123,
        content_hash=("c" if instrument_id == SPOT_ID else "d") * 64,
    )


def _settlement(
    *, instrument_id: str = FUTURE_ID, day: int = 0, price: str = "100000", digest_char: str = "e"
) -> DatedSettlement:
    stamp = datetime(2026, 10, 12, 8, 0, tzinfo=UTC) + timedelta(days=day)
    digest = digest_char * 64
    return DatedSettlement(
        venue="deribit",
        instrument_id=instrument_id,
        settlement_key=f"deribit:{instrument_id}:daily:{int(stamp.timestamp() * 1000)}",
        kind="daily",
        period_start=stamp - timedelta(days=1),
        period_end=stamp,
        official_index_price=Decimal(price),
        source="Deribit official public settlement history",
        received_at=stamp + timedelta(seconds=1),
        content_hash=digest,
        correction_version=f"sha256:{digest}",
        event_timestamp=stamp,
    )


class FakeMarket:
    def __init__(self, *, expiry_days: int = 30) -> None:
        self.instruments = (
            _instrument(SPOT_ID),
            _instrument(FUTURE_ID, expiry=NOW + timedelta(days=expiry_days)),
        )
        self.now = NOW
        self.books_requested: list[str] = []
        self.book_depths: list[int] = []
        self.settlement_cursors: list[str | None] = []
        self.fail_books: set[str] = set()
        self.settlement_events: tuple[DatedSettlement, ...] | None = None

    def list_active_instruments(self):
        return self.instruments

    def get_book(self, instrument_id: str, *, depth: int = 20):
        self.books_requested.append(instrument_id)
        self.book_depths.append(depth)
        if instrument_id in self.fail_books:
            raise TimeoutError("public endpoint timeout")
        return _book(instrument_id, now=self.now)

    def get_settlements(self, instrument_id: str, *, after_key: str | None = None):
        self.settlement_cursors.append(after_key)
        if self.settlement_events is not None:
            events = self.settlement_events
        else:
            events = (_settlement(instrument_id=instrument_id),)
        if after_key is None:
            return events
        keys = [item.settlement_key for item in events]
        if after_key not in keys:
            raise ValueError("settlement cursor was not found")
        return events[keys.index(after_key) :]


def _collector(tmp_path: Path, market: FakeMarket, **kwargs) -> DatedMarketCollector:
    return DatedMarketCollector(
        database=tmp_path / "market.sqlite3",
        archive_dir=tmp_path / "archive",
        status_path=tmp_path / "collector_status.json",
        market=market,
        clock=lambda: NOW,
        settlement_interval_seconds=60,
        archive_age_hours=kwargs.pop("archive_age_hours", 24),
        retention_days=kwargs.pop("retention_days", 700),
        max_total_bytes=kwargs.pop("max_total_bytes", 2 * 1024 * 1024),
        min_free_bytes=kwargs.pop("min_free_bytes", 0),
        **kwargs,
    )


def _wait_for_snapshot_lock(lock_path: str, attempted, acquired, release) -> None:
    attempted.set()
    with exclusive_lock(Path(lock_path), blocking=True):
        acquired.set()
        release.wait(timeout=5)


def test_collects_exact_observed_books_metadata_and_settlement_idempotently(tmp_path: Path) -> None:
    market = FakeMarket()
    collector = _collector(tmp_path, market)

    first = collector.collect_once(now=NOW)
    first_counts = (
        sqlite3.connect(collector.database)
        .execute("SELECT kind, COUNT(*) FROM observations GROUP BY kind")
        .fetchall()
    )
    market.now = NOW + timedelta(seconds=60)
    second = collector.collect_once(now=NOW + timedelta(seconds=60))
    third = collector.collect_once(now=NOW + timedelta(seconds=61))

    assert first["state"] == "OK"
    assert first["paper_only"] is True
    assert second["state"] == "OK"
    assert third["state"] == "OK"
    assert first["details"]["records_inserted"] == 5
    assert {kind: count for kind, count in first_counts} == {
        "book": 2,
        "cycle": 1,
        "instrument": 2,
        "settlement": 1,
    }
    connection = sqlite3.connect(collector.database)
    rows = connection.execute(
        "SELECT payload_json FROM observations WHERE kind='book' ORDER BY record_key"
    ).fetchall()
    assert (
        connection.execute("SELECT COUNT(*) FROM observations WHERE kind='book'").fetchone()[0] == 4
    )
    connection.close()
    book_payloads = [json.loads(row[0]) for row in rows]
    future_payload = next(row for row in book_payloads if row["instrument_id"] == FUTURE_ID)
    assert future_payload["bids"] == [["99999", "0.025"]]
    assert future_payload["mark_price"] == "100000"
    assert future_payload["quantity_unit"] == "BTC"
    assert future_payload["price_unit"] == "USDC per BTC"
    assert future_payload["source"] == SOURCE
    assert market.settlement_cursors == [None, "deribit:BTC_USDC-11NOV26:daily:1791792000000"]
    status = json.loads(collector.status_path.read_text(encoding="utf-8"))
    assert status["state"] == "OK"
    assert market.book_depths == [20, 20, 20, 20, 20, 20]
    assert status["book_depth_requested"] == 20
    assert status["poll_interval_seconds"] == 30


def test_runner_book_source_avoids_duplicate_public_book_polls(tmp_path: Path) -> None:
    market = FakeMarket()
    collector = _collector(tmp_path, market, books_from_runner=True)
    recorder = DatedMarketEvidenceRecorder(
        database=collector.database,
        archive_dir=collector.archive_dir,
        max_total_bytes=2 * 1024 * 1024,
        min_free_bytes=0,
    )
    for instrument_id in (SPOT_ID, FUTURE_ID):
        recorder.persist(
            _book(instrument_id),
            purpose="ENTRY_DECISION",
            requested_depth=20,
            protocol_version="carry-dated-market-evidence-v2",
            now=NOW,
        )

    result = collector.collect_once(now=NOW)

    assert result["state"] == "OK"
    assert result["book_source"] == "runner_market_evidence"
    assert result["details"]["book_source"] == "runner_market_evidence"
    assert market.books_requested == []
    with sqlite3.connect(collector.database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM observations WHERE kind='runner_book'"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM observations WHERE kind='book'"
        ).fetchone()[0] == 0


def test_runner_book_archive_restores_exact_evidence_identity(tmp_path: Path) -> None:
    market = FakeMarket()
    collector = _collector(tmp_path, market, archive_age_hours=1, max_total_bytes=4 * 1024 * 1024)
    recorder = DatedMarketEvidenceRecorder(
        database=collector.database,
        archive_dir=collector.archive_dir,
        max_total_bytes=4 * 1024 * 1024,
        min_free_bytes=0,
    )
    evidence_ids = {
        recorder.persist(
            replace(
                _book(instrument_id),
                bids=(BookLevel(Decimal("99999.00"), Decimal("0.0250")),),
                asks=(BookLevel(Decimal("100001.00"), Decimal("0.0300")),),
                mark_price=(
                    Decimal("100000.00") if instrument_id != SPOT_ID else None
                ),
                index_price=(
                    Decimal("100000.00") if instrument_id != SPOT_ID else None
                ),
            ),
            purpose="ENTRY_DECISION",
            requested_depth=20,
            protocol_version="carry-dated-market-evidence-v2",
            now=NOW,
        )
        for instrument_id in (SPOT_ID, FUTURE_ID)
    }
    connection = open_database(collector.database)
    with connection:
        archived = collector._archive_old(connection, now=NOW + timedelta(days=2))
    connection.close()

    assert archived == 2
    manifest = next(collector.archive_dir.glob("*.manifest.json"))
    restored = tmp_path / "clean-room.sqlite3"
    report = verify_archive(manifest, restore_database=restored)
    assert report["status"] == "PASS"
    with sqlite3.connect(restored) as connection:
        restored_rows = connection.execute(
            "SELECT payload_json FROM observations WHERE kind='runner_book'"
        ).fetchall()
    restored_ids = {json.loads(payload_json)["evidence_id"] for (payload_json,) in restored_rows}
    assert restored_ids == evidence_ids
    for (payload_json,) in restored_rows:
        evidence = json.loads(payload_json)
        snapshot_hash = hashlib.sha256(
            json.dumps(
                evidence["book_snapshot"],
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        assert evidence["snapshot_sha256"] == snapshot_hash
        assert evidence["book_snapshot"]["bids"][0]["price"] == "99999"
        assert evidence["book_snapshot"]["bids"][0]["quantity"] == "0.025"


def test_variable_depth20_archive_projects_within_frozen_700_day_budget(
    tmp_path: Path,
) -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    rng = Random(3477)
    database = tmp_path / "market.sqlite3"
    archive = tmp_path / "archive"
    archive.mkdir()
    recorder = DatedMarketEvidenceRecorder(
        database=database,
        archive_dir=archive,
        max_total_bytes=5 * 1024 * 1024 * 1024,
        min_free_bytes=0,
    )
    connection = open_database(database)
    sampled_polls = 300
    for poll in range(sampled_polls):
        now = base + timedelta(seconds=30 * poll)
        for instrument_id, center in ((SPOT_ID, 100000), (FUTURE_ID, 100200)):
            bids = tuple(
                BookLevel(
                    Decimal(center - level - rng.randrange(3)),
                    Decimal(str(round(rng.uniform(0.001, 1.0), 6))),
                )
                for level in range(20)
            )
            asks = tuple(
                BookLevel(
                    Decimal(center + 2 + level + rng.randrange(3)),
                    Decimal(str(round(rng.uniform(0.001, 1.0), 6))),
                )
                for level in range(20)
            )
            book = DatedBook(
                instrument_id=instrument_id,
                bids=bids,
                asks=asks,
                market_timestamp=now - timedelta(milliseconds=150),
                received_timestamp=now,
                source=SOURCE,
                instrument_state="open",
                mark_price=Decimal(center) if instrument_id != SPOT_ID else None,
                index_price=Decimal(center - 100) if instrument_id != SPOT_ID else None,
                sequence=poll,
                content_hash=f"{poll:064x}",
            )
            recorder.persist(
                book,
                purpose="ENTRY_DECISION",
                requested_depth=20,
                protocol_version="carry-dated-market-evidence-v2",
                now=now,
            )
        with connection:
            _record(
                connection,
                kind="cycle",
                key=now.isoformat(),
                payload={"state": "OK", "records_inserted": 2},
                received_at=now,
            )
    collector = _collector(
        tmp_path,
        FakeMarket(),
        max_total_bytes=5 * 1024 * 1024 * 1024,
        min_free_bytes=0,
    )
    with connection:
        archived = collector._archive_old(connection, now=base + timedelta(days=3))
    connection.close()

    assert archived == sampled_polls * 3
    manifest = next(archive.glob("*.manifest.json"))
    report = verify_archive(manifest, restore_database=tmp_path / "restored.sqlite3")
    assert report["record_count"] == sampled_polls * 3
    archive_name = json.loads(manifest.read_text())["archive_file"]
    compressed_bytes = (manifest.parent / archive_name).stat().st_size
    polls_in_700_days = 700 * 24 * 60 * 2
    projected_bytes = compressed_bytes / sampled_polls * polls_in_700_days
    assert projected_bytes < 4 * 1024 * 1024 * 1024


def test_collector_preserves_late_settlement_correction_versions(tmp_path: Path) -> None:
    market = FakeMarket()
    earlier = _settlement(day=0)
    later = _settlement(day=1, digest_char="f")
    market.settlement_events = (earlier, later)
    collector = _collector(tmp_path, market)
    first = collector.collect_once(now=NOW)
    corrected = replace(
        earlier,
        official_index_price=Decimal("99000"),
        content_hash="a" * 64,
        correction_version="sha256:" + "a" * 64,
        received_at=NOW + timedelta(hours=2),
    )
    market.settlement_events = (corrected, later)

    market.now = NOW + timedelta(hours=2)
    second = collector.collect_once(now=NOW + timedelta(hours=2))

    assert first["state"] == "OK"
    assert second["state"] == "OK"
    assert market.settlement_cursors == [None, earlier.settlement_key]
    connection = sqlite3.connect(collector.database)
    rows = connection.execute(
        "SELECT record_key, payload_sha256, payload_json FROM observations WHERE kind='settlement'"
    ).fetchall()
    connection.close()
    corrected_rows = [row for row in rows if row[0] == earlier.settlement_key]
    assert len(corrected_rows) == 2
    assert corrected_rows[0][1] != corrected_rows[1][1]


def test_captures_near_expiry_contract_without_making_it_an_entry_candidate(tmp_path: Path) -> None:
    market = FakeMarket(expiry_days=3)
    collector = _collector(tmp_path, market)

    result = collector.collect_once(now=NOW)

    assert result["state"] == "OK"
    assert FUTURE_ID not in market.books_requested
    assert market.books_requested == [SPOT_ID]
    cycle = (
        sqlite3.connect(collector.database)
        .execute("SELECT payload_json FROM observations WHERE kind='cycle'")
        .fetchone()[0]
    )
    payload = json.loads(cycle)
    assert payload["tracked_futures"] == [FUTURE_ID]
    assert payload["entry_candidate_futures"] == []
    assert payload["selected_entry_future"] is None


def test_archives_before_delete_and_restores_to_clean_database(tmp_path: Path) -> None:
    market = FakeMarket()
    collector = _collector(tmp_path, market, archive_age_hours=1)
    connection = open_database(collector.database)
    old_received = NOW - timedelta(days=3)
    with connection:
        _record(
            connection,
            kind="book",
            key="observed-book-1",
            payload={"instrument_id": FUTURE_ID, "bids": [["100000", "0.1"]]},
            received_at=old_received,
            market_at=old_received,
        )
    with connection:
        archived = collector._archive_old(connection, now=NOW)
    connection.close()

    manifests = list(collector.archive_dir.glob("*.manifest.json"))
    assert archived == 1
    assert len(manifests) == 1
    verification = verify_archive(manifests[0])
    assert verification["status"] == "PASS"
    assert verification["record_count"] == 1
    assert (
        sqlite3.connect(collector.database)
        .execute("SELECT COUNT(*) FROM observations")
        .fetchone()[0]
        == 0
    )

    restored = tmp_path / "clean-room" / "restored.sqlite3"
    restored_result = verify_archive(manifests[0], restore_database=restored)
    assert restored_result["record_count"] == 1
    restored_rows = (
        sqlite3.connect(restored)
        .execute("SELECT kind, record_key, payload_json FROM observations")
        .fetchall()
    )
    assert restored_rows == [
        (
            "book",
            "observed-book-1",
            '{"bids":[["100000","0.1"]],"instrument_id":"BTC_USDC-11NOV26"}',
        )
    ]


def test_archive_tampering_is_detected(tmp_path: Path) -> None:
    collector = _collector(tmp_path, FakeMarket(), archive_age_hours=1)
    connection = open_database(collector.database)
    with connection:
        _record(
            connection,
            kind="cycle",
            key="old-cycle",
            payload={"state": "OK"},
            received_at=NOW - timedelta(days=2),
        )
    with connection:
        collector._archive_old(connection, now=NOW)
    connection.close()
    manifest = next(collector.archive_dir.glob("*.manifest.json"))
    archive = manifest.parent / json.loads(manifest.read_text())["archive_file"]
    archive.write_bytes(archive.read_bytes() + b"corruption")

    with pytest.raises(ValueError, match="ARCHIVE_HASH_MISMATCH"):
        verify_archive(manifest)


def test_background_scrub_detects_later_archive_corruption_and_stops_collection(tmp_path: Path):
    market = FakeMarket()
    collector = _collector(tmp_path, market, archive_age_hours=1)
    connection = open_database(collector.database)
    with connection:
        _record(
            connection,
            kind="cycle",
            key="old-cycle",
            payload={"state": "OK"},
            received_at=NOW - timedelta(days=2),
        )
        collector._archive_old(connection, now=NOW)
    connection.close()
    manifest = next(collector.archive_dir.glob("*.manifest.json"))
    archive = manifest.parent / json.loads(manifest.read_text())["archive_file"]
    archive.write_bytes(archive.read_bytes() + b"late-corruption")

    result = collector.collect_once(now=NOW + timedelta(days=2))

    assert result["state"] == "FAILED"
    assert result["details"]["reason"] == "ARCHIVE_INTEGRITY_FAILURE"
    assert result["details"]["archive_integrity_status"] == "FAILED"
    assert market.books_requested == []


def test_archive_row_payload_hash_is_checked_even_when_outer_manifest_matches(
    tmp_path: Path,
) -> None:
    collector = _collector(tmp_path, FakeMarket(), archive_age_hours=1)
    connection = open_database(collector.database)
    with connection:
        _record(
            connection,
            kind="cycle",
            key="old-cycle",
            payload={"state": "OK"},
            received_at=NOW - timedelta(days=2),
        )
    with connection:
        collector._archive_old(connection, now=NOW)
    connection.close()
    manifest_path = next(collector.archive_dir.glob("*.manifest.json"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    archive_path = manifest_path.parent / manifest["archive_file"]
    record = json.loads(gzip.decompress(archive_path.read_bytes()).decode("utf-8"))
    record["payload_json"] = '{"state":"CORRUPTED"}'
    compressed = gzip.compress(
        (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"),
        compresslevel=9,
        mtime=0,
    )
    archive_path.write_bytes(compressed)
    manifest["archive_sha256"] = hashlib.sha256(compressed).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="ARCHIVE_ROW_PAYLOAD_HASH_MISMATCH"):
        verify_archive(manifest_path)


def test_archive_recovers_after_crash_between_segment_and_manifest(tmp_path: Path) -> None:
    collector = _collector(tmp_path, FakeMarket(), archive_age_hours=1)
    connection = open_database(collector.database)
    old_received = NOW - timedelta(days=2)
    with connection:
        _record(
            connection,
            kind="cycle",
            key="old-cycle",
            payload={"state": "OK"},
            received_at=old_received,
        )
    row = dict(connection.execute("SELECT * FROM observations").fetchone())
    archive_path = collector.archive_dir / f"observations-{old_received:%Y%m%d}-1-1.jsonl.gz"
    archive_path.write_bytes(
        gzip.compress(
            (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"),
            compresslevel=9,
            mtime=0,
        )
    )

    archived = collector._archive_old(connection, now=NOW)
    connection.commit()

    assert archived == 1
    assert archive_path.with_suffix(archive_path.suffix + ".manifest.json").is_file()
    assert (
        verify_archive(archive_path.with_suffix(archive_path.suffix + ".manifest.json"))["status"]
        == "PASS"
    )
    assert connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
    connection.close()


def test_clean_room_restore_rebuilds_instrument_and_settlement_cursor(tmp_path: Path) -> None:
    collector = _collector(tmp_path, FakeMarket(), archive_age_hours=1)
    connection = open_database(collector.database)
    old_received = NOW - timedelta(days=2)
    settlement = _settlement()
    with connection:
        _record(
            connection,
            kind="instrument",
            key=f"{FUTURE_ID}:metadata-v1",
            payload={
                "instrument_id": FUTURE_ID,
                "kind": "dated_future",
                "expiration_utc": (NOW + timedelta(days=30)).isoformat().replace("+00:00", "Z"),
            },
            received_at=old_received,
        )
        _record(
            connection,
            kind="settlement",
            key=settlement.settlement_key,
            payload={
                "settlement_key": settlement.settlement_key,
                "instrument_id": FUTURE_ID,
            },
            received_at=old_received + timedelta(seconds=1),
            market_at=settlement.event_timestamp,
        )
    collector._archive_old(connection, now=NOW)
    connection.close()
    manifest_path = next(collector.archive_dir.glob("*.manifest.json"))
    restored = tmp_path / "clean-room" / "market.sqlite3"

    verify_archive(manifest_path, restore_database=restored)

    restored_connection = sqlite3.connect(restored)
    row = restored_connection.execute(
        "SELECT instrument_id, last_settlement_key FROM instruments"
    ).fetchone()
    restored_connection.close()
    assert row == (FUTURE_ID, settlement.settlement_key)


def test_quota_suspends_collection_before_requesting_public_data(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "existing-segment").write_bytes(b"x" * (1024 * 1024 + 1))
    market = FakeMarket()
    collector = _collector(tmp_path, market, max_total_bytes=1024 * 1024)

    result = collector.collect_once(now=NOW)

    assert result["state"] == "SUSPENDED_STORAGE_BUDGET"
    assert market.books_requested == []
    assert not collector.database.exists()


def test_collector_lock_rejects_a_second_instance(tmp_path: Path) -> None:
    lock = tmp_path / "collector.lock"
    with exclusive_lock(lock):
        with pytest.raises(RuntimeError, match="ALREADY_RUNNING"):
            with exclusive_lock(lock):
                raise AssertionError("second collector should not acquire the lock")


def test_snapshot_lock_serializes_independent_processes(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    lock = tmp_path / "market.sqlite3.snapshot.lock"
    attempted = context.Event()
    acquired = context.Event()
    release_child = context.Event()
    child = context.Process(
        target=_wait_for_snapshot_lock,
        args=(str(lock), attempted, acquired, release_child),
    )
    try:
        with exclusive_lock(lock, blocking=True):
            child.start()
            assert attempted.wait(timeout=5)
            assert not acquired.wait(timeout=0.2)
        assert acquired.wait(timeout=5)
    finally:
        release_child.set()
        if child.pid is not None:
            child.join(timeout=5)
            if child.is_alive():
                child.terminate()
                child.join(timeout=5)
    assert child.exitcode == 0


def test_failed_book_is_recorded_as_partial_without_losing_other_books(tmp_path: Path) -> None:
    market = FakeMarket()
    market.fail_books.add(FUTURE_ID)
    collector = _collector(tmp_path, market)

    result = collector.collect_once(now=NOW)

    assert result["state"] == "PARTIAL"
    assert result["details"]["book_errors"] == [
        {"instrument_id": FUTURE_ID, "reason": "TimeoutError"}
    ]
    connection = sqlite3.connect(collector.database)
    assert (
        connection.execute("SELECT COUNT(*) FROM observations WHERE kind='book'").fetchone()[0] == 1
    )
    assert (
        connection.execute("SELECT COUNT(*) FROM observations WHERE kind='cycle'").fetchone()[0]
        == 1
    )
    connection.close()
