"""SQLite persistence boundary for engine checkpoints and projections."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from typing import Any, Protocol

from .errors import ReconciliationRequired
from .financial_application_plan import sha256_json


class _CheckpointPayload(Protocol):
    def __call__(
        self,
        connection: sqlite3.Connection,
        engine: str,
        payload: Mapping[str, Any],
        *,
        allow_reconciliation_clear: bool = False,
    ) -> dict[str, Any]: ...


class _PositionProjection(Protocol):
    def __call__(
        self,
        connection: sqlite3.Connection,
        engine: str,
        payload: Mapping[str, Any],
        now: str,
    ) -> None: ...


class _EventWriter(Protocol):
    def __call__(
        self,
        connection: sqlite3.Connection,
        engine: str,
        event_type: str,
        payload: dict[str, Any],
        aggregate_type: str | None = None,
        aggregate_id: str | None = None,
        correlation_id: str | None = None,
        ts: str | None = None,
    ) -> int: ...


class _StateEvent(Protocol):
    def __call__(
        self,
        state: Mapping[str, Any],
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...


class EngineStateRepository:
    """Persist one engine checkpoint and its coupled projections atomically."""

    def __init__(
        self,
        *,
        connect: Callable[[], sqlite3.Connection],
        transaction: Callable[[], AbstractContextManager[sqlite3.Connection]],
        encode_json: Callable[[Any], str],
        checkpoint_payload: _CheckpointPayload,
        sync_positions: _PositionProjection,
        insert_event: _EventWriter,
        state_event: _StateEvent,
        now: Callable[[], str],
    ) -> None:
        self._connect_factory = connect
        self._transaction_factory = transaction
        self._encode_json = encode_json
        self._checkpoint_payload = checkpoint_payload
        self._sync_positions = sync_positions
        self._insert_event = insert_event
        self._state_event = state_event
        self._now = now

    def _connect(self) -> sqlite3.Connection:
        return self._connect_factory()

    def _transaction(self) -> AbstractContextManager[sqlite3.Connection]:
        return self._transaction_factory()

    def load(self, engine: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM engine_state WHERE engine = ?", (engine,)
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def save(
        self,
        engine: str,
        payload: Mapping[str, Any],
        *,
        event_type: str = "checkpoint",
        event_payload: dict[str, Any] | None = None,
        event_aggregate_type: str | None = None,
        event_aggregate_id: str | None = None,
    ) -> None:
        now = self._now()
        with self._transaction() as connection:
            self.save_in_transaction(
                connection,
                engine,
                payload,
                now=now,
                event_type=event_type,
                event_payload=event_payload,
                event_aggregate_type=event_aggregate_type,
                event_aggregate_id=event_aggregate_id,
            )

    def save_in_transaction(
        self,
        connection: sqlite3.Connection,
        engine: str,
        payload: Mapping[str, Any],
        *,
        now: str,
        event_type: str,
        event_payload: dict[str, Any] | None = None,
        event_aggregate_type: str | None = None,
        event_aggregate_id: str | None = None,
        allow_reconciliation_clear: bool = False,
    ) -> None:
        """Write state, positions, and event using the supplied connection."""

        checkpoint = self._checkpoint_payload(
            connection,
            engine,
            payload,
            allow_reconciliation_clear=allow_reconciliation_clear,
        )
        connection.execute(
            """
            INSERT INTO engine_state(engine, payload, updated_at) VALUES(?, ?, ?)
            ON CONFLICT(engine) DO UPDATE SET
                payload=excluded.payload, updated_at=excluded.updated_at
            """,
            (engine, self._encode_json(checkpoint), now),
        )
        self._sync_positions(connection, engine, checkpoint, now)
        self._insert_event(
            connection,
            engine,
            event_type,
            self._state_event(checkpoint, event_payload),
            aggregate_type=event_aggregate_type or "engine",
            aggregate_id=event_aggregate_id or engine,
        )

    def save_after_reconciliation(
        self,
        engine: str,
        payload: Mapping[str, Any],
        *,
        expected_state_sha256: str,
        resolution: str,
        event_payload: dict[str, Any] | None = None,
    ) -> None:
        """Clear a durable reconciliation latch only after hash validation."""

        if not isinstance(expected_state_sha256, str) or len(expected_state_sha256) != 64:
            raise ValueError("expected_state_sha256 doit être un SHA-256 hexadécimal")
        if any(character not in "0123456789abcdef" for character in expected_state_sha256):
            raise ValueError("expected_state_sha256 doit être un SHA-256 hexadécimal")
        if not isinstance(resolution, str) or not resolution.strip():
            raise ValueError("resolution doit être non vide")
        candidate = json.loads(self._encode_json(payload))
        if not isinstance(candidate, dict):
            raise ValueError("État engine invalide : objet JSON attendu")
        if candidate.get("reconciliation_required") is not False:
            raise ValueError("La résolution qualifiée doit produire reconciliation_required=false")

        now = self._now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT payload FROM engine_state WHERE engine = ?", (engine,)
            ).fetchone()
            if row is None:
                raise ReconciliationRequired(f"Moteur {engine} absent : résolution non qualifiable")
            current = json.loads(row["payload"])
            if not isinstance(current, dict):
                raise ValueError("État engine durable invalide : objet JSON attendu")
            if current.get("reconciliation_required") is not True:
                raise ReconciliationRequired(
                    f"Moteur {engine} sans verrou reconciliation_required à résoudre"
                )
            if sha256_json(current) != expected_state_sha256:
                raise ReconciliationRequired(
                    f"Moteur {engine} modifié depuis la preuve de réconciliation"
                )
            self.save_in_transaction(
                connection,
                engine,
                candidate,
                now=now,
                event_type="reconciliation_resolved",
                event_payload={
                    **(event_payload or {}),
                    "resolution": resolution.strip(),
                    "expected_state_sha256": expected_state_sha256,
                },
                allow_reconciliation_clear=True,
            )
