from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess

import pytest

from scripts import stream_encrypt_archive, verify_backup

REPO_ROOT = Path(__file__).resolve().parents[1]


def _sqlite_fixture(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE sample (value TEXT NOT NULL)")
        connection.execute("INSERT INTO sample VALUES ('paper-fixture')")
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_streamed_encrypted_archive_verifies_and_restores_without_plain_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path / "work"
    state = work / "state"
    _sqlite_fixture(state / "btcquant.db")
    encrypted = tmp_path / "state-20261018-1200.tar.gz.enc"
    digest_path = tmp_path / "state.tar.sha256"
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", "small-fixture-key")

    source_digest = stream_encrypt_archive.stream_encrypt_archive(work, encrypted, digest_path)
    assert digest_path.read_text(encoding="ascii").strip() == source_digest
    assert source_digest == hashlib.sha256(
        subprocess.run(
            ["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "200000",
             "-in", str(encrypted), "-pass", "env:BACKUP_ENCRYPTION_KEY"],
            env=os.environ.copy(),
            check=True,
            capture_output=True,
        ).stdout
    ).hexdigest()
    assert not list(tmp_path.rglob("*.tar.gz"))

    proof_path = encrypted.with_name(f"{encrypted.name}.verification.json")
    verify_backup.write_archive_verification_record(
        encrypted,
        proof_path,
        expected_plaintext_sha256=source_digest,
    )
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    assert proof["verification_checks"] == {
        "encrypted_round_trip": "PASS",
        "archive_read": "PASS",
        "sqlite_snapshots": "PASS",
    }

    restored = tmp_path / "restore"
    report = verify_backup.verify_archive(encrypted, extract_to=restored)
    assert report["integrity"] == "ok"
    with sqlite3.connect(restored / "state" / "btcquant.db") as connection:
        assert connection.execute("SELECT value FROM sample").fetchone()[0] == "paper-fixture"


def test_streamed_archive_rejects_symlink_members_before_sidecar_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path / "work"
    state = work / "state"
    _sqlite_fixture(state / "btcquant.db")
    (state / "escape").symlink_to("../../outside")
    encrypted = tmp_path / "state-20261018-1201.tar.gz.enc"
    digest_path = tmp_path / "state.tar.sha256"
    proof_path = encrypted.with_name(f"{encrypted.name}.verification.json")
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", "small-fixture-key")
    digest = stream_encrypt_archive.stream_encrypt_archive(work, encrypted, digest_path)

    with pytest.raises(ValueError, match="link is not allowed"):
        verify_backup.write_archive_verification_record(
            encrypted,
            proof_path,
            expected_plaintext_sha256=digest,
        )
    assert not proof_path.exists()


def test_streamed_archive_rejects_wrong_plaintext_digest_without_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path / "work"
    _sqlite_fixture(work / "state" / "btcquant.db")
    encrypted = tmp_path / "state-20261018-1202.tar.gz.enc"
    digest_path = tmp_path / "state.tar.sha256"
    proof_path = encrypted.with_name(f"{encrypted.name}.verification.json")
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", "small-fixture-key")
    stream_encrypt_archive.stream_encrypt_archive(work, encrypted, digest_path)

    with pytest.raises(RuntimeError, match="do not match"):
        verify_backup.write_archive_verification_record(
            encrypted,
            proof_path,
            expected_plaintext_sha256="0" * 64,
        )
    assert not proof_path.exists()


def test_backup_pipeline_keeps_reserve_and_pushes_verified_encrypted_bytes(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "opt" / "btcquant"
    state = runtime / "state"
    backups = runtime / "backups"
    release = runtime / "releases" / "fixture"
    repo = runtime / "backups-repo"
    remote = tmp_path / "offsite.git"
    tmpdir = tmp_path / "staging"
    for path in (state, backups, release / "scripts", repo, tmpdir):
        path.mkdir(parents=True, exist_ok=True)
    _sqlite_fixture(state / "btcquant.db")
    for name in (
        "backup_state.sh",
        "backup_database.py",
        "verify_backup.py",
        "stream_encrypt_archive.py",
        "backup_carry_dated_evidence.py",
    ):
        shutil.copy2(REPO_ROOT / "scripts" / name, release / "scripts" / name)

    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Fixture"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "fixture@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", str(remote)], check=True
    )
    env = os.environ.copy()
    env.update(
        {
            "BACKUP_ENCRYPTION_KEY": "isolated-backup-fixture-key",
            "BTCQUANT_ROOT": str(runtime),
            "TMPDIR": str(tmpdir),
        }
    )
    result = subprocess.run(
        ["bash", str(release / "scripts" / "backup_state.sh")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    archive = next(backups.glob("state-*.tar.gz.enc"))
    proof = archive.with_name(f"{archive.name}.verification.json")
    assert json.loads(proof.read_text(encoding="utf-8"))["status"] == "PASS"
    assert not list(tmp_path.rglob("state-*.tar.gz"))
    assert os.stat(repo / archive.name).st_ino == os.stat(archive).st_ino
    for path in (archive.name, proof.name):
        remote_bytes = subprocess.run(
            ["git", "--git-dir", str(remote), "show", f"refs/heads/backups:{path}"],
            check=True,
            capture_output=True,
        ).stdout
        assert remote_bytes == (backups / path).read_bytes()
    assert not list(tmpdir.glob("btcquant-backup.*"))
