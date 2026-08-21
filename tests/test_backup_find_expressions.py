"""GNU find expressions used by backup_state.sh must parse as one command."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "backup_state.sh"

SQLITE_LIKE_FIND = r"""
find "$1" -maxdepth 1 -type f \
  \( -name '*.db' -o -name '*.db-*' -o -name '*.sqlite' \
     -o -name '*.sqlite-*' -o -name '*.sqlite3' -o -name '*.sqlite3-*' \) \
  -print
"""

BROKEN_SPLIT_FIND = r"""
find "$1" -maxdepth 1 -type f \(
    -name '*.db' -o -name '*.db-*' \
    -o -name '*.sqlite' -o -name '*.sqlite-*' \
    -o -name '*.sqlite3' -o -name '*.sqlite3-*' \
  \) -print
"""

OFFHOST_PLAINTEXT_FIND = r"""
find "$1" -maxdepth 1 -type f \
  \( -name 'state-*.tar.gz' -o -name '*.db' -o -name '*.db-*' \
  -o -name '*.sqlite' -o -name '*.sqlite-*' \
  -o -name '*.sqlite3' -o -name '*.sqlite3-*' -o -name '.env' \) \
  -print
"""

RETENTION_FIND = r"""
find "$1" \( -name 'state-*.tar.gz' -o -name 'state-*.tar.gz.enc' \) \
  -mtime +30 -print
"""

OFFHOST_ENC_RETENTION_FIND = r"""
find "$1" -maxdepth 1 -name 'state-*.tar.gz.enc' -mtime +30 -print
"""


def _run_find(snippet: str, directory: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            "--noprofile",
            "--norc",
            "-c",
            "set -euo pipefail\n" + snippet,
            "_",
            str(directory),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def _touch_old(path: Path) -> None:
    path.write_bytes(b"x")
    os.utime(path, (0, 0))


def test_source_does_not_split_find_after_open_paren() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    assert 'find "${STATE_DIR}" -maxdepth 1 -type f \\\n' in script
    assert "\\( -name '*.db'" in script
    assert "type f \\(\n" not in script
    assert (
        SQLITE_LIKE_FIND.strip().splitlines()[1].strip()
        in {line.strip() for line in script.splitlines()}
        or "\\( -name '*.db' -o -name '*.db-*' -o -name '*.sqlite'" in script
    )


def test_broken_newline_after_open_paren_reproduces_gnu_find_error(tmp_path: Path) -> None:
    (tmp_path / "btcquant.db").write_bytes(b"db")
    result = _run_find(BROKEN_SPLIT_FIND, tmp_path)
    assert result.returncode != 0
    assert "expected to find a ')'" in result.stderr


def test_unknown_sqlite_scan_parses_and_lists_expected_names(tmp_path: Path) -> None:
    for name in (
        "btcquant.db",
        "btcquant.db-wal",
        "unknown.db",
        "foo.sqlite",
        "foo.sqlite3",
        "notes.txt",
        "state-20260821-1847.tar.gz.enc",
        ".env",
    ):
        (tmp_path / name).write_text("x", encoding="utf-8")
    result = _run_find(SQLITE_LIKE_FIND, tmp_path)
    assert result.returncode == 0, result.stderr
    names = {Path(line).name for line in result.stdout.splitlines() if line}
    assert names == {
        "btcquant.db",
        "btcquant.db-wal",
        "unknown.db",
        "foo.sqlite",
        "foo.sqlite3",
    }
    assert "notes.txt" not in names
    assert ".env" not in names
    assert "state-20260821-1847.tar.gz.enc" not in names


def test_offhost_plaintext_scan_detects_prohibited_artifacts(tmp_path: Path) -> None:
    (tmp_path / "unknown.db").write_text("x", encoding="utf-8")
    (tmp_path / "foo.sqlite").write_text("x", encoding="utf-8")
    (tmp_path / "foo.sqlite3").write_text("x", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=1", encoding="utf-8")
    (tmp_path / "state-20260821-0300.tar.gz").write_text("plain", encoding="utf-8")
    (tmp_path / "state-20260821-1847.tar.gz.enc").write_text("ok", encoding="utf-8")
    result = _run_find(OFFHOST_PLAINTEXT_FIND, tmp_path)
    assert result.returncode == 0, result.stderr
    names = {Path(line).name for line in result.stdout.splitlines() if line}
    assert "unknown.db" in names
    assert "foo.sqlite" in names
    assert "foo.sqlite3" in names
    assert ".env" in names
    assert "state-20260821-0300.tar.gz" in names
    assert "state-20260821-1847.tar.gz.enc" not in names


def test_allowed_encrypted_only_repo_passes_plaintext_scan(tmp_path: Path) -> None:
    (tmp_path / "state-20260821-1847.tar.gz.enc").write_text("ok", encoding="utf-8")
    result = _run_find(OFFHOST_PLAINTEXT_FIND, tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


def test_retention_selection_parses_and_selects_old_archives_only(tmp_path: Path) -> None:
    old_enc = tmp_path / "state-20260101-0000.tar.gz.enc"
    old_plain = tmp_path / "state-20260101-0000.tar.gz"
    fresh = tmp_path / "state-20260821-1847.tar.gz.enc"
    _touch_old(old_enc)
    _touch_old(old_plain)
    fresh.write_text("new", encoding="utf-8")
    result = _run_find(RETENTION_FIND, tmp_path)
    assert result.returncode == 0, result.stderr
    names = {Path(line).name for line in result.stdout.splitlines() if line}
    assert names == {old_enc.name, old_plain.name}


def test_offhost_encrypted_retention_parses(tmp_path: Path) -> None:
    old = tmp_path / "state-20260101-0000.tar.gz.enc"
    fresh = tmp_path / "state-20260821-1847.tar.gz.enc"
    _touch_old(old)
    fresh.write_text("new", encoding="utf-8")
    result = _run_find(OFFHOST_ENC_RETENTION_FIND, tmp_path)
    assert result.returncode == 0, result.stderr
    names = {Path(line).name for line in result.stdout.splitlines() if line}
    assert names == {old.name}


def test_fixed_script_snippet_executes_without_find_syntax_error(tmp_path: Path) -> None:
    (tmp_path / "unknown.db").write_text("x", encoding="utf-8")
    result = _run_find(SQLITE_LIKE_FIND, tmp_path)
    assert result.returncode == 0, result.stderr
    assert "invalid expression" not in result.stderr
    assert Path(result.stdout.strip()).name == "unknown.db"
