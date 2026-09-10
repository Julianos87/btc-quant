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

    def read_equity(self, engine: str) -> list[dict[str, Any]]:
        with open_state_db_readonly(self.path) as connection:
            rows = connection.execute(
                """
                SELECT ts, equity FROM equity_samples
                WHERE engine = ? ORDER BY ts
                """,
                (engine,),
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
