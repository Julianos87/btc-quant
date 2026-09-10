"""Read-only operational health observations from an existing state database."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .readonly_state_db import open_state_db_readonly


class OperationalStateReader:
    """Read incidents, engine freshness and database integrity without writes."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
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
        with open_state_db_readonly(self.path) as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def engine_age_seconds(
        self,
        engine: str,
        *,
        now: datetime | None = None,
    ) -> float | None:
        with open_state_db_readonly(self.path) as connection:
            row = connection.execute(
                "SELECT updated_at FROM engine_state WHERE engine = ?", (engine,)
            ).fetchone()
        if row is None:
            return None
        updated = datetime.fromisoformat(row["updated_at"])
        return ((now or datetime.now(UTC)) - updated).total_seconds()

    def engine_updated_at(self, engine: str) -> datetime | None:
        """Return the persisted engine timestamp without filesystem inference."""

        with open_state_db_readonly(self.path) as connection:
            row = connection.execute(
                "SELECT updated_at FROM engine_state WHERE engine = ?", (engine,)
            ).fetchone()
        if row is None:
            return None
        parsed = datetime.fromisoformat(str(row["updated_at"]))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)

    def integrity_check(self) -> bool:
        with open_state_db_readonly(self.path) as connection:
            row = connection.execute("PRAGMA integrity_check").fetchone()
        return bool(row and row[0] == "ok")
