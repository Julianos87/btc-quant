from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from btcquant.operations.retention import measure_footprints, plan_retention


def _release(root: Path, name: str, payload: bytes = b"release") -> Path:
    path = root / "releases" / name
    path.mkdir(parents=True)
    (path / "manifest.json").write_bytes(payload)
    return path


def test_plan_protects_current_previous_maturity_and_state(tmp_path: Path) -> None:
    releases = tmp_path / "releases"
    releases.mkdir()
    current = _release(tmp_path, "a" * 40)
    previous = _release(tmp_path, "b" * 40)
    _release(tmp_path, "c" * 40)
    (tmp_path / "current").symlink_to(current, target_is_directory=True)
    (tmp_path / "previous").symlink_to(previous, target_is_directory=True)
    (tmp_path / "state").mkdir()
    backup = tmp_path / "backups"
    backup.mkdir()
    (backup / "state-latest.tar.gz.enc").write_bytes(b"backup")

    result = plan_retention(
        tmp_path,
        now=datetime(2026, 9, 15, tzinfo=UTC),
        protected_release_sha="c" * 40,
        latest_verified_backup="state-latest.tar.gz.enc",
    )
    by_name = {Path(item["path"]).name: item for item in result["items"]}

    assert by_name["a" * 40]["eligible_for_cleanup"] is False
    assert by_name["b" * 40]["eligible_for_cleanup"] is False
    assert by_name["c" * 40]["eligible_for_cleanup"] is False
    assert by_name["state"]["eligible_for_cleanup"] is False
    assert result["deletions_performed"] == 0


def test_unknown_backup_is_never_eligible(tmp_path: Path) -> None:
    backup = tmp_path / "backups"
    backup.mkdir()
    archive = backup / "old.tar.gz.enc"
    archive.write_bytes(b"unverified")

    result = plan_retention(tmp_path)
    item = next(item for item in result["items"] if item["path"] == str(archive))

    assert item["classification"] == "UNKNOWN"
    assert item["eligible_for_cleanup"] is False
    assert result["unknown_or_unverified_preserved"] is True


def test_symlinked_release_is_unknown_and_not_eligible(tmp_path: Path) -> None:
    releases = tmp_path / "releases"
    releases.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = releases / "outside-release"
    link.symlink_to(outside, target_is_directory=True)

    result = plan_retention(tmp_path)
    item = next(item for item in result["items"] if item["path"] == str(link))

    assert item["classification"] == "UNKNOWN"
    assert item["eligible_for_cleanup"] is False


def test_footprints_are_bounded_and_non_mutating(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "btcquant.db-wal").write_bytes(b"wal")
    (state / "runner.log").write_bytes(b"log")
    before = (state / "runner.log").stat().st_mtime_ns

    result = measure_footprints(tmp_path)

    assert result["sqlite_wal_shm"]["bytes"] == 3
    assert result["application_logs"]["bytes"] == 3
    assert (state / "runner.log").stat().st_mtime_ns == before
