"""SQLite persistence boundary for operational incidents."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
import sqlite3
from typing import Any


class IncidentRepository:
    """Persist incident lifecycle changes without owning incident policy."""

    def __init__(
        self,
        *,
        transaction: Callable[[], AbstractContextManager[sqlite3.Connection]],
        encode_json: Callable[[Any], str],
        now: Callable[[], str],
    ) -> None:
        self._transaction_factory = transaction
        self._encode_json = encode_json
        self._now = now

    def _transaction(self) -> AbstractContextManager[sqlite3.Connection]:
        return self._transaction_factory()

    def record_incident(
        self,
        fingerprint: str,
        *,
        severity: str,
        kind: str,
        message: str,
        engine: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create, reopen, or update one incident identity atomically."""

        if severity not in ("WARNING", "CRITICAL"):
            raise ValueError("severity doit valoir WARNING ou CRITICAL")
        now = self._now()
        with self._transaction() as connection:
            previous = connection.execute(
                "SELECT status FROM incidents WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO incidents(
                    fingerprint, engine, severity, kind, message, context,
                    status, occurrences, first_seen, last_seen
                ) VALUES(?, ?, ?, ?, ?, ?, 'OPEN', 1, ?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    engine=excluded.engine,
                    severity=excluded.severity,
                    kind=excluded.kind,
                    message=excluded.message,
                    context=excluded.context,
                    status='OPEN',
                    occurrences=incidents.occurrences + 1,
                    last_seen=excluded.last_seen,
                    resolved_at=NULL
                """,
                (
                    fingerprint,
                    engine,
                    severity,
                    kind,
                    message,
                    self._encode_json(context or {}),
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM incidents WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchone()
        assert row is not None
        result = dict(row)
        result["is_new_or_reopened"] = previous is None or previous["status"] != "OPEN"
        return result

    def resolve_incident(self, fingerprint: str) -> bool:
        """Resolve an open incident and report whether a row changed."""

        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE incidents
                SET status='RESOLVED', resolved_at=?
                WHERE fingerprint=? AND status='OPEN'
                """,
                (self._now(), fingerprint),
            )
        return cursor.rowcount > 0
