"""Read-only access to historical observations in an existing state database."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .readonly_state_db import open_state_db_readonly


class HistoricalStateReader:
    """Read equity, trades and capital flows without owning schema or writes."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(self.path)

    def read_equity(
        self,
        engine: str,
        *,
        start: str | None = None,
        end: str | None = None,
    ) -> list[dict[str, Any]]:
        """Read equity samples, optionally bounded to an inclusive UTC window.

        ``equity_samples`` stores canonical ISO timestamps and has an
        ``(engine, ts)`` index. Keeping the range in SQL is important for
        readiness checks: they need the current campaign window, not the
        complete lifetime history on every dashboard poll.
        """

        clauses = ["engine = ?"]
        params: list[str] = [engine]
        if start is not None:
            clauses.append("ts >= ?")
            params.append(start)
        if end is not None:
            clauses.append("ts <= ?")
            params.append(end)
        with open_state_db_readonly(self.path) as connection:
            rows = connection.execute(
                "SELECT ts, equity FROM equity_samples WHERE "
                + " AND ".join(clauses)
                + " ORDER BY ts",
                tuple(params),
            ).fetchall()
        return [dict(row) for row in rows]

    def read_trades(self) -> list[dict[str, Any]]:
        with open_state_db_readonly(self.path) as connection:
            rows = connection.execute("SELECT * FROM trades ORDER BY exit_ts").fetchall()
        return [dict(row) for row in rows]

    def read_flows(self) -> list[dict[str, Any]]:
        with open_state_db_readonly(self.path) as connection:
            rows = connection.execute("SELECT * FROM flows ORDER BY ts").fetchall()
        return [dict(row) for row in rows]
