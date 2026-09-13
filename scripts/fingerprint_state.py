"""Build a deterministic, non-secret fingerprint of a temporary SQLite snapshot.

This tool is deliberately a snapshot tool, not a production operator command.
It opens an explicitly supplied database read-only, verifies the database
before hashing, and emits counts and digests only. Row contents are never
printed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
from pathlib import Path
from typing import Any

SNAPSHOT_ROOTS = (Path("/tmp").resolve(), Path("/home/ubuntu/worktrees").resolve())
FINGERPRINT_TABLES = (
    "orders",
    "external_fills",
    "financial_fill_applications",
    "trades",
    "engine_state",
    "incidents",
    "qualification_campaigns",
    "readiness_reports",
)


def _reject_production_path(database: Path) -> Path:
    candidate = database.expanduser().absolute()
    production_root = Path("/opt/btcquant")
    if candidate == production_root or production_root in candidate.parents:
        raise ValueError("production database paths are not accepted")
    resolved = candidate.resolve(strict=True)
    if resolved == production_root or production_root in resolved.parents:
        raise ValueError("production database paths are not accepted")
    if not any(resolved == root or root in resolved.parents for root in SNAPSHOT_ROOTS):
        raise ValueError("database must be under an approved temporary snapshot root")
    return resolved


def _canonical_value(value: Any) -> list[Any]:
    """Encode SQLite scalar types without depending on Python's repr()."""

    if value is None:
        return ["null", None]
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, int):
        return ["int", value]
    if isinstance(value, float):
        if math.isnan(value):
            encoded = "nan"
        elif math.isinf(value):
            encoded = "inf" if value > 0 else "-inf"
        else:
            encoded = value.hex()
        return ["float", encoded]
    if isinstance(value, str):
        return ["text", value]
    if isinstance(value, (bytes, bytearray, memoryview)):
        return ["blob", bytes(value).hex()]
    raise TypeError(f"unsupported SQLite value type: {type(value).__name__}")


def _canonical_rows(connection: sqlite3.Connection, table: str) -> tuple[int, str]:
    escaped_table = table.replace(chr(34), chr(34) * 2)
    rows = [
        json.dumps(
            [_canonical_value(value) for value in tuple(row)],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        for row in connection.execute(f'SELECT * FROM "{escaped_table}"')
    ]
    # Sort canonical row encodings rather than rowid. This supports WITHOUT
    # ROWID tables and makes the digest independent of physical insertion order.
    payload = "\n".join(sorted(rows)).encode("utf-8")
    return len(rows), hashlib.sha256(payload).hexdigest()


def fingerprint_database(database: str | Path) -> dict[str, Any]:
    """Return non-secret integrity, schema, counts, and row digests."""

    path = _reject_production_path(Path(database))
    uri = f"{path.as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=15.0) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
            raise RuntimeError("snapshot connection is not read-only")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        connection.execute("BEGIN")
        try:
            schema_row = connection.execute(
                "SELECT value FROM metadata WHERE key = 'schema_version'"
            ).fetchone()
            schema_version = int(schema_row[0]) if schema_row is not None else None
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
            if integrity != "ok":
                raise RuntimeError(f"snapshot integrity check failed: {integrity}")
            if foreign_keys:
                raise RuntimeError("snapshot foreign-key check failed")

            available = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            missing = [table for table in FINGERPRINT_TABLES if table not in available]
            if missing:
                raise RuntimeError(f"snapshot is missing required tables: {','.join(missing)}")
            tables = {
                table: {
                    "count": count,
                    "sha256": digest,
                }
                for table in FINGERPRINT_TABLES
                for count, digest in [_canonical_rows(connection, table)]
            }
            return {
                "schema_version": schema_version,
                "integrity": integrity,
                "foreign_key_errors": foreign_keys,
                "tables": tables,
            }
        finally:
            connection.rollback()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    args = parser.parse_args()
    print(json.dumps(fingerprint_database(args.database), sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
