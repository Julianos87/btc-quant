from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest

from btcquant.entrypoints.carry_dated_experimental import _validate_status_path
from btcquant.research.carry_dated_collector import _runtime_paths, _validate_cli_paths


def test_runner_status_path_must_remain_inside_its_experiment_state(tmp_path: Path) -> None:
    state_root = tmp_path / "state" / "carry-dated-experiment"
    state_root.mkdir(parents=True)
    database = state_root / "carry_dated.db"

    path = _validate_status_path(
        state_root / "carry_dated_status.json",
        state_root=state_root,
        database=database,
    )

    assert path == state_root.resolve() / "carry_dated_status.json"
    with pytest.raises(ValueError, match="isolated state"):
        _validate_status_path(
            tmp_path / "state" / "btcquant.db",
            state_root=state_root,
            database=database,
        )
    with pytest.raises(ValueError, match="isolated state"):
        _validate_status_path(database, state_root=state_root, database=database)


def test_runner_status_rejects_symlink_to_canonical_database(tmp_path: Path) -> None:
    state_root = tmp_path / "state" / "carry-dated-experiment"
    state_root.mkdir(parents=True)
    canonical = tmp_path / "state" / "btcquant.db"
    canonical.write_text("protected", encoding="utf-8")
    link = state_root / "carry_dated_status.json"
    link.symlink_to(canonical)

    with pytest.raises(ValueError, match="symlink"):
        _validate_status_path(link, state_root=state_root, database=state_root / "carry_dated.db")

    assert canonical.read_text(encoding="utf-8") == "protected"


def test_collector_cli_accepts_only_its_isolated_runtime_paths(tmp_path: Path) -> None:
    database, archive_dir, status_path = _runtime_paths(tmp_path)
    args = Namespace(
        database=str(database),
        archive_dir=str(archive_dir),
        status_path=str(status_path),
        restore_manifest=None,
        restore_database=None,
    )
    _validate_cli_paths(args, runtime_root=tmp_path)

    args.status_path = str(tmp_path / "state" / "btcquant.db")
    with pytest.raises(ValueError, match="isolated runtime path"):
        _validate_cli_paths(args, runtime_root=tmp_path)


def test_collector_restore_is_confined_to_archive_and_clean_room_paths(tmp_path: Path) -> None:
    database, archive_dir, status_path = _runtime_paths(tmp_path)
    archive_dir.mkdir(parents=True)
    manifest = archive_dir / "observations.manifest.json"
    manifest.touch()
    args = Namespace(
        database=str(database),
        archive_dir=str(archive_dir),
        status_path=str(status_path),
        restore_manifest=str(manifest),
        restore_database=str(
            tmp_path / "state" / "carry-dated-experiment" / "restore-test" / "restored.sqlite3"
        ),
    )
    _validate_cli_paths(args, runtime_root=tmp_path)

    args.restore_database = str(tmp_path / "state" / "btcquant.db")
    with pytest.raises(ValueError, match="clean-room"):
        _validate_cli_paths(args, runtime_root=tmp_path)
