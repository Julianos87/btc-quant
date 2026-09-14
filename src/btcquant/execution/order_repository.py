"""Durable order-intent and submission persistence boundary.

This module owns the SQLite rows and transitions that describe one local order
from intent reservation through external observation or terminal local
recovery.  It deliberately has no broker, venue, strategy, risk, settlement,
or notification dependency.  Larger transactions that also checkpoint engine
state or apply financial effects remain orchestrated by :mod:`state_store` and
must pass their existing SQLite connection through the boundary.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any

from .errors import InvalidOrderStateTransition, OrderIdentityCollision
from .order_state import ExternalOrderState, LocalOrderState, LogicalOrderIdentity


@dataclass(frozen=True)
class OrderReservation:
    """The durable ownership result for one logical order intent."""

    order_id: int
    intent_id: str
    logical_order_key: str
    acquired: bool
    status: str
    local_state: str
    external_state: str | None
    filled_qty: float
    remaining_qty: float


class OrderRepository:
    """Persist order identity and execution-boundary state.

    The callbacks are deliberately narrow adapters supplied by ``StateStore``.
    They let this repository participate in the same transaction and event
    journal without importing the store, broker, or financial application
    services.
    """

    def __init__(
        self,
        *,
        connect: Callable[[], sqlite3.Connection],
        transaction: Callable[[], AbstractContextManager[sqlite3.Connection]],
        insert_event: Callable[..., int],
        assert_reconciliation_clear: Callable[[sqlite3.Connection, str], None],
        now: Callable[[], str],
    ) -> None:
        self._connect_factory = connect
        self._transaction_factory = transaction
        self._insert_event = insert_event
        self._assert_reconciliation_clear = assert_reconciliation_clear
        self._now = now

    def _connect(self) -> sqlite3.Connection:
        return self._connect_factory()

    def _transaction(self) -> AbstractContextManager[sqlite3.Connection]:
        return self._transaction_factory()

    @staticmethod
    def _reservation_from_row(
        row: sqlite3.Row, *, acquired: bool, logical_order_key: str | None = None
    ) -> OrderReservation:
        return OrderReservation(
            order_id=int(row["id"]),
            intent_id=str(row["intent_id"]),
            logical_order_key=str(
                row["logical_order_key"] if logical_order_key is None else logical_order_key
            ),
            acquired=acquired,
            status=str(row["status"]),
            local_state=str(row["local_state"]),
            external_state=row["external_state"],
            filled_qty=float(row["filled_qty"]),
            remaining_qty=float(row["remaining_qty"]),
        )

    @staticmethod
    def _local_state_for_legacy_status(status: str) -> LocalOrderState:
        if status == "OPEN":
            return LocalOrderState.AWAITING_EXTERNAL
        if status in ("PENDING", "UNBALANCED"):
            return LocalOrderState.PENDING_RECONCILIATION
        return LocalOrderState.TERMINAL

    @staticmethod
    def _external_state_for_legacy_status(status: str) -> ExternalOrderState | None:
        mapping = {
            "OPEN": ExternalOrderState.OPEN,
            "FILLED": ExternalOrderState.FILLED,
            "PARTIAL": ExternalOrderState.PARTIAL_TERMINAL,
            "REJECTED": ExternalOrderState.REJECTED,
            "CANCELED": ExternalOrderState.CANCELED,
            "UNBALANCED": ExternalOrderState.UNKNOWN,
        }
        return mapping.get(status)

    def reserve_market_order(
        self,
        identity: LogicalOrderIdentity,
        *,
        side: str,
        requested_qty: float,
        reason: str,
        reference_price: float,
    ) -> OrderReservation:
        """Atomically reserve one logical MARKET intent, idempotently."""

        if not math.isfinite(requested_qty) or requested_qty <= 0:
            raise ValueError("requested_qty doit être finie et strictement positive")
        if not math.isfinite(reference_price) or reference_price <= 0:
            raise ValueError("reference_price doit être fini et strictement positif")
        if side not in {"BUY", "SELL"}:
            raise ValueError(f"Côté d'ordre non normalisé : {side!r}")
        logical_key = identity.logical_key
        intent_id = identity.intent_id
        now = self._now()
        with self._transaction() as connection:
            self._assert_reconciliation_clear(connection, identity.engine)
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO orders(
                        engine, slot, intent_id, logical_order_key, order_type,
                        side, requested_qty, reference_price, remaining_qty,
                        local_state, status, reason, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, 'MARKET', ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?)
                    """,
                    (
                        identity.engine,
                        identity.slot,
                        intent_id,
                        logical_key,
                        side,
                        requested_qty,
                        reference_price,
                        requested_qty,
                        LocalOrderState.INTENT_CREATED.value,
                        reason,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as error:
                existing = connection.execute(
                    """
                    SELECT id, intent_id, logical_order_key, status,
                           local_state, external_state, filled_qty, remaining_qty
                    FROM orders
                    WHERE logical_order_key = ? OR intent_id = ?
                    ORDER BY id LIMIT 1
                    """,
                    (logical_key, intent_id),
                ).fetchone()
                if existing is None:
                    raise
                if (
                    existing["logical_order_key"] != logical_key
                    or existing["intent_id"] != intent_id
                ):
                    raise OrderIdentityCollision(
                        "Collision entre l'empreinte d'intention et la clé logique complète"
                    ) from error
                return self._reservation_from_row(
                    existing, acquired=False, logical_order_key=logical_key
                )
            order_id = cursor.lastrowid
            if order_id is None:
                raise RuntimeError("SQLite n'a pas retourné l'identifiant de l'ordre")
            self._insert_event(
                connection,
                identity.engine,
                "order_intent_reserved",
                {
                    "order_id": order_id,
                    "logical_order_key": logical_key,
                    "intent_id": intent_id,
                    "transition_type": identity.transition_type.value,
                    "decision_checkpoint": identity.decision_checkpoint,
                    "position_generation": identity.position_generation,
                    "transition_sequence": identity.transition_sequence,
                    "side": side,
                    "requested_qty": requested_qty,
                    "reference_price": reference_price,
                    "reason": reason,
                },
                "order",
                str(order_id),
                intent_id,
            )
            return OrderReservation(
                order_id=int(order_id),
                intent_id=intent_id,
                logical_order_key=logical_key,
                acquired=True,
                status="PENDING",
                local_state=LocalOrderState.INTENT_CREATED.value,
                external_state=None,
                filled_qty=0.0,
                remaining_qty=requested_qty,
            )

    def insert_market_order_in_transaction(
        self,
        connection: sqlite3.Connection,
        identity: LogicalOrderIdentity,
        *,
        side: str,
        requested_qty: float,
        reference_price: float,
        reason: str,
        now: str,
    ) -> int:
        cursor = connection.execute(
            """
            INSERT INTO orders(
                engine, slot, intent_id, logical_order_key, order_type, side,
                requested_qty, reference_price, remaining_qty, local_state,
                status, reason, created_at, updated_at
            ) VALUES(?, ?, ?, ?, 'MARKET', ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?)
            """,
            (
                identity.engine,
                identity.slot,
                identity.intent_id,
                identity.logical_key,
                side,
                requested_qty,
                reference_price,
                requested_qty,
                LocalOrderState.INTENT_CREATED.value,
                reason,
                now,
                now,
            ),
        )
        order_id = cursor.lastrowid
        if order_id is None:
            raise RuntimeError("SQLite n'a pas retourné l'identifiant de l'ordre")
        return int(order_id)

    def mark_submitting(self, order_id: int) -> None:
        now = self._now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT engine, intent_id, local_state FROM orders WHERE id = ?",
                (order_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Ordre journalisé introuvable : {order_id}")
            if row["local_state"] != LocalOrderState.INTENT_CREATED.value:
                raise InvalidOrderStateTransition(
                    f"Ordre {order_id}: {row['local_state']} ne peut pas devenir SUBMITTING"
                )
            connection.execute(
                "UPDATE orders SET local_state=?, updated_at=? WHERE id=?",
                (LocalOrderState.SUBMITTING.value, now, order_id),
            )
            self._insert_event(
                connection,
                row["engine"],
                "order_submission_started",
                {"order_id": order_id},
                "order",
                str(order_id),
                row["intent_id"],
            )

    def reclaim_safe_market_order(
        self, order_id: int, *, allow_local_failure: bool = False
    ) -> bool:
        """Reclaim only a locally proven pre-submit failure."""

        now = self._now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT engine, intent_id, status FROM orders WHERE id = ?", (order_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Ordre journalisé introuvable : {order_id}")
            cursor = connection.execute(
                """
                UPDATE orders SET status='PENDING', local_state='SUBMITTING',
                    remaining_qty=requested_qty, error=NULL, updated_at=?
                WHERE id=? AND logical_order_key IS NOT NULL
                  AND (status='RECOVERED_ABORTED' OR (? AND status='FAILED'))
                  AND local_state='TERMINAL'
                  AND external_state IS NULL AND broker_order_id IS NULL
                  AND filled_qty=0
                """,
                (now, order_id, allow_local_failure),
            )
            if cursor.rowcount != 1:
                return False
            self._insert_event(
                connection,
                row["engine"],
                "order_submission_reclaimed",
                {"order_id": order_id, "previous_status": row["status"]},
                "order",
                str(order_id),
                row["intent_id"],
            )
            return True

    def recover_local_market_order(self, order_id: int, *, error: str) -> bool:
        """Mark a market intent aborted only when no external effect exists."""

        now = self._now()
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT engine, intent_id, status, order_type, local_state,
                       external_state, filled_qty, broker_order_id
                FROM orders WHERE id = ?
                """,
                (order_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Ordre journalisé introuvable : {order_id}")
            if row["local_state"] == LocalOrderState.TERMINAL.value:
                return bool(
                    row["status"] == "RECOVERED_ABORTED"
                    and row["order_type"] == "MARKET"
                    and row["external_state"] is None
                    and float(row["filled_qty"]) == 0.0
                    and row["broker_order_id"] is None
                )
            cursor = connection.execute(
                """
                UPDATE orders SET status='RECOVERED_ABORTED', local_state='TERMINAL',
                    external_state=NULL, filled_qty=0, remaining_qty=0, price=NULL,
                    fee=0, broker_order_id=NULL, error=?, updated_at=?
                WHERE id=? AND order_type='MARKET' AND local_state<>'TERMINAL'
                """,
                (error, now, order_id),
            )
            if cursor.rowcount != 1:
                return False
            self._insert_event(
                connection,
                row["engine"],
                "local_order_recovered",
                {"order_id": order_id, "previous_status": row["status"], "error": error},
                "order",
                str(order_id),
                row["intent_id"],
            )
            return True

    def record_observation(
        self,
        order_id: int,
        *,
        external_state: ExternalOrderState,
        filled_qty: float,
        remaining_qty: float,
        price: float | None,
        fee: float,
        broker_order_id: str | None,
    ) -> None:
        """Persist broker observation before any financial checkpoint."""

        external_state = ExternalOrderState(external_state)
        local_state = (
            LocalOrderState.PENDING_RECONCILIATION
            if external_state == ExternalOrderState.UNKNOWN or external_state.is_terminal
            else LocalOrderState.AWAITING_EXTERNAL
        )
        status = "PENDING" if local_state == LocalOrderState.PENDING_RECONCILIATION else "OPEN"
        now = self._now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT engine, intent_id, local_state FROM orders WHERE id = ?",
                (order_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Ordre journalisé introuvable : {order_id}")
            if row["local_state"] != LocalOrderState.SUBMITTING.value:
                raise InvalidOrderStateTransition(
                    f"Ordre {order_id}: réponse broker reçue depuis {row['local_state']}"
                )
            connection.execute(
                """
                UPDATE orders SET status=?, local_state=?, external_state=?,
                    filled_qty=?, remaining_qty=?, price=?, fee=?, broker_order_id=?,
                    updated_at=? WHERE id=?
                """,
                (
                    status,
                    local_state.value,
                    external_state.value,
                    filled_qty,
                    remaining_qty,
                    price,
                    fee,
                    broker_order_id,
                    now,
                    order_id,
                ),
            )
            self._insert_event(
                connection,
                row["engine"],
                "order_external_observed",
                {
                    "order_id": order_id,
                    "external_state": external_state.value,
                    "filled_qty": filled_qty,
                    "remaining_qty": remaining_qty,
                    "price": price,
                    "fee": fee,
                },
                "order",
                str(order_id),
                row["intent_id"],
            )

    def record_submission_error(self, order_id: int, *, error: str, ambiguous: bool) -> None:
        now = self._now()
        local_state = (
            LocalOrderState.PENDING_RECONCILIATION if ambiguous else LocalOrderState.TERMINAL
        )
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT engine, intent_id, local_state FROM orders WHERE id = ?", (order_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Ordre journalisé introuvable : {order_id}")
            if row["local_state"] != LocalOrderState.SUBMITTING.value:
                raise InvalidOrderStateTransition(
                    f"Ordre {order_id}: échec de soumission reçu depuis {row['local_state']}"
                )
            connection.execute(
                """
                UPDATE orders SET status=?, local_state=?, external_state=?, error=?, updated_at=?
                WHERE id=?
                """,
                (
                    "PENDING" if ambiguous else "FAILED",
                    local_state.value,
                    ExternalOrderState.UNKNOWN.value if ambiguous else None,
                    error,
                    now,
                    order_id,
                ),
            )
            self._insert_event(
                connection,
                row["engine"],
                "order_submission_failed",
                {"order_id": order_id, "ambiguous": ambiguous, "error": error},
                "order",
                str(order_id),
                row["intent_id"],
            )

    def begin_order(
        self,
        engine: str,
        slot: str,
        intent_id: str,
        order_type: str,
        side: str,
        requested_qty: float,
        reason: str,
        reference_price: float | None = None,
    ) -> int:
        now = self._now()
        with self._transaction() as connection:
            self._assert_reconciliation_clear(connection, engine)
            cursor = connection.execute(
                """
                INSERT INTO orders(
                    engine, slot, intent_id, order_type, side, requested_qty,
                    reference_price, remaining_qty, local_state, status, reason,
                    created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?)
                """,
                (
                    engine,
                    slot,
                    intent_id,
                    order_type,
                    side,
                    requested_qty,
                    reference_price,
                    requested_qty,
                    LocalOrderState.PENDING_RECONCILIATION.value,
                    reason,
                    now,
                    now,
                ),
            )
            order_id = cursor.lastrowid
            if order_id is None:
                raise RuntimeError("SQLite n'a pas retourné l'identifiant de l'ordre")
            self._insert_event(
                connection,
                engine,
                "order_intent",
                {
                    "order_id": order_id,
                    "side": side,
                    "requested_qty": requested_qty,
                    "reference_price": reference_price,
                    "reason": reason,
                },
                "order",
                str(order_id),
                intent_id,
            )
            return int(order_id)

    def complete_order(
        self,
        order_id: int,
        *,
        status: str,
        filled_qty: float = 0.0,
        remaining_qty: float | None = None,
        price: float | None = None,
        fee: float = 0.0,
        broker_order_id: str | None = None,
        error: str | None = None,
        external_state: ExternalOrderState | str | None = None,
        local_state: LocalOrderState | str | None = None,
    ) -> None:
        now = self._now()
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT engine, intent_id, logical_order_key, local_state,
                       external_state, remaining_qty FROM orders WHERE id = ?
                """,
                (order_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Ordre journalisé introuvable : {order_id}")
            resolved_local = (
                LocalOrderState(local_state)
                if local_state is not None
                else self._local_state_for_legacy_status(status)
            )
            resolved_external = (
                ExternalOrderState(external_state)
                if external_state is not None
                else ExternalOrderState(row["external_state"])
                if row["external_state"] is not None
                else self._external_state_for_legacy_status(status)
            )
            if (
                row["logical_order_key"] is not None
                and resolved_local == LocalOrderState.TERMINAL
                and not (
                    status == "RECOVERED_ABORTED"
                    and row["local_state"] == LocalOrderState.INTENT_CREATED.value
                )
                and (resolved_external is None or not resolved_external.is_terminal)
            ):
                raise InvalidOrderStateTransition(
                    f"Ordre {order_id}: terminalité locale sans preuve externe terminale"
                )
            resolved_remaining = (
                remaining_qty
                if remaining_qty is not None
                else 0.0
                if resolved_local == LocalOrderState.TERMINAL
                else float(row["remaining_qty"])
            )
            connection.execute(
                """
                UPDATE orders SET status=?, local_state=?, external_state=?,
                    filled_qty=?, remaining_qty=?, price=?, fee=?,
                    broker_order_id=?, error=?, updated_at=? WHERE id=?
                """,
                (
                    status,
                    resolved_local.value,
                    resolved_external.value if resolved_external is not None else None,
                    filled_qty,
                    resolved_remaining,
                    price,
                    fee,
                    broker_order_id,
                    error,
                    now,
                    order_id,
                ),
            )
            self._insert_event(
                connection,
                row["engine"],
                "order_updated",
                {
                    "order_id": order_id,
                    "status": status,
                    "local_state": resolved_local.value,
                    "external_state": (
                        resolved_external.value if resolved_external is not None else None
                    ),
                    "filled_qty": filled_qty,
                    "remaining_qty": resolved_remaining,
                    "price": price,
                    "fee": fee,
                    "error": error,
                },
                "order",
                str(order_id),
                row["intent_id"],
            )

    def pending_orders(self, engine: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM orders WHERE engine = ? AND status = 'PENDING' ORDER BY id",
                (engine,),
            ).fetchall()
        return [dict(row) for row in rows]

    def unresolved_orders(self, engine: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM orders
                WHERE engine = ? AND local_state != 'TERMINAL'
                  AND NOT (order_type = 'STOP' AND status = 'OPEN')
                ORDER BY id
                """,
                (engine,),
            ).fetchall()
        return [dict(row) for row in rows]

    def read_order_by_intent(self, intent_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM orders WHERE intent_id = ?", (intent_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def read_orders(self, engine: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM orders"
        params: tuple[str, ...] = ()
        if engine is not None:
            query += " WHERE engine = ?"
            params = (engine,)
        query += " ORDER BY id"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]


__all__ = ["OrderRepository", "OrderReservation"]
