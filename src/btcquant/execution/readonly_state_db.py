"""Minimal read-only SQLite connection support for state readers."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def open_state_db_readonly(
    path: Path, *, timeout_seconds: float = 15.0
) -> Iterator[sqlite3.Connection]:
    """Open an existing state database without permitting writes."""

    if timeout_seconds < 0:
        raise ValueError("timeout_seconds doit être positif ou nul")
    uri = f"{path.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=timeout_seconds)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {int(timeout_seconds * 1000)}")
        yield connection
    finally:
        connection.close()
