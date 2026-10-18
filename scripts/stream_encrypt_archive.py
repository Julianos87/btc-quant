"""Create an encrypted tar stream without staging a plaintext archive."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import subprocess
import sys


def stream_encrypt_archive(source_dir: Path, encrypted_path: Path, digest_path: Path) -> str:
    key = os.environ.get("BACKUP_ENCRYPTION_KEY", "")
    if not key.strip():
        raise RuntimeError("BACKUP_ENCRYPTION_KEY is required")
    source_dir = source_dir.resolve(strict=True)
    state_dir = source_dir / "state"
    if not state_dir.is_dir() or state_dir.is_symlink():
        raise RuntimeError("archive source must contain a regular state directory")
    if encrypted_path.exists() or encrypted_path.is_symlink():
        raise FileExistsError(f"encrypted output already exists: {encrypted_path}")
    if digest_path.exists() or digest_path.is_symlink():
        raise FileExistsError(f"plaintext digest already exists: {digest_path}")

    # O_EXCL and O_NOFOLLOW make a pre-existing output path unable to redirect
    # OpenSSL through a symlink. The temporary directory itself is private.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    encrypted_fd = os.open(encrypted_path, flags, 0o600)
    encrypted_created = True
    digest_created = False
    digest = hashlib.sha256()
    tar_command = ["tar", "-czf", "-", "-C", str(source_dir), "--", "state"]
    openssl_command = [
        "openssl",
        "enc",
        "-aes-256-cbc",
        "-pbkdf2",
        "-iter",
        "200000",
        "-salt",
        "-pass",
        "env:BACKUP_ENCRYPTION_KEY",
    ]
    tar_process: subprocess.Popen[bytes] | None = None
    encrypt_process: subprocess.Popen[bytes] | None = None
    try:
        with os.fdopen(encrypted_fd, "wb", closefd=True) as encrypted_stream:
            tar_process = subprocess.Popen(tar_command, stdout=subprocess.PIPE)
            assert tar_process.stdout is not None
            encrypt_process = subprocess.Popen(
                openssl_command,
                stdin=subprocess.PIPE,
                stdout=encrypted_stream,
            )
            assert encrypt_process.stdin is not None
            try:
                for chunk in iter(lambda: tar_process.stdout.read(1024 * 1024), b""):
                    digest.update(chunk)
                    encrypt_process.stdin.write(chunk)
            except (BrokenPipeError, OSError):
                tar_process.kill()
                encrypt_process.kill()
                raise
            finally:
                tar_process.stdout.close()
                encrypt_process.stdin.close()

            tar_status = tar_process.wait()
            encrypt_status = encrypt_process.wait()
            if tar_status != 0:
                raise subprocess.CalledProcessError(tar_status, tar_command)
            if encrypt_status != 0:
                raise subprocess.CalledProcessError(encrypt_status, openssl_command)
            encrypted_stream.flush()
            os.fsync(encrypted_stream.fileno())

        checksum = digest.hexdigest()
        digest_fd = os.open(digest_path, flags, 0o600)
        digest_created = True
        with os.fdopen(digest_fd, "w", encoding="ascii", closefd=True) as digest_stream:
            digest_stream.write(checksum + "\n")
            digest_stream.flush()
            os.fsync(digest_stream.fileno())
        return checksum
    except Exception:
        for process in (tar_process, encrypt_process):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()
        if encrypted_created:
            encrypted_path.unlink(missing_ok=True)
        if digest_created:
            digest_path.unlink(missing_ok=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_dir", type=Path)
    parser.add_argument("encrypted_output", type=Path)
    parser.add_argument("plaintext_sha256_output", type=Path)
    args = parser.parse_args()
    checksum = stream_encrypt_archive(
        args.source_dir,
        args.encrypted_output,
        args.plaintext_sha256_output,
    )
    print(checksum)


if __name__ == "__main__":
    main()
