"""Persistance transactionnelle et journal opérationnel SQLite.

La base est la source de vérité des runners. Chaque méthode d'écriture ouvre
une transaction ``BEGIN IMMEDIATE`` afin qu'un checkpoint soit soit entièrement
visible, soit entièrement absent après un crash.
"""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 3


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


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
                    broker_order_id TEXT,
                    order_type TEXT NOT NULL,
                    side TEXT NOT NULL,
                    requested_qty REAL NOT NULL CHECK(requested_qty >= 0),
                    reference_price REAL,
                    filled_qty REAL NOT NULL DEFAULT 0 CHECK(filled_qty >= 0),
                    price REAL,
                    fee REAL NOT NULL DEFAULT 0,
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
                connection.execute(
                    "UPDATE metadata SET value = ? WHERE key = 'schema_version'",
                    (str(current_version),),
                )
            # WAL est persistant. Il est activé hors d'une transaction sur
            # certaines versions SQLite ; l'échec est sans impact fonctionnel.
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")

    @staticmethod
    def _json(payload: Any) -> str:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def _state_event(
        cls,
        state: dict[str, Any],
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
        payload: dict[str, Any],
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

    def save_states_and_flow(
        self,
        states: dict[str, dict[str, Any]],
        *,
        kind: str,
        trend_flow: float,
        carry_flow: float,
    ) -> None:
        self.save_states_and_flows(
            states,
            [
                {
                    "kind": kind,
                    "trend_flow": trend_flow,
                    "carry_flow": carry_flow,
                }
            ],
        )

    def save_states_and_flows(
        self,
        states: dict[str, dict[str, Any]],
        flows: list[dict[str, Any]],
    ) -> None:
        """Checkpoint de plusieurs moteurs et flux dans une seule transaction."""

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

    def _sync_positions(
        self,
        connection: sqlite3.Connection,
        engine: str,
        payload: dict[str, Any],
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

    def append_event(
        self,
        engine: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        aggregate_type: str | None = None,
        aggregate_id: str | None = None,
        correlation_id: str | None = None,
    ) -> None:
        with self._transaction() as connection:
            self._insert_event(
                connection,
                engine,
                event_type,
                payload,
                aggregate_type,
                aggregate_id,
                correlation_id,
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
                    reference_price, status, reason, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?)
                """,
                (
                    engine,
                    slot,
                    intent_id,
                    order_type,
                    side,
                    requested_qty,
                    reference_price,
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
        state: dict[str, Any],
        reference_price: float | None = None,
    ) -> int:
        """Journalise l'intention et l'état transitoire dans une transaction."""

        now = utc_now()
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO orders(
                    engine, slot, intent_id, order_type, side, requested_qty,
                    reference_price, status, reason, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?)
                """,
                (
                    engine,
                    slot,
                    intent_id,
                    order_type,
                    side,
                    requested_qty,
                    reference_price,
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
        price: float | None = None,
        fee: float = 0.0,
        broker_order_id: str | None = None,
        error: str | None = None,
    ) -> None:
        now = utc_now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT engine, intent_id FROM orders WHERE id = ?", (order_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Ordre journalisé introuvable : {order_id}")
            connection.execute(
                """
                UPDATE orders SET status=?, filled_qty=?, price=?, fee=?,
                    broker_order_id=?, error=?, updated_at=?
                WHERE id=?
                """,
                (
                    status,
                    filled_qty,
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
                    "filled_qty": filled_qty,
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
        state: dict[str, Any],
        status: str,
        filled_qty: float = 0.0,
        price: float | None = None,
        fee: float = 0.0,
        broker_order_id: str | None = None,
        error: str | None = None,
        trade: dict[str, Any] | None = None,
    ) -> None:
        """Valide résultat d'ordre, position/checkpoint et trade atomiquement."""

        now = utc_now()
        with self._transaction() as connection:
            order = connection.execute(
                "SELECT engine, intent_id FROM orders WHERE id = ?", (order_id,)
            ).fetchone()
            if order is None:
                raise KeyError(f"Ordre journalisé introuvable : {order_id}")
            if order["engine"] != engine:
                raise ValueError("L'ordre et le checkpoint appartiennent à deux moteurs différents")
            connection.execute(
                """
                UPDATE orders SET status=?, filled_qty=?, price=?, fee=?,
                    broker_order_id=?, error=?, updated_at=?
                WHERE id=?
                """,
                (
                    status,
                    filled_qty,
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
                    "filled_qty": filled_qty,
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
        state: dict[str, Any],
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
                    requested_qty, reference_price, filled_qty, price, fee,
                    status, reason, created_at, updated_at
                ) VALUES(?, ?, ?, ?, 'STOP', ?, ?, ?, ?, ?, ?, 'FILLED', ?, ?, ?)
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
                WHERE engine = ? AND status IN ('PENDING', 'OPEN', 'UNBALANCED')
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

    def read_events(self, engine: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM events"
        params: tuple[str, ...] = ()
        if engine is not None:
            query += " WHERE engine = ?"
            params = (engine,)
        query += " ORDER BY id"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
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
