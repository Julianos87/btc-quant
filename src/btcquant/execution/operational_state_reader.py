"""Read-only operational health observations from an existing state database."""

from __future__ import annotations

import json
import math
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

    def engine_progress(
        self,
        engine: str,
        *,
        now: datetime | None = None,
        slot_timeframes: dict[str, int] | None = None,
        poll_buffer_seconds: float = 20.0,
    ) -> dict[str, Any]:
        """Separate heartbeat from completed strategy bars (timestamps are bar opens).

        No market observation is inferred from wall-clock time: the expected
        checkpoint is a deadline, not proof that a candle was received.
        """
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        with open_state_db_readonly(self.path, timeout_seconds=self.timeout_seconds) as connection:
            row = connection.execute(
                "SELECT payload FROM engine_state WHERE engine = ?", (engine,)
            ).fetchone()
            decision = connection.execute(
                "SELECT ts FROM events WHERE engine = ? AND event_type LIKE '%decision%' "
                "ORDER BY id DESC LIMIT 1",
                (engine,),
            ).fetchone()
        state = json.loads(row["payload"]) if row else {}
        persisted_slots = state.get("slots") or {}
        configured = (
            slot_timeframes
            if slot_timeframes is not None
            else {name: 14400 for name in persisted_slots}
        )
        slots = []
        for name, seconds in configured.items():
            raw = (persisted_slots.get(name) or {}).get("last_bar_ts")
            expected = (
                math.floor((current.timestamp() - poll_buffer_seconds) / seconds) - 1
            ) * seconds
            try:
                parsed = datetime.fromisoformat(str(raw))
                if parsed.tzinfo is None:
                    raise ValueError("checkpoint timezone missing")
                timestamp = parsed.timestamp()
                # A checkpoint beyond the latest bar eligible after the poll
                # buffer may belong to an unfinished candle or a bad clock.
                latest_closed = (math.floor(current.timestamp() / seconds) - 1) * seconds
                if timestamp > min(latest_closed, expected):
                    raise ValueError("checkpoint beyond eligible closed bar")
                lag = expected - timestamp
                status = "STALE" if lag > 0 else "FRESH"
            except (ValueError, TypeError, OverflowError):
                lag, status = None, "UNKNOWN"
            slots.append(
                {
                    "name": name,
                    "status": status,
                    "last_bar_ts": raw,
                    "expected_last_bar_ts": datetime.fromtimestamp(expected, UTC).isoformat(),
                    "lag_seconds": lag,
                    "timeframe_seconds": seconds,
                }
            )
        status = (
            "STALE"
            if any(slot["status"] == "STALE" for slot in slots)
            else "UNKNOWN"
            if not slots or any(slot["status"] == "UNKNOWN" for slot in slots)
            else "FRESH"
        )
        return {
            "status": status,
            "slots": slots,
            "reason_codes": [] if status == "FRESH" else [f"{engine.upper()}_PROGRESSION_{status}"],
            "heartbeat_age_seconds": self.engine_age_seconds(engine, now=current),
            "poll_buffer_seconds": poll_buffer_seconds,
            "last_available_bar_ts": None,
            "last_decision_at": decision["ts"] if decision else None,
        }

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
