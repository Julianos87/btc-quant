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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import ReconciliationRequired

SCHEMA_VERSION = 5
DEPOSIT_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,127}")


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
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS engine_state (
                    engine TEXT PRIMARY KEY,
                    payload TEXT NOT NULL CHECK(json_valid(payload)),
                    updated_at TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0)
                );

                CREATE TRIGGER IF NOT EXISTS engine_state_revision_bump
                AFTER UPDATE OF payload ON engine_state
                WHEN NEW.revision = OLD.revision
                BEGIN
                    UPDATE engine_state
                    SET revision = OLD.revision + 1
                    WHERE engine = NEW.engine;
                END;

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
                if current_version < 5:
                    columns = {
                        item["name"]
                        for item in connection.execute("PRAGMA table_info(engine_state)").fetchall()
                    }
                    if "revision" not in columns:
                        connection.execute(
                            "ALTER TABLE engine_state ADD COLUMN revision INTEGER NOT NULL DEFAULT 0"
                        )
                    current_version = 5
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

    def load_engine_state_with_revision(
        self,
        engine: str,
    ) -> tuple[dict[str, Any] | None, int]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload, revision FROM engine_state WHERE engine = ?", (engine,)
            ).fetchone()
        if row is None:
            return None, 0
        return json.loads(row["payload"]), int(row["revision"])

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

    def _checkpoint_payload(
        self,
        connection: sqlite3.Connection,
        engine: str,
        payload: Mapping[str, Any],
        *,
        preserve_active_transitions: bool = True,
        allowed_transition_intent: str | None = None,
    ) -> dict[str, Any]:
        candidate = json.loads(self._json(payload))
        if not isinstance(candidate, dict):
            raise ValueError("État engine invalide : objet JSON attendu")
        row = connection.execute(
            "SELECT payload FROM engine_state WHERE engine = ?", (engine,)
        ).fetchone()
        existing: dict[str, Any] | None = None
        if row is not None:
            loaded = json.loads(row["payload"])
            if isinstance(loaded, dict):
                existing = loaded
                if loaded.get("reconciliation_required"):
                    candidate["reconciliation_required"] = True

        if existing is None:
            return candidate

        existing_slots = existing.get("slots")
        candidate_slots = candidate.get("slots")
        if not isinstance(existing_slots, Mapping):
            return candidate
        if not isinstance(candidate_slots, dict):
            raise ReconciliationRequired(
                f"Checkpoint {engine} invalide : impossible de préserver les transitions actives"
            )

        for slot, existing_slot_value in existing_slots.items():
            if not isinstance(existing_slot_value, Mapping):
                continue
            existing_transition = existing_slot_value.get("active_transition")
            if not isinstance(existing_transition, Mapping):
                continue
            intent_id = existing_transition.get("intent_id")
            if not isinstance(intent_id, str) or not intent_id:
                raise ReconciliationRequired(f"Transition active invalide pour {engine}/{slot}")
            order = connection.execute(
                "SELECT status FROM orders WHERE intent_id = ?", (intent_id,)
            ).fetchone()
            if order is None:
                raise ReconciliationRequired(
                    f"Transition {intent_id} sans intention SQLite pour {engine}/{slot}"
                )
            if str(order["status"]) not in {"PENDING", "OPEN", "UNBALANCED"}:
                continue

            candidate_slot = candidate_slots.get(slot)
            if not isinstance(candidate_slot, dict):
                raise ReconciliationRequired(
                    f"Checkpoint {engine}/{slot} absent alors que l'intention {intent_id} est active"
                )
            candidate_transition = candidate_slot.get("active_transition")
            candidate_intent = (
                candidate_transition.get("intent_id")
                if isinstance(candidate_transition, Mapping)
                else None
            )
            if preserve_active_transitions:
                if candidate_transition is None:
                    candidate_slot["active_transition"] = dict(existing_transition)
                    if candidate_slot.get("position_cycle_id") is None:
                        candidate_slot["position_cycle_id"] = existing_slot_value.get(
                            "position_cycle_id"
                        )
                elif candidate_intent != intent_id:
                    raise ReconciliationRequired(
                        f"Checkpoint {engine}/{slot} tente d'écraser l'intention active {intent_id}"
                    )
            else:
                if intent_id != allowed_transition_intent:
                    raise ReconciliationRequired(
                        f"Checkpoint {engine}/{slot} ne possède pas l'intention active {intent_id}"
                    )
                if candidate_transition is not None and candidate_intent != intent_id:
                    raise ReconciliationRequired(
                        f"Checkpoint {engine}/{slot} tente de remplacer l'intention {intent_id}"
                    )
        return candidate

    def _write_engine_state(
        self,
        connection: sqlite3.Connection,
        engine: str,
        payload: Mapping[str, Any],
        now: str,
        *,
        expected_revision: int | None = None,
    ) -> int:
        row = connection.execute(
            "SELECT revision FROM engine_state WHERE engine = ?", (engine,)
        ).fetchone()
        current_revision = int(row["revision"]) if row is not None else 0
        if expected_revision is not None and expected_revision != current_revision:
            raise ReconciliationRequired(
                f"Checkpoint périmé pour {engine}: attendu revision {expected_revision}, "
                f"durable {current_revision}"
            )
        serialized = self._json(payload)
        next_revision = current_revision + 1
        if row is None:
            connection.execute(
                "INSERT INTO engine_state(engine, payload, updated_at, revision) VALUES(?, ?, ?, ?)",
                (engine, serialized, now, next_revision),
            )
            return next_revision
        cursor = connection.execute(
            "UPDATE engine_state SET payload=?, updated_at=?, revision=? "
            "WHERE engine=? AND revision=?",
            (serialized, now, next_revision, engine, current_revision),
        )
        if cursor.rowcount != 1:
            raise ReconciliationRequired(f"Écriture concurrente du checkpoint {engine}")
        return next_revision

    def save_engine_state(
        self,
        engine: str,
        payload: Mapping[str, Any],
        *,
        event_type: str = "checkpoint",
        event_payload: dict[str, Any] | None = None,
        expected_revision: int | None = None,
    ) -> int:
        now = utc_now()
        with self._transaction() as connection:
            checkpoint = self._checkpoint_payload(connection, engine, payload)
            revision = self._write_engine_state(
                connection, engine, checkpoint, now, expected_revision=expected_revision
            )
            self._sync_positions(connection, engine, checkpoint, now)
            self._insert_event(
                connection,
                engine,
                event_type,
                self._state_event(checkpoint, event_payload),
                aggregate_type="engine",
                aggregate_id=engine,
            )

        return revision

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
                checkpoint = self._checkpoint_payload(connection, engine, payload)
                connection.execute(
                    """
                    INSERT INTO engine_state(engine, payload, updated_at) VALUES(?, ?, ?)
                    ON CONFLICT(engine) DO UPDATE SET
                        payload=excluded.payload, updated_at=excluded.updated_at
                    """,
                    (engine, self._json(checkpoint), now),
                )
                self._sync_positions(connection, engine, checkpoint, now)
                self._insert_event(
                    connection,
                    engine,
                    "state_checkpoint",
                    self._state_event(checkpoint),
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

    @staticmethod
    def _resolve_order_ambiguity(
        connection: sqlite3.Connection,
        engine: str,
        intent_id: str,
        now: str,
    ) -> None:
        connection.execute(
            """
            UPDATE incidents
            SET status='RESOLVED', resolved_at=?
            WHERE fingerprint=? AND status='OPEN'
            """,
            (now, f"execution:{engine}:order_ambiguous:{intent_id}"),
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

    def reserve_order(
        self,
        engine: str,
        slot: str,
        intent_id: str,
        order_type: str,
        side: str,
        requested_qty: float,
        reason: str,
        reference_price: float | None = None,
    ) -> tuple[int, bool, dict[str, Any] | None]:
        """Réserve une intention sans jamais créer deux lignes pour son ID.

        La transaction BEGIN IMMEDIATE sérialise la lecture et l'insertion.
        Le second appel concurrent récupère donc la ligne PENDING déjà réservée
        au lieu de pouvoir atteindre le broker.
        """

        now = utc_now()
        with self._transaction() as connection:
            state_row = connection.execute(
                "SELECT payload FROM engine_state WHERE engine = ?", (engine,)
            ).fetchone()
            if state_row is not None:
                current_state = json.loads(state_row["payload"])
                if isinstance(current_state, dict) and current_state.get("reconciliation_required"):
                    raise ReconciliationRequired(f"Moteur {engine} marqué reconciliation_required")
            existing = connection.execute(
                "SELECT * FROM orders WHERE intent_id = ?", (intent_id,)
            ).fetchone()
            if existing is not None:
                return int(existing["id"]), False, dict(existing)
            blocking = connection.execute(
                "SELECT intent_id, status FROM orders WHERE engine = ? AND slot = ? "
                'AND status IN ("PENDING", "OPEN", "UNBALANCED") '
                'AND NOT (order_type = "STOP" AND status = "OPEN") '
                "ORDER BY id LIMIT 1",
                (engine, slot),
            ).fetchone()
            if blocking is not None:
                raise ReconciliationRequired(
                    f"Intention {blocking['intent_id']} déjà active ({blocking['status']})"
                )
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
            return int(order_id), True, None

    def reserve_order_and_transition(
        self,
        engine: str,
        slot: str,
        transition: Mapping[str, Any],
        order_type: str,
        side: str,
        requested_qty: float,
        reason: str,
        state: Mapping[str, Any],
        reference_price: float | None = None,
    ) -> tuple[int, bool, dict[str, Any], dict[str, Any], dict[str, Any]]:
        """Réserve atomiquement une transition trend et son intention market.

        active_transition est lu depuis engine_state sous le même verrou
        SQLite que l'insertion de orders. Deux processus qui partent
        simultanément d'un état flat convergent ainsi vers la même transition
        générée par le premier processus.
        """

        now = utc_now()
        with self._transaction() as connection:
            state_row = connection.execute(
                "SELECT payload FROM engine_state WHERE engine = ?", (engine,)
            ).fetchone()
            current_state = json.loads(state_row["payload"]) if state_row is not None else None
            if current_state is not None and not isinstance(current_state, dict):
                raise ValueError("État engine_state invalide : objet JSON attendu")
            if isinstance(current_state, dict) and current_state.get("reconciliation_required"):
                raise ReconciliationRequired(f"Moteur {engine} marqué reconciliation_required")
            current_slot: Mapping[str, Any] | None = None
            if isinstance(current_state, dict):
                slots = current_state.get("slots")
                if isinstance(slots, Mapping):
                    candidate = slots.get(slot)
                    if isinstance(candidate, Mapping):
                        current_slot = candidate
            current_transition = (
                current_slot.get("active_transition") if current_slot is not None else None
            )
            if current_transition is not None and not isinstance(current_transition, Mapping):
                raise ReconciliationRequired(
                    f"Transition active trend invalide pour {engine}/{slot}"
                )

            if current_transition is None:
                blocking = connection.execute(
                    'SELECT id, intent_id, status FROM orders WHERE engine = ? AND slot = ? AND status IN ("PENDING", "OPEN", "UNBALANCED") AND NOT (order_type = "STOP" AND status = "OPEN") ORDER BY id LIMIT 1',
                    (engine, slot),
                ).fetchone()
                if blocking is not None:
                    blocking_intent_id = blocking["intent_id"]
                    blocking_status = blocking["status"]
                    raise ReconciliationRequired(
                        f"Intention {blocking_intent_id} déjà active ({blocking_status})"
                    )
            if current_transition is not None:
                effective_transition = dict(current_transition)
                if not isinstance(current_state, dict):
                    raise ReconciliationRequired(
                        f"Transition active sans état trend pour {engine}/{slot}"
                    )
                persisted_state = current_state
            else:
                effective_transition = dict(transition)
                if isinstance(current_state, dict):
                    # Le checkpoint déjà en base est la source de vérité sous
                    # le verrou IMMEDIATE : un appelant peut avoir décidé sur
                    # un snapshot devenu obsolète entre deux ticks.
                    persisted_state = json.loads(self._json(current_state))
                else:
                    persisted_state = json.loads(self._json(state))
                if not isinstance(persisted_state, dict):
                    raise ValueError("État trend invalide : objet JSON attendu")
                if persisted_state.get("reconciliation_required"):
                    raise ReconciliationRequired(f"Moteur {engine} marqué reconciliation_required")
                slots = persisted_state.setdefault("slots", {})
                if not isinstance(slots, dict):
                    raise ValueError("État trend invalide : slots doit être un objet")
                slot_state = slots.setdefault(slot, {})
                if not isinstance(slot_state, dict):
                    raise ValueError(f"État trend invalide : slot {slot!r}")
                kind = str(effective_transition.get("kind") or reason).upper()
                current_position = slot_state.get("position")
                if kind == "ENTRY" and current_position is not None:
                    raise ReconciliationRequired(
                        f"État {engine}/{slot} déjà en position : entrée obsolète refusée"
                    )
                position_kinds = {"EXIT", "PYRAMID", "KILL_SWITCH", "STOP"}
                if kind in position_kinds and current_position is None:
                    raise ReconciliationRequired(
                        f"État {engine}/{slot} flat : transition {kind} obsolète refusée"
                    )
                current_cycle_id = slot_state.get("position_cycle_id")
                requested_cycle_id = effective_transition.get("position_cycle_id")
                if (
                    kind in position_kinds
                    and current_cycle_id is not None
                    and requested_cycle_id != current_cycle_id
                ):
                    raise ReconciliationRequired(f"Cycle de position obsolète pour {engine}/{slot}")
                slot_state["active_transition"] = effective_transition

            intent_id = str(effective_transition.get("intent_id") or "")
            if not intent_id:
                raise ValueError("Une transition active doit posséder un intent_id")
            existing = connection.execute(
                "SELECT * FROM orders WHERE intent_id = ?", (intent_id,)
            ).fetchone()
            if existing is None and current_transition is not None:
                raise ReconciliationRequired(f"Transition {intent_id} sans intention SQLite")
            if existing is not None and str(existing["status"]) not in {
                "PENDING",
                "OPEN",
                "UNBALANCED",
            }:
                raise ReconciliationRequired(f"Transition {intent_id} terminal mais encore active")
            if existing is not None:
                if existing["engine"] != engine or existing["slot"] != slot:
                    raise ReconciliationRequired(
                        f"Collision d'intention {intent_id} entre {existing['engine']}/{existing['slot']} "
                        f"et {engine}/{slot}"
                    )
                if str(existing["order_type"]) != str(order_type):
                    raise ReconciliationRequired(
                        f"Intention {intent_id} réutilisée avec un type d'ordre différent"
                    )
                if str(existing["side"]).upper() != str(side).upper():
                    raise ReconciliationRequired(
                        f"Intention {intent_id} réutilisée avec un côté différent"
                    )
                return (
                    int(existing["id"]),
                    False,
                    dict(existing),
                    persisted_state,
                    effective_transition,
                )

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
            checkpoint = self._checkpoint_payload(connection, engine, persisted_state)
            connection.execute(
                """
                INSERT INTO engine_state(engine, payload, updated_at) VALUES(?, ?, ?)
                ON CONFLICT(engine) DO UPDATE SET
                    payload=excluded.payload, updated_at=excluded.updated_at
                """,
                (engine, self._json(checkpoint), now),
            )
            self._sync_positions(connection, engine, checkpoint, now)
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
                    "transition": effective_transition,
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
                    checkpoint,
                    {
                        "order_id": order_id,
                        "intent_id": intent_id,
                        "transition": effective_transition,
                    },
                ),
                "engine",
                engine,
                intent_id,
            )
            inserted = connection.execute(
                "SELECT * FROM orders WHERE id = ?", (int(order_id),)
            ).fetchone()
            assert inserted is not None
            return (
                int(order_id),
                True,
                dict(inserted),
                persisted_state,
                effective_transition,
            )

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
            checkpoint = self._checkpoint_payload(connection, engine, state)
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
                (engine, self._json(checkpoint), now),
            )
            self._sync_positions(connection, engine, checkpoint, now)
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
                    checkpoint,
                    {
                        "order_id": order_id,
                        "execution_state": checkpoint.get("execution_state"),
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

    def complete_order_and_clear_transition(
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
        """Termine un ordre sans laisser sa transition active en SQLite."""

        now = utc_now()
        with self._transaction() as connection:
            order = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            if order is None:
                raise KeyError(f"Ordre journalisé introuvable : {order_id}")
            normalized_status = str(status).upper()
            if normalized_status not in {
                "FILLED",
                "PARTIAL",
                "REJECTED",
                "CANCELED",
                "RECOVERED_ABORTED",
                "FAILED",
            }:
                raise ValueError(
                    f"Une transition active ne peut être effacée qu'à un état terminal, reçu {status!r}"
                )
            status = normalized_status
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
                order["engine"],
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
            self._resolve_order_ambiguity(
                connection,
                str(order["engine"]),
                str(order["intent_id"]),
                now,
            )
            state_row = connection.execute(
                "SELECT payload FROM engine_state WHERE engine = ?",
                (order["engine"],),
            ).fetchone()
            if state_row is None:
                return
            payload = json.loads(state_row["payload"])
            if not isinstance(payload, dict):
                raise ValueError("État engine invalide : objet JSON attendu")
            slots = payload.get("slots")
            slot_state = slots.get(order["slot"]) if isinstance(slots, dict) else None
            if not isinstance(slot_state, dict):
                return
            active = slot_state.get("active_transition")
            if not isinstance(active, dict) or active.get("intent_id") != order["intent_id"]:
                return
            slot_state["active_transition"] = None
            if (
                str(active.get("kind", "")).upper() == "ENTRY"
                or str(order["reason"] or "").lower() == "entry"
            ) and slot_state.get("position") is None:
                slot_state["position_cycle_id"] = None
            checkpoint = self._checkpoint_payload(
                connection,
                order["engine"],
                payload,
                preserve_active_transitions=False,
                allowed_transition_intent=str(order["intent_id"]),
            )
            connection.execute(
                """
                INSERT INTO engine_state(engine, payload, updated_at) VALUES(?, ?, ?)
                ON CONFLICT(engine) DO UPDATE SET
                    payload=excluded.payload, updated_at=excluded.updated_at
                """,
                (order["engine"], self._json(checkpoint), now),
            )
            self._sync_positions(connection, order["engine"], checkpoint, now)
            self._insert_event(
                connection,
                order["engine"],
                "order_transition_cleared",
                {"order_id": order_id, "intent_id": order["intent_id"]},
                "order",
                str(order_id),
                order["intent_id"],
            )
            self._resolve_order_ambiguity(
                connection,
                str(order["engine"]),
                str(order["intent_id"]),
                now,
            )

    def complete_order_and_checkpoint(
        self,
        order_id: int,
        *,
        engine: str,
        state: Mapping[str, Any],
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
                "SELECT engine, slot, intent_id FROM orders WHERE id = ?", (order_id,)
            ).fetchone()
            if order is None:
                raise KeyError(f"Ordre journalisé introuvable : {order_id}")
            checkpoint = self._checkpoint_payload(
                connection,
                engine,
                state,
                preserve_active_transitions=False,
                allowed_transition_intent=str(order["intent_id"]),
            )
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
                (engine, self._json(checkpoint), now),
            )
            self._sync_positions(connection, engine, checkpoint, now)
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
                self._state_event(checkpoint, {"order_id": order_id}),
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

    def mark_order_ambiguous_and_checkpoint(
        self,
        order_id: int,
        *,
        engine: str,
        state: Mapping[str, Any] | None,
        error: str,
        status: str | None = None,
        filled_qty: float | None = None,
        price: float | None = None,
        fee: float | None = None,
        broker_order_id: str | None = None,
    ) -> str:
        """Conserve une intention ambiguë et bloque l'état engine.

        L'ordre reste PENDING, OPEN ou UNBALANCED selon son état courant. Une
        intention ambiguë ne devient jamais REJECTED pour rendre un retry
        possible.
        """

        now = utc_now()
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            if row is None:
                raise KeyError(f"Ordre journalisé introuvable : {order_id}")
            if row["engine"] != engine:
                raise ValueError("L'ordre et le moteur appartiennent à deux périmètres différents")
            current_status = str(row["status"])
            if current_status not in {"PENDING", "OPEN", "UNBALANCED"}:
                return current_status
            effective_status = current_status if status is None else str(status).upper()
            if effective_status == "UNKNOWN":
                effective_status = "UNBALANCED" if (filled_qty or 0.0) > 0 else current_status
            if effective_status not in {"PENDING", "OPEN", "UNBALANCED"}:
                raise ValueError(f"Statut ambigu invalide : {effective_status}")

            connection.execute(
                """
                UPDATE orders
                SET status=?, filled_qty=?, price=?, fee=?, broker_order_id=?,
                    error=?, updated_at=?
                WHERE id=?
                """,
                (
                    effective_status,
                    row["filled_qty"] if filled_qty is None else filled_qty,
                    row["price"] if price is None else price,
                    row["fee"] if fee is None else fee,
                    row["broker_order_id"] if broker_order_id is None else broker_order_id,
                    error,
                    now,
                    order_id,
                ),
            )
            persisted_state: dict[str, Any] | None = None
            if state is not None:
                persisted_state = json.loads(self._json(state))
                if not isinstance(persisted_state, dict):
                    raise ValueError("État engine invalide : objet JSON attendu")
            else:
                state_row = connection.execute(
                    "SELECT payload FROM engine_state WHERE engine = ?", (engine,)
                ).fetchone()
                if state_row is not None:
                    loaded_state = json.loads(state_row["payload"])
                    if not isinstance(loaded_state, dict):
                        raise ValueError("État engine invalide : objet JSON attendu")
                    persisted_state = loaded_state
            if persisted_state is not None:
                persisted_state = self._checkpoint_payload(connection, engine, persisted_state)
                persisted_state["reconciliation_required"] = True
                connection.execute(
                    """
                    INSERT INTO engine_state(engine, payload, updated_at) VALUES(?, ?, ?)
                    ON CONFLICT(engine) DO UPDATE SET
                        payload=excluded.payload, updated_at=excluded.updated_at
                    """,
                    (engine, self._json(persisted_state), now),
                )
                self._sync_positions(connection, engine, persisted_state, now)

            self._insert_event(
                connection,
                engine,
                "order_reconciliation_required",
                {
                    "order_id": order_id,
                    "intent_id": row["intent_id"],
                    "status": effective_status,
                    "error": error,
                },
                "order",
                str(order_id),
                row["intent_id"],
            )
            fingerprint = f"execution:{engine}:order_ambiguous:{row['intent_id']}"
            context = self._json(
                {
                    "order_id": order_id,
                    "intent_id": row["intent_id"],
                    "status": effective_status,
                    "error": error,
                }
            )
            connection.execute(
                """
                INSERT INTO incidents(
                    fingerprint, engine, severity, kind, message, context,
                    status, occurrences, first_seen, last_seen
                ) VALUES(?, ?, 'CRITICAL', 'order_ambiguous', ?, ?,
                          'OPEN', 1, ?, ?)
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
                (fingerprint, engine, error, context, now, now),
            )
            return effective_status

    def clear_order_transition(self, order_id: int) -> None:
        now = utc_now()
        with self._transaction() as connection:
            order = connection.execute(
                "SELECT engine, slot, intent_id, reason, status FROM orders WHERE id = ?",
                (order_id,),
            ).fetchone()
            if order is None:
                raise KeyError(f"Ordre journalisé introuvable : {order_id}")
            if str(order["status"]) in {"PENDING", "OPEN", "UNBALANCED"}:
                raise ReconciliationRequired(
                    f"Transition de l'ordre {order_id} encore non terminale"
                )
            row = connection.execute(
                "SELECT payload FROM engine_state WHERE engine = ?",
                (order["engine"],),
            ).fetchone()
            if row is None:
                return
            payload = json.loads(row["payload"])
            if not isinstance(payload, dict):
                raise ValueError("État engine invalide : objet JSON attendu")
            slots = payload.get("slots")
            slot_state = slots.get(order["slot"]) if isinstance(slots, dict) else None
            if not isinstance(slot_state, dict):
                return
            active = slot_state.get("active_transition")
            if not isinstance(active, dict) or active.get("intent_id") != order["intent_id"]:
                return
            slot_state["active_transition"] = None
            if (
                str(active.get("kind", "")).upper() == "ENTRY"
                or str(order["reason"] or "").lower() == "entry"
            ) and slot_state.get("position") is None:
                slot_state["position_cycle_id"] = None
            checkpoint = self._checkpoint_payload(
                connection,
                order["engine"],
                payload,
                preserve_active_transitions=False,
                allowed_transition_intent=str(order["intent_id"]),
            )
            connection.execute(
                """
                INSERT INTO engine_state(engine, payload, updated_at) VALUES(?, ?, ?)
                ON CONFLICT(engine) DO UPDATE SET
                    payload=excluded.payload, updated_at=excluded.updated_at
                """,
                (order["engine"], self._json(checkpoint), now),
            )
            self._sync_positions(connection, order["engine"], checkpoint, now)
            self._insert_event(
                connection,
                order["engine"],
                "order_transition_cleared",
                {"order_id": order_id, "intent_id": order["intent_id"]},
                "order",
                str(order_id),
                order["intent_id"],
            )
            self._resolve_order_ambiguity(
                connection,
                str(order["engine"]),
                str(order["intent_id"]),
                now,
            )

    def clear_reconciliation_if_safe(self, engine: str) -> bool:
        now = utc_now()
        with self._transaction() as connection:
            unresolved = connection.execute(
                'SELECT 1 FROM orders WHERE engine = ? AND status IN ("PENDING", "OPEN", "UNBALANCED") AND NOT (order_type = "STOP" AND status = "OPEN") LIMIT 1',
                (engine,),
            ).fetchone()
            open_ambiguity = connection.execute(
                "SELECT 1 FROM incidents WHERE engine = ? AND kind = 'order_ambiguous' AND status = 'OPEN' LIMIT 1",
                (engine,),
            ).fetchone()
            if unresolved is not None or open_ambiguity is not None:
                return False
            row = connection.execute(
                "SELECT payload FROM engine_state WHERE engine = ?", (engine,)
            ).fetchone()
            if row is None:
                return True
            payload = json.loads(row["payload"])
            if not isinstance(payload, dict):
                raise ValueError("État engine invalide : objet JSON attendu")
            if not payload.get("reconciliation_required"):
                return True
            payload["reconciliation_required"] = False
            connection.execute(
                "INSERT INTO engine_state(engine, payload, updated_at) VALUES(?, ?, ?) ON CONFLICT(engine) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at",
                (engine, self._json(payload), now),
            )
            self._sync_positions(connection, engine, payload, now)
            self._insert_event(
                connection,
                engine,
                "reconciliation_cleared",
                {"engine": engine},
                "engine",
                engine,
            )
            return True

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
