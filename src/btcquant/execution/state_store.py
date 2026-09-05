"""Persistance transactionnelle et journal opérationnel SQLite.

La base est la source de vérité des runners. Chaque méthode d'écriture ouvre
une transaction ``BEGIN IMMEDIATE`` afin qu'un checkpoint soit soit entièrement
visible, soit entièrement absent après un crash.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from types import MappingProxyType
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

from .errors import (
    AccountingIdentityCollision,
    ExternalFillConflict,
    ExternalObservationConflict,
    FinancialApplicationPlanConflict,
    InvalidExternalObservation,
    InvalidOrderStateTransition,
    MigrationRequiredError,
    OrderIdentityCollision,
)
from .external_evidence import ExternalEvidenceSource, ExternalFill, ExternalOrderObservation
from .paper_execution_evidence import (
    PAPER_EVIDENCE_VERSION,
    PAPER_EXECUTION_EVIDENCE_AGGREGATE_TYPE,
    PAPER_EXECUTION_EVIDENCE_EVENT_TYPE,
    PaperExecutionEvidence,
    PaperExecutionEvidencePersistenceResult,
)
from .financial_application_plan import (
    FinancialApplicationPlan,
    PersistedFinancialApplicationPlan,
    canonical_json,
    parse_logical_order_identity,
    sha256_json,
)
from .financial_fill_application import (
    FinancialApplicationLedgerConflict,
    FinancialFillApplicationError,
    FinancialFillApplicationRequest,
    FinancialFillCommitResult,
    FinancialFillApplicationResult,
    PersistedFinancialFillApplication,
    calculate_financial_fill_application,
    financial_fill_application_key,
    recompute_committed_financial_fill_application,
)
from .financial_order_settlement import (
    ExternalOrderSettlement,
    FinancialSettlementCommitResult,
    FinancialSettlementError,
    FINANCIAL_SETTLEMENT_APPLICATION_VERSION,
    FinancialSettlementResult,
    PersistedFinancialSettlementApplication,
    calculate_financial_order_settlement,
    financial_settlement_application_key,
)
from .paper_order_finalization import (
    PaperFinalizationDecision,
    PaperFinalizationStatus,
    decide_paper_finalization,
)
from .paper_zero_effect import (
    PAPER_ZERO_EFFECT_AGGREGATE_TYPE,
    PAPER_ZERO_EFFECT_EVENT_TYPE,
    PAPER_ZERO_EFFECT_EVIDENCE_VERSION,
    PaperZeroEffectStatus,
    decide_paper_zero_effect,
)
from .order_state import (
    ExternalOrderState,
    FinancialTransitionType,
    LocalOrderState,
    LogicalOrderIdentity,
)
from .state_contract import validate_trend_state

SCHEMA_VERSION = 13
DEPOSIT_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,127}")


@dataclass(frozen=True)
class OrderReservation:
    order_id: int
    intent_id: str
    logical_order_key: str
    acquired: bool
    status: str
    local_state: str
    external_state: str | None
    filled_qty: float
    remaining_qty: float


@dataclass(frozen=True)
class PersistedLookupEvent:
    """Un événement de lookup brut lu dans un snapshot SQLite cohérent."""

    event_id: int
    ts: str
    engine: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    payload: str


@dataclass(frozen=True)
class ResolutionSnapshot:
    """Entrée read-only bornée pour la projection de résolution."""

    order: Mapping[str, Any] | None
    order_observations: tuple[ExternalOrderObservation, ...]
    fills: tuple[ExternalFill, ...]
    lookup_events: tuple[PersistedLookupEvent, ...]


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _unserializable_paths(payload: Any, prefix: str = "") -> list[str]:
    """Chemins des valeurs que ``json`` refuse, pour un message exploitable."""

    if isinstance(payload, dict):
        found: list[str] = []
        for key, value in payload.items():
            found.extend(_unserializable_paths(value, f"{prefix}.{key}" if prefix else str(key)))
        return found
    if isinstance(payload, (list, tuple)):
        found = []
        for index, value in enumerate(payload):
            found.extend(_unserializable_paths(value, f"{prefix}[{index}]"))
        return found
    if payload is None or isinstance(payload, (str, int, float, bool)):
        return []
    # Le module est indispensable : `numpy.bool` s'affiche « bool » comme le
    # type natif, alors que c'est précisément lui qui casse la sérialisation.
    kind = type(payload)
    return [f"{prefix or '<racine>'} ({kind.__module__}.{kind.__qualname__})"]


def database_path(state_path: str | Path) -> Path:
    """Retourne la base partagée du dossier d'état.

    Les anciens chemins ``*.json`` restent acceptés pour permettre une
    migration automatique sans casser les configurations existantes.
    """

    path = Path(state_path)
    return path if path.suffix == ".db" else path.parent / "btcquant.db"


class StateStore:
    def __init__(
        self,
        path: str | Path,
        *,
        initialize: bool = True,
        allow_migration: bool = False,
        read_only: bool = False,
    ) -> None:
        self.path = Path(path)
        self.allow_migration = allow_migration
        self.read_only = read_only
        if not read_only:
            # Every production StateStore writer shares the restore gate.  This
            # central check covers runners, timers, watchdogs and qualification
            # writers instead of relying on individual entrypoints.
            from ..backup import assert_writer_recovery_clear

            assert_writer_recovery_clear(self.path.parent)
        if read_only:
            if not self.path.exists():
                raise FileNotFoundError(self.path)
            return
        if initialize:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._initialize()
        elif not self.path.exists():
            raise FileNotFoundError(self.path)

    def _connect(self) -> sqlite3.Connection:
        if self.read_only:
            uri = f"{self.path.resolve().as_uri()}?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=15.0)
            connection.execute("PRAGMA query_only = ON")
        else:
            connection = sqlite3.connect(self.path, timeout=15.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        if not self.read_only:
            connection.execute("PRAGMA synchronous = FULL")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def _read_transaction(self) -> Iterator[sqlite3.Connection]:
        """Snapshot de lecture cohérent, sans verrou d'écriture volontaire."""

        connection = self._connect()
        try:
            connection.execute("BEGIN")
            yield connection
            connection.rollback()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def _read_connection(
        self, connection: sqlite3.Connection | None
    ) -> Iterator[sqlite3.Connection]:
        """Use an existing transaction when a caller already owns one."""

        if connection is not None:
            yield connection
            return
        with self._read_transaction() as owned_connection:
            yield owned_connection

    def _initialize(self) -> None:
        existing_version, has_schema = self._existing_schema()
        if (
            has_schema
            and (existing_version is None or existing_version < SCHEMA_VERSION)
            and not self.allow_migration
        ):
            raise MigrationRequiredError(str(self.path), existing_version, SCHEMA_VERSION)
        if existing_version is not None and existing_version > SCHEMA_VERSION:
            raise RuntimeError(
                f"Base SQLite version {existing_version} plus récente que le code "
                f"(version {SCHEMA_VERSION})"
            )
        with self._transaction() as connection:
            connection.executescript(
                """
                -- sqlite3.executescript valide implicitement toute transaction
                -- déjà ouverte. Reprendre le verrou ici maintient donc schéma,
                -- migration et index UNIQUE dans une seule transaction.
                BEGIN IMMEDIATE;

                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS engine_state (
                    engine TEXT PRIMARY KEY,
                    payload TEXT NOT NULL CHECK(json_valid(payload)),
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS positions (
                    engine TEXT NOT NULL,
                    slot TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(
                        status IN ('FLAT', 'OPEN', 'UNBALANCED')
                    ),
                    cash REAL,
                    entry_time TEXT,
                    entry_price REAL,
                    qty REAL NOT NULL DEFAULT 0 CHECK(qty >= 0),
                    stop_price REAL,
                    direction INTEGER CHECK(direction IN (-1, 1) OR direction IS NULL),
                    bars_held INTEGER NOT NULL DEFAULT 0,
                    best_close REAL,
                    stop_order_id TEXT,
                    entry_fee REAL NOT NULL DEFAULT 0,
                    last_bar_ts TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (engine, slot)
                );

                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    engine TEXT NOT NULL,
                    slot TEXT NOT NULL,
                    intent_id TEXT NOT NULL UNIQUE,
                    logical_order_key TEXT,
                    broker_order_id TEXT,
                    order_type TEXT NOT NULL,
                    side TEXT NOT NULL,
                    requested_qty REAL NOT NULL CHECK(requested_qty >= 0),
                    reference_price REAL,
                    filled_qty REAL NOT NULL DEFAULT 0 CHECK(filled_qty >= 0),
                    remaining_qty REAL NOT NULL DEFAULT 0 CHECK(remaining_qty >= 0),
                    price REAL,
                    fee REAL NOT NULL DEFAULT 0,
                    local_state TEXT NOT NULL DEFAULT 'TERMINAL' CHECK(
                        local_state IN (
                            'INTENT_CREATED', 'SUBMITTING', 'AWAITING_EXTERNAL',
                            'PENDING_RECONCILIATION', 'TERMINAL'
                        )
                    ),
                    external_state TEXT CHECK(
                        external_state IS NULL OR external_state IN (
                            'OPEN', 'PARTIAL_OPEN', 'FILLED', 'PARTIAL_TERMINAL',
                            'CANCELED', 'REJECTED', 'EXPIRED', 'UNKNOWN'
                        )
                    ),
                    status TEXT NOT NULL CHECK(
                        status IN (
                            'PENDING', 'OPEN', 'FILLED', 'PARTIAL', 'REJECTED',
                            'FAILED', 'CANCELED', 'UNBALANCED', 'RECOVERED_ABORTED'
                        )
                    ),
                    reason TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);

                CREATE TABLE IF NOT EXISTS incidents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fingerprint TEXT NOT NULL UNIQUE,
                    engine TEXT,
                    severity TEXT NOT NULL CHECK(severity IN ('WARNING', 'CRITICAL')),
                    kind TEXT NOT NULL,
                    message TEXT NOT NULL,
                    context TEXT NOT NULL CHECK(json_valid(context)),
                    status TEXT NOT NULL CHECK(status IN ('OPEN', 'RESOLVED')),
                    occurrences INTEGER NOT NULL DEFAULT 1 CHECK(occurrences > 0),
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    resolved_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_incidents_status_last_seen
                    ON incidents(status, last_seen);

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    engine TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    aggregate_type TEXT,
                    aggregate_id TEXT,
                    payload TEXT NOT NULL CHECK(json_valid(payload)),
                    correlation_id TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_events_engine_id ON events(engine, id);

                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    exit_ts TEXT NOT NULL,
                    entry_ts TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    qty REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL NOT NULL,
                    pnl REAL NOT NULL,
                    bars_held INTEGER NOT NULL,
                    reason TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS equity_samples (
                    engine TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    equity REAL NOT NULL,
                    PRIMARY KEY (engine, ts)
                );
                CREATE INDEX IF NOT EXISTS idx_equity_engine_ts
                    ON equity_samples(engine, ts);

                CREATE TABLE IF NOT EXISTS flows (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    trend_flow REAL NOT NULL,
                    carry_flow REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS capital_deposits (
                    deposit_id TEXT PRIMARY KEY,
                    amount REAL NOT NULL CHECK(amount > 0),
                    status TEXT NOT NULL CHECK(status IN ('PENDING', 'APPLIED')),
                    requested_at TEXT NOT NULL,
                    applied_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_capital_deposits_status
                    ON capital_deposits(status, requested_at);

                CREATE TABLE IF NOT EXISTS qualification_campaigns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    protocol_version INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(
                        status IN ('RUNNING', 'PASSED', 'CANCELED')
                    ),
                    policy TEXT NOT NULL CHECK(json_valid(policy)),
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    final_report TEXT CHECK(
                        final_report IS NULL OR json_valid(final_report)
                    )
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_one_running_qualification
                    ON qualification_campaigns(status) WHERE status = 'RUNNING';

                CREATE TABLE IF NOT EXISTS readiness_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    campaign_id INTEGER,
                    protocol_version INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('PASS', 'FAIL')),
                    generated_at TEXT NOT NULL,
                    payload TEXT NOT NULL CHECK(json_valid(payload)),
                    FOREIGN KEY(campaign_id) REFERENCES qualification_campaigns(id)
                );
                CREATE INDEX IF NOT EXISTS idx_readiness_reports_campaign
                    ON readiness_reports(campaign_id, id);
                """
            )
            row = connection.execute(
                "SELECT value FROM metadata WHERE key = 'schema_version'"
            ).fetchone()
            new_schema = row is None
            if row is None:
                # La métadonnée est écrite après la création du dernier objet
                # afin qu'une panne v9 ne puisse jamais valider un schéma partiel.
                current_version = SCHEMA_VERSION
            elif int(row["value"]) > SCHEMA_VERSION:
                raise RuntimeError(
                    f"Base SQLite version {row['value']} plus récente que le code "
                    f"(version {SCHEMA_VERSION})"
                )
            else:
                current_version = int(row["value"])
                if current_version < 2:
                    columns = {
                        item["name"]
                        for item in connection.execute("PRAGMA table_info(orders)").fetchall()
                    }
                    if "reference_price" not in columns:
                        connection.execute("ALTER TABLE orders ADD COLUMN reference_price REAL")
                    current_version = 2
                if current_version < 3:
                    current_version = 3
                if current_version < 4:
                    current_version = 4
            self._ensure_order_safety_schema(connection)
            if current_version < 5:
                current_version = 5
            if current_version < 6:
                self._migrate_v6(connection)
                current_version = 6
            else:
                self._ensure_funding_accounting_schema(connection)
            if current_version < 7:
                self._migrate_v7(connection)
                current_version = 7
            else:
                self._ensure_external_evidence_schema(
                    connection, require_status_event_at=current_version >= 11
                )
            if current_version < 8:
                self._migrate_v8(connection)
                current_version = 8
            if current_version < 9:
                self._migrate_v9(connection)
                current_version = 9
            else:
                self._ensure_financial_application_plan_schema(connection)
            if current_version < 10:
                self._migrate_v10(connection)
                current_version = 10
            else:
                self._ensure_financial_fill_application_schema(connection)
            if current_version < 11:
                self._migrate_v11(connection)
                current_version = 11
            if current_version < 12:
                self._migrate_v12(connection)
                current_version = 12
            self._ensure_external_order_settlement_schema(connection)
            if current_version < 13:
                self._migrate_v13(connection)
                current_version = 13
            else:
                self._ensure_financial_settlement_application_schema(connection)

            if new_schema:
                connection.execute(
                    "INSERT INTO metadata(key, value) VALUES('schema_version', ?)",
                    (str(current_version),),
                )
            else:
                connection.execute(
                    "UPDATE metadata SET value = ? WHERE key = 'schema_version'",
                    (str(current_version),),
                )
            # WAL est persistant. Il est activé hors d'une transaction sur
            # certaines versions SQLite ; l'échec est sans impact fonctionnel.
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")

    def _existing_schema(self) -> tuple[int | None, bool]:
        """Inspecte une base existante sans l'ouvrir en écriture."""

        if not self.path.exists() or self.path.stat().st_size == 0:
            return None, False
        uri = f"{self.path.resolve().as_uri()}?mode=ro"
        try:
            with sqlite3.connect(uri, uri=True) as connection:
                connection.execute("PRAGMA query_only = ON")
                tables = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                if not tables:
                    return None, False
                row = connection.execute(
                    "SELECT value FROM metadata WHERE key = 'schema_version'"
                ).fetchone()
                return (int(row[0]) if row is not None else None), True
        except sqlite3.OperationalError as error:
            if "no such table" in str(error).lower():
                return None, True
            raise

    @staticmethod
    def _ensure_order_safety_schema(connection: sqlite3.Connection) -> None:
        """Migration v5 additive, idempotente et sans reconstruction de table."""

        columns = {
            item["name"] for item in connection.execute("PRAGMA table_info(orders)").fetchall()
        }
        local_added = "local_state" not in columns
        external_added = "external_state" not in columns
        remaining_added = "remaining_qty" not in columns
        if "logical_order_key" not in columns:
            connection.execute("ALTER TABLE orders ADD COLUMN logical_order_key TEXT")
        if local_added:
            connection.execute(
                """
                ALTER TABLE orders ADD COLUMN local_state TEXT NOT NULL DEFAULT 'TERMINAL'
                CHECK(local_state IN (
                    'INTENT_CREATED', 'SUBMITTING', 'AWAITING_EXTERNAL',
                    'PENDING_RECONCILIATION', 'TERMINAL'
                ))
                """
            )
        if external_added:
            connection.execute(
                """
                ALTER TABLE orders ADD COLUMN external_state TEXT
                CHECK(external_state IS NULL OR external_state IN (
                    'OPEN', 'PARTIAL_OPEN', 'FILLED', 'PARTIAL_TERMINAL',
                    'CANCELED', 'REJECTED', 'EXPIRED', 'UNKNOWN'
                ))
                """
            )
        if remaining_added:
            connection.execute(
                """
                ALTER TABLE orders ADD COLUMN remaining_qty REAL NOT NULL DEFAULT 0
                CHECK(remaining_qty >= 0)
                """
            )
        if local_added:
            connection.execute(
                """
                UPDATE orders SET local_state = CASE
                    WHEN status IN ('PENDING', 'UNBALANCED') THEN 'PENDING_RECONCILIATION'
                    WHEN status = 'OPEN' THEN 'AWAITING_EXTERNAL'
                    WHEN order_type = 'MARKET' AND status IN ('PARTIAL', 'REJECTED')
                    THEN 'PENDING_RECONCILIATION'
                    ELSE 'TERMINAL'
                END
                """
            )
        if external_added:
            connection.execute(
                """
                UPDATE orders SET external_state = CASE status
                    WHEN 'OPEN' THEN 'OPEN'
                    WHEN 'FILLED' THEN 'FILLED'
                    WHEN 'PARTIAL' THEN CASE
                        WHEN order_type = 'MARKET' THEN 'UNKNOWN'
                        ELSE 'PARTIAL_TERMINAL'
                    END
                    WHEN 'REJECTED' THEN CASE
                        WHEN order_type = 'MARKET' THEN 'UNKNOWN'
                        ELSE 'REJECTED'
                    END
                    WHEN 'CANCELED' THEN 'CANCELED'
                    WHEN 'UNBALANCED' THEN 'UNKNOWN'
                    ELSE NULL
                END
                """
            )
        if remaining_added:
            connection.execute(
                """
                UPDATE orders SET remaining_qty = CASE
                    WHEN status IN ('PENDING', 'OPEN', 'UNBALANCED')
                      OR (order_type = 'MARKET' AND status IN ('PARTIAL', 'REJECTED'))
                    THEN MAX(0, requested_qty - filled_qty)
                    ELSE 0
                END
                """
            )
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_logical_order_key
            ON orders(logical_order_key) WHERE logical_order_key IS NOT NULL
            """
        )
        index = next(
            (
                row
                for row in connection.execute("PRAGMA index_list(orders)").fetchall()
                if row["name"] == "idx_orders_logical_order_key"
            ),
            None,
        )
        index_columns = [
            row["name"]
            for row in connection.execute(
                "PRAGMA index_info(idx_orders_logical_order_key)"
            ).fetchall()
        ]
        index_sql_row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
            ("idx_orders_logical_order_key",),
        ).fetchone()
        normalized_index_sql = (
            " ".join(str(index_sql_row["sql"]).lower().split())
            if index_sql_row is not None and index_sql_row["sql"] is not None
            else ""
        )
        if (
            index is None
            or int(index["unique"]) != 1
            or int(index["partial"]) != 1
            or index_columns != ["logical_order_key"]
            or "where logical_order_key is not null" not in normalized_index_sql
        ):
            raise RuntimeError(
                "Migration v5 refusée : idx_orders_logical_order_key existe "
                "sans garantir l'unicité partielle attendue"
            )

    @staticmethod
    def _ensure_funding_accounting_schema(connection: sqlite3.Connection) -> None:
        """Crée le journal v6 sans réécriture des tables existantes."""
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS funding_ledger (
                event_key TEXT PRIMARY KEY,
                venue TEXT NOT NULL,
                instrument TEXT NOT NULL,
                funding_timestamp TEXT NOT NULL,
                native_funding_rate REAL NOT NULL,
                position_generation TEXT NOT NULL,
                funding_notional REAL NOT NULL CHECK(funding_notional >= 0),
                funding_notional_price REAL,
                funding_notional_price_source TEXT,
                funding_notional_price_timestamp TEXT,
                funding_pnl REAL NOT NULL,
                borrow_principal REAL NOT NULL CHECK(borrow_principal >= 0),
                borrow_rate_ann REAL NOT NULL CHECK(borrow_rate_ann >= 0),
                borrow_dt_seconds REAL NOT NULL CHECK(borrow_dt_seconds >= 0),
                borrow_cost REAL NOT NULL CHECK(borrow_cost >= 0),
                applied_at TEXT NOT NULL
            )
            """
        )
        columns = {
            item["name"]
            for item in connection.execute("PRAGMA table_info(funding_ledger)").fetchall()
        }
        for name, definition in (
            ("funding_notional_price", "REAL"),
            ("funding_notional_price_source", "TEXT"),
            ("funding_notional_price_timestamp", "TEXT"),
        ):
            if name not in columns:
                connection.execute(f"ALTER TABLE funding_ledger ADD COLUMN {name} {definition}")
        columns = {
            item["name"]
            for item in connection.execute("PRAGMA table_info(funding_ledger)").fetchall()
        }
        required = {
            "event_key",
            "venue",
            "instrument",
            "funding_timestamp",
            "native_funding_rate",
            "position_generation",
            "funding_notional",
            "funding_notional_price",
            "funding_notional_price_source",
            "funding_notional_price_timestamp",
            "funding_pnl",
            "borrow_principal",
            "borrow_rate_ann",
            "borrow_dt_seconds",
            "borrow_cost",
            "applied_at",
        }
        columns = {
            item["name"]
            for item in connection.execute("PRAGMA table_info(funding_ledger)").fetchall()
        }
        if not required <= columns:
            raise RuntimeError(f"Migration v6 incomplète : {sorted(required - columns)}")
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_funding_ledger_instrument_ts
            ON funding_ledger(venue, instrument, funding_timestamp)
            """
        )

    @classmethod
    def _migrate_v6(cls, connection: sqlite3.Connection) -> None:
        """Migration additive, idempotente, de v5 vers le ledger funding v6."""
        cls._ensure_funding_accounting_schema(connection)

    @staticmethod
    def _ensure_external_evidence_schema(
        connection: sqlite3.Connection, *, require_status_event_at: bool = False
    ) -> None:
        """Crée les preuves externes sans modifier les ordres existants."""
        connection.executescript(
            """
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS external_order_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                local_order_id INTEGER NOT NULL,
                intent_id TEXT NOT NULL,
                venue TEXT NOT NULL,
                account_scope TEXT NOT NULL,
                instrument TEXT NOT NULL,
                side TEXT NOT NULL CHECK(side IN ('BUY', 'SELL')),
                source_kind TEXT NOT NULL CHECK(source_kind IN (
                    'ORDER_LOOKUP', 'OPEN_ORDERS', 'HISTORICAL_ORDERS',
                    'FILL_LOOKUP', 'PRIVATE_EVENT', 'SUBMISSION_RESPONSE'
                )),
                external_state TEXT NOT NULL CHECK(external_state IN (
                    'OPEN', 'PARTIAL_OPEN', 'FILLED', 'PARTIAL_TERMINAL',
                    'CANCELED', 'REJECTED', 'EXPIRED', 'UNKNOWN'
                )),
                client_order_id TEXT,
                external_order_id TEXT,
                requested_qty REAL NOT NULL CHECK(requested_qty > 0),
                cumulative_filled_qty REAL CHECK(
                    cumulative_filled_qty IS NULL OR cumulative_filled_qty >= 0
                ),
                remaining_qty REAL CHECK(remaining_qty IS NULL OR remaining_qty >= 0),
                venue_event_at TEXT,
                status_event_at TEXT,
                observed_at TEXT NOT NULL,
                persisted_at TEXT NOT NULL,
                observation_key TEXT NOT NULL UNIQUE,
                raw_payload_hash TEXT NOT NULL,
                FOREIGN KEY(local_order_id) REFERENCES orders(id)
            );
            CREATE INDEX IF NOT EXISTS idx_external_order_observations_order_id
                ON external_order_observations(local_order_id, id);
            CREATE INDEX IF NOT EXISTS idx_external_order_observations_venue_order
                ON external_order_observations(venue, account_scope, external_order_id, id);

            CREATE TABLE IF NOT EXISTS external_fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                local_order_id INTEGER NOT NULL,
                intent_id TEXT NOT NULL,
                venue TEXT NOT NULL,
                account_scope TEXT NOT NULL,
                instrument TEXT NOT NULL,
                side TEXT NOT NULL CHECK(side IN ('BUY', 'SELL')),
                source_kind TEXT NOT NULL CHECK(source_kind IN (
                    'ORDER_LOOKUP', 'OPEN_ORDERS', 'HISTORICAL_ORDERS',
                    'FILL_LOOKUP', 'PRIVATE_EVENT', 'SUBMISSION_RESPONSE'
                )),
                client_order_id TEXT,
                external_order_id TEXT,
                venue_fill_id TEXT,
                quantity REAL NOT NULL CHECK(quantity > 0),
                price REAL NOT NULL CHECK(price > 0),
                fee REAL,
                fee_asset TEXT,
                venue_event_at TEXT,
                observed_at TEXT NOT NULL,
                persisted_at TEXT NOT NULL,
                fill_key TEXT NOT NULL UNIQUE,
                raw_payload_hash TEXT NOT NULL,
                FOREIGN KEY(local_order_id) REFERENCES orders(id)
            );
            CREATE INDEX IF NOT EXISTS idx_external_fills_order_id
                ON external_fills(local_order_id, id);
            CREATE INDEX IF NOT EXISTS idx_external_fills_venue_fill
                ON external_fills(venue, account_scope, venue_fill_id);
            """
        )
        required_observation = {
            "id",
            "local_order_id",
            "intent_id",
            "venue",
            "account_scope",
            "instrument",
            "side",
            "source_kind",
            "external_state",
            "client_order_id",
            "external_order_id",
            "requested_qty",
            "cumulative_filled_qty",
            "remaining_qty",
            "venue_event_at",
            "observed_at",
            "persisted_at",
            "observation_key",
            "raw_payload_hash",
        }
        required_fill = {
            "id",
            "local_order_id",
            "intent_id",
            "venue",
            "account_scope",
            "instrument",
            "side",
            "source_kind",
            "client_order_id",
            "external_order_id",
            "venue_fill_id",
            "quantity",
            "price",
            "fee",
            "fee_asset",
            "venue_event_at",
            "observed_at",
            "persisted_at",
            "fill_key",
            "raw_payload_hash",
        }
        if require_status_event_at:
            required_observation.add("status_event_at")
        observed_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(external_order_observations)"
            ).fetchall()
        }
        fill_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(external_fills)").fetchall()
        }
        if not required_observation <= observed_columns:
            raise RuntimeError(
                "Schéma de preuves incomplet pour external_order_observations: "
                f"{sorted(required_observation - observed_columns)}"
            )
        if not required_fill <= fill_columns:
            raise RuntimeError(
                "Schéma de preuves incomplet pour external_fills: "
                f"{sorted(required_fill - fill_columns)}"
            )

    @classmethod
    def _migrate_v7(cls, connection: sqlite3.Connection) -> None:
        """Migration additive, idempotente, de v6 vers les preuves externes."""
        cls._ensure_external_evidence_schema(connection)

    @classmethod
    def _migrate_v8(cls, connection: sqlite3.Connection) -> None:
        """Reconstruit external_fills pour autoriser les fees signées."""

        source_columns = [
            "id",
            "local_order_id",
            "intent_id",
            "venue",
            "account_scope",
            "instrument",
            "side",
            "source_kind",
            "client_order_id",
            "external_order_id",
            "venue_fill_id",
            "quantity",
            "price",
            "fee",
            "fee_asset",
            "venue_event_at",
            "observed_at",
            "persisted_at",
            "fill_key",
            "raw_payload_hash",
        ]
        source_count = connection.execute("SELECT COUNT(*) FROM external_fills").fetchone()[0]
        sequence_row = connection.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'external_fills'"
        ).fetchone()
        connection.execute(
            """
            CREATE TABLE external_fills_v8 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                local_order_id INTEGER NOT NULL,
                intent_id TEXT NOT NULL,
                venue TEXT NOT NULL,
                account_scope TEXT NOT NULL,
                instrument TEXT NOT NULL,
                side TEXT NOT NULL CHECK(side IN ('BUY', 'SELL')),
                source_kind TEXT NOT NULL CHECK(source_kind IN (
                    'ORDER_LOOKUP', 'OPEN_ORDERS', 'HISTORICAL_ORDERS',
                    'FILL_LOOKUP', 'PRIVATE_EVENT', 'SUBMISSION_RESPONSE'
                )),
                client_order_id TEXT,
                external_order_id TEXT,
                venue_fill_id TEXT,
                quantity REAL NOT NULL CHECK(quantity > 0),
                price REAL NOT NULL CHECK(price > 0),
                fee REAL,
                fee_asset TEXT,
                venue_event_at TEXT,
                observed_at TEXT NOT NULL,
                persisted_at TEXT NOT NULL,
                fill_key TEXT NOT NULL UNIQUE,
                raw_payload_hash TEXT NOT NULL,
                FOREIGN KEY(local_order_id) REFERENCES orders(id)
            )
            """
        )
        columns_sql = ", ".join(source_columns)
        connection.execute(
            f"INSERT INTO external_fills_v8 ({columns_sql}) "
            f"SELECT {columns_sql} FROM external_fills ORDER BY id"
        )
        copied_count = connection.execute("SELECT COUNT(*) FROM external_fills_v8").fetchone()[0]
        if copied_count != source_count:
            raise RuntimeError(
                "Migration v8: le nombre de fills copiés ne correspond pas à la source"
            )
        connection.execute("DROP TABLE external_fills")
        connection.execute("ALTER TABLE external_fills_v8 RENAME TO external_fills")
        connection.execute(
            """
            CREATE INDEX idx_external_fills_order_id
                ON external_fills(local_order_id, id)
            """
        )
        connection.execute(
            """
            CREATE INDEX idx_external_fills_venue_fill
                ON external_fills(venue, account_scope, venue_fill_id)
            """
        )
        if sequence_row is not None:
            connection.execute(
                "UPDATE sqlite_sequence SET seq = ? WHERE name = 'external_fills'",
                (sequence_row[0],),
            )

    @staticmethod
    def _ensure_financial_application_plan_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS financial_application_plans (
                local_order_id INTEGER PRIMARY KEY,
                intent_id TEXT NOT NULL UNIQUE,
                plan_key TEXT NOT NULL UNIQUE,
                application_version INTEGER NOT NULL,
                logical_order_key TEXT NOT NULL,
                engine TEXT NOT NULL,
                slot TEXT NOT NULL,
                transition_type TEXT NOT NULL,
                decision_checkpoint TEXT NOT NULL,
                position_generation TEXT,
                transition_sequence INTEGER NOT NULL,
                side TEXT NOT NULL CHECK(side IN ('BUY', 'SELL')),
                requested_qty REAL NOT NULL CHECK(requested_qty > 0),
                reference_price REAL NOT NULL CHECK(reference_price > 0),
                reason TEXT NOT NULL,
                reduce_only INTEGER NOT NULL CHECK(reduce_only IN (0, 1)),
                planned_effect_at TEXT NOT NULL,
                entry_direction INTEGER CHECK(entry_direction IN (-1, 1) OR entry_direction IS NULL),
                entry_stop_price REAL CHECK(entry_stop_price IS NULL OR entry_stop_price > 0),
                pre_state_payload TEXT NOT NULL CHECK(json_valid(pre_state_payload)),
                pre_state_sha256 TEXT NOT NULL,
                protection_mode TEXT NOT NULL CHECK(protection_mode IN ('SOFTWARE', 'EXCHANGE')),
                created_at TEXT NOT NULL,
                FOREIGN KEY(local_order_id) REFERENCES orders(id)
            )
            """
        )

    @classmethod
    def _migrate_v9(cls, connection: sqlite3.Connection) -> None:
        """Migration additive v8 vers le plan financier durable v1."""
        cls._ensure_financial_application_plan_schema(connection)

    @classmethod
    def _migrate_v11(cls, connection: sqlite3.Connection) -> None:
        """Add the venue status transition timestamp to schema v11."""
        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(external_order_observations)"
            ).fetchall()
        }
        if "status_event_at" not in columns:
            connection.execute(
                "ALTER TABLE external_order_observations ADD COLUMN status_event_at TEXT"
            )
        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(external_order_observations)"
            ).fetchall()
        }
        if "status_event_at" not in columns:
            raise RuntimeError("Migration v11 incomplete: status_event_at absent")

    @staticmethod
    def _ensure_external_order_settlement_schema(connection: sqlite3.Connection) -> None:
        """Create and validate the v12 terminal settlement evidence table."""

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS external_order_settlements (
                settlement_key TEXT PRIMARY KEY,
                settlement_version INTEGER NOT NULL CHECK(settlement_version = 1),
                local_order_id INTEGER NOT NULL,
                intent_id TEXT NOT NULL,
                venue TEXT NOT NULL,
                environment TEXT NOT NULL,
                account_scope TEXT NOT NULL,
                instrument TEXT NOT NULL,
                side TEXT NOT NULL CHECK(side IN ('BUY', 'SELL')),
                client_order_id TEXT NOT NULL,
                external_order_id TEXT NOT NULL,
                terminal_status TEXT NOT NULL CHECK(terminal_status IN (
                    'FILLED', 'PARTIAL_TERMINAL', 'CANCELED', 'REJECTED', 'EXPIRED'
                )),
                terminal_status_event_at TEXT NOT NULL,
                completeness_version INTEGER NOT NULL CHECK(completeness_version = 1),
                completeness_payload TEXT NOT NULL CHECK(json_valid(completeness_payload)),
                raw_fill_count INTEGER NOT NULL CHECK(raw_fill_count >= 0),
                fill_multiset_sha256 TEXT NOT NULL,
                settlement_payload TEXT NOT NULL CHECK(json_valid(settlement_payload)),
                observed_at TEXT NOT NULL,
                persisted_at TEXT NOT NULL,
                FOREIGN KEY(local_order_id) REFERENCES orders(id)
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_external_order_settlements_order_id
                ON external_order_settlements(local_order_id, persisted_at, settlement_key)
            """
        )
        required = {
            "settlement_key",
            "settlement_version",
            "local_order_id",
            "intent_id",
            "venue",
            "environment",
            "account_scope",
            "instrument",
            "side",
            "client_order_id",
            "external_order_id",
            "terminal_status",
            "terminal_status_event_at",
            "completeness_version",
            "completeness_payload",
            "raw_fill_count",
            "fill_multiset_sha256",
            "settlement_payload",
            "observed_at",
            "persisted_at",
        }
        table_info = connection.execute("PRAGMA table_info(external_order_settlements)").fetchall()
        columns = {str(row["name"]) for row in table_info}
        if not required <= columns:
            raise RuntimeError(
                f"Incomplete v12 external_order_settlements schema: {sorted(required - columns)}"
            )
        primary_key = [str(row["name"]) for row in table_info if int(row["pk"]) > 0]
        if primary_key != ["settlement_key"]:
            raise RuntimeError("Invalid v12 external_order_settlements primary key")
        indexes = connection.execute("PRAGMA index_list(external_order_settlements)").fetchall()
        order_index_valid = False
        for index in indexes:
            if str(index["name"]) != "idx_external_order_settlements_order_id":
                continue
            index_columns = tuple(
                str(item["name"])
                for item in connection.execute(
                    f"PRAGMA index_info({str(index['name'])!r})"
                ).fetchall()
                if item["name"] is not None
            )
            order_index_valid = (
                index_columns
                == (
                    "local_order_id",
                    "persisted_at",
                    "settlement_key",
                )
                and int(index["unique"]) == 0
            )
        if not order_index_valid:
            raise RuntimeError("Invalid v12 external_order_settlements order index")
        foreign_keys = {
            (str(row["from"]), str(row["table"]), str(row["to"]))
            for row in connection.execute(
                "PRAGMA foreign_key_list(external_order_settlements)"
            ).fetchall()
        }
        if ("local_order_id", "orders", "id") not in foreign_keys:
            raise RuntimeError("Invalid v12 external_order_settlements foreign key")

    @classmethod
    def _migrate_v12(cls, connection: sqlite3.Connection) -> None:
        """Migration additive v11 to durable terminal settlement evidence v12."""

        cls._ensure_external_order_settlement_schema(connection)

    @classmethod
    def _migrate_v13(cls, connection: sqlite3.Connection) -> None:
        """Migration additive v12 to the atomic external settlement ledger."""

        cls._ensure_financial_settlement_application_schema(connection)

    @staticmethod
    def _ensure_financial_settlement_application_schema(
        connection: sqlite3.Connection,
    ) -> None:
        """Create and validate the v13 external settlement application ledger."""

        table_exists = (
            connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='financial_settlement_applications'"
            ).fetchone()
            is not None
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS financial_settlement_applications (
                application_key TEXT PRIMARY KEY,
                application_version INTEGER NOT NULL CHECK(application_version = 1),
                settlement_key TEXT NOT NULL,
                local_order_id INTEGER NOT NULL,
                intent_id TEXT NOT NULL,
                plan_key TEXT NOT NULL,
                state_before_sha256 TEXT NOT NULL,
                state_after_sha256 TEXT NOT NULL,
                result_payload TEXT NOT NULL CHECK(json_valid(result_payload)),
                result_sha256 TEXT NOT NULL,
                applied_at TEXT NOT NULL,
                UNIQUE(local_order_id, plan_key, application_version),
                UNIQUE(local_order_id, settlement_key, application_version),
                FOREIGN KEY(local_order_id) REFERENCES orders(id),
                FOREIGN KEY(settlement_key) REFERENCES external_order_settlements(settlement_key),
                FOREIGN KEY(plan_key) REFERENCES financial_application_plans(plan_key)
            )
            """
        )
        if not table_exists:
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_financial_settlement_applications_order_id
                    ON financial_settlement_applications(local_order_id, applied_at, application_key)
                """
            )

        required = {
            "application_key",
            "application_version",
            "settlement_key",
            "local_order_id",
            "intent_id",
            "plan_key",
            "state_before_sha256",
            "state_after_sha256",
            "result_payload",
            "result_sha256",
            "applied_at",
        }
        table_info = connection.execute(
            "PRAGMA table_info(financial_settlement_applications)"
        ).fetchall()
        columns = {str(row["name"]) for row in table_info}
        if not required <= columns:
            raise RuntimeError(
                "Incomplete v13 financial_settlement_applications schema: "
                f"{sorted(required - columns)}"
            )
        primary_key = [str(row["name"]) for row in table_info if int(row["pk"]) > 0]
        if primary_key != ["application_key"]:
            raise RuntimeError("Invalid v13 financial_settlement_applications primary key")

        index_rows = connection.execute(
            "PRAGMA index_list(financial_settlement_applications)"
        ).fetchall()
        unique_signatures: set[tuple[str, ...]] = set()
        order_index_valid = False
        for index in index_rows:
            index_name = str(index["name"])
            index_columns = tuple(
                str(item["name"])
                for item in connection.execute(f"PRAGMA index_info({index_name!r})").fetchall()
                if item["name"] is not None
            )
            if int(index["unique"]) == 1 and int(index["partial"]) == 0:
                unique_signatures.add(index_columns)
            if (
                index_name == "idx_financial_settlement_applications_order_id"
                and int(index["unique"]) == 0
                and int(index["partial"]) == 0
                and index_columns == ("local_order_id", "applied_at", "application_key")
            ):
                order_index_valid = True
        if {
            ("local_order_id", "plan_key", "application_version"),
            ("local_order_id", "settlement_key", "application_version"),
        } - unique_signatures:
            raise RuntimeError("Invalid v13 financial settlement unique constraints")
        if not order_index_valid:
            raise RuntimeError("Invalid v13 financial settlement order index")

        foreign_keys = {
            (str(row["from"]), str(row["table"]), str(row["to"]))
            for row in connection.execute(
                "PRAGMA foreign_key_list(financial_settlement_applications)"
            ).fetchall()
        }
        required_foreign_keys = {
            ("local_order_id", "orders", "id"),
            ("settlement_key", "external_order_settlements", "settlement_key"),
            ("plan_key", "financial_application_plans", "plan_key"),
        }
        if not required_foreign_keys <= foreign_keys:
            raise RuntimeError("Invalid v13 financial settlement foreign keys")

    @staticmethod
    def _ensure_financial_fill_application_schema(connection: sqlite3.Connection) -> None:
        """Create or validate the v10 ledger without rebuilding prior tables."""

        table_exists = (
            connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='financial_fill_applications'"
            ).fetchone()
            is not None
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS financial_fill_applications (
                application_key TEXT PRIMARY KEY,
                application_version INTEGER NOT NULL,
                local_order_id INTEGER NOT NULL,
                intent_id TEXT NOT NULL,
                plan_key TEXT NOT NULL,
                fill_key TEXT NOT NULL,
                application_index INTEGER NOT NULL CHECK(application_index >= 0),
                previous_application_key TEXT,
                transition_type TEXT NOT NULL,
                economic_effect_at TEXT NOT NULL,
                state_before_sha256 TEXT NOT NULL,
                state_after_sha256 TEXT NOT NULL,
                result_payload TEXT NOT NULL CHECK(json_valid(result_payload)),
                result_sha256 TEXT NOT NULL,
                applied_at TEXT NOT NULL,
                UNIQUE(local_order_id, fill_key, application_version),
                UNIQUE(local_order_id, application_index),
                FOREIGN KEY(local_order_id) REFERENCES orders(id),
                FOREIGN KEY(plan_key) REFERENCES financial_application_plans(plan_key),
                FOREIGN KEY(fill_key) REFERENCES external_fills(fill_key),
                FOREIGN KEY(previous_application_key)
                    REFERENCES financial_fill_applications(application_key)
            )
            """
        )
        if not table_exists:
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_financial_fill_applications_previous
                ON financial_fill_applications(previous_application_key)
                WHERE previous_application_key IS NOT NULL
                """
            )

        required = {
            "application_key",
            "application_version",
            "local_order_id",
            "intent_id",
            "plan_key",
            "fill_key",
            "application_index",
            "previous_application_key",
            "transition_type",
            "economic_effect_at",
            "state_before_sha256",
            "state_after_sha256",
            "result_payload",
            "result_sha256",
            "applied_at",
        }
        table_info = connection.execute("PRAGMA table_info(financial_fill_applications)").fetchall()
        columns = {row["name"] for row in table_info}
        if not required <= columns:
            raise RuntimeError(
                f"Incomplete v10 financial_fill_applications schema: {sorted(required - columns)}"
            )
        primary_key = [row["name"] for row in table_info if int(row["pk"]) > 0]
        if primary_key != ["application_key"]:
            raise RuntimeError("Invalid v10 financial_fill_applications primary key")

        index_rows = connection.execute("PRAGMA index_list(financial_fill_applications)").fetchall()
        unique_signatures: set[tuple[str, ...]] = set()
        previous_index_valid = False
        for index in index_rows:
            index_name = str(index["name"])
            index_columns = tuple(
                str(item["name"])
                for item in connection.execute(f"PRAGMA index_info({index_name!r})").fetchall()
                if item["name"] is not None
            )
            is_unique = int(index["unique"]) == 1
            is_partial = int(index["partial"]) == 1
            if is_unique and not is_partial:
                unique_signatures.add(index_columns)
            if (
                index_name == "idx_financial_fill_applications_previous"
                and is_unique
                and is_partial
                and index_columns == ("previous_application_key",)
            ):
                index_sql_row = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='index' AND name = ?",
                    (index_name,),
                ).fetchone()
                index_sql = "" if index_sql_row is None else str(index_sql_row["sql"] or "")
                normalized_sql = " ".join(index_sql.casefold().split()).rstrip(";").rstrip()
                previous_index_valid = normalized_sql.endswith(
                    " where previous_application_key is not null"
                )
        if {
            ("local_order_id", "fill_key", "application_version"),
            ("local_order_id", "application_index"),
        } - unique_signatures:
            raise RuntimeError("Invalid v10 financial_fill_applications unique constraints")
        if not previous_index_valid:
            raise RuntimeError("Invalid v10 financial_fill_applications previous index predicate")

        foreign_keys = {
            (str(row["from"]), str(row["table"]), str(row["to"]))
            for row in connection.execute(
                "PRAGMA foreign_key_list(financial_fill_applications)"
            ).fetchall()
        }
        required_foreign_keys = {
            ("local_order_id", "orders", "id"),
            ("plan_key", "financial_application_plans", "plan_key"),
            ("fill_key", "external_fills", "fill_key"),
            (
                "previous_application_key",
                "financial_fill_applications",
                "application_key",
            ),
        }
        if not required_foreign_keys <= foreign_keys:
            raise RuntimeError("Invalid v10 financial_fill_applications foreign keys")

    @classmethod
    def _migrate_v10(cls, connection: sqlite3.Connection) -> None:
        """Migration additive v9 to the financial application ledger."""
        cls._ensure_financial_fill_application_schema(connection)

    @staticmethod
    def _json(payload: Any) -> str:
        """Sérialise un checkpoint, en nommant le champ fautif s'il échoue.

        Un `TypeError: Object of type bool is not JSON serializable` — le
        message que produit `numpy.bool_` — n'indique ni la clé ni le moteur
        concernés. Comme cet échec fait échouer toute la transaction de
        checkpoint, il faut qu'il soit diagnosticable du premier coup.
        """

        try:
            return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        except TypeError as error:
            culprits = sorted(_unserializable_paths(payload))
            raise TypeError(
                f"Checkpoint non sérialisable ({error}) ; champs en cause : {culprits}"
            ) from error

    @classmethod
    def _state_event(
        cls,
        state: Mapping[str, Any],
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        canonical = json.dumps(
            state,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return {
            **(metadata or {}),
            "state": state,
            "state_sha256": hashlib.sha256(canonical).hexdigest(),
        }

    def load_engine_state(self, engine: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM engine_state WHERE engine = ?", (engine,)
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    @staticmethod
    def _validate_deposit(deposit_id: str, amount: float) -> tuple[str, float]:
        normalized_id = deposit_id.strip()
        if not DEPOSIT_ID_PATTERN.fullmatch(normalized_id):
            raise ValueError(
                "Identifiant d'apport invalide : utiliser 1 à 128 lettres, chiffres, "
                "points, deux-points, tirets ou underscores"
            )
        normalized_amount = float(amount)
        if not math.isfinite(normalized_amount) or normalized_amount <= 0:
            raise ValueError("Montant d'apport invalide : nombre fini strictement positif requis")
        return normalized_id, normalized_amount

    def register_deposit(self, deposit_id: str, amount: float) -> tuple[dict[str, Any], bool]:
        """Enregistre une demande une seule fois et retourne ``(dépôt, créé)``."""

        normalized_id, normalized_amount = self._validate_deposit(deposit_id, amount)
        now = utc_now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM capital_deposits WHERE deposit_id = ?",
                (normalized_id,),
            ).fetchone()
            if row is not None:
                existing = dict(row)
                if not math.isclose(
                    float(existing["amount"]),
                    normalized_amount,
                    rel_tol=0.0,
                    abs_tol=1e-9,
                ):
                    raise ValueError(
                        f"L'apport {normalized_id!r} existe déjà avec un autre montant"
                    )
                return existing, False
            connection.execute(
                """
                INSERT INTO capital_deposits(
                    deposit_id, amount, status, requested_at, applied_at
                ) VALUES(?, ?, 'PENDING', ?, NULL)
                """,
                (normalized_id, normalized_amount, now),
            )
            payload = {
                "deposit_id": normalized_id,
                "amount": normalized_amount,
                "status": "PENDING",
                "requested_at": now,
                "applied_at": None,
            }
            self._insert_event(
                connection,
                "portfolio",
                "capital_deposit_requested",
                payload,
                "deposit",
                normalized_id,
            )
        return payload, True

    def read_deposits(self, *, status: str | None = None) -> list[dict[str, Any]]:
        if status not in (None, "PENDING", "APPLIED"):
            raise ValueError(f"Statut d'apport invalide : {status!r}")
        with self._connect() as connection:
            if status is None:
                rows = connection.execute(
                    "SELECT * FROM capital_deposits ORDER BY requested_at, deposit_id"
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM capital_deposits
                    WHERE status = ? ORDER BY requested_at, deposit_id
                    """,
                    (status,),
                ).fetchall()
        return [dict(row) for row in rows]

    def replay_engine_state(self, engine: str) -> dict[str, Any] | None:
        """Reconstruit le dernier checkpoint uniquement depuis le journal."""

        replayed: dict[str, Any] | None = None
        for event in self.read_events(engine):
            payload = json.loads(event["payload"])
            if not isinstance(payload, dict) or "state" not in payload:
                continue
            state = payload["state"]
            if not isinstance(state, dict):
                raise RuntimeError(f"Événement d'état invalide #{event['id']}")
            expected = self._state_event(state)["state_sha256"]
            if payload.get("state_sha256") != expected:
                raise RuntimeError(f"Hash d'état invalide dans l'événement #{event['id']}")
            replayed = state
        return replayed

    def migrate_legacy_json(self, engine: str, legacy_path: str | Path) -> bool:
        path = Path(legacy_path)
        if (
            path.suffix.lower() != ".json"
            or self.load_engine_state(engine) is not None
            or not path.exists()
        ):
            return False
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.save_engine_state(
            engine,
            payload,
            event_type="legacy_json_migrated",
            event_payload={"source": path.name},
        )
        return True

    def migrate_legacy_journals(self, state_dir: str | Path) -> dict[str, int]:
        """Importe une fois les historiques CSV existants.

        Les fichiers sont conservés comme sauvegarde froide. Une table non vide
        n'est jamais réimportée, ce qui rend la migration idempotente.
        """

        root = Path(state_dir)
        equity_rows: dict[str, list[tuple[str, float]]] = {}
        for engine, filename in (
            ("trend", "equity_trend.csv"),
            ("carry", "equity_carry.csv"),
        ):
            rows: list[tuple[str, float]] = []
            path = root / filename
            if path.exists():
                with path.open(encoding="utf-8", errors="replace", newline="") as stream:
                    for row in csv.DictReader(stream):
                        try:
                            rows.append((row["ts"], float(row["equity"])))
                        except (KeyError, TypeError, ValueError):
                            continue
            equity_rows[engine] = rows

        trade_rows: list[dict[str, Any]] = []
        trades_path = root / "trades.csv"
        if trades_path.exists():
            with trades_path.open(encoding="utf-8", errors="replace", newline="") as stream:
                for row in csv.DictReader(stream):
                    try:
                        trade_rows.append(
                            {
                                **row,
                                "qty": float(row["qty"]),
                                "entry_price": float(row["entry_price"]),
                                "exit_price": float(row["exit_price"]),
                                "pnl": float(row["pnl"]),
                                "bars_held": int(row["bars_held"]),
                            }
                        )
                    except (KeyError, TypeError, ValueError):
                        continue

        flow_rows: list[dict[str, Any]] = []
        flows_path = root / "flows.csv"
        if flows_path.exists():
            with flows_path.open(encoding="utf-8", errors="replace", newline="") as stream:
                for row in csv.DictReader(stream):
                    try:
                        flow_rows.append(
                            {
                                **row,
                                "trend_flow": float(row["trend_flow"]),
                                "carry_flow": float(row["carry_flow"]),
                            }
                        )
                    except (KeyError, TypeError, ValueError):
                        continue

        imported = {"equity": 0, "trades": 0, "flows": 0}
        with self._transaction() as connection:
            for engine, rows in equity_rows.items():
                equity_count = connection.execute(
                    "SELECT COUNT(*) FROM equity_samples WHERE engine = ?",
                    (engine,),
                ).fetchone()[0]
                if equity_count == 0:
                    connection.executemany(
                        """
                        INSERT OR IGNORE INTO equity_samples(engine, ts, equity)
                        VALUES(?, ?, ?)
                        """,
                        ((engine, ts, equity) for ts, equity in rows),
                    )
                    imported["equity"] += len(rows)

            trade_count = connection.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
            if trade_count == 0 and trade_rows:
                connection.executemany(
                    """
                    INSERT INTO trades(
                        exit_ts, entry_ts, strategy, direction, qty, entry_price,
                        exit_price, pnl, bars_held, reason
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        (
                            row["exit_ts"],
                            row["entry_ts"],
                            row["strategy"],
                            row["direction"],
                            row["qty"],
                            row["entry_price"],
                            row["exit_price"],
                            row["pnl"],
                            row["bars_held"],
                            row["reason"],
                        )
                        for row in trade_rows
                    ),
                )
                imported["trades"] = len(trade_rows)

            flow_count = connection.execute("SELECT COUNT(*) FROM flows").fetchone()[0]
            if flow_count == 0 and flow_rows:
                connection.executemany(
                    """
                    INSERT INTO flows(ts, kind, trend_flow, carry_flow)
                    VALUES(?, ?, ?, ?)
                    """,
                    (
                        (
                            row["ts"],
                            row["kind"],
                            row["trend_flow"],
                            row["carry_flow"],
                        )
                        for row in flow_rows
                    ),
                )
                imported["flows"] = len(flow_rows)

            if any(imported.values()):
                self._insert_event(
                    connection,
                    "portfolio",
                    "legacy_csv_migrated",
                    imported,
                )
        return imported

    def save_engine_state(
        self,
        engine: str,
        payload: Mapping[str, Any],
        *,
        event_type: str = "checkpoint",
        event_payload: dict[str, Any] | None = None,
        event_aggregate_type: str | None = None,
        event_aggregate_id: str | None = None,
    ) -> None:
        now = utc_now()
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO engine_state(engine, payload, updated_at) VALUES(?, ?, ?)
                ON CONFLICT(engine) DO UPDATE SET
                    payload=excluded.payload, updated_at=excluded.updated_at
                """,
                (engine, self._json(payload), now),
            )
            self._sync_positions(connection, engine, payload, now)
            self._insert_event(
                connection,
                engine,
                event_type,
                self._state_event(payload, event_payload),
                aggregate_type=event_aggregate_type or "engine",
                aggregate_id=event_aggregate_id or engine,
            )

    def _insert_funding_ledger(
        self,
        connection: sqlite3.Connection,
        ledger: Mapping[str, Any],
    ) -> None:
        connection.execute(
            """
            INSERT INTO funding_ledger(
                event_key, venue, instrument, funding_timestamp,
                native_funding_rate, position_generation, funding_notional,
                funding_notional_price, funding_notional_price_source,
                funding_notional_price_timestamp,
                funding_pnl, borrow_principal, borrow_rate_ann,
                borrow_dt_seconds, borrow_cost, applied_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ledger["event_key"],
                ledger["venue"],
                ledger["instrument"],
                ledger["funding_timestamp"],
                ledger["native_funding_rate"],
                ledger["position_generation"],
                ledger["funding_notional"],
                ledger["funding_notional_price"],
                ledger["funding_notional_price_source"],
                ledger["funding_notional_price_timestamp"],
                ledger["funding_pnl"],
                ledger["borrow_principal"],
                ledger["borrow_rate_ann"],
                ledger["borrow_dt_seconds"],
                ledger["borrow_cost"],
                ledger["applied_at"],
            ),
        )

    def read_funding_ledger(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM funding_ledger ORDER BY funding_timestamp"
            ).fetchall()
        return [dict(row) for row in rows]

    def apply_carry_accounting_event_and_checkpoint(
        self,
        ledger: Mapping[str, Any],
        state: Mapping[str, Any],
        *,
        event_payload: dict[str, Any] | None = None,
        engine: str = "carry",
    ) -> str:
        required = {
            "event_key",
            "venue",
            "instrument",
            "funding_timestamp",
            "native_funding_rate",
            "position_generation",
            "funding_notional",
            "funding_notional_price",
            "funding_notional_price_source",
            "funding_notional_price_timestamp",
            "funding_pnl",
            "borrow_principal",
            "borrow_rate_ann",
            "borrow_dt_seconds",
            "borrow_cost",
            "applied_at",
        }
        missing = required - ledger.keys()
        if missing:
            raise ValueError(f"Événement funding incomplet : {sorted(missing)}")
        event_key = str(ledger["event_key"])
        if not event_key:
            raise ValueError("event_key funding vide")
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM funding_ledger WHERE event_key = ?",
                (event_key,),
            ).fetchone()
            if existing is not None:
                identity_fields = (
                    "venue",
                    "instrument",
                    "funding_notional_price_source",
                    "funding_notional_price_timestamp",
                    "funding_timestamp",
                    "native_funding_rate",
                    "position_generation",
                )
                numeric_fields = (
                    "funding_notional",
                    "funding_notional_price",
                    "funding_pnl",
                    "borrow_principal",
                    "borrow_rate_ann",
                    "borrow_dt_seconds",
                    "borrow_cost",
                )
                for field in identity_fields:
                    if str(existing[field]) != str(ledger[field]):
                        raise AccountingIdentityCollision(event_key, field)
                for field in numeric_fields:
                    existing_value = existing[field]
                    ledger_value = ledger[field]
                    if existing_value is None and ledger_value is None:
                        continue
                    if (
                        existing_value is None
                        or ledger_value is None
                        or not math.isclose(
                            float(existing_value), float(ledger_value), rel_tol=0.0, abs_tol=1e-15
                        )
                    ):
                        raise AccountingIdentityCollision(event_key, field)
                return "replayed"
            numeric_validation_fields = (
                "native_funding_rate",
                "funding_notional",
                "funding_pnl",
                "borrow_principal",
                "borrow_rate_ann",
                "borrow_dt_seconds",
                "borrow_cost",
            )
            for field in numeric_validation_fields:
                value = float(ledger[field])
                if not math.isfinite(value):
                    raise ValueError(f"{field} funding doit être fini")
            price = ledger["funding_notional_price"]
            if price is not None and (not math.isfinite(float(price)) or float(price) <= 0):
                raise ValueError("funding_notional_price doit être fini et positif")
            self._insert_funding_ledger(connection, ledger)
            now = str(ledger.get("applied_at") or utc_now())
            connection.execute(
                """
                INSERT INTO engine_state(engine, payload, updated_at) VALUES(?, ?, ?)
                ON CONFLICT(engine) DO UPDATE SET
                    payload=excluded.payload, updated_at=excluded.updated_at
                """,
                (engine, self._json(state), now),
            )
            self._sync_positions(connection, engine, state, now)
            self._insert_event(
                connection,
                engine,
                "funding_payment",
                self._state_event(state, event_payload),
                aggregate_type="funding_event",
                aggregate_id=event_key,
            )
            return "applied"

    def has_event(
        self,
        engine: str,
        event_type: str,
        aggregate_id: str,
    ) -> bool:
        """Recherche idempotente d'un événement métier déjà journalisé."""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM events
                WHERE engine = ? AND event_type = ? AND aggregate_id = ?
                LIMIT 1
                """,
                (engine, event_type, aggregate_id),
            ).fetchone()
        return row is not None

    def save_states_and_flows(
        self,
        states: dict[str, dict[str, Any]],
        flows: list[dict[str, Any]],
        *,
        applied_deposit_ids: Sequence[str] = (),
    ) -> None:
        """Checkpoint des moteurs, flux et apports appliqués atomiquement."""

        now = utc_now()
        with self._transaction() as connection:
            for engine, payload in states.items():
                connection.execute(
                    """
                    INSERT INTO engine_state(engine, payload, updated_at) VALUES(?, ?, ?)
                    ON CONFLICT(engine) DO UPDATE SET
                        payload=excluded.payload, updated_at=excluded.updated_at
                    """,
                    (engine, self._json(payload), now),
                )
                self._sync_positions(connection, engine, payload, now)
                self._insert_event(
                    connection,
                    engine,
                    "state_checkpoint",
                    self._state_event(payload),
                    "engine",
                    engine,
                )
            for flow in flows:
                connection.execute(
                    """
                    INSERT INTO flows(ts, kind, trend_flow, carry_flow)
                    VALUES(?, ?, ?, ?)
                    """,
                    (
                        now,
                        flow["kind"],
                        flow["trend_flow"],
                        flow["carry_flow"],
                    ),
                )
                self._insert_event(
                    connection,
                    "portfolio",
                    "capital_flow",
                    flow,
                )
            for deposit_id in applied_deposit_ids:
                row = connection.execute(
                    """
                    SELECT amount, status FROM capital_deposits
                    WHERE deposit_id = ?
                    """,
                    (deposit_id,),
                ).fetchone()
                if row is None or row["status"] != "PENDING":
                    raise RuntimeError(
                        f"Apport {deposit_id!r} absent ou déjà appliqué pendant la transaction"
                    )
                connection.execute(
                    """
                    UPDATE capital_deposits
                    SET status = 'APPLIED', applied_at = ?
                    WHERE deposit_id = ?
                    """,
                    (now, deposit_id),
                )
                self._insert_event(
                    connection,
                    "portfolio",
                    "capital_deposit_applied",
                    {
                        "deposit_id": deposit_id,
                        "amount": float(row["amount"]),
                        "applied_at": now,
                    },
                    "deposit",
                    deposit_id,
                )

    def _sync_positions(
        self,
        connection: sqlite3.Connection,
        engine: str,
        payload: Mapping[str, Any],
        now: str,
    ) -> None:
        connection.execute("DELETE FROM positions WHERE engine = ?", (engine,))
        if engine == "trend":
            for slot, state in payload.get("slots", {}).items():
                position = state.get("position")
                connection.execute(
                    """
                    INSERT INTO positions(
                        engine, slot, status, cash, entry_time, entry_price, qty,
                        stop_price, direction, bars_held, best_close, stop_order_id,
                        entry_fee, last_bar_ts, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        engine,
                        slot,
                        "OPEN" if position else "FLAT",
                        state.get("cash"),
                        position.get("entry_time") if position else None,
                        position.get("entry_price") if position else None,
                        position.get("qty", 0.0) if position else 0.0,
                        position.get("stop_price") if position else None,
                        position.get("direction") if position else None,
                        position.get("bars_held", 0) if position else 0,
                        position.get("best_close") if position else None,
                        state.get("stop_order_id"),
                        state.get("entry_fee", 0.0),
                        state.get("last_bar_ts"),
                        now,
                    ),
                )
        elif engine == "carry":
            execution_state = payload.get(
                "execution_state",
                "OPEN" if payload.get("in_position") else "FLAT",
            )
            position_status = (
                execution_state if execution_state in ("FLAT", "OPEN", "UNBALANCED") else "OPEN"
            )
            connection.execute(
                """
                INSERT INTO positions(
                    engine, slot, status, cash, qty, updated_at
                ) VALUES(?, 'carry', ?, ?, ?, ?)
                """,
                (
                    engine,
                    position_status,
                    payload.get("equity"),
                    payload.get("qty", 0.0),
                    now,
                ),
            )

    def _insert_event(
        self,
        connection: sqlite3.Connection,
        engine: str,
        event_type: str,
        payload: dict[str, Any],
        aggregate_type: str | None = None,
        aggregate_id: str | None = None,
        correlation_id: str | None = None,
        ts: str | None = None,
    ) -> int:
        cursor = connection.execute(
            """
            INSERT INTO events(
                ts, engine, event_type, aggregate_type, aggregate_id,
                payload, correlation_id
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts or utc_now(),
                engine,
                event_type,
                aggregate_type,
                aggregate_id,
                self._json(payload),
                correlation_id,
            ),
        )
        assert cursor.lastrowid is not None
        return int(cursor.lastrowid)

    def append_external_order_lookup_attempt(
        self,
        *,
        engine: str,
        aggregate_id: str,
        payload: dict[str, Any],
        event_type: str,
    ) -> None:
        """Journalise une tentative de lookup sans modifier l'état métier."""

        if self.read_only:
            raise RuntimeError("Un StateStore read-only ne peut pas journaliser une tentative")
        normalized_engine = str(engine).strip()
        normalized_aggregate_id = str(aggregate_id).strip()
        if not normalized_engine or not normalized_aggregate_id:
            raise ValueError("engine et aggregate_id doivent être non vides")
        with self._transaction() as connection:
            self._append_external_order_lookup_attempt_in_transaction(
                connection,
                engine=normalized_engine,
                aggregate_id=normalized_aggregate_id,
                payload=payload,
                event_type=event_type,
            )

    def _append_external_order_lookup_attempt_in_transaction(
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

    @staticmethod
    def _reservation_from_row(row: sqlite3.Row, *, acquired: bool) -> OrderReservation:
        return OrderReservation(
            order_id=int(row["id"]),
            intent_id=str(row["intent_id"]),
            logical_order_key=str(row["logical_order_key"]),
            acquired=acquired,
            status=str(row["status"]),
            local_state=str(row["local_state"]),
            external_state=row["external_state"],
            filled_qty=float(row["filled_qty"]),
            remaining_qty=float(row["remaining_qty"]),
        )

    @staticmethod
    def _plan_from_row(
        row: sqlite3.Row,
        order_row: sqlite3.Row | None,
    ) -> PersistedFinancialApplicationPlan:
        def conflict(detail: str) -> NoReturn:
            raise FinancialApplicationPlanConflict(f"FINANCIAL_APPLICATION_PLAN_CONFLICT: {detail}")

        if order_row is None:
            conflict("ordre local référencé absent")
        try:
            if int(row["local_order_id"]) != int(order_row["id"]):
                conflict("local_order_id diverge de orders.id")
            for name in ("intent_id", "logical_order_key", "engine", "slot"):
                if row[name] != order_row[name]:
                    conflict(f"{name} diverge de orders")
            if order_row["order_type"] != "MARKET":
                conflict("order_type n'est pas MARKET")
            if row["side"] != order_row["side"]:
                conflict("side diverge de orders")
            if not math.isclose(
                float(row["requested_qty"]),
                float(order_row["requested_qty"]),
                rel_tol=0.0,
                abs_tol=1e-12,
            ):
                conflict("requested_qty diverge de orders")
            if (
                row["reference_price"] is None
                or order_row["reference_price"] is None
                or not math.isclose(
                    float(row["reference_price"]),
                    float(order_row["reference_price"]),
                    rel_tol=0.0,
                    abs_tol=1e-12,
                )
            ):
                conflict("reference_price diverge de orders")
            if row["reason"] != order_row["reason"]:
                conflict("reason diverge de orders")

            identity = parse_logical_order_identity(
                str(order_row["logical_order_key"]),
                intent_id=str(order_row["intent_id"]),
                engine=str(order_row["engine"]),
                slot=str(order_row["slot"]),
            )
            if (
                row["transition_type"] != identity.transition_type.value
                or row["decision_checkpoint"] != identity.decision_checkpoint
                or row["position_generation"] != identity.position_generation
                or int(row["transition_sequence"]) != identity.transition_sequence
            ):
                conflict("colonnes dénormalisées divergentes de logical_order_key")

            pre_state = json.loads(str(row["pre_state_payload"]))
            plan = FinancialApplicationPlan(
                identity=identity,
                side=str(row["side"]),
                requested_qty=float(row["requested_qty"]),
                reference_price=float(row["reference_price"]),
                reason=str(row["reason"]),
                reduce_only=bool(row["reduce_only"]),
                planned_effect_at=str(row["planned_effect_at"]),
                pre_state_payload=pre_state,
                protection_mode=str(row["protection_mode"]),
                entry_direction=row["entry_direction"],
                entry_stop_price=row["entry_stop_price"],
                application_version=int(row["application_version"]),
            )
            if str(row["reason"]) != plan.reason:
                conflict("reason non canonique")
            if (
                str(row["pre_state_sha256"]) != plan.pre_state_sha256
                or str(row["plan_key"]) != plan.plan_key
            ):
                conflict("hash durable invalide")
        except FinancialApplicationPlanConflict:
            raise
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            raise FinancialApplicationPlanConflict(
                f"FINANCIAL_APPLICATION_PLAN_CONFLICT: {error}"
            ) from error
        return PersistedFinancialApplicationPlan(
            local_order_id=int(row["local_order_id"]),
            intent_id=str(row["intent_id"]),
            plan=plan,
            created_at=str(row["created_at"]),
        )

    def get_financial_application_plan(
        self, local_order_id: int
    ) -> PersistedFinancialApplicationPlan | None:
        with self._read_transaction() as connection:
            plan_row = connection.execute(
                "SELECT * FROM financial_application_plans WHERE local_order_id = ?",
                (local_order_id,),
            ).fetchone()
            if plan_row is None:
                return None
            order_row = connection.execute(
                "SELECT * FROM orders WHERE id = ?",
                (plan_row["local_order_id"],),
            ).fetchone()
            return self._plan_from_row(plan_row, order_row)

    def get_financial_application_plan_by_intent(
        self, intent_id: str
    ) -> PersistedFinancialApplicationPlan | None:
        with self._read_transaction() as connection:
            plan_row = connection.execute(
                "SELECT * FROM financial_application_plans WHERE intent_id = ?",
                (intent_id,),
            ).fetchone()
            if plan_row is None:
                return None
            order_row = connection.execute(
                "SELECT * FROM orders WHERE id = ?",
                (plan_row["local_order_id"],),
            ).fetchone()
            return self._plan_from_row(plan_row, order_row)

    @staticmethod
    def _insert_financial_application_plan(
        connection: sqlite3.Connection,
        *,
        order_id: int,
        intent_id: str,
        plan: FinancialApplicationPlan,
        now: str,
    ) -> None:
        pre_state = json.loads(canonical_json(plan.pre_state_payload))
        connection.execute(
            """
            INSERT INTO financial_application_plans(
                local_order_id, intent_id, plan_key, application_version, logical_order_key,
                engine, slot, transition_type, decision_checkpoint, position_generation,
                transition_sequence, side, requested_qty, reference_price, reason, reduce_only,
                planned_effect_at, entry_direction, entry_stop_price, pre_state_payload,
                pre_state_sha256, protection_mode, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_id,
                intent_id,
                plan.plan_key,
                plan.application_version,
                plan.identity.logical_key,
                plan.identity.engine,
                plan.identity.slot,
                plan.identity.transition_type.value,
                plan.identity.decision_checkpoint,
                plan.identity.position_generation,
                plan.identity.transition_sequence,
                plan.side,
                plan.requested_qty,
                plan.reference_price,
                plan.reason,
                int(plan.reduce_only),
                plan.planned_effect_at,
                plan.entry_direction,
                plan.entry_stop_price,
                canonical_json(pre_state),
                plan.pre_state_sha256,
                plan.protection_mode,
                now,
            ),
        )

    def reserve_market_order_with_application_plan(
        self,
        identity: LogicalOrderIdentity,
        *,
        plan: FinancialApplicationPlan,
    ) -> OrderReservation:
        """Réserve l'ordre, son plan et le checkpoint pré-application en un commit."""

        if plan.identity != identity:
            raise FinancialApplicationPlanConflict(
                "Le plan ne correspond pas à l'identité réservée"
            )
        now = utc_now()
        logical_key = identity.logical_key
        intent_id = identity.intent_id
        pre_state = json.loads(canonical_json(plan.pre_state_payload))
        with self._transaction() as connection:
            existing = connection.execute(
                """SELECT * FROM orders
                   WHERE logical_order_key = ? OR intent_id = ? ORDER BY id LIMIT 1""",
                (logical_key, intent_id),
            ).fetchone()
            if existing is not None:
                if (
                    existing["logical_order_key"] != logical_key
                    or existing["intent_id"] != intent_id
                ):
                    raise OrderIdentityCollision(
                        "Collision entre l'empreinte d'intention et la clé logique complète"
                    )
                plan_row = connection.execute(
                    "SELECT * FROM financial_application_plans WHERE local_order_id = ?",
                    (existing["id"],),
                ).fetchone()
                if plan_row is None:
                    raise FinancialApplicationPlanConflict("LEGACY_APPLICATION_CONTEXT_INCOMPLETE")
                persisted = self._plan_from_row(plan_row, existing)
                if persisted.plan.semantic_content() != plan.semantic_content():
                    raise FinancialApplicationPlanConflict("Le plan financier existant diffère")
                return self._reservation_from_row(existing, acquired=False)
            cursor = connection.execute(
                """
                INSERT INTO orders(
                    engine, slot, intent_id, logical_order_key, order_type, side, requested_qty,
                    reference_price, remaining_qty, local_state, status, reason, created_at, updated_at
                ) VALUES(?, ?, ?, ?, 'MARKET', ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?)
                """,
                (
                    identity.engine,
                    identity.slot,
                    intent_id,
                    logical_key,
                    plan.side,
                    plan.requested_qty,
                    plan.reference_price,
                    plan.requested_qty,
                    LocalOrderState.INTENT_CREATED.value,
                    plan.reason,
                    now,
                    now,
                ),
            )
            order_id = cursor.lastrowid
            if order_id is None:
                raise RuntimeError("SQLite n'a pas retourné l'identifiant de l'ordre")
            self._insert_financial_application_plan(
                connection, order_id=int(order_id), intent_id=intent_id, plan=plan, now=now
            )
            connection.execute(
                """INSERT INTO engine_state(engine, payload, updated_at) VALUES(?, ?, ?)
                   ON CONFLICT(engine) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at""",
                (identity.engine, canonical_json(pre_state), now),
            )
            self._sync_positions(connection, identity.engine, pre_state, now)
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
                    "side": plan.side,
                    "requested_qty": plan.requested_qty,
                    "reference_price": plan.reference_price,
                    "reason": plan.reason,
                },
                "order",
                str(order_id),
                intent_id,
            )
            self._insert_event(
                connection,
                identity.engine,
                "financial_application_plan_persisted",
                {
                    "order_id": order_id,
                    "intent_id": intent_id,
                    "plan_key": plan.plan_key,
                    "pre_state_sha256": plan.pre_state_sha256,
                },
                "financial_application_plan",
                str(order_id),
                intent_id,
            )
            self._insert_event(
                connection,
                identity.engine,
                "order_pre_submission_checkpoint",
                self._state_event(
                    pre_state,
                    {
                        "order_id": order_id,
                        "intent_id": intent_id,
                        "plan_key": plan.plan_key,
                        "pre_state_sha256": plan.pre_state_sha256,
                    },
                ),
                "order",
                str(order_id),
                intent_id,
            )
            row = connection.execute(
                """SELECT id, intent_id, logical_order_key, status, local_state, external_state,
                          filled_qty, remaining_qty FROM orders WHERE id = ?""",
                (order_id,),
            ).fetchone()
            assert row is not None
            return self._reservation_from_row(row, acquired=True)

    def reserve_market_order(
        self,
        identity: LogicalOrderIdentity,
        *,
        side: str,
        requested_qty: float,
        reason: str,
        reference_price: float,
    ) -> OrderReservation:
        """Arbitre atomiquement la propriété d'une transition financière."""

        if not math.isfinite(requested_qty) or requested_qty <= 0:
            raise ValueError("requested_qty doit être finie et strictement positive")
        if not math.isfinite(reference_price) or reference_price <= 0:
            raise ValueError("reference_price doit être fini et strictement positif")
        if side not in {"BUY", "SELL"}:
            raise ValueError(f"Côté d'ordre non normalisé : {side!r}")
        logical_key = identity.logical_key
        intent_id = identity.intent_id
        now = utc_now()
        with self._transaction() as connection:
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
                return OrderReservation(
                    order_id=int(existing["id"]),
                    intent_id=str(existing["intent_id"]),
                    logical_order_key=logical_key,
                    acquired=False,
                    status=str(existing["status"]),
                    local_state=str(existing["local_state"]),
                    external_state=existing["external_state"],
                    filled_qty=float(existing["filled_qty"]),
                    remaining_qty=float(existing["remaining_qty"]),
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

    def mark_order_submitting(self, order_id: int) -> None:
        now = utc_now()
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
        self,
        order_id: int,
        *,
        allow_local_failure: bool = False,
    ) -> bool:
        """Réclame par CAS une intention sans effet externe possible.

        La ligne et son client_order_id sont réutilisés : aucune seconde
        intention financière n'est créée. Seuls un crash prouvé avant le
        broker, ou un échec d'un broker explicitement local, sont admissibles.
        Un état ayant pu atteindre l'exchange ne satisfait jamais le prédicat.
        """

        now = utc_now()
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
                  AND (
                      status='RECOVERED_ABORTED'
                      OR (? AND status='FAILED')
                  )
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
        """Abandonne un ordre Paper interrompu, sans prétendre à un état exchange.

        Seul l'appelant qui sait que le broker n'a aucun effet durable externe
        peut utiliser cette transition. L'événement d'observation antérieur reste
        dans le journal, mais la ligne redevient réclamable avec le même identifiant.
        """

        now = utc_now()
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT engine, intent_id, status, order_type, local_state, external_state,
                       filled_qty, broker_order_id
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

    def record_order_observation(
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
        """Persiste la réponse broker avant tout checkpoint métier."""

        external_state = ExternalOrderState(external_state)
        local_state = (
            LocalOrderState.PENDING_RECONCILIATION
            if external_state == ExternalOrderState.UNKNOWN or external_state.is_terminal
            else LocalOrderState.AWAITING_EXTERNAL
        )
        status = "PENDING" if local_state == LocalOrderState.PENDING_RECONCILIATION else "OPEN"
        now = utc_now()
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
                    updated_at=?
                WHERE id=?
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

    @staticmethod
    def _external_order_observation_from_row(row: sqlite3.Row) -> ExternalOrderObservation:
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
    def _external_fill_from_row(row: sqlite3.Row) -> ExternalFill:
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

    def append_external_order_observation(
        self,
        observation: ExternalOrderObservation,
    ) -> tuple[ExternalOrderObservation, bool]:
        """Ajoute une preuve d'ordre une seule fois, sans toucher à l'ordre local."""

        persisted = (
            observation
            if observation.persisted_at is not None
            else observation.with_persisted_at(utc_now())
        )
        with self._transaction() as connection:
            return self._append_external_order_observation_in_transaction(connection, persisted)

    def _append_external_order_observation_in_transaction(
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
            existing = self._external_order_observation_from_row(existing_row)
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
                requested_qty, cumulative_filled_qty, remaining_qty, venue_event_at, status_event_at,
                observed_at, persisted_at, observation_key, raw_payload_hash
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
        """Persiste atomiquement une observation éventuelle et sa tentative."""

        if self.read_only:
            raise RuntimeError("Un StateStore read-only ne peut pas persister une preuve")
        normalized_engine = str(engine).strip()
        normalized_aggregate_id = str(aggregate_id).strip()
        if not normalized_engine or not normalized_aggregate_id:
            raise ValueError("engine et aggregate_id doivent être non vides")
        persisted = (
            None
            if observation is None
            else observation
            if observation.persisted_at is not None
            else observation.with_persisted_at(utc_now())
        )
        with self._transaction() as connection:
            persisted_observation: ExternalOrderObservation | None = None
            observation_created = False
            if persisted is not None:
                persisted_observation, observation_created = (
                    self._append_external_order_observation_in_transaction(connection, persisted)
                )
            self._append_external_order_lookup_attempt_in_transaction(
                connection,
                engine=normalized_engine,
                aggregate_id=normalized_aggregate_id,
                payload=payload,
                event_type=event_type,
            )
        return persisted_observation, observation_created

    def append_external_fill(self, fill: ExternalFill) -> tuple[ExternalFill, bool]:
        """Ajoute un fill externe une seule fois, sans application métier."""

        persisted = fill if fill.persisted_at is not None else fill.with_persisted_at(utc_now())
        with self._transaction() as connection:
            return self._append_external_fill_in_transaction(connection, persisted)

    def _append_external_fill_in_transaction(
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
            existing = self._external_fill_from_row(existing_row)
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

    def _append_external_fill_lookup_attempt_in_transaction(
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
        """Persiste atomiquement 0..N fills et une tentative de lookup."""

        if self.read_only:
            raise RuntimeError("Un StateStore read-only ne peut pas persister des fills")
        normalized_engine = str(engine).strip()
        normalized_aggregate_id = str(aggregate_id).strip()
        if not normalized_engine or not normalized_aggregate_id:
            raise ValueError("engine et aggregate_id doivent être non vides")
        persisted_fills = tuple(
            fill if fill.persisted_at is not None else fill.with_persisted_at(utc_now())
            for fill in fills
        )
        persisted: list[ExternalFill] = []
        created: list[bool] = []
        with self._transaction() as connection:
            for fill in persisted_fills:
                persisted_fill, fill_created = self._append_external_fill_in_transaction(
                    connection, fill
                )
                persisted.append(persisted_fill)
                created.append(fill_created)
            self._append_external_fill_lookup_attempt_in_transaction(
                connection,
                engine=normalized_engine,
                aggregate_id=normalized_aggregate_id,
                payload=payload,
                event_type=event_type,
            )
        return tuple(persisted), tuple(created)

    def persist_external_order_settlement(
        self,
        settlement: ExternalOrderSettlement,
        *,
        engine: str,
        observed_at: str | None = None,
    ) -> tuple[ExternalOrderSettlement, bool]:
        """Atomically append one terminal settlement and its audit event.

        The writer is intentionally passive.  It never submits, cancels, or
        applies a fill; it only records an already acquired settlement proof.
        Replaying the same settlement key is idempotent while still producing
        an event for the new persistence invocation.
        """

        if self.read_only:
            raise RuntimeError("Un StateStore read-only ne peut pas persister un settlement")
        if not isinstance(settlement, ExternalOrderSettlement):
            raise TypeError("settlement must be ExternalOrderSettlement")
        normalized_engine = str(engine).strip()
        if not normalized_engine:
            raise ValueError("engine doit être non vide")
        persisted_at = utc_now()
        normalized_observed_at = persisted_at if observed_at is None else str(observed_at).strip()
        if not normalized_observed_at:
            raise ValueError("observed_at doit être non vide")
        try:
            parsed_observed_at = datetime.fromisoformat(
                normalized_observed_at[:-1] + "+00:00"
                if normalized_observed_at.endswith(("Z", "z"))
                else normalized_observed_at
            )
        except ValueError as error:
            raise ValueError("observed_at doit être ISO 8601 avec fuseau") from error
        if parsed_observed_at.tzinfo is None:
            raise ValueError("observed_at doit contenir un fuseau explicite")
        normalized_observed_at = parsed_observed_at.astimezone(UTC).isoformat()
        payload = settlement.to_persistence_payload()
        payload_json = canonical_json(payload)
        completeness_json = canonical_json(payload["completeness"])
        created = False
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT settlement_payload FROM external_order_settlements "
                "WHERE settlement_key = ?",
                (settlement.settlement_key,),
            ).fetchone()
            if existing is not None:
                try:
                    existing_payload = json.loads(str(existing["settlement_payload"]))
                except json.JSONDecodeError as error:
                    raise FinancialSettlementError(
                        "EXTERNAL_ORDER_SETTLEMENT_CORRUPT_PAYLOAD"
                    ) from error
                if canonical_json(existing_payload) != payload_json:
                    raise FinancialSettlementError("EXTERNAL_ORDER_SETTLEMENT_KEY_CONFLICT")
            else:
                connection.execute(
                    """
                    INSERT INTO external_order_settlements(
                        settlement_key, settlement_version, local_order_id, intent_id,
                        venue, environment, account_scope, instrument, side,
                        client_order_id, external_order_id, terminal_status,
                        terminal_status_event_at, completeness_version,
                        completeness_payload, raw_fill_count, fill_multiset_sha256,
                        settlement_payload, observed_at, persisted_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        settlement.settlement_key,
                        settlement.version,
                        settlement.local_order_id,
                        settlement.intent_id,
                        settlement.venue,
                        settlement.environment,
                        settlement.account_scope,
                        settlement.instrument,
                        settlement.side,
                        settlement.client_order_id,
                        settlement.external_order_id,
                        ExternalOrderState(settlement.terminal_status).value,
                        settlement.terminal_status_event_at,
                        settlement.completeness.completeness_version,
                        completeness_json,
                        settlement.raw_fill_count,
                        settlement.fill_multiset_sha256,
                        payload_json,
                        normalized_observed_at,
                        persisted_at,
                    ),
                )
                created = True
            self._insert_event(
                connection,
                normalized_engine,
                "external_order_settlement_persisted",
                {
                    "contract": settlement.version,
                    "local_order_id": settlement.local_order_id,
                    "intent_id": settlement.intent_id,
                    "settlement_key": settlement.settlement_key,
                    "fill_multiset_sha256": settlement.fill_multiset_sha256,
                    "raw_fill_count": settlement.raw_fill_count,
                    "deduplicated": not created,
                },
                aggregate_type="external_order_settlement",
                aggregate_id=settlement.settlement_key,
                correlation_id=settlement.intent_id,
            )
        return settlement, created

    def persist_paper_execution_evidence(
        self,
        evidence: PaperExecutionEvidence,
    ) -> PaperExecutionEvidencePersistenceResult:
        """Commit one local PAPER submission observation and optional fill together.

        This is a deliberately separate local-domain boundary. It does not
        project a Hyperliquid lookup result and cannot manufacture evidence for
        an external broker. A crash before commit leaves neither evidence row
        nor its audit event visible.
        """

        if self.read_only:
            raise RuntimeError("Un StateStore read-only ne peut pas persister une preuve PAPER")
        if not isinstance(evidence, PaperExecutionEvidence):
            raise TypeError("evidence must be PaperExecutionEvidence")
        persisted_at = utc_now()
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
                self._append_external_order_observation_in_transaction(connection, observation)
            )
            persisted_fill: ExternalFill | None = None
            fill_created = False
            if fill is not None:
                persisted_fill, fill_created = self._append_external_fill_in_transaction(
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

    def finalize_paper_order_atomically(self, local_order_id: int) -> PaperFinalizationDecision:
        """Finalize one positive PAPER order after durable fill application.

        This is a separate local-domain transaction from E3. It never
        interprets an external exchange status or an absence of evidence as a
        zero-effect result. The order, next financial sequence, durable
        position projection and finalization event commit together.
        """

        if self.read_only:
            raise RuntimeError("Un StateStore read-only ne peut pas finaliser un ordre PAPER")
        if (
            isinstance(local_order_id, bool)
            or not isinstance(local_order_id, int)
            or local_order_id <= 0
        ):
            raise ValueError("local_order_id must be a positive integer")

        with self._transaction() as connection:
            order = connection.execute(
                "SELECT * FROM orders WHERE id = ?", (local_order_id,)
            ).fetchone()
            if order is None:
                raise FinancialFillApplicationError("FINANCIAL_ORDER_MISSING")

            finalization_events = connection.execute(
                """
                SELECT id FROM events
                WHERE engine = ? AND event_type = 'PAPER_ORDER_FINALIZED'
                  AND aggregate_type = 'order' AND aggregate_id = ?
                ORDER BY id
                """,
                (order["engine"], str(local_order_id)),
            ).fetchall()
            if order["local_state"] == LocalOrderState.TERMINAL.value:
                if len(finalization_events) != 1:
                    raise FinancialFillApplicationError("PAPER_FINALIZATION_EVENT_CONFLICT")
                return PaperFinalizationDecision(
                    PaperFinalizationStatus.ALREADY_FINALIZED,
                    "PAPER_ORDER_ALREADY_TERMINAL",
                )
            if order["local_state"] != LocalOrderState.PENDING_RECONCILIATION.value:
                raise FinancialFillApplicationError("PAPER_ORDER_NOT_RECONCILIATION_READY")
            if finalization_events:
                raise FinancialFillApplicationError("PAPER_FINALIZATION_EVENT_CONFLICT")

            plan_row = connection.execute(
                "SELECT * FROM financial_application_plans WHERE local_order_id = ?",
                (local_order_id,),
            ).fetchone()
            if plan_row is None:
                raise FinancialFillApplicationError("LEGACY_APPLICATION_CONTEXT_INCOMPLETE")
            try:
                persisted_plan = self._plan_from_row(plan_row, order)
            except (FinancialApplicationPlanConflict, ValueError, TypeError) as error:
                raise FinancialFillApplicationError(
                    "FINANCIAL_APPLICATION_PLAN_CONFLICT"
                ) from error

            snapshot = self._read_resolution_snapshot_in_transaction(connection, local_order_id)
            paper_events = tuple(
                event
                for event in snapshot.lookup_events
                if event.event_type == PAPER_EXECUTION_EVIDENCE_EVENT_TYPE
            )
            if not paper_events or len(paper_events) != len(snapshot.lookup_events):
                raise FinancialFillApplicationError("PAPER_EVIDENCE_REQUIRED")

            from .resolution import assess_resolution
            from .resolution_projection import ProjectionStatus, project_resolution_snapshot

            projection = project_resolution_snapshot(snapshot)
            if projection.status != ProjectionStatus.READY or projection.bundle is None:
                raise FinancialFillApplicationError("PAPER_EVIDENCE_NOT_READY")
            assessment = assess_resolution(projection.bundle)
            if not assessment.binding_complete or assessment.conflicts:
                raise FinancialFillApplicationError("PAPER_EVIDENCE_CONFLICT")

            paper_fills = tuple(projection.bundle.fills)
            if any(
                fill.source_kind != ExternalEvidenceSource.SUBMISSION_RESPONSE
                or fill.venue != "paper-local"
                or fill.venue_fill_id is None
                or not fill.venue_fill_id.startswith("paper-local-fill-v1-")
                for fill in paper_fills
            ):
                raise FinancialFillApplicationError("PAPER_EVIDENCE_PROVENANCE_CONFLICT")
            paper_fill_keys = tuple(
                sorted(fill.fill_key for fill in paper_fills if fill.fill_key is not None)
            )
            observations = tuple(projection.bundle.order_observations)
            if not observations or any(
                observation.source_kind != ExternalEvidenceSource.SUBMISSION_RESPONSE
                or observation.venue != "paper-local"
                for observation in observations
            ):
                raise FinancialFillApplicationError("PAPER_EVIDENCE_PROVENANCE_CONFLICT")
            try:
                paper_state = ExternalOrderState(order["external_state"])
            except ValueError as error:
                raise FinancialFillApplicationError("PAPER_TERMINAL_STATE_MISSING") from error
            if paper_state not in {
                ExternalOrderState.FILLED,
                ExternalOrderState.PARTIAL_TERMINAL,
            }:
                raise FinancialFillApplicationError("PAPER_POSITIVE_TERMINAL_EVIDENCE_REQUIRED")
            if any(
                ExternalOrderState(observation.normalized_external_status) != paper_state
                for observation in observations
            ):
                raise FinancialFillApplicationError("PAPER_TERMINAL_STATE_CONFLICT")

            chain = self._read_financial_fill_application_chain_in_transaction(
                connection, local_order_id
            )
            applied_fill_keys = tuple(sorted(record.fill_key for record in chain))
            current_payload, current_raw_payload = self._load_engine_state_in_transaction(
                connection, persisted_plan.plan.identity.engine
            )
            current_state_sha256 = sha256_json(current_payload)
            expected_state_sha256 = (
                persisted_plan.plan.pre_state_sha256 if not chain else chain[-1].state_after_sha256
            )
            if current_state_sha256 != expected_state_sha256:
                raise FinancialFillApplicationError("FINANCIAL_APPLICATION_STATE_CONFLICT")
            self._assert_positions_projection_in_transaction(
                connection,
                engine=persisted_plan.plan.identity.engine,
                payload=current_payload,
            )
            slot_payload = current_payload["slots"][persisted_plan.plan.identity.slot]
            decision = decide_paper_finalization(
                local_state=order["local_state"],
                external_state=paper_state,
                paper_evidence_present=True,
                paper_fill_keys=paper_fill_keys,
                financially_applicable_fill_keys=assessment.financially_applicable_fill_keys,
                financially_ambiguous_fill_keys=assessment.financially_ambiguous_fill_keys,
                applied_fill_keys=applied_fill_keys,
                binding_complete=assessment.binding_complete,
                evidence_conflicts=assessment.conflicts,
                current_transition_sequence=int(slot_payload.get("financial_transition_seq", 0)),
                expected_transition_sequence=persisted_plan.plan.identity.transition_sequence,
            )
            if decision.status != PaperFinalizationStatus.FINALIZABLE:
                raise FinancialFillApplicationError(decision.reason)

            quantity_tolerance = max(1e-9, float(order["requested_qty"]) * 1e-9)
            applied_quantity = sum(record.result.quantity for record in chain)
            if (
                not math.isclose(
                    applied_quantity,
                    float(order["filled_qty"]),
                    rel_tol=0.0,
                    abs_tol=quantity_tolerance,
                )
                or float(order["remaining_qty"]) > quantity_tolerance
            ):
                raise FinancialFillApplicationError("PAPER_ORDER_QUANTITY_CONFLICT")

            finalized_payload = json.loads(canonical_json(current_payload))
            finalized_slot = finalized_payload["slots"][persisted_plan.plan.identity.slot]
            finalized_slot["financial_transition_seq"] = (
                persisted_plan.plan.identity.transition_sequence + 1
            )
            validate_trend_state(finalized_payload)
            final_state_sha256 = sha256_json(finalized_payload)
            finalized_at = utc_now()
            self._update_engine_state_cas_in_transaction(
                connection,
                engine=persisted_plan.plan.identity.engine,
                expected_raw_payload=current_raw_payload,
                state_after_payload=finalized_payload,
                applied_at=finalized_at,
            )
            self._sync_positions(
                connection,
                persisted_plan.plan.identity.engine,
                finalized_payload,
                finalized_at,
            )
            final_status = "FILLED" if paper_state == ExternalOrderState.FILLED else "PARTIAL"
            cursor = connection.execute(
                """
                UPDATE orders SET status=?, local_state='TERMINAL', updated_at=?
                WHERE id=? AND local_state='PENDING_RECONCILIATION'
                """,
                (final_status, finalized_at, local_order_id),
            )
            if cursor.rowcount != 1:
                raise FinancialFillApplicationError("PAPER_FINALIZATION_STATE_CONFLICT")
            self._insert_event(
                connection,
                str(order["engine"]),
                "PAPER_ORDER_FINALIZED",
                {
                    "order_id": local_order_id,
                    "intent_id": str(order["intent_id"]),
                    "plan_key": persisted_plan.plan.plan_key,
                    "status": final_status,
                    "external_state": paper_state.value,
                    "applied_fill_keys": list(applied_fill_keys),
                    "state_before_sha256": current_state_sha256,
                    "state_after_sha256": final_state_sha256,
                    "transition_sequence_before": (
                        persisted_plan.plan.identity.transition_sequence
                    ),
                    "transition_sequence_after": (
                        persisted_plan.plan.identity.transition_sequence + 1
                    ),
                },
                "order",
                str(local_order_id),
                str(order["intent_id"]),
            )
            return decision

    def _read_resolution_snapshot_in_transaction(
        self, connection: sqlite3.Connection, local_order_id: int
    ) -> ResolutionSnapshot:
        order_row = connection.execute(
            "SELECT * FROM orders WHERE id = ?", (local_order_id,)
        ).fetchone()
        if order_row is None:
            return ResolutionSnapshot(None, (), (), ())
        order = MappingProxyType(dict(order_row))
        observations = tuple(
            self._external_order_observation_from_row(row)
            for row in connection.execute(
                """
                SELECT * FROM external_order_observations
                WHERE local_order_id = ?
                ORDER BY id
                """,
                (local_order_id,),
            ).fetchall()
        )
        fills = tuple(
            self._external_fill_from_row(row)
            for row in connection.execute(
                """
                SELECT * FROM external_fills
                WHERE local_order_id = ?
                ORDER BY id
                """,
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
                    OR (
                      aggregate_id = ?
                      AND event_type = ?
                    )
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
        """Lit ordre, preuves et lookup events dans un unique snapshot SQLite."""

        if (
            isinstance(local_order_id, bool)
            or not isinstance(local_order_id, int)
            or local_order_id <= 0
        ):
            raise ValueError("local_order_id doit être un entier strictement positif")
        with self._read_transaction() as connection:
            return self._read_resolution_snapshot_in_transaction(connection, local_order_id)

    def get_external_order_observations(
        self,
        local_order_id: int,
    ) -> list[ExternalOrderObservation]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM external_order_observations
                WHERE local_order_id = ?
                ORDER BY id
                """,
                (local_order_id,),
            ).fetchall()
        return [self._external_order_observation_from_row(row) for row in rows]

    def get_external_fills(self, local_order_id: int) -> list[ExternalFill]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM external_fills WHERE local_order_id = ? ORDER BY id",
                (local_order_id,),
            ).fetchall()
        return [self._external_fill_from_row(row) for row in rows]

    def get_external_order_settlements(
        self,
        local_order_id: int,
    ) -> list[ExternalOrderSettlement]:
        """Read immutable terminal settlements and validate their envelope."""

        if (
            isinstance(local_order_id, bool)
            or not isinstance(local_order_id, int)
            or local_order_id <= 0
        ):
            raise ValueError("local_order_id doit être un entier strictement positif")
        with self._read_transaction() as connection:
            rows = connection.execute(
                """
                SELECT * FROM external_order_settlements
                WHERE local_order_id = ?
                ORDER BY persisted_at, settlement_key
                """,
                (local_order_id,),
            ).fetchall()
        settlements: list[ExternalOrderSettlement] = []
        for row in rows:
            try:
                payload = json.loads(str(row["settlement_payload"]))
                if not isinstance(payload, Mapping):
                    raise FinancialSettlementError("settlement payload non objet")
                settlement = ExternalOrderSettlement.from_persistence_payload(payload)
                if (
                    settlement.settlement_key != str(row["settlement_key"])
                    or settlement.local_order_id != int(row["local_order_id"])
                    or settlement.intent_id != str(row["intent_id"])
                    or settlement.fill_multiset_sha256 != str(row["fill_multiset_sha256"])
                    or settlement.raw_fill_count != int(row["raw_fill_count"])
                ):
                    raise FinancialSettlementError("EXTERNAL_ORDER_SETTLEMENT_ENVELOPE_CONFLICT")
            except (json.JSONDecodeError, TypeError, ValueError) as error:
                raise FinancialSettlementError("EXTERNAL_ORDER_SETTLEMENT_CORRUPT") from error
            settlements.append(settlement)
        return settlements

    @staticmethod
    def _external_order_settlement_from_row(
        row: sqlite3.Row,
    ) -> ExternalOrderSettlement:
        try:
            payload = json.loads(str(row["settlement_payload"]))
            if not isinstance(payload, Mapping):
                raise FinancialSettlementError("settlement payload non objet")
            settlement = ExternalOrderSettlement.from_persistence_payload(payload)
            if (
                settlement.settlement_key != str(row["settlement_key"])
                or settlement.version != int(row["settlement_version"])
                or settlement.local_order_id != int(row["local_order_id"])
                or settlement.intent_id != str(row["intent_id"])
                or settlement.venue != str(row["venue"])
                or settlement.environment != str(row["environment"])
                or settlement.account_scope != str(row["account_scope"])
                or settlement.instrument != str(row["instrument"])
                or settlement.side != str(row["side"])
                or settlement.client_order_id != str(row["client_order_id"])
                or settlement.external_order_id != str(row["external_order_id"])
                or ExternalOrderState(settlement.terminal_status).value
                != str(row["terminal_status"])
                or settlement.terminal_status_event_at != str(row["terminal_status_event_at"])
                or settlement.completeness.completeness_version != int(row["completeness_version"])
                or settlement.fill_multiset_sha256 != str(row["fill_multiset_sha256"])
                or settlement.raw_fill_count != int(row["raw_fill_count"])
            ):
                raise FinancialSettlementError("EXTERNAL_ORDER_SETTLEMENT_ENVELOPE_CONFLICT")
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise FinancialSettlementError("EXTERNAL_ORDER_SETTLEMENT_CORRUPT") from error
        return settlement

    @staticmethod
    def _financial_settlement_application_from_row(
        row: sqlite3.Row,
    ) -> PersistedFinancialSettlementApplication:
        try:
            payload = json.loads(str(row["result_payload"]))
            if not isinstance(payload, Mapping):
                raise FinancialSettlementError("settlement result payload non objet")
            result = FinancialSettlementResult.from_persistence_payload(
                payload,
                expected_result_sha256=str(row["result_sha256"]),
            )
            return PersistedFinancialSettlementApplication(
                application_key=str(row["application_key"]),
                application_version=int(row["application_version"]),
                settlement_key=str(row["settlement_key"]),
                local_order_id=int(row["local_order_id"]),
                intent_id=str(row["intent_id"]),
                plan_key=str(row["plan_key"]),
                state_before_sha256=str(row["state_before_sha256"]),
                state_after_sha256=str(row["state_after_sha256"]),
                result=result,
                applied_at=str(row["applied_at"]),
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise FinancialSettlementError("FINANCIAL_SETTLEMENT_APPLICATION_CORRUPT") from error

    def _read_financial_settlement_applications_in_transaction(
        self,
        connection: sqlite3.Connection,
        local_order_id: int,
    ) -> tuple[PersistedFinancialSettlementApplication, ...]:
        rows = connection.execute(
            """
            SELECT * FROM financial_settlement_applications
            WHERE local_order_id = ?
            ORDER BY applied_at, application_key
            """,
            (local_order_id,),
        ).fetchall()
        if not rows:
            return ()
        order_row = connection.execute(
            "SELECT * FROM orders WHERE id = ?", (local_order_id,)
        ).fetchone()
        if order_row is None:
            raise FinancialSettlementError("FINANCIAL_ORDER_MISSING")
        plan_row = connection.execute(
            "SELECT * FROM financial_application_plans WHERE local_order_id = ?",
            (local_order_id,),
        ).fetchone()
        if plan_row is None:
            raise FinancialSettlementError("LEGACY_APPLICATION_CONTEXT_INCOMPLETE")
        try:
            persisted_plan = self._plan_from_row(plan_row, order_row)
        except (FinancialApplicationPlanConflict, TypeError, ValueError) as error:
            raise FinancialSettlementError("FINANCIAL_APPLICATION_PLAN_CONFLICT") from error

        records: list[PersistedFinancialSettlementApplication] = []
        for row in rows:
            try:
                record = self._financial_settlement_application_from_row(row)
                if (
                    record.local_order_id != local_order_id
                    or record.intent_id != str(order_row["intent_id"])
                    or record.plan_key != persisted_plan.plan.plan_key
                ):
                    raise FinancialSettlementError("FINANCIAL_SETTLEMENT_APPLICATION_CONFLICT")
                settlement_row = connection.execute(
                    "SELECT * FROM external_order_settlements WHERE settlement_key = ?",
                    (record.settlement_key,),
                ).fetchone()
                if settlement_row is None:
                    raise FinancialSettlementError("EXTERNAL_ORDER_SETTLEMENT_MISSING")
                settlement = self._external_order_settlement_from_row(settlement_row)
                expected_result = calculate_financial_order_settlement(settlement, persisted_plan)
                if canonical_json(record.result.to_persistence_payload()) != canonical_json(
                    expected_result.to_persistence_payload()
                ):
                    raise FinancialSettlementError(
                        "FINANCIAL_SETTLEMENT_APPLICATION_REPLAY_CONFLICT"
                    )
                records.append(record)
            except FinancialSettlementError:
                raise
            except (KeyError, TypeError, ValueError) as error:
                raise FinancialSettlementError(
                    "FINANCIAL_SETTLEMENT_APPLICATION_CORRUPT"
                ) from error
        return tuple(records)

    def get_financial_settlement_applications(
        self,
        local_order_id: int,
    ) -> tuple[PersistedFinancialSettlementApplication, ...]:
        """Read settlement applications and replay their pure economics."""

        if (
            isinstance(local_order_id, bool)
            or not isinstance(local_order_id, int)
            or local_order_id <= 0
        ):
            raise ValueError("local_order_id must be a positive integer")
        with self._read_transaction() as connection:
            return self._read_financial_settlement_applications_in_transaction(
                connection, local_order_id
            )

    def read_financial_settlement_application_chain(
        self,
        local_order_id: int,
        *,
        _connection: sqlite3.Connection | None = None,
    ) -> tuple[PersistedFinancialSettlementApplication, ...]:
        """Compatibility name for the validated settlement application reader."""

        if (
            isinstance(local_order_id, bool)
            or not isinstance(local_order_id, int)
            or local_order_id <= 0
        ):
            raise ValueError("local_order_id must be a positive integer")
        with self._read_connection(_connection) as connection:
            return self._read_financial_settlement_applications_in_transaction(
                connection, local_order_id
            )

    @staticmethod
    def _insert_financial_settlement_application_in_transaction(
        connection: sqlite3.Connection,
        record: PersistedFinancialSettlementApplication,
    ) -> None:
        cursor = connection.execute(
            """
            INSERT INTO financial_settlement_applications(
                application_key, application_version, settlement_key, local_order_id,
                intent_id, plan_key, state_before_sha256, state_after_sha256,
                result_payload, result_sha256, applied_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.application_key,
                record.application_version,
                record.settlement_key,
                record.local_order_id,
                record.intent_id,
                record.plan_key,
                record.state_before_sha256,
                record.state_after_sha256,
                canonical_json(record.result.to_persistence_payload()),
                record.result.result_sha256,
                record.applied_at,
            ),
        )
        if cursor.rowcount != 1:
            raise FinancialSettlementError("FINANCIAL_SETTLEMENT_APPLICATION_INSERT_FAILED")

    def apply_external_settlement_atomically(
        self,
        *,
        local_order_id: int,
        settlement_key: str,
    ) -> FinancialSettlementCommitResult:
        """Apply one complete terminal settlement in one durable transaction.

        This is a dormant primitive for the future orchestration layer.  It
        does not acquire evidence, finalize the order, authorize retry, or
        wire any runtime reader.  A repeated settlement is a no-op only after
        its persisted application has been fully revalidated.
        """

        if self.read_only:
            raise RuntimeError("Un StateStore read-only ne peut pas appliquer un settlement")
        if (
            isinstance(local_order_id, bool)
            or not isinstance(local_order_id, int)
            or local_order_id <= 0
        ):
            raise ValueError("local_order_id must be a positive integer")
        if not isinstance(settlement_key, str) or not settlement_key.strip():
            raise ValueError("settlement_key must be a non-empty string")
        settlement_key = settlement_key.strip()

        with self._transaction() as connection:
            order_row = connection.execute(
                "SELECT * FROM orders WHERE id = ?", (local_order_id,)
            ).fetchone()
            if order_row is None:
                raise FinancialSettlementError("FINANCIAL_ORDER_MISSING")
            plan_row = connection.execute(
                "SELECT * FROM financial_application_plans WHERE local_order_id = ?",
                (local_order_id,),
            ).fetchone()
            if plan_row is None:
                raise FinancialSettlementError("LEGACY_APPLICATION_CONTEXT_INCOMPLETE")
            try:
                persisted_plan = self._plan_from_row(plan_row, order_row)
            except (FinancialApplicationPlanConflict, TypeError, ValueError) as error:
                raise FinancialSettlementError("FINANCIAL_APPLICATION_PLAN_CONFLICT") from error
            settlement_row = connection.execute(
                "SELECT * FROM external_order_settlements WHERE settlement_key = ?",
                (settlement_key,),
            ).fetchone()
            if settlement_row is None:
                raise FinancialSettlementError("EXTERNAL_ORDER_SETTLEMENT_MISSING")
            settlement = self._external_order_settlement_from_row(settlement_row)
            if settlement.local_order_id != local_order_id:
                raise FinancialSettlementError("SETTLEMENT_ORDER_BINDING_CONFLICT")

            existing_rows = connection.execute(
                """
                SELECT * FROM financial_settlement_applications
                WHERE local_order_id = ? AND plan_key = ?
                ORDER BY applied_at, application_key
                """,
                (local_order_id, persisted_plan.plan.plan_key),
            ).fetchall()
            if existing_rows:
                if len(existing_rows) != 1:
                    raise FinancialSettlementError("FINANCIAL_SETTLEMENT_APPLICATION_CONFLICT")
                existing = self._financial_settlement_application_from_row(existing_rows[0])
                if existing.settlement_key != settlement_key:
                    raise FinancialSettlementError("FINANCIAL_SETTLEMENT_APPLICATION_CONFLICT")
                expected_result = calculate_financial_order_settlement(settlement, persisted_plan)
                if canonical_json(existing.result.to_persistence_payload()) != canonical_json(
                    expected_result.to_persistence_payload()
                ):
                    raise FinancialSettlementError(
                        "FINANCIAL_SETTLEMENT_APPLICATION_REPLAY_CONFLICT"
                    )
                return FinancialSettlementCommitResult(
                    application=existing,
                    applied=False,
                    already_applied=True,
                    trade_inserted=False,
                    event_id=None,
                )

            if order_row["local_state"] != LocalOrderState.PENDING_RECONCILIATION.value:
                raise FinancialSettlementError("FINANCIAL_ORDER_NOT_RECONCILIATION_READY")
            current_payload, current_raw_payload = self._load_engine_state_in_transaction(
                connection, persisted_plan.plan.identity.engine
            )
            current_state_sha256 = sha256_json(current_payload)
            if current_state_sha256 != persisted_plan.plan.pre_state_sha256:
                raise FinancialSettlementError("FINANCIAL_APPLICATION_STATE_CONFLICT")
            self._assert_positions_projection_in_transaction(
                connection,
                engine=persisted_plan.plan.identity.engine,
                payload=current_payload,
            )
            result = calculate_financial_order_settlement(settlement, persisted_plan)
            if result.state_before_sha256 != current_state_sha256:
                raise FinancialSettlementError("FINANCIAL_APPLICATION_STATE_CONFLICT")
            applied_at = utc_now()
            record = PersistedFinancialSettlementApplication(
                application_key=financial_settlement_application_key(
                    application_version=FINANCIAL_SETTLEMENT_APPLICATION_VERSION,
                    local_order_id=local_order_id,
                    intent_id=persisted_plan.intent_id,
                    plan_key=persisted_plan.plan.plan_key,
                    settlement_key=settlement_key,
                ),
                application_version=FINANCIAL_SETTLEMENT_APPLICATION_VERSION,
                settlement_key=settlement_key,
                local_order_id=local_order_id,
                intent_id=persisted_plan.intent_id,
                plan_key=persisted_plan.plan.plan_key,
                state_before_sha256=result.state_before_sha256,
                state_after_sha256=result.state_after_sha256,
                result=result,
                applied_at=applied_at,
            )
            self._insert_financial_settlement_application_in_transaction(connection, record)
            self._update_engine_state_cas_in_transaction(
                connection,
                engine=persisted_plan.plan.identity.engine,
                expected_raw_payload=current_raw_payload,
                state_after_payload=result.state_after_payload,
                applied_at=applied_at,
            )
            self._sync_positions(
                connection,
                persisted_plan.plan.identity.engine,
                result.state_after_payload,
                applied_at,
            )
            trade_inserted = self._insert_financial_fill_trade_in_transaction(
                connection, result.trade_payload
            )
            event_id = self._insert_event(
                connection,
                persisted_plan.plan.identity.engine,
                "EXTERNAL_ORDER_SETTLEMENT_APPLIED",
                {
                    "application_key": record.application_key,
                    "application_version": record.application_version,
                    "settlement_key": settlement_key,
                    "plan_key": record.plan_key,
                    "quantity": result.quantity,
                    "total_notional": result.total_notional,
                    "total_fee": result.total_fee,
                    "fee_asset": result.fee_asset,
                    "terminal_status": ExternalOrderState(settlement.terminal_status).value,
                    "raw_fill_count": settlement.raw_fill_count,
                    "state_before_sha256": result.state_before_sha256,
                    "state_after_sha256": result.state_after_sha256,
                    "trade_inserted": trade_inserted,
                },
                aggregate_type="order",
                aggregate_id=str(local_order_id),
                correlation_id=record.application_key,
                ts=applied_at,
            )
            return FinancialSettlementCommitResult(
                application=record,
                applied=True,
                already_applied=False,
                trade_inserted=trade_inserted,
                event_id=event_id,
            )

    def read_financial_fill_application_chain(
        self,
        local_order_id: int,
        *,
        _connection: sqlite3.Connection | None = None,
    ) -> tuple[PersistedFinancialFillApplication, ...]:
        """Read and economically replay one complete financial chain."""

        if (
            isinstance(local_order_id, bool)
            or not isinstance(local_order_id, int)
            or local_order_id <= 0
        ):
            raise ValueError("local_order_id must be a positive integer")
        with self._read_connection(_connection) as connection:
            rows = connection.execute(
                """
                SELECT * FROM financial_fill_applications
                WHERE local_order_id = ?
                ORDER BY application_index
                """,
                (local_order_id,),
            ).fetchall()
            if not rows:
                return ()
            order_row = connection.execute(
                "SELECT * FROM orders WHERE id = ?", (local_order_id,)
            ).fetchone()
            if order_row is None:
                raise FinancialApplicationLedgerConflict(
                    "FINANCIAL_APPLICATION_LEDGER_CONFLICT: local order missing"
                )
            plan_row = connection.execute(
                "SELECT * FROM financial_application_plans WHERE local_order_id = ?",
                (local_order_id,),
            ).fetchone()
            if plan_row is None:
                raise FinancialApplicationLedgerConflict(
                    "FINANCIAL_APPLICATION_LEDGER_CONFLICT: financial plan missing"
                )
            try:
                persisted_plan = self._plan_from_row(plan_row, order_row)
            except (FinancialApplicationPlanConflict, ValueError, TypeError) as error:
                raise FinancialApplicationLedgerConflict(
                    f"FINANCIAL_APPLICATION_LEDGER_CONFLICT: invalid plan: {error}"
                ) from error

            records: list[PersistedFinancialFillApplication] = []
            committed_fills: list[ExternalFill] = []
            seen_keys: set[str] = set()
            seen_fills: set[str] = set()
            for row in rows:
                try:
                    if int(row["local_order_id"]) != local_order_id:
                        raise ValueError("local_order_id divergent")
                    if str(row["intent_id"]) != str(order_row["intent_id"]):
                        raise ValueError("intent_id divergent from orders")
                    if str(row["plan_key"]) != persisted_plan.plan.plan_key:
                        raise ValueError("plan_key divergent from durable plan")
                    fill_row = connection.execute(
                        "SELECT * FROM external_fills WHERE fill_key = ?",
                        (row["fill_key"],),
                    ).fetchone()
                    if fill_row is None:
                        raise ValueError("referenced fill_key missing")
                    fill = self._external_fill_from_row(fill_row)
                    fill_key = str(row["fill_key"])
                    if (
                        fill.fill_key != fill_key
                        or fill.local_order_id != local_order_id
                        or fill.intent_id != str(order_row["intent_id"])
                    ):
                        raise ValueError("fill belongs to another order")
                    if fill.venue_fill_id is None:
                        raise ValueError("venue fill identity is unproven")
                    if (
                        fill.fee is None
                        or fill.fee_asset is None
                        or fill.fee_asset.upper() != "USDC"
                    ):
                        raise ValueError("financial fee is missing or unsupported")
                    if fill.side != persisted_plan.plan.side:
                        raise ValueError("fill side diverges from financial plan")
                    if fill_key in seen_fills:
                        raise ValueError("duplicate fill in chain")
                    if str(row["application_key"]) in seen_keys:
                        raise ValueError("duplicate application_key in chain")

                    payload = json.loads(str(row["result_payload"]))
                    if not isinstance(payload, Mapping):
                        raise ValueError("result_payload is not an object")
                    result = FinancialFillApplicationResult.from_payload(
                        payload,
                        expected_result_sha256=str(row["result_sha256"]),
                    )
                    expected_key = financial_fill_application_key(
                        application_version=int(row["application_version"]),
                        local_order_id=local_order_id,
                        intent_id=str(order_row["intent_id"]),
                        plan_key=persisted_plan.plan.plan_key,
                        fill_key=fill_key,
                    )
                    if str(row["application_key"]) != expected_key:
                        raise ValueError("FINANCIAL_APPLICATION_KEY_CONFLICT")
                    requested_qty = float(persisted_plan.plan.requested_qty)
                    quantity_tolerance = max(1e-9, abs(requested_qty) * 1e-9)
                    expected_effect_at = (
                        fill.venue_event_at or persisted_plan.plan.planned_effect_at
                    )
                    if (
                        result.application_key != str(row["application_key"])
                        or result.application_version != int(row["application_version"])
                        or result.local_order_id != local_order_id
                        or result.intent_id != str(row["intent_id"])
                        or result.plan_key != str(row["plan_key"])
                        or result.fill_key != fill_key
                        or FinancialTransitionType(result.transition_type).value
                        != str(row["transition_type"])
                        or result.transition_type != persisted_plan.plan.identity.transition_type
                        or result.economic_effect_at != str(row["economic_effect_at"])
                        or result.economic_effect_at != expected_effect_at
                        or result.quantity <= 0
                        or result.quantity > requested_qty + quantity_tolerance
                        or not math.isclose(
                            result.quantity, fill.quantity, rel_tol=0.0, abs_tol=quantity_tolerance
                        )
                        or not math.isclose(
                            result.price, fill.price, rel_tol=0.0, abs_tol=quantity_tolerance
                        )
                        or not math.isclose(
                            result.fee, fill.fee, rel_tol=0.0, abs_tol=quantity_tolerance
                        )
                        or result.fee_asset != fill.fee_asset.upper()
                        or result.state_before_sha256 != str(row["state_before_sha256"])
                        or result.state_after_sha256 != str(row["state_after_sha256"])
                    ):
                        raise ValueError("ledger row or result diverges from referenced fill/plan")

                    expected_state_before = (
                        persisted_plan.plan.pre_state_sha256
                        if not committed_fills
                        else records[-1].state_after_sha256
                    )
                    expected_result = recompute_committed_financial_fill_application(
                        persisted_plan,
                        (*committed_fills, fill),
                        state_before_sha256=expected_state_before,
                    )
                    if result.as_payload() != expected_result.as_payload():
                        raise ValueError(
                            "FINANCIAL_APPLICATION_LEDGER_CONFLICT: economic replay mismatch"
                        )

                    seen_fills.add(fill_key)
                    seen_keys.add(str(row["application_key"]))
                    records.append(
                        PersistedFinancialFillApplication(
                            application_key=str(row["application_key"]),
                            application_version=int(row["application_version"]),
                            local_order_id=local_order_id,
                            intent_id=str(row["intent_id"]),
                            plan_key=str(row["plan_key"]),
                            fill_key=fill_key,
                            application_index=int(row["application_index"]),
                            previous_application_key=row["previous_application_key"],
                            transition_type=str(row["transition_type"]),
                            economic_effect_at=str(row["economic_effect_at"]),
                            state_before_sha256=str(row["state_before_sha256"]),
                            state_after_sha256=str(row["state_after_sha256"]),
                            result=result,
                            applied_at=str(row["applied_at"]),
                        )
                    )
                    committed_fills.append(fill)
                except FinancialApplicationLedgerConflict:
                    raise
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                    raise FinancialApplicationLedgerConflict(
                        f"FINANCIAL_APPLICATION_LEDGER_CONFLICT: {error}"
                    ) from error

            for index, record in enumerate(records):
                if record.application_index != index:
                    raise FinancialApplicationLedgerConflict(
                        "FINANCIAL_APPLICATION_LEDGER_CONFLICT: non-contiguous index"
                    )
                if index == 0:
                    if record.previous_application_key is not None:
                        raise FinancialApplicationLedgerConflict(
                            "FINANCIAL_APPLICATION_LEDGER_CONFLICT: root has a parent"
                        )
                    if record.state_before_sha256 != persisted_plan.plan.pre_state_sha256:
                        raise FinancialApplicationLedgerConflict(
                            "FINANCIAL_APPLICATION_LEDGER_CONFLICT: root pre-state diverges"
                        )
                else:
                    previous = records[index - 1]
                    if record.previous_application_key != previous.application_key:
                        raise FinancialApplicationLedgerConflict(
                            "FINANCIAL_APPLICATION_LEDGER_CONFLICT: previous chain diverges"
                        )
                    if record.state_before_sha256 != previous.state_after_sha256:
                        raise FinancialApplicationLedgerConflict(
                            "FINANCIAL_APPLICATION_LEDGER_CONFLICT: state_before diverges"
                        )
        return tuple(records)

    def _read_financial_fill_application_chain_in_transaction(
        self, connection: sqlite3.Connection, local_order_id: int
    ) -> tuple[PersistedFinancialFillApplication, ...]:
        return self.read_financial_fill_application_chain(local_order_id, _connection=connection)

    @staticmethod
    def _load_engine_state_in_transaction(
        connection: sqlite3.Connection, engine: str
    ) -> tuple[dict[str, Any], str]:
        row = connection.execute(
            "SELECT payload FROM engine_state WHERE engine = ?", (engine,)
        ).fetchone()
        if row is None:
            raise FinancialFillApplicationError("FINANCIAL_ENGINE_STATE_MISSING")
        raw_payload = str(row["payload"])
        try:
            payload = json.loads(raw_payload)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise FinancialFillApplicationError("FINANCIAL_ENGINE_STATE_INVALID") from error
        if not isinstance(payload, dict):
            raise FinancialFillApplicationError("FINANCIAL_ENGINE_STATE_INVALID")
        return payload, raw_payload

    @staticmethod
    def _assert_positions_projection_in_transaction(
        connection: sqlite3.Connection,
        *,
        engine: str,
        payload: Mapping[str, Any],
    ) -> None:
        if engine != "trend":
            raise FinancialFillApplicationError("FINANCIAL_ENGINE_UNSUPPORTED")
        slots = payload.get("slots")
        if not isinstance(slots, Mapping):
            raise FinancialFillApplicationError("FINANCIAL_POSITION_PROJECTION_CONFLICT")
        rows = connection.execute(
            "SELECT * FROM positions WHERE engine = ? ORDER BY slot", (engine,)
        ).fetchall()
        if {str(row["slot"]) for row in rows} != {str(slot) for slot in slots}:
            raise FinancialFillApplicationError("FINANCIAL_POSITION_PROJECTION_CONFLICT")
        numeric_fields = {
            "cash",
            "entry_price",
            "qty",
            "stop_price",
            "direction",
            "bars_held",
            "best_close",
            "entry_fee",
        }
        for row in rows:
            slot_name = str(row["slot"])
            slot_state = slots.get(slot_name)
            if not isinstance(slot_state, Mapping):
                raise FinancialFillApplicationError("FINANCIAL_POSITION_PROJECTION_CONFLICT")
            position = slot_state.get("position")
            if position is not None and not isinstance(position, Mapping):
                raise FinancialFillApplicationError("FINANCIAL_POSITION_PROJECTION_CONFLICT")
            expected: dict[str, Any] = {
                "status": "OPEN" if position is not None else "FLAT",
                "cash": slot_state.get("cash"),
                "entry_time": position.get("entry_time") if position else None,
                "entry_price": position.get("entry_price") if position else None,
                "qty": position.get("qty", 0.0) if position else 0.0,
                "stop_price": position.get("stop_price") if position else None,
                "direction": position.get("direction") if position else None,
                "bars_held": position.get("bars_held", 0) if position else 0,
                "best_close": position.get("best_close") if position else None,
                "stop_order_id": slot_state.get("stop_order_id"),
                "entry_fee": slot_state.get("entry_fee", 0.0),
                "last_bar_ts": slot_state.get("last_bar_ts"),
            }
            for field_name, expected_value in expected.items():
                actual_value = row[field_name]
                if field_name in numeric_fields:
                    if expected_value is None:
                        matches = actual_value is None
                    else:
                        matches = actual_value is not None and math.isclose(
                            float(actual_value), float(expected_value), rel_tol=0.0, abs_tol=1e-12
                        )
                else:
                    matches = actual_value == expected_value
                if not matches:
                    raise FinancialFillApplicationError("FINANCIAL_POSITION_PROJECTION_CONFLICT")

    def _financial_fill_assessment_in_transaction(
        self, connection: sqlite3.Connection, local_order_id: int
    ) -> Any:
        from .resolution import assess_resolution
        from .resolution_projection import project_resolution_snapshot

        snapshot = self._read_resolution_snapshot_in_transaction(connection, local_order_id)
        projection = project_resolution_snapshot(snapshot)
        if projection.bundle is None:
            raise FinancialFillApplicationError("FINANCIAL_FILL_ELIGIBILITY_CHANGED")
        try:
            return assess_resolution(projection.bundle)
        except (TypeError, ValueError, KeyError) as error:
            raise FinancialFillApplicationError("FINANCIAL_FILL_ELIGIBILITY_CHANGED") from error

    @staticmethod
    def _insert_financial_fill_application_in_transaction(
        connection: sqlite3.Connection,
        record: PersistedFinancialFillApplication,
    ) -> None:
        cursor = connection.execute(
            """
            INSERT INTO financial_fill_applications(
                application_key, application_version, local_order_id, intent_id,
                plan_key, fill_key, application_index, previous_application_key,
                transition_type, economic_effect_at, state_before_sha256,
                state_after_sha256, result_payload, result_sha256, applied_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.application_key,
                record.application_version,
                record.local_order_id,
                record.intent_id,
                record.plan_key,
                record.fill_key,
                record.application_index,
                record.previous_application_key,
                FinancialTransitionType(record.transition_type).value,
                record.economic_effect_at,
                record.state_before_sha256,
                record.state_after_sha256,
                canonical_json(record.result.as_payload()),
                record.result.result_sha256,
                record.applied_at,
            ),
        )
        if cursor.rowcount != 1:
            raise FinancialFillApplicationError("FINANCIAL_APPLICATION_LEDGER_INSERT_FAILED")

    @staticmethod
    def _update_engine_state_cas_in_transaction(
        connection: sqlite3.Connection,
        *,
        engine: str,
        expected_raw_payload: str,
        state_after_payload: Mapping[str, Any],
        applied_at: str,
    ) -> None:
        cursor = connection.execute(
            """
            UPDATE engine_state SET payload = ?, updated_at = ?
            WHERE engine = ? AND payload = ?
            """,
            (canonical_json(state_after_payload), applied_at, engine, expected_raw_payload),
        )
        if cursor.rowcount != 1:
            raise FinancialFillApplicationError("FINANCIAL_APPLICATION_STATE_CONFLICT")

    @staticmethod
    def _insert_financial_fill_trade_in_transaction(
        connection: sqlite3.Connection,
        trade_payload: Mapping[str, Any] | None,
    ) -> bool:
        if trade_payload is None:
            return False
        connection.execute(
            """
            INSERT INTO trades(
                exit_ts, entry_ts, strategy, direction, qty, entry_price,
                exit_price, pnl, bars_held, reason
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trade_payload["exit_ts"],
                trade_payload["entry_ts"],
                trade_payload["strategy"],
                trade_payload["direction"],
                trade_payload["qty"],
                trade_payload["entry_price"],
                trade_payload["exit_price"],
                trade_payload["pnl"],
                trade_payload["bars_held"],
                trade_payload["reason"],
            ),
        )
        return True

    def apply_financial_fill_atomically(
        self, *, local_order_id: int, fill_key: str
    ) -> FinancialFillCommitResult:
        """Atomically claim one qualified external fill financially."""

        if self.read_only:
            raise RuntimeError("Un StateStore read-only ne peut pas appliquer un fill")
        if (
            isinstance(local_order_id, bool)
            or not isinstance(local_order_id, int)
            or local_order_id <= 0
        ):
            raise ValueError("local_order_id must be a positive integer")
        if not isinstance(fill_key, str) or not fill_key.strip():
            raise ValueError("fill_key must be a non-empty string")
        fill_key = fill_key.strip()

        with self._transaction() as connection:
            order_row = connection.execute(
                "SELECT * FROM orders WHERE id = ?", (local_order_id,)
            ).fetchone()
            if order_row is None:
                raise FinancialFillApplicationError("FINANCIAL_ORDER_MISSING")
            plan_row = connection.execute(
                "SELECT * FROM financial_application_plans WHERE local_order_id = ?",
                (local_order_id,),
            ).fetchone()
            if plan_row is None:
                raise FinancialFillApplicationError("LEGACY_APPLICATION_CONTEXT_INCOMPLETE")
            try:
                persisted_plan = self._plan_from_row(plan_row, order_row)
            except (FinancialApplicationPlanConflict, ValueError, TypeError) as error:
                raise FinancialFillApplicationError(
                    "FINANCIAL_APPLICATION_PLAN_CONFLICT"
                ) from error

            fill_rows = connection.execute(
                "SELECT * FROM external_fills WHERE local_order_id = ? ORDER BY id",
                (local_order_id,),
            ).fetchall()
            fills_by_key = {
                str(row["fill_key"]): self._external_fill_from_row(row) for row in fill_rows
            }
            fill = fills_by_key.get(fill_key)
            if fill is None:
                raise FinancialFillApplicationError("FINANCIAL_FILL_NOT_FOUND")

            chain = self._read_financial_fill_application_chain_in_transaction(
                connection, local_order_id
            )
            chain_fills: tuple[ExternalFill, ...] = tuple(
                fills_by_key[record.fill_key] for record in chain
            )
            existing = next((record for record in chain if record.fill_key == fill_key), None)
            if existing is not None:
                head = chain[-1]
                return FinancialFillCommitResult(
                    application=existing,
                    applied=False,
                    already_applied=True,
                    application_index=existing.application_index,
                    state_after_sha256=head.state_after_sha256,
                    ledger_head_application_key=head.application_key,
                    trade_inserted=False,
                    event_id=None,
                )

            if order_row["local_state"] != LocalOrderState.PENDING_RECONCILIATION.value:
                raise FinancialFillApplicationError("FINANCIAL_ORDER_NOT_RECONCILIATION_READY")
            current_payload, current_raw_payload = self._load_engine_state_in_transaction(
                connection, persisted_plan.plan.identity.engine
            )
            current_state_sha256 = sha256_json(current_payload)
            expected_state_before = (
                persisted_plan.plan.pre_state_sha256 if not chain else chain[-1].state_after_sha256
            )
            if current_state_sha256 != expected_state_before:
                raise FinancialFillApplicationError("FINANCIAL_APPLICATION_STATE_CONFLICT")
            self._assert_positions_projection_in_transaction(
                connection,
                engine=persisted_plan.plan.identity.engine,
                payload=current_payload,
            )

            assessment = self._financial_fill_assessment_in_transaction(connection, local_order_id)
            request = FinancialFillApplicationRequest(
                persisted_plan=persisted_plan,
                fill=fill,
                assessment=assessment,
                current_state_payload=current_payload,
                current_state_sha256=current_state_sha256,
                previously_applied_fills=chain_fills,
            )
            result = calculate_financial_fill_application(request)
            applied_at = utc_now()
            record = PersistedFinancialFillApplication(
                application_key=result.application_key,
                application_version=result.application_version,
                local_order_id=result.local_order_id,
                intent_id=result.intent_id,
                plan_key=result.plan_key,
                fill_key=result.fill_key,
                application_index=(chain[-1].application_index + 1) if chain else 0,
                previous_application_key=chain[-1].application_key if chain else None,
                transition_type=result.transition_type,
                economic_effect_at=result.economic_effect_at,
                state_before_sha256=result.state_before_sha256,
                state_after_sha256=result.state_after_sha256,
                result=result,
                applied_at=applied_at,
            )
            self._insert_financial_fill_application_in_transaction(connection, record)
            self._update_engine_state_cas_in_transaction(
                connection,
                engine=persisted_plan.plan.identity.engine,
                expected_raw_payload=current_raw_payload,
                state_after_payload=result.state_after_payload,
                applied_at=applied_at,
            )
            self._sync_positions(
                connection,
                persisted_plan.plan.identity.engine,
                result.state_after_payload,
                applied_at,
            )
            trade_inserted = self._insert_financial_fill_trade_in_transaction(
                connection, result.trade_payload
            )
            event_id = self._insert_event(
                connection,
                persisted_plan.plan.identity.engine,
                "FINANCIAL_FILL_APPLIED",
                {
                    "application_key": record.application_key,
                    "application_index": record.application_index,
                    "plan_key": record.plan_key,
                    "fill_key": record.fill_key,
                    "venue_fill_id": fill.venue_fill_id,
                    "transition_type": FinancialTransitionType(record.transition_type).value,
                    "quantity": record.result.quantity,
                    "price": record.result.price,
                    "fee": record.result.fee,
                    "fee_asset": record.result.fee_asset,
                    "economic_effect_at": record.economic_effect_at,
                    "state_before_sha256": record.state_before_sha256,
                    "state_after_sha256": record.state_after_sha256,
                    "trade_inserted": trade_inserted,
                },
                aggregate_type="order",
                aggregate_id=str(local_order_id),
                correlation_id=record.application_key,
                ts=applied_at,
            )
            return FinancialFillCommitResult(
                application=record,
                applied=True,
                already_applied=False,
                application_index=record.application_index,
                state_after_sha256=record.state_after_sha256,
                ledger_head_application_key=record.application_key,
                trade_inserted=trade_inserted,
                event_id=event_id,
            )

    def record_submission_error(self, order_id: int, *, error: str, ambiguous: bool) -> None:
        now = utc_now()
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
        now = utc_now()
        with self._transaction() as connection:
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
                    # API historique sans frontière explicite avant/après
                    # appel broker : ne jamais y inventer la preuve que la
                    # soumission n'a pas commencé.
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
            return order_id

    def begin_order_and_checkpoint(
        self,
        engine: str,
        slot: str,
        intent_id: str,
        order_type: str,
        side: str,
        requested_qty: float,
        reason: str,
        state: Mapping[str, Any],
        reference_price: float | None = None,
    ) -> int:
        """Journalise l'intention et l'état transitoire dans une transaction."""

        now = utc_now()
        with self._transaction() as connection:
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
                    # Même règle que begin_order : seul reserve_market_order
                    # peut prouver INTENT_CREATED avant mark_order_submitting.
                    LocalOrderState.PENDING_RECONCILIATION.value,
                    reason,
                    now,
                    now,
                ),
            )
            order_id = cursor.lastrowid
            assert order_id is not None
            connection.execute(
                """
                INSERT INTO engine_state(engine, payload, updated_at) VALUES(?, ?, ?)
                ON CONFLICT(engine) DO UPDATE SET
                    payload=excluded.payload, updated_at=excluded.updated_at
                """,
                (engine, self._json(state), now),
            )
            self._sync_positions(connection, engine, state, now)
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
            self._insert_event(
                connection,
                engine,
                "transitional_checkpoint",
                self._state_event(
                    state,
                    {
                        "order_id": order_id,
                        "execution_state": state.get("execution_state"),
                    },
                ),
                "engine",
                engine,
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
        now = utc_now()
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT engine, intent_id, logical_order_key, local_state,
                       external_state, remaining_qty
                FROM orders WHERE id = ?
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
                    broker_order_id=?, error=?, updated_at=?
                WHERE id=?
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

    def complete_order_and_checkpoint(
        self,
        order_id: int,
        *,
        engine: str,
        state: Mapping[str, Any],
        status: str,
        filled_qty: float = 0.0,
        remaining_qty: float | None = None,
        price: float | None = None,
        fee: float = 0.0,
        broker_order_id: str | None = None,
        error: str | None = None,
        trade: dict[str, Any] | None = None,
        external_state: ExternalOrderState | str | None = None,
    ) -> None:
        """Valide résultat d'ordre, position/checkpoint et trade atomiquement."""

        now = utc_now()
        with self._transaction() as connection:
            order = connection.execute(
                """
                SELECT engine, intent_id, logical_order_key, local_state,
                       external_state, remaining_qty
                FROM orders WHERE id = ?
                """,
                (order_id,),
            ).fetchone()
            if order is None:
                raise KeyError(f"Ordre journalisé introuvable : {order_id}")
            if order["engine"] != engine:
                raise ValueError("L'ordre et le checkpoint appartiennent à deux moteurs différents")
            resolved_local = self._local_state_for_legacy_status(status)
            resolved_external = (
                ExternalOrderState(external_state)
                if external_state is not None
                else ExternalOrderState(order["external_state"])
                if order["external_state"] is not None
                else self._external_state_for_legacy_status(status)
            )
            if (
                order["logical_order_key"] is not None
                and resolved_local == LocalOrderState.TERMINAL
                and (resolved_external is None or not resolved_external.is_terminal)
            ):
                raise InvalidOrderStateTransition(
                    f"Ordre {order_id}: checkpoint terminal sans preuve externe terminale"
                )
            resolved_remaining = (
                remaining_qty
                if remaining_qty is not None
                else 0.0
                if resolved_local == LocalOrderState.TERMINAL
                else float(order["remaining_qty"])
            )
            connection.execute(
                """
                UPDATE orders SET status=?, local_state=?, external_state=?,
                    filled_qty=?, remaining_qty=?, price=?, fee=?,
                    broker_order_id=?, error=?, updated_at=?
                WHERE id=?
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
            connection.execute(
                """
                INSERT INTO engine_state(engine, payload, updated_at) VALUES(?, ?, ?)
                ON CONFLICT(engine) DO UPDATE SET
                    payload=excluded.payload, updated_at=excluded.updated_at
                """,
                (engine, self._json(state), now),
            )
            self._sync_positions(connection, engine, state, now)
            self._insert_event(
                connection,
                engine,
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
                order["intent_id"],
            )
            self._insert_event(
                connection,
                engine,
                "order_checkpoint",
                self._state_event(state, {"order_id": order_id}),
                "engine",
                engine,
                order["intent_id"],
            )
            if trade is not None:
                connection.execute(
                    """
                    INSERT INTO trades(
                        exit_ts, entry_ts, strategy, direction, qty, entry_price,
                        exit_price, pnl, bars_held, reason
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        trade["exit_ts"],
                        trade["entry_ts"],
                        trade["strategy"],
                        trade["direction"],
                        trade["qty"],
                        trade["entry_price"],
                        trade["exit_price"],
                        trade["pnl"],
                        trade["bars_held"],
                        trade["reason"],
                    ),
                )
                self._insert_event(
                    connection,
                    engine,
                    "trade_closed",
                    trade,
                    "strategy",
                    str(trade["strategy"]),
                    order["intent_id"],
                )

    def record_observed_fill_and_checkpoint(
        self,
        *,
        engine: str,
        slot: str,
        intent_id: str,
        broker_order_id: str,
        side: str,
        requested_qty: float,
        filled_qty: float,
        price: float,
        fee: float,
        reason: str,
        state: Mapping[str, Any],
        trade: dict[str, Any],
    ) -> bool:
        """Matérialise atomiquement un fill externe observé hors processus.

        Un stop peut être exécuté par l'exchange pendant l'arrêt du runner.
        L'insertion terminale, le checkpoint et le trade sont donc regroupés,
        et ``intent_id`` rend l'observation idempotente.
        """

        now = utc_now()
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT id FROM orders WHERE intent_id = ?",
                (intent_id,),
            ).fetchone()
            if existing is not None:
                return False
            cursor = connection.execute(
                """
                INSERT INTO orders(
                    engine, slot, intent_id, broker_order_id, order_type, side,
                    requested_qty, reference_price, filled_qty, remaining_qty,
                    price, fee, local_state, external_state, status, reason,
                    created_at, updated_at
                ) VALUES(
                    ?, ?, ?, ?, 'STOP', ?, ?, ?, ?, 0, ?, ?,
                    'TERMINAL', 'FILLED', 'FILLED', ?, ?, ?
                )
                """,
                (
                    engine,
                    slot,
                    intent_id,
                    broker_order_id,
                    side,
                    requested_qty,
                    price,
                    filled_qty,
                    price,
                    fee,
                    reason,
                    now,
                    now,
                ),
            )
            order_id = cursor.lastrowid
            assert order_id is not None
            connection.execute(
                """
                INSERT INTO engine_state(engine, payload, updated_at) VALUES(?, ?, ?)
                ON CONFLICT(engine) DO UPDATE SET
                    payload=excluded.payload, updated_at=excluded.updated_at
                """,
                (engine, self._json(state), now),
            )
            self._sync_positions(connection, engine, state, now)
            connection.execute(
                """
                INSERT INTO trades(
                    exit_ts, entry_ts, strategy, direction, qty, entry_price,
                    exit_price, pnl, bars_held, reason
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade["exit_ts"],
                    trade["entry_ts"],
                    trade["strategy"],
                    trade["direction"],
                    trade["qty"],
                    trade["entry_price"],
                    trade["exit_price"],
                    trade["pnl"],
                    trade["bars_held"],
                    trade["reason"],
                ),
            )
            self._insert_event(
                connection,
                engine,
                "external_stop_fill_observed",
                self._state_event(
                    state,
                    {
                        "order_id": order_id,
                        "broker_order_id": broker_order_id,
                        "filled_qty": filled_qty,
                        "price": price,
                        "fee": fee,
                    },
                ),
                "order",
                str(order_id),
                intent_id,
            )
            self._insert_event(
                connection,
                engine,
                "trade_closed",
                trade,
                "strategy",
                str(trade["strategy"]),
                intent_id,
            )
        return True

    def pending_orders(self, engine: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM orders
                WHERE engine = ? AND status = 'PENDING'
                ORDER BY id
                """,
                (engine,),
            ).fetchall()
        return [dict(row) for row in rows]

    def unresolved_orders(self, engine: str) -> list[dict[str, Any]]:
        """Ordres qui interdisent une reprise normale du moteur."""

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
                "SELECT * FROM orders WHERE intent_id = ?",
                (intent_id,),
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

    def read_events(
        self,
        engine: str | None = None,
        *,
        limit: int | None = None,
        since_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Journal, du plus ancien au plus récent.

        ``limit`` retient les N ÉVÉNEMENTS LES PLUS RÉCENTS, tout en les
        renvoyant dans l'ordre chronologique : un appelant qui veut afficher
        l'activité récente n'a pas à charger l'intégralité du journal, dont la
        taille croît d'un checkpoint par tick.
        """

        conditions: list[str] = []
        params: list[Any] = []
        if engine is not None:
            conditions.append("engine = ?")
            params.append(engine)
        if since_id is not None:
            conditions.append("id > ?")
            params.append(since_id)
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        if limit is None:
            query = f"SELECT * FROM events{where} ORDER BY id"
        else:
            if limit <= 0:
                raise ValueError("limit doit être strictement positif")
            query = (
                f"SELECT * FROM (SELECT * FROM events{where} ORDER BY id DESC LIMIT ?) ORDER BY id"
            )
            params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [dict(row) for row in rows]

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
        """Crée, réouvre ou actualise un incident sans dupliquer son identité."""

        if severity not in ("WARNING", "CRITICAL"):
            raise ValueError("severity doit valoir WARNING ou CRITICAL")
        now = utc_now()
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
                    self._json(context or {}),
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
        now = utc_now()
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE incidents
                SET status='RESOLVED', resolved_at=?
                WHERE fingerprint=? AND status='OPEN'
                """,
                (now, fingerprint),
            )
        return cursor.rowcount > 0

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
        with self._connect() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def record_trade(self, trade: dict[str, Any]) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO trades(
                    exit_ts, entry_ts, strategy, direction, qty, entry_price,
                    exit_price, pnl, bars_held, reason
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade["exit_ts"],
                    trade["entry_ts"],
                    trade["strategy"],
                    trade["direction"],
                    trade["qty"],
                    trade["entry_price"],
                    trade["exit_price"],
                    trade["pnl"],
                    trade["bars_held"],
                    trade["reason"],
                ),
            )
            self._insert_event(
                connection,
                "trend",
                "trade_closed",
                trade,
                "strategy",
                str(trade["strategy"]),
            )

    def append_equity(self, engine: str, equity: float, ts: str | None = None) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO equity_samples(engine, ts, equity)
                VALUES(?, ?, ?)
                """,
                (engine, ts or utc_now(), equity),
            )

    def read_equity(self, engine: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT ts, equity FROM equity_samples
                WHERE engine = ? ORDER BY ts
                """,
                (engine,),
            ).fetchall()
        return [dict(row) for row in rows]

    def read_trades(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM trades ORDER BY exit_ts").fetchall()
        return [dict(row) for row in rows]

    def read_flows(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM flows ORDER BY ts").fetchall()
        return [dict(row) for row in rows]

    def engine_age_seconds(
        self,
        engine: str,
        *,
        now: datetime | None = None,
    ) -> float | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT updated_at FROM engine_state WHERE engine = ?", (engine,)
            ).fetchone()
        if row is None:
            return None
        updated = datetime.fromisoformat(row["updated_at"])
        return ((now or datetime.now(UTC)) - updated).total_seconds()

    def engine_updated_at(self, engine: str) -> datetime | None:
        """Return the persisted engine timestamp without filesystem inference."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT updated_at FROM engine_state WHERE engine = ?", (engine,)
            ).fetchone()
        if row is None:
            return None
        parsed = datetime.fromisoformat(str(row["updated_at"]))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)

    def integrity_check(self) -> bool:
        with self._connect() as connection:
            row = connection.execute("PRAGMA integrity_check").fetchone()
        return bool(row and row[0] == "ok")

    def start_qualification_campaign(
        self,
        *,
        protocol_version: int,
        policy: dict[str, Any],
        started_at: str | None = None,
    ) -> dict[str, Any]:
        """Démarre une campagne immuable ; une seule peut être active."""

        now = started_at or utc_now()
        with self._transaction() as connection:
            active = connection.execute(
                "SELECT id FROM qualification_campaigns WHERE status = 'RUNNING'"
            ).fetchone()
            if active is not None:
                raise RuntimeError(f"La campagne de qualification {active['id']} est déjà active")
            cursor = connection.execute(
                """
                INSERT INTO qualification_campaigns(
                    protocol_version, status, policy, started_at
                ) VALUES(?, 'RUNNING', ?, ?)
                """,
                (protocol_version, self._json(policy), now),
            )
            campaign_id = cursor.lastrowid
        assert campaign_id is not None
        return self.read_qualification_campaign(int(campaign_id))

    def read_qualification_campaign(self, campaign_id: int) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM qualification_campaigns WHERE id = ?",
                (campaign_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Campagne de qualification introuvable : {campaign_id}")
        result = dict(row)
        result["policy"] = json.loads(result["policy"])
        if result["final_report"]:
            result["final_report"] = json.loads(result["final_report"])
        return result

    def active_qualification_campaign(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id FROM qualification_campaigns
                WHERE status = 'RUNNING' ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
        return self.read_qualification_campaign(int(row["id"])) if row else None

    def latest_passed_qualification(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id FROM qualification_campaigns
                WHERE status = 'PASSED' ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
        return self.read_qualification_campaign(int(row["id"])) if row else None

    def save_readiness_report(
        self,
        report: dict[str, Any],
        *,
        campaign_id: int | None,
    ) -> int:
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO readiness_reports(
                    campaign_id, protocol_version, status, generated_at, payload
                ) VALUES(?, ?, ?, ?, ?)
                """,
                (
                    campaign_id,
                    int(report["protocol_version"]),
                    str(report["status"]),
                    str(report["generated_at"]),
                    self._json(report),
                ),
            )
        assert cursor.lastrowid is not None
        return int(cursor.lastrowid)

    def latest_readiness_report(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM readiness_reports ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def finish_qualification_campaign(
        self,
        campaign_id: int,
        *,
        status: str,
        final_report: dict[str, Any] | None = None,
        ended_at: str | None = None,
    ) -> None:
        if status not in ("PASSED", "CANCELED"):
            raise ValueError("status doit valoir PASSED ou CANCELED")
        if status == "PASSED" and (final_report is None or final_report.get("status") != "PASS"):
            raise ValueError("Une campagne ne peut passer qu'avec un rapport PASS")
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE qualification_campaigns
                SET status=?, ended_at=?, final_report=?
                WHERE id=? AND status='RUNNING'
                """,
                (
                    status,
                    ended_at or utc_now(),
                    self._json(final_report) if final_report else None,
                    campaign_id,
                ),
            )
        if cursor.rowcount != 1:
            raise RuntimeError("La campagne n'est plus active")

    #: Événements de simple checkpoint périodique. Ils portent l'état complet
    #: du moteur et sont réémis à chaque tick : leur valeur d'audit décroît
    #: immédiatement, contrairement aux ordres, fills, stops et flux.
    ROUTINE_EVENT_TYPES = ("checkpoint", "state_checkpoint")

    def compact_events(self, cutoff: str, *, keep_per_engine: int = 500) -> tuple[int, int]:
        """Purge les checkpoints périodiques anciens, garde tout le reste.

        Le journal grossit d'un événement par tick et par moteur — environ
        1 440 par jour pour le trend — chacun portant l'état sérialisé complet
        et son SHA-256. Sur une campagne de 90 jours la table dépasse la
        centaine de milliers de lignes, que `read_events` chargeait
        intégralement en mémoire.

        Ce qui est SUPPRIMÉ : les checkpoints de routine antérieurs à
        ``cutoff``, au-delà des ``keep_per_engine`` plus récents de chaque
        moteur. Ce qui est CONSERVÉ inconditionnellement : tout événement
        d'ordre, de fill, de stop protecteur, de funding, de flux de capital ou
        de migration — c'est-à-dire toute la trace d'audit qui a une valeur
        après coup. La reconstruction d'état par `replay_engine_state` reste
        possible sur la fenêtre conservée.
        """

        placeholders = ",".join("?" for _ in self.ROUTINE_EVENT_TYPES)
        with self._transaction() as connection:
            before = int(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0])
            engines = [
                row[0]
                for row in connection.execute(
                    f"SELECT DISTINCT engine FROM events WHERE event_type IN ({placeholders})",
                    self.ROUTINE_EVENT_TYPES,
                ).fetchall()
            ]
            for engine in engines:
                connection.execute(
                    f"""
                    DELETE FROM events
                    WHERE engine = ?
                      AND event_type IN ({placeholders})
                      AND ts < ?
                      AND id NOT IN (
                        SELECT id FROM events
                        WHERE engine = ? AND event_type IN ({placeholders})
                        ORDER BY id DESC LIMIT ?
                      )
                    """,
                    (
                        engine,
                        *self.ROUTINE_EVENT_TYPES,
                        cutoff,
                        engine,
                        *self.ROUTINE_EVENT_TYPES,
                        keep_per_engine,
                    ),
                )
            after = int(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0])
        return before, after

    def compact_equity(self, engine: str, cutoff: str, *, min_rows: int = 5_000) -> tuple[int, int]:
        """Conserve un point / 5 min avant ``cutoff`` et tous les points récents.

        Un point horaire laissait 50 minutes « stale » pour une fraîcheur
        readiness de 10 minutes, ce qui faisait échouer l'uptime d'une
        campagne saine. Cinq minutes restent couvertes par le seuil 600 s.
        """

        with self._transaction() as connection:
            before = int(
                connection.execute(
                    "SELECT COUNT(*) FROM equity_samples WHERE engine = ?", (engine,)
                ).fetchone()[0]
            )
            if before < min_rows:
                return before, before
            connection.execute(
                """
                DELETE FROM equity_samples
                WHERE engine = ? AND ts < ?
                  AND ts NOT IN (
                    SELECT MAX(ts) FROM equity_samples
                    WHERE engine = ? AND ts < ?
                    GROUP BY substr(ts, 1, 14)
                           || printf(
                                '%02d',
                                (CAST(substr(ts, 15, 2) AS INTEGER) / 5) * 5
                              )
                  )
                """,
                (engine, cutoff, engine, cutoff),
            )
            after = int(
                connection.execute(
                    "SELECT COUNT(*) FROM equity_samples WHERE engine = ?", (engine,)
                ).fetchone()[0]
            )
        return before, after
