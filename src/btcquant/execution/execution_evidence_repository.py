"""Durable persistence boundary for externally observed execution evidence.

This repository records facts about an attempted or observed external
execution.  It deliberately does not call a broker, decide retry policy,
apply financial effects, clear reconciliation latches, or create trades.
StateStore supplies the connection/transaction callbacks so evidence can be
persisted either in its own transaction or inside a larger caller-owned
financial transaction.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from .errors import ExternalFillConflict, ExternalObservationConflict, InvalidExternalObservation
from .external_evidence import ExternalFill, ExternalOrderObservation
from .external_submission_commitment import (
    ExternalSubmissionResponse,
    SubmissionCommitmentError,
    SUBMISSION_RESPONSE_AGGREGATE_TYPE,
    SUBMISSION_RESPONSE_EVENT_TYPE,
)
from .paper_execution_evidence import (
    PAPER_EVIDENCE_VERSION,
    PAPER_EXECUTION_EVIDENCE_AGGREGATE_TYPE,
    PAPER_EXECUTION_EVIDENCE_EVENT_TYPE,
    PaperExecutionEvidence,
    PaperExecutionEvidencePersistenceResult,
)
from .paper_zero_effect import (
    PAPER_ZERO_EFFECT_AGGREGATE_TYPE,
    PAPER_ZERO_EFFECT_EVENT_TYPE,
    PAPER_ZERO_EFFECT_EVIDENCE_VERSION,
    PaperZeroEffectStatus,
    decide_paper_zero_effect,
)
from .order_state import ExternalOrderState


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


class ExecutionEvidenceRepository:
    """Persist external execution facts without owning execution policy."""

    def __init__(
        self,
        *,
        connect: Callable[[], sqlite3.Connection],
        transaction: Callable[[], AbstractContextManager[sqlite3.Connection]],
        read_transaction: Callable[[], AbstractContextManager[sqlite3.Connection]],
        insert_event: Callable[..., int],
        now: Callable[[], str],
        is_read_only: Callable[[], bool],
        observation_from_row: Callable[[sqlite3.Row], ExternalOrderObservation] | None = None,
        fill_from_row: Callable[[sqlite3.Row], ExternalFill] | None = None,
    ) -> None:
        self._connect_factory = connect
        self._transaction_factory = transaction
        self._read_transaction_factory = read_transaction
        self._insert_event = insert_event
        self._now = now
        self._is_read_only = is_read_only
        self._observation_from_row = (
            observation_from_row or self.external_order_observation_from_row
        )
        self._fill_from_row = fill_from_row or self.external_fill_from_row

    def _transaction(self) -> AbstractContextManager[sqlite3.Connection]:
        return self._transaction_factory()

    def _read_transaction(self) -> AbstractContextManager[sqlite3.Connection]:
        return self._read_transaction_factory()

    def _assert_writable(self, message: str) -> None:
        if self._is_read_only():
            raise RuntimeError(message)

    @staticmethod
    def external_order_observation_from_row(row: sqlite3.Row) -> ExternalOrderObservation:
        return ExternalOrderObservation(
            local_order_id=int(row["local_order_id"]),
            intent_id=str(row["intent_id"]),
            venue=str(row["venue"]),
            account_scope=str(row["account_scope"]),
            instrument=str(row["instrument"]),
            side=str(row["side"]),
            source_kind=str(row["source_kind"]),
            normalized_external_status=str(row["external_state"]),
            requested_qty=float(row["requested_qty"]),
            cumulative_filled_qty=(
                float(row["cumulative_filled_qty"])
                if row["cumulative_filled_qty"] is not None
                else None
            ),
            remaining_qty=(
                float(row["remaining_qty"]) if row["remaining_qty"] is not None else None
            ),
            client_order_id=row["client_order_id"],
            external_order_id=row["external_order_id"],
            venue_event_at=row["venue_event_at"],
            status_event_at=row["status_event_at"],
            observed_at=str(row["observed_at"]),
            persisted_at=str(row["persisted_at"]),
            observation_key=str(row["observation_key"]),
            raw_payload_hash=str(row["raw_payload_hash"]),
        )

    @staticmethod
    def external_fill_from_row(row: sqlite3.Row) -> ExternalFill:
        return ExternalFill(
            local_order_id=int(row["local_order_id"]),
            intent_id=str(row["intent_id"]),
            venue=str(row["venue"]),
            account_scope=str(row["account_scope"]),
            instrument=str(row["instrument"]),
            side=str(row["side"]),
            source_kind=str(row["source_kind"]),
            client_order_id=row["client_order_id"],
            external_order_id=row["external_order_id"],
            venue_fill_id=row["venue_fill_id"],
            quantity=float(row["quantity"]),
            price=float(row["price"]),
            fee=float(row["fee"]) if row["fee"] is not None else None,
            fee_asset=row["fee_asset"],
            venue_event_at=row["venue_event_at"],
            observed_at=str(row["observed_at"]),
            persisted_at=str(row["persisted_at"]),
            fill_key=str(row["fill_key"]),
            raw_payload_hash=str(row["raw_payload_hash"]),
        )

    @staticmethod
    def _assert_evidence_attribution(
        connection: sqlite3.Connection,
        *,
        local_order_id: int,
        intent_id: str,
    ) -> None:
        row = connection.execute(
            "SELECT intent_id FROM orders WHERE id = ?", (local_order_id,)
        ).fetchone()
        if row is None:
            raise InvalidExternalObservation(
                f"Preuve externe refusée : ordre local {local_order_id} introuvable"
            )
        if str(row["intent_id"]) != intent_id:
            raise InvalidExternalObservation(
                f"Preuve externe refusée : intent_id incohérent pour l'ordre {local_order_id}"
            )

    def append_external_order_lookup_attempt(
        self,
        *,
        engine: str,
        aggregate_id: str,
        payload: dict[str, Any],
        event_type: str,
    ) -> None:
        """Journal one lookup attempt without changing business state."""

        self._assert_writable("Un StateStore read-only ne peut pas journaliser une tentative")
        normalized_engine = str(engine).strip()
        normalized_aggregate_id = str(aggregate_id).strip()
        if not normalized_engine or not normalized_aggregate_id:
            raise ValueError("engine et aggregate_id doivent être non vides")
        with self._transaction() as connection:
            self.append_external_order_lookup_attempt_in_transaction(
                connection,
                engine=normalized_engine,
                aggregate_id=normalized_aggregate_id,
                payload=payload,
                event_type=event_type,
            )

    def append_external_order_lookup_attempt_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        engine: str,
        aggregate_id: str,
        payload: dict[str, Any],
        event_type: str,
    ) -> None:
        self._insert_event(
            connection,
            engine,
            event_type,
            payload,
            aggregate_type="external_order_lookup",
            aggregate_id=aggregate_id,
        )

    def append_external_submission_response(
        self,
        response: ExternalSubmissionResponse,
        *,
        engine: str,
    ) -> tuple[ExternalSubmissionResponse, bool]:
        """Durably append one submission response with conflict detection."""

        self._assert_writable("Un StateStore read-only ne peut pas persister une réponse")
        if not isinstance(response, ExternalSubmissionResponse):
            raise TypeError("response must be ExternalSubmissionResponse")
        normalized_engine = str(engine).strip()
        if not normalized_engine:
            raise ValueError("engine doit être non vide")
        payload = response.to_payload()
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT engine, aggregate_id, payload FROM events "
                "WHERE event_type = ? AND correlation_id = ? ORDER BY id",
                (SUBMISSION_RESPONSE_EVENT_TYPE, response.intent_id),
            ).fetchall()
            if rows:
                for row in rows:
                    if row["engine"] != normalized_engine or row["aggregate_id"] != str(
                        response.local_order_id
                    ):
                        raise SubmissionCommitmentError(
                            "EXTERNAL_SUBMISSION_RESPONSE_BINDING_CONFLICT"
                        )
                    try:
                        existing_payload = json.loads(str(row["payload"]))
                    except json.JSONDecodeError as error:
                        raise SubmissionCommitmentError(
                            "corrupt external submission response journal"
                        ) from error
                    if not isinstance(existing_payload, dict):
                        raise SubmissionCommitmentError(
                            "external submission response journal is not an object"
                        )
                    if existing_payload.get("submission_key") != response.submission_key:
                        raise SubmissionCommitmentError("EXTERNAL_SUBMISSION_RESPONSE_KEY_CONFLICT")
                    existing_compare = json.loads(_canonical_json(existing_payload))
                    current_compare = json.loads(_canonical_json(payload))
                    existing_compare.pop("response_acquired_at", None)
                    current_compare.pop("response_acquired_at", None)
                    for candidate in (existing_compare, current_compare):
                        commitment_payload = candidate.get("commitment")
                        if isinstance(commitment_payload, dict):
                            commitment_payload.pop("response_acquired_at", None)
                    if _canonical_json(existing_compare) != _canonical_json(current_compare):
                        raise SubmissionCommitmentError("EXTERNAL_SUBMISSION_RESPONSE_CONFLICT")
                return response, False
            self._insert_event(
                connection,
                normalized_engine,
                SUBMISSION_RESPONSE_EVENT_TYPE,
                payload,
                aggregate_type=SUBMISSION_RESPONSE_AGGREGATE_TYPE,
                aggregate_id=str(response.local_order_id),
                correlation_id=response.intent_id,
                ts=response.response_acquired_at,
            )
        return response, True

    def read_external_submission_responses(
        self,
        intent_id: str | None = None,
        *,
        engine: str | None = None,
    ) -> list[ExternalSubmissionResponse]:
        """Read and validate durable submission response envelopes."""

        conditions = ["event_type = ?"]
        params: list[Any] = [SUBMISSION_RESPONSE_EVENT_TYPE]
        if intent_id is not None:
            conditions.append("correlation_id = ?")
            params.append(intent_id)
        if engine is not None:
            conditions.append("engine = ?")
            params.append(engine)
        query = (
            "SELECT aggregate_type, payload FROM events WHERE "
            + " AND ".join(conditions)
            + " ORDER BY id"
        )
        with self._read_transaction() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        responses: list[ExternalSubmissionResponse] = []
        for row in rows:
            if row["aggregate_type"] != SUBMISSION_RESPONSE_AGGREGATE_TYPE:
                raise SubmissionCommitmentError("EXTERNAL_SUBMISSION_RESPONSE_PROVENANCE_CONFLICT")
            try:
                payload = json.loads(str(row["payload"]))
            except json.JSONDecodeError as error:
                raise SubmissionCommitmentError(
                    "corrupt external submission response journal"
                ) from error
            if not isinstance(payload, dict):
                raise SubmissionCommitmentError(
                    "external submission response payload is not an object"
                )
            responses.append(ExternalSubmissionResponse.from_payload(payload))
        return responses

    def append_external_order_observation(
        self,
        observation: ExternalOrderObservation,
    ) -> tuple[ExternalOrderObservation, bool]:
        """Append one immutable external order observation."""

        persisted = (
            observation
            if observation.persisted_at is not None
            else observation.with_persisted_at(self._now())
        )
        with self._transaction() as connection:
            return self.append_external_order_observation_in_transaction(connection, persisted)

    def append_external_order_observation_in_transaction(
        self,
        connection: sqlite3.Connection,
        observation: ExternalOrderObservation,
    ) -> tuple[ExternalOrderObservation, bool]:
        self._assert_evidence_attribution(
            connection,
            local_order_id=observation.local_order_id,
            intent_id=observation.intent_id,
        )
        existing_row = connection.execute(
            "SELECT * FROM external_order_observations WHERE observation_key = ?",
            (observation.observation_key,),
        ).fetchone()
        if existing_row is not None:
            existing = self._observation_from_row(existing_row)
            if existing.semantic_content() != observation.semantic_content():
                raise ExternalObservationConflict(
                    f"Observation externe conflictuelle pour {observation.observation_key}"
                )
            return existing, False
        connection.execute(
            """
            INSERT INTO external_order_observations(
                local_order_id, intent_id, venue, account_scope, instrument, side,
                source_kind, external_state, client_order_id, external_order_id,
                requested_qty, cumulative_filled_qty, remaining_qty, venue_event_at,
                status_event_at, observed_at, persisted_at, observation_key, raw_payload_hash
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                observation.local_order_id,
                observation.intent_id,
                observation.venue,
                observation.account_scope,
                observation.instrument,
                observation.side,
                str(observation.source_kind),
                str(observation.normalized_external_status),
                observation.client_order_id,
                observation.external_order_id,
                observation.requested_qty,
                observation.cumulative_filled_qty,
                observation.remaining_qty,
                observation.venue_event_at,
                observation.status_event_at,
                observation.observed_at,
                observation.persisted_at,
                observation.observation_key,
                observation.raw_payload_hash,
            ),
        )
        return observation, True

    def persist_external_order_lookup_evidence(
        self,
        *,
        observation: ExternalOrderObservation | None,
        engine: str,
        aggregate_id: str,
        payload: dict[str, Any],
        event_type: str,
    ) -> tuple[ExternalOrderObservation | None, bool]:
        """Persist an optional observation and lookup attempt atomically."""

        self._assert_writable("Un StateStore read-only ne peut pas persister une preuve")
        normalized_engine = str(engine).strip()
        normalized_aggregate_id = str(aggregate_id).strip()
        if not normalized_engine or not normalized_aggregate_id:
            raise ValueError("engine et aggregate_id doivent être non vides")
        persisted = (
            None
            if observation is None
            else observation
            if observation.persisted_at is not None
            else observation.with_persisted_at(self._now())
        )
        with self._transaction() as connection:
            persisted_observation: ExternalOrderObservation | None = None
            observation_created = False
            if persisted is not None:
                persisted_observation, observation_created = (
                    self.append_external_order_observation_in_transaction(connection, persisted)
                )
            self.append_external_order_lookup_attempt_in_transaction(
                connection,
                engine=normalized_engine,
                aggregate_id=normalized_aggregate_id,
                payload=payload,
                event_type=event_type,
            )
        return persisted_observation, observation_created

    def append_external_fill(self, fill: ExternalFill) -> tuple[ExternalFill, bool]:
        """Append one immutable external fill with identity conflict checks."""

        persisted = fill if fill.persisted_at is not None else fill.with_persisted_at(self._now())
        with self._transaction() as connection:
            return self.append_external_fill_in_transaction(connection, persisted)

    def append_external_fill_in_transaction(
        self,
        connection: sqlite3.Connection,
        fill: ExternalFill,
    ) -> tuple[ExternalFill, bool]:
        self._assert_evidence_attribution(
            connection,
            local_order_id=fill.local_order_id,
            intent_id=fill.intent_id,
        )
        existing_row = connection.execute(
            "SELECT * FROM external_fills WHERE fill_key = ?", (fill.fill_key,)
        ).fetchone()
        if existing_row is not None:
            existing = self._fill_from_row(existing_row)
            if not existing.is_semantically_compatible_with(fill):
                raise ExternalFillConflict(f"Fill externe conflictuel pour {fill.fill_key}")
            return existing, False
        connection.execute(
            """
            INSERT INTO external_fills(
                local_order_id, intent_id, venue, account_scope, instrument, side,
                source_kind, client_order_id, external_order_id, venue_fill_id,
                quantity, price, fee, fee_asset, venue_event_at, observed_at,
                persisted_at, fill_key, raw_payload_hash
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fill.local_order_id,
                fill.intent_id,
                fill.venue,
                fill.account_scope,
                fill.instrument,
                fill.side,
                str(fill.source_kind),
                fill.client_order_id,
                fill.external_order_id,
                fill.venue_fill_id,
                fill.quantity,
                fill.price,
                fill.fee,
                fill.fee_asset,
                fill.venue_event_at,
                fill.observed_at,
                fill.persisted_at,
                fill.fill_key,
                fill.raw_payload_hash,
            ),
        )
        return fill, True

    def append_external_fill_lookup_attempt_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        engine: str,
        aggregate_id: str,
        payload: dict[str, Any],
        event_type: str,
    ) -> None:
        self._insert_event(
            connection,
            engine,
            event_type,
            payload,
            aggregate_type="external_fill_lookup",
            aggregate_id=aggregate_id,
        )

    def persist_external_fill_lookup_evidence(
        self,
        *,
        fills: Sequence[ExternalFill],
        engine: str,
        aggregate_id: str,
        payload: dict[str, Any],
        event_type: str,
    ) -> tuple[tuple[ExternalFill, ...], tuple[bool, ...]]:
        """Persist zero or more fills and one lookup attempt atomically."""

        self._assert_writable("Un StateStore read-only ne peut pas persister des fills")
        normalized_engine = str(engine).strip()
        normalized_aggregate_id = str(aggregate_id).strip()
        if not normalized_engine or not normalized_aggregate_id:
            raise ValueError("engine et aggregate_id doivent être non vides")
        persisted_fills = tuple(
            fill if fill.persisted_at is not None else fill.with_persisted_at(self._now())
            for fill in fills
        )
        persisted: list[ExternalFill] = []
        created: list[bool] = []
        with self._transaction() as connection:
            for fill in persisted_fills:
                persisted_fill, fill_created = self.append_external_fill_in_transaction(
                    connection, fill
                )
                persisted.append(persisted_fill)
                created.append(fill_created)
            self.append_external_fill_lookup_attempt_in_transaction(
                connection,
                engine=normalized_engine,
                aggregate_id=normalized_aggregate_id,
                payload=payload,
                event_type=event_type,
            )
        return tuple(persisted), tuple(created)

    def persist_paper_execution_evidence(
        self,
        evidence: PaperExecutionEvidence,
    ) -> PaperExecutionEvidencePersistenceResult:
        """Persist PAPER observation, optional fill, and zero-effect evidence."""

        self._assert_writable("Un StateStore read-only ne peut pas persister une preuve PAPER")
        if not isinstance(evidence, PaperExecutionEvidence):
            raise TypeError("evidence must be PaperExecutionEvidence")
        persisted_at = self._now()
        observation = (
            evidence.observation
            if evidence.observation.persisted_at is not None
            else evidence.observation.with_persisted_at(persisted_at)
        )
        fill = (
            None
            if evidence.fill is None
            else evidence.fill
            if evidence.fill.persisted_at is not None
            else evidence.fill.with_persisted_at(persisted_at)
        )
        with self._transaction() as connection:
            persisted_observation, observation_created = (
                self.append_external_order_observation_in_transaction(connection, observation)
            )
            persisted_fill: ExternalFill | None = None
            fill_created = False
            if fill is not None:
                persisted_fill, fill_created = self.append_external_fill_in_transaction(
                    connection, fill
                )
            zero_effect = decide_paper_zero_effect(
                external_execution=False,
                evidence_persisted=True,
                external_state=persisted_observation.normalized_external_status,
                filled_qty=persisted_observation.cumulative_filled_qty,
                remaining_qty=evidence.reported_remaining_qty,
                individual_fill_present=persisted_fill is not None,
            )
            self._insert_event(
                connection,
                evidence.context.engine,
                PAPER_EXECUTION_EVIDENCE_EVENT_TYPE,
                {
                    "contract": PAPER_EVIDENCE_VERSION,
                    "local_order_id": evidence.context.local_order_id,
                    "intent_id": evidence.context.intent_id,
                    "venue": evidence.observation.venue,
                    "account_scope": evidence.observation.account_scope,
                    "instrument": evidence.observation.instrument,
                    "side": evidence.observation.side,
                    "observation_key": persisted_observation.observation_key,
                    "fill_key": None if persisted_fill is None else persisted_fill.fill_key,
                    "venue_fill_id": (
                        None if persisted_fill is None else persisted_fill.venue_fill_id
                    ),
                    "raw_payload_hash": evidence.raw_payload_hash,
                },
                aggregate_type=PAPER_EXECUTION_EVIDENCE_AGGREGATE_TYPE,
                aggregate_id=str(evidence.context.local_order_id),
                correlation_id=evidence.context.intent_id,
            )
            if zero_effect.status == PaperZeroEffectStatus.PROVEN:
                self._insert_event(
                    connection,
                    evidence.context.engine,
                    PAPER_ZERO_EFFECT_EVENT_TYPE,
                    {
                        "contract": PAPER_ZERO_EFFECT_EVIDENCE_VERSION,
                        "local_order_id": evidence.context.local_order_id,
                        "intent_id": evidence.context.intent_id,
                        "observation_key": persisted_observation.observation_key,
                        "raw_payload_hash": evidence.raw_payload_hash,
                        "external_state": ExternalOrderState(
                            persisted_observation.normalized_external_status
                        ).value,
                        "filled_qty": persisted_observation.cumulative_filled_qty,
                        "remaining_qty": evidence.reported_remaining_qty,
                        "reason": zero_effect.reason,
                    },
                    aggregate_type=PAPER_ZERO_EFFECT_AGGREGATE_TYPE,
                    aggregate_id=str(evidence.context.local_order_id),
                    correlation_id=evidence.context.intent_id,
                )
        return PaperExecutionEvidencePersistenceResult(
            observation=persisted_observation,
            observation_created=observation_created,
            fill=persisted_fill,
            fill_created=fill_created,
        )

    def read_resolution_snapshot_in_transaction(
        self, connection: sqlite3.Connection, local_order_id: int
    ) -> ResolutionSnapshot:
        order_row = connection.execute(
            "SELECT * FROM orders WHERE id = ?", (local_order_id,)
        ).fetchone()
        if order_row is None:
            return ResolutionSnapshot(None, (), (), ())
        order = MappingProxyType(dict(order_row))
        observations = tuple(
            self._observation_from_row(row)
            for row in connection.execute(
                """
                SELECT * FROM external_order_observations
                WHERE local_order_id = ? ORDER BY id
                """,
                (local_order_id,),
            ).fetchall()
        )
        fills = tuple(
            self._fill_from_row(row)
            for row in connection.execute(
                "SELECT * FROM external_fills WHERE local_order_id = ? ORDER BY id",
                (local_order_id,),
            ).fetchall()
        )
        events = tuple(
            PersistedLookupEvent(
                event_id=int(row["id"]),
                ts=str(row["ts"]),
                engine=str(row["engine"]),
                event_type=str(row["event_type"]),
                aggregate_type=str(row["aggregate_type"]),
                aggregate_id=str(row["aggregate_id"]),
                payload=str(row["payload"]),
            )
            for row in connection.execute(
                """
                SELECT id, ts, engine, event_type, aggregate_type, aggregate_id, payload
                FROM events
                WHERE engine = ?
                  AND (
                    (
                      aggregate_id = ?
                      AND aggregate_type IN ('external_order_lookup', 'external_fill_lookup')
                    )
                    OR (aggregate_id = ? AND event_type = ?)
                  )
                ORDER BY id
                """,
                (
                    str(order["engine"]),
                    str(order["intent_id"]),
                    str(order["id"]),
                    PAPER_EXECUTION_EVIDENCE_EVENT_TYPE,
                ),
            ).fetchall()
        )
        return ResolutionSnapshot(order, observations, fills, events)

    def read_resolution_snapshot(self, local_order_id: int) -> ResolutionSnapshot:
        """Read order, observations, fills, and lookup events atomically."""

        if (
            isinstance(local_order_id, bool)
            or not isinstance(local_order_id, int)
            or local_order_id <= 0
        ):
            raise ValueError("local_order_id doit être un entier strictement positif")
        with self._read_transaction() as connection:
            return self.read_resolution_snapshot_in_transaction(connection, local_order_id)

    def get_external_order_observations(
        self, local_order_id: int
    ) -> list[ExternalOrderObservation]:
        with self._connect_factory() as connection:
            rows = connection.execute(
                """
                SELECT * FROM external_order_observations
                WHERE local_order_id = ? ORDER BY id
                """,
                (local_order_id,),
            ).fetchall()
        return [self._observation_from_row(row) for row in rows]

    def get_external_fills(self, local_order_id: int) -> list[ExternalFill]:
        with self._connect_factory() as connection:
            rows = connection.execute(
                "SELECT * FROM external_fills WHERE local_order_id = ? ORDER BY id",
                (local_order_id,),
            ).fetchall()
        return [self._fill_from_row(row) for row in rows]


@dataclass(frozen=True)
class PersistedLookupEvent:
    """One lookup/evidence event read from a coherent SQLite snapshot."""

    event_id: int
    ts: str
    engine: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    payload: str


@dataclass(frozen=True)
class ResolutionSnapshot:
    """Read-only input for resolution projection."""

    order: Mapping[str, Any] | None
    order_observations: tuple[ExternalOrderObservation, ...]
    fills: tuple[ExternalFill, ...]
    lookup_events: tuple[PersistedLookupEvent, ...]


__all__ = [
    "ExecutionEvidenceRepository",
    "PersistedLookupEvent",
    "ResolutionSnapshot",
]
