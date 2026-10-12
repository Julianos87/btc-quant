"""Minimal read-only SQLite connection support for state readers."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def sqlite_error_metadata(error: BaseException) -> dict[str, int | str]:
    """Expose SQLite's stable error class/code without its message or SQL text."""

    if not isinstance(error, sqlite3.Error):
        return {}
    metadata: dict[str, int | str] = {}
    code = getattr(error, "sqlite_errorcode", None)
    name = getattr(error, "sqlite_errorname", None)
    if isinstance(code, int) and not isinstance(code, bool):
        metadata["sqlite_errorcode"] = code
    if isinstance(name, str) and name.startswith("SQLITE_") and name.isascii():
        metadata["sqlite_errorname"] = name
    return metadata


@contextmanager
def open_state_db_readonly(path: Path) -> Iterator[sqlite3.Connection]:
    """Open an existing state database without permitting writes."""

    uri = f"{path.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=15.0)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        yield connection
    finally:
        connection.close()
