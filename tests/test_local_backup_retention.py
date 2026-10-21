from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location(
    "backup_retention", SCRIPTS / "prune_local_backups.py"
)
assert spec and spec.loader
retention = importlib.util.module_from_spec(spec)
spec.loader.exec_module(retention)


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()


@pytest.fixture
def layout(tmp_path):
    backups, repo = tmp_path / "backups", tmp_path / "backups-repo"
    backups.mkdir()
    repo.mkdir()
    git(repo, "init", "-b", "backups")
    git(repo, "config", "user.name", "test")
    git(repo, "config", "user.email", "test@example.invalid")
    names = [f"state-202610{day}-0300.tar.gz.enc" for day in ("18", "20", "21")]
    for name in names:
        data = name.encode()
        (backups / name).write_bytes(data)
        record = {
            "status": "PASS",
            "archive_name": name,
            "archive_size_bytes": len(data),
            "archive_sha256": hashlib.sha256(data).hexdigest(),
            "verification_checks": {
                key: "PASS" for key in ("encrypted_round_trip", "archive_read", "sqlite_snapshots")
            },
        }
        (backups / (name + ".verification.json")).write_text(json.dumps(record))
    for path in backups.iterdir():
        shutil.copy2(path, repo / path.name)
    git(repo, "add", ".")
    git(repo, "commit", "-m", "fixture")
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "--bare", str(repo), str(remote)], check=True)
    git(remote, "config", "uploadpack.allowFilter", "true")
    git(repo, "remote", "add", "origin", remote.as_uri())
    return tmp_path, backups, repo, remote, names


def test_prune_keeps_two_days_and_remote_history(layout, monkeypatch):
    root, backups, repo, remote, names = layout
    real_rename = retention.os.rename

    def confined_rename(source, destination):
        # Distinct systemd writable mounts reject cross-root renames even
        # when both paths report the same underlying physical device.
        assert Path(source).is_relative_to(repo)
        assert Path(destination).is_relative_to(repo)
        return real_rename(source, destination)

    monkeypatch.setattr(retention.os, "rename", confined_rename)
    head = git(remote, "rev-parse", "HEAD")
    plan = retention.prune(root, apply=False, compact=False)
    assert plan["candidates"] == names[:1]
    assert (backups / names[0]).exists()
    result = retention.prune(root, apply=True, compact=True)
    assert result["removed"] == names[:1]
    assert not (backups / names[0]).exists()
    assert not (repo / names[0]).exists()
    assert all((backups / name).exists() and (repo / name).exists() for name in names[1:])
    assert git(remote, "rev-parse", "HEAD") == head
    assert git(remote, "show", f"HEAD:{names[0]}") == names[0]
    assert git(repo, "status", "--porcelain") == ""
    # The previous release writer can still add tomorrow's archive after compaction.
    (repo / "state-20261022-0300.tar.gz.enc").write_bytes(b"next")
    git(repo, "add", "--", "state-20261022-0300.tar.gz.enc")
    assert "A  state-20261022" in git(repo, "status", "--porcelain")


@pytest.mark.parametrize("failure", ["remote_mismatch", "bad_record", "legacy_unverified"])
def test_retention_fails_closed(layout, monkeypatch, failure):
    root, backups, repo, _, names = layout
    if failure == "remote_mismatch":
        (backups / names[0]).write_bytes(b"changed")
    elif failure == "bad_record":
        (backups / (names[-1] + ".verification.json")).write_text("{}")
    else:
        (backups / (names[0] + ".verification.json")).unlink()

        def unavailable(*args):
            raise ValueError("restore failed")

        monkeypatch.setattr(retention, "verify_archive", unavailable)
    with pytest.raises(ValueError):
        retention.prune(root, apply=True, compact=False)
    assert all((backups / name).exists() for name in names)
    assert git(repo, "status", "--porcelain") == ""


def test_same_day_copies_do_not_replace_previous_day():
    assert retention.retained_names(
        [
            "state-20261020-0300.tar.gz.enc",
            "state-20261021-0300.tar.gz.enc",
            "state-20261021-1200.tar.gz.enc",
        ]
    ) == {"state-20261020-0300.tar.gz.enc", "state-20261021-1200.tar.gz.enc"}
    with pytest.raises(ValueError):
        retention.retained_names(["state-20261021-0300.tar.gz.enc"])
