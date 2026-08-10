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
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import InvalidOrderStateTransition, OrderIdentityCollision
from .order_state import ExternalOrderState, LocalOrderState, LogicalOrderIdentity

SCHEMA_VERSION = 5
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
    def __init__(self, path: str | Path, *, initialize: bool = True) -> None:
        self.path = Path(path)
        if initialize:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._initialize()
        elif not self.path.exists():
            raise FileNotFoundError(self.path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
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
            if row is None:
                connection.execute(
                    "INSERT INTO metadata(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
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
            current_version = SCHEMA_VERSION
            connection.execute(
                "UPDATE metadata SET value = ? WHERE key = 'schema_version'",
                (str(current_version),),
            )
            # WAL est persistant. Il est activé hors d'une transaction sur
            # certaines versions SQLite ; l'échec est sans impact fonctionnel.
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")

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
                aggregate_type="engine",
                aggregate_id=engine,
            )

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
    ) -> None:
        connection.execute(
            """
            INSERT INTO events(
                ts, engine, event_type, aggregate_type, aggregate_id,
                payload, correlation_id
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now(),
                engine,
                event_type,
                aggregate_type,
                aggregate_id,
                self._json(payload),
                correlation_id,
            ),
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

    def compact_equity(self, engine: str, cutoff: str) -> tuple[int, int]:
        """Conserve un point horaire avant ``cutoff`` et tous les points récents."""

        with self._transaction() as connection:
            before = int(
                connection.execute(
                    "SELECT COUNT(*) FROM equity_samples WHERE engine = ?", (engine,)
                ).fetchone()[0]
            )
            if before < 5_000:
                return before, before
            connection.execute(
                """
                DELETE FROM equity_samples
                WHERE engine = ? AND ts < ?
                  AND ts NOT IN (
                    SELECT MAX(ts) FROM equity_samples
                    WHERE engine = ? AND ts < ?
                    GROUP BY substr(ts, 1, 13)
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
