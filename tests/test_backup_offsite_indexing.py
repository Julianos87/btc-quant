from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def _run(*args: str, cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        list(args), cwd=cwd, env=env, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _git(repo: Path, *args: str) -> str:
    return _run("git", "-C", str(repo), *args)


def _init_mirror(repo: Path, remote: Path) -> None:
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(
        ["git", "init", "--quiet", "--initial-branch=backups", str(repo)],
        check=True,
        capture_output=True,
    )
    _git(repo, "config", "user.name", "BTCQuant backup tests")
    _git(repo, "config", "user.email", "backup-tests@example.invalid")
    _git(repo, "config", "extensions.worktreeConfig", "true")
    _git(repo, "commit", "--quiet", "--allow-empty", "-m", "fixture base")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "--quiet", "origin", "HEAD:refs/heads/backups")


def _store_blob_without_checkout(repo: Path, source: Path, name: str) -> None:
    blob = _git(repo, "hash-object", "-w", f"--path={name}", "--", str(source))
    _git(repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},{name}")
    _git(repo, "update-index", "--skip-worktree", "--", name)


def _write_verification(archive: Path) -> None:
    payload = {
        "status": "PASS",
        "archive_name": archive.name,
        "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "archive_size_bytes": archive.stat().st_size,
        "verification_checks": {
            "encrypted_round_trip": "PASS",
            "archive_read": "PASS",
            "sqlite_snapshots": "PASS",
        },
    }
    Path(str(archive) + ".verification.json").write_text(json.dumps(payload), encoding="utf-8")


def _write_executable(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")
    path.chmod(0o755)


def test_backup_offsite_falls_back_when_hard_link_is_rejected(tmp_path: Path) -> None:
    runtime = tmp_path / "opt" / "btcquant"
    state = runtime / "state"
    backups = runtime / "backups"
    repo = runtime / "backups-repo"
    release = runtime / "releases" / ("a" * 40)
    state.mkdir(parents=True)
    backups.mkdir()
    (runtime / "data").mkdir()
    (release / "scripts").mkdir(parents=True)
    (release / "venv" / "bin").mkdir(parents=True)
    (release / "state").symlink_to(Path("../../state"))
    (release / "backups").symlink_to(Path("../../backups"))
    (release / "backups-repo").symlink_to(Path("../../backups-repo"))
    (runtime / "current").symlink_to(release)
    for name in (
        "backup_state.sh",
        "backup_database.py",
        "stream_encrypt_archive.py",
        "backup_carry_dated_evidence.py",
        "verify_backup.py",
        "prune_local_backups.py",
    ):
        (release / "scripts" / name).write_text(
            (SCRIPTS / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    (release / "scripts" / "backup_state.sh").chmod(0o755)
    python = release / "venv" / "bin" / "python"
    _write_executable(python, f'#!/bin/sh\nexec {sys.executable} "$@"\n')

    with sqlite3.connect(state / "btcquant.db") as connection:
        connection.execute("CREATE TABLE sample (value TEXT)")
        connection.execute("INSERT INTO sample VALUES ('fixture')")

    repo.mkdir()
    remote = tmp_path / "remote.git"
    _init_mirror(repo, remote)

    fakebin = tmp_path / "fake-bin"
    link_log = tmp_path / "ln-called"
    _write_executable(fakebin / "ln", f"#!/bin/sh\nprintf called >> '{link_log}'\nexit 1\n")
    env = os.environ.copy()
    env.update(
        {
            "BACKUP_ENCRYPTION_KEY": "fixture-only-key",
            "BACKUP_OFFHOST_REMOTE": "origin",
            "BACKUP_OFFHOST_BRANCH": "backups",
            "BTCQUANT_BACKUP_POST_FREE_BYTES": "0",
            "BTCQUANT_ROOT": str(runtime),
            "PYTHONPATH": str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", ""),
            "TMPDIR": str(tmp_path),
            "PATH": str(fakebin) + os.pathsep + env["PATH"],
        }
    )
    result = subprocess.run(
        ["bash", str(release / "scripts" / "backup_state.sh")],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert link_log.read_text(encoding="utf-8") == "calledcalled"
    assert "Invalid cross-device link" not in result.stderr

    archive = next(backups.glob("state-*.tar.gz.enc"))
    proof = Path(str(archive) + ".verification.json")
    head = _git(repo, "rev-parse", "HEAD")
    assert _run("git", "--git-dir", str(remote), "rev-parse", "refs/heads/backups") == head
    for source in (archive, proof):
        blob = _git(repo, "rev-parse", f"{head}:{source.name}")
        assert blob == _git(repo, "hash-object", "--", str(source))
        assert not (repo / source.name).exists()
        assert _git(repo, "ls-files", "-v", "--", source.name).startswith("S ")
    assert _git(repo, "status", "--porcelain") == ""


def test_compaction_supports_verified_blobs_without_checkout_files(tmp_path: Path) -> None:
    runtime = tmp_path / "opt" / "btcquant"
    backups = runtime / "backups"
    repo = runtime / "backups-repo"
    backups.mkdir(parents=True)
    repo.mkdir(parents=True)
    remote = tmp_path / "remote.git"
    _init_mirror(repo, remote)

    names = (
        "state-20250101-0300.tar.gz.enc",
        "state-20250102-0300.tar.gz.enc",
    )
    for index, name in enumerate(names):
        archive = backups / name
        archive.write_bytes(f"ciphertext-{index}".encode())
        _write_verification(archive)
        proof = Path(str(archive) + ".verification.json")
        _store_blob_without_checkout(repo, archive, name)
        _store_blob_without_checkout(repo, proof, proof.name)
        assert not (repo / name).exists()
        assert not (repo / proof.name).exists()

    evidence_dir = backups / "carry-dated-evidence"
    evidence_dir.mkdir()
    evidence = evidence_dir / "observations-20250102-1-2.jsonl.gz.enc"
    evidence.write_bytes(b"encrypted observation evidence")
    _store_blob_without_checkout(repo, evidence, f"carry-dated-evidence/{evidence.name}")

    _git(repo, "commit", "--quiet", "-m", "verified encrypted backups")
    _git(repo, "push", "--quiet", "origin", "HEAD:refs/heads/backups")
    assert _git(repo, "status", "--porcelain") == ""

    env = os.environ.copy()
    env.update({"BACKUP_OFFHOST_REMOTE": "origin", "BACKUP_OFFHOST_BRANCH": "backups"})
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "prune_local_backups.py"),
            "--root",
            str(runtime),
            "--apply",
            "--compact-mirror",
        ],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert '"mirror_compacted"' in result.stdout
    for name in names:
        archive = backups / name
        proof = Path(str(archive) + ".verification.json")
        assert (repo / name).read_bytes() == archive.read_bytes()
        assert (repo / proof.name).read_bytes() == proof.read_bytes()
    mirrored_evidence = repo / "carry-dated-evidence" / evidence.name
    assert mirrored_evidence.read_bytes() == evidence.read_bytes()
    assert _git(repo, "status", "--porcelain") == ""
    assert _git(repo, "rev-parse", "HEAD") == _run(
        "git", "--git-dir", str(remote), "rev-parse", "refs/heads/backups"
    )
