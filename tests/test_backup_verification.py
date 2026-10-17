from __future__ import annotations

import io
import os
import subprocess
import json
import tarfile
from pathlib import Path

import pytest

from scripts import verify_backup


def _write_plain_archive(path: Path) -> None:
    payload = b"paper snapshot fixture"
    info = tarfile.TarInfo("state/btcquant.db")
    info.size = len(payload)
    with tarfile.open(path, "w:gz") as archive:
        archive.addfile(info, io.BytesIO(payload))


def test_verification_reuses_staged_plaintext_and_records_matching_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    encrypted = tmp_path / "state-20261017-1200.tar.gz.enc"
    encrypted.write_bytes(b"encrypted fixture")
    plaintext = tmp_path / "state.tar.gz"
    _write_plain_archive(plaintext)
    destination = encrypted.with_name(f"{encrypted.name}.verification.json")

    monkeypatch.setattr(
        verify_backup,
        "_sha256_decrypted",
        lambda _archive: verify_backup._sha256_file(plaintext),
    )
    monkeypatch.setattr(
        verify_backup,
        "_decrypt",
        lambda *_args: pytest.fail("the staged plaintext must be reused"),
    )

    result = verify_backup.write_archive_verification_record(
        encrypted,
        destination,
        plaintext_archive=plaintext,
    )

    record = json.loads(result.read_text(encoding="utf-8"))
    assert record["status"] == "PASS"
    assert record["archive_name"] == encrypted.name
    assert record["verification_checks"]["archive_read"] == "PASS"


def test_verification_rejects_plaintext_mismatch_without_publishing_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    encrypted = tmp_path / "state-20261017-1200.tar.gz.enc"
    encrypted.write_bytes(b"encrypted fixture")
    plaintext = tmp_path / "state.tar.gz"
    _write_plain_archive(plaintext)
    destination = encrypted.with_name(f"{encrypted.name}.verification.json")

    monkeypatch.setattr(verify_backup, "_sha256_decrypted", lambda _archive: "0" * 64)

    with pytest.raises(RuntimeError, match="decrypted bytes do not match"):
        verify_backup.write_archive_verification_record(
            encrypted,
            destination,
            plaintext_archive=plaintext,
        )
    assert not destination.exists()


def test_streaming_decryption_matches_real_encrypted_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    encrypted = tmp_path / "state-20261017-1200.tar.gz.enc"
    plaintext = tmp_path / "state.tar.gz"
    _write_plain_archive(plaintext)
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", "test-fixture-key")
    subprocess.run(
        [
            "openssl",
            "enc",
            "-aes-256-cbc",
            "-pbkdf2",
            "-iter",
            "200000",
            "-salt",
            "-in",
            str(plaintext),
            "-out",
            str(encrypted),
            "-pass",
            "env:BACKUP_ENCRYPTION_KEY",
        ],
        env=os.environ.copy(),
        check=True,
        capture_output=True,
    )
    destination = encrypted.with_name(f"{encrypted.name}.verification.json")

    result = verify_backup.write_archive_verification_record(
        encrypted,
        destination,
        plaintext_archive=plaintext,
    )

    record = json.loads(result.read_text(encoding="utf-8"))
    assert record["verification_checks"] == {
        "encrypted_round_trip": "PASS",
        "archive_read": "PASS",
        "sqlite_snapshots": "PASS",
    }
