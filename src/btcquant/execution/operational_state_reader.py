"""Read-only operational health observations from an existing state database."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .readonly_state_db import open_state_db_readonly


class OperationalStateReader:
    """Read operational health from an existing database without writes."""

    def __init__(self, path: str | Path, *, timeout_seconds: float = 15.0) -> None:
        self.path = Path(path)
        self.timeout_seconds = timeout_seconds
        if not self.path.exists():
            raise FileNotFoundError(self.path)

    def read_incidents(
        self,
        *,
        open_only: bool = False,
        engine: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[str] = []
        if open_only:
            clauses.append("status = 'OPEN'")
        if engine is not None:
            clauses.append("engine = ?")
            params.append(engine)
        query = "SELECT * FROM incidents"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY last_seen DESC, id DESC"
        with open_state_db_readonly(self.path, timeout_seconds=self.timeout_seconds) as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def engine_age_seconds(
        self,
        engine: str,
        *,
        now: datetime | None = None,
    ) -> float | None:
        with open_state_db_readonly(self.path, timeout_seconds=self.timeout_seconds) as connection:
            row = connection.execute(
                "SELECT updated_at FROM engine_state WHERE engine = ?", (engine,)
            ).fetchone()
        if row is None:
            return None
        updated = datetime.fromisoformat(row["updated_at"])
        return ((now or datetime.now(UTC)) - updated).total_seconds()

    def engine_updated_at(self, engine: str) -> datetime | None:
        """Return the persisted engine timestamp without filesystem inference."""

        with open_state_db_readonly(self.path, timeout_seconds=self.timeout_seconds) as connection:
            row = connection.execute(
                "SELECT updated_at FROM engine_state WHERE engine = ?", (engine,)
            ).fetchone()
        if row is None:
            return None
        parsed = datetime.fromisoformat(str(row["updated_at"]))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)

    def integrity_check(self) -> bool:
        """Run the deep SQLite integrity scan for backup/qualification workflows."""

        with open_state_db_readonly(self.path, timeout_seconds=self.timeout_seconds) as connection:
            row = connection.execute("PRAGMA integrity_check").fetchone()
        return bool(row and row[0] == "ok")

    def database_probe(self, *, expected_schema_version: int | None = None) -> dict[str, Any]:
        """Confirm bounded read access and required schema; does not prove integrity."""

        required_tables = {"metadata", "engine_state", "orders", "incidents"}
        with open_state_db_readonly(self.path, timeout_seconds=self.timeout_seconds) as connection:
            connection.execute("SELECT 1").fetchone()
            schema_row = connection.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()
            schema_version = int(schema_row[0]) if schema_row is not None else None
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name IN ('metadata','engine_state','orders','incidents')"
                )
            }
            connection.execute(
                "SELECT engine, updated_at FROM engine_state WHERE engine IN ('trend','carry')"
            ).fetchall()
        schema_matches = (
            expected_schema_version is None or schema_version == expected_schema_version
        )
        return {
            "accessible": True,
            "schema_version": schema_version,
            "expected_schema_version": expected_schema_version,
            "schema_matches": schema_matches,
            "required_tables_present": required_tables.issubset(tables),
            "integrity_check": "NOT_RUN",
        }
