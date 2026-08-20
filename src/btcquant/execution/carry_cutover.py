"""Transition explicite d'un checkpoint Carry synthétique legacy vers v6 FLAT.

Cette opération n'est pas une migration de schéma, ni un chemin de démarrage
du runner. Elle n'écrit rien tant que le motif legacy exact et le hash
compare-and-swap ne sont pas vérifiés dans la même transaction.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import math
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ..backup import RecoveryRequired, assert_writer_recovery_clear
from ..config import load_config
from ..deployment import inspect_sqlite
from .errors import EngineInstanceAlreadyRunning
from .instance_lock import EngineInstanceLock
from .state_store import SCHEMA_VERSION, StateStore, utc_now

CUTOVER_EVENT_TYPE = "legacy_synthetic_carry_cutover"
CUTOVER_REASON = "LEGACY_SYNTHETIC_OPEN_QTY0"
CUTOVER_APPLIED = "CUTOVER_APPLIED"
NO_OP_ALREADY_CUT_OVER = "NO_OP_ALREADY_CUT_OVER"
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class CutoverRefused(RuntimeError):
    """Précondition de cutover non satisfaite : aucune écriture n'est appliquée."""


CutoverStatus = Literal["CUTOVER_APPLIED", "NO_OP_ALREADY_CUT_OVER"]


@dataclass(frozen=True)
class CutoverResult:
    status: CutoverStatus
    old_state_sha256: str
    new_state_sha256: str
    equity: float
    cutover_timestamp_utc: str | None


def canonical_carry_state_sha256(payload: Mapping[str, Any]) -> str:
    """Empreinte déterministe du checkpoint Carry persisté."""

    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def require_paper_carry_config(config_path: str | Path) -> dict[str, Any]:
    """Charge la config et refuse tout profil autre que paper."""

    cfg = load_config(config_path)
    environment = cfg.get("environment")
    raw_execution = cfg.get("execution")
    if not isinstance(raw_execution, dict):
        raise CutoverRefused("CUTOVER_BLOCKED: section execution absente")
    execution = raw_execution
    mode = execution.get("mode", "paper")
    # Le profil paper de production porte encore `execution.testnet: true`
    # comme reliquat de baseline ; le critère opératoire est le mode.
    if environment != "paper" or mode != "paper":
        raise CutoverRefused(
            f"CUTOVER_BLOCKED: configuration non paper "
            f"(environment={environment!r}, execution.mode={mode!r})"
        )
    if execution.get("live") is True or mode == "live":
        raise CutoverRefused("CUTOVER_BLOCKED: configuration live")
    return cfg


def require_schema_6(database: str | Path) -> int:
    inspection = inspect_sqlite(database)
    version = inspection.metadata_schema_version
    if version is None:
        raise CutoverRefused("CUTOVER_BLOCKED: version de schéma inconnue")
    if version < SCHEMA_VERSION:
        raise CutoverRefused(
            f"CUTOVER_BLOCKED: schéma {version} < 6 ; migration officielle requise, "
            "aucune auto-migration"
        )
    if version > SCHEMA_VERSION:
        raise CutoverRefused(f"CUTOVER_BLOCKED: schéma {version} > 6 non prouvé compatible")
    return version


def _is_null(value: object) -> bool:
    return value is None


def _absent_or_null(payload: Mapping[str, Any], key: str) -> bool:
    return key not in payload or _is_null(payload[key])


def _finite_zero(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    number = float(value)
    return math.isfinite(number) and number == 0.0


def _absent_or_zero(payload: Mapping[str, Any], key: str) -> bool:
    if key not in payload or _is_null(payload[key]):
        return True
    return _finite_zero(payload[key])


def _qty(payload: Mapping[str, Any], key: str) -> float:
    if key not in payload or _is_null(payload[key]):
        return 0.0
    if isinstance(payload[key], bool) or not isinstance(payload[key], (int, float)):
        raise CutoverRefused(f"CUTOVER_BLOCKED: {key} non numérique")
    number = float(payload[key])
    if not math.isfinite(number):
        raise CutoverRefused(f"CUTOVER_BLOCKED: {key} non fini")
    return number


def diagnose_legacy_synthetic_pattern(payload: Mapping[str, Any]) -> list[str]:
    """Retourne les motifs d'échec ; liste vide si le motif legacy exact matche."""

    failures: list[str] = []
    if payload.get("in_position") is not True:
        failures.append("in_position != true")
    if payload.get("execution_state") != "OPEN":
        failures.append(f"execution_state={payload.get('execution_state')!r}")
    for key in ("qty", "spot_qty", "perp_qty"):
        try:
            if _qty(payload, key) != 0.0:
                failures.append(f"{key}={payload.get(key)!r}")
        except CutoverRefused as error:
            failures.append(str(error))
    for key in (
        "entry_equity",
        "entry_timestamp",
        "entry_price",
        "position_generation",
        "funding_notional_price",
        "funding_notional_price_source",
        "funding_notional_price_timestamp",
    ):
        if not _absent_or_null(payload, key):
            failures.append(f"{key} présent")
    for key in ("spot_notional", "perp_notional", "borrow_principal"):
        if not _absent_or_zero(payload, key):
            failures.append(f"{key}={payload.get(key)!r}")
    if payload.get("accounting_uncertain") is True:
        failures.append("accounting_uncertain=true")
    if payload.get("execution_state") in {"OPENING", "CLOSING", "UNBALANCED"}:
        failures.append(f"état transitoire {payload.get('execution_state')}")
    return failures


def diagnose_flat_cutover_result(payload: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    if payload.get("in_position") is not False:
        failures.append("in_position != false")
    if payload.get("execution_state") != "FLAT":
        failures.append(f"execution_state={payload.get('execution_state')!r}")
    for key in (
        "qty",
        "spot_qty",
        "perp_qty",
        "spot_notional",
        "perp_notional",
        "borrow_principal",
    ):
        if not _finite_zero(payload.get(key, 0.0)):
            failures.append(f"{key}={payload.get(key)!r}")
    for key in (
        "entry_equity",
        "entry_timestamp",
        "entry_price",
        "position_generation",
        "funding_notional_price",
        "funding_notional_price_source",
        "funding_notional_price_timestamp",
        "accounting_uncertainty_reason",
    ):
        if not _absent_or_null(payload, key):
            failures.append(f"{key} présent")
    if payload.get("accounting_uncertain") not in (False, None):
        failures.append("accounting_uncertain")
    return failures


def build_flat_payload(
    source: Mapping[str, Any],
    *,
    last_funding_ts: str,
) -> dict[str, Any]:
    """Construit le checkpoint FLAT en recopiant exactement l'état non-positionnel."""

    missing = [field for field in ("equity", "peak_equity") if field not in source]
    if missing:
        raise CutoverRefused(f"CUTOVER_BLOCKED: champs financiers absents {missing}")
    return {
        "equity": source["equity"],
        "in_position": False,
        "execution_state": "FLAT",
        "qty": 0.0,
        "spot_qty": 0.0,
        "perp_qty": 0.0,
        "entry_equity": None,
        "entry_timestamp": None,
        "entry_price": None,
        "funding_notional_price_source": None,
        "funding_notional_price_timestamp": None,
        "spot_notional": 0.0,
        "perp_notional": 0.0,
        "borrow_principal": 0.0,
        "position_generation": None,
        "funding_notional_price": None,
        "last_funding_ts": last_funding_ts,
        "peak_equity": source["peak_equity"],
        "day": source.get("day"),
        "day_start_equity": source.get("day_start_equity", source["equity"]),
        "halted": bool(source.get("halted", False)),
        "daily_lockout": bool(source.get("daily_lockout", False)),
        "accounting_uncertain": False,
        "accounting_uncertainty_reason": None,
    }


def _event_inner_payload(raw: Mapping[str, Any] | str) -> dict[str, Any]:
    if isinstance(raw, str):
        parsed = json.loads(raw)
    else:
        parsed = dict(raw)
    return parsed


def _matching_cutover_event(
    events: Sequence[Mapping[str, Any]],
    *,
    current_payload: Mapping[str, Any],
    current_sha: str,
) -> Mapping[str, Any] | None:
    matches: list[Mapping[str, Any]] = []
    for event in events:
        if event.get("event_type") != CUTOVER_EVENT_TYPE:
            continue
        payload = _event_inner_payload(event["payload"])
        if payload.get("reason") != CUTOVER_REASON:
            continue
        if payload.get("new_state_sha256") != current_sha:
            continue
        matches.append(payload)
    if not matches:
        return None
    latest = matches[-1]
    if diagnose_flat_cutover_result(current_payload):
        return None
    if latest.get("equity_after") != current_payload.get("equity"):
        return None
    return latest


def _count_carry_orders(connection: Any) -> tuple[int, int]:
    total = int(
        connection.execute("SELECT COUNT(*) FROM orders WHERE engine = 'carry'").fetchone()[0]
    )
    unresolved = int(
        connection.execute(
            """
            SELECT COUNT(*) FROM orders
            WHERE engine = 'carry'
              AND (
                    status IN ('PENDING', 'OPEN', 'UNBALANCED')
                 OR local_state != 'TERMINAL'
              )
            """
        ).fetchone()[0]
    )
    return total, unresolved


def _count_carry_funding_ledger(connection: Any) -> int:
    tables = {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "funding_ledger" not in tables:
        raise CutoverRefused("CUTOVER_BLOCKED: table funding_ledger absente")
    return int(connection.execute("SELECT COUNT(*) FROM funding_ledger").fetchone()[0])


def _open_critical_carry_incidents(connection: Any) -> list[str]:
    rows = connection.execute(
        """
        SELECT fingerprint FROM incidents
        WHERE engine = 'carry' AND status = 'OPEN' AND severity = 'CRITICAL'
        ORDER BY id
        """
    ).fetchall()
    return [str(row[0]) for row in rows]


def _require_consistent_position_row(
    connection: Any,
    *,
    expected_status: str,
    expected_qty: float,
    expected_cash: float,
) -> None:
    row = connection.execute(
        "SELECT status, qty, cash FROM positions WHERE engine = 'carry' AND slot = 'carry'"
    ).fetchone()
    if row is None:
        raise CutoverRefused("CUTOVER_BLOCKED: ligne positions carry absente")
    if str(row["status"]) != expected_status:
        raise CutoverRefused(
            f"CUTOVER_BLOCKED: positions.status={row['status']!r} incohérent avec {expected_status}"
        )
    qty = 0.0 if row["qty"] is None else float(row["qty"])
    if qty != expected_qty:
        raise CutoverRefused(f"CUTOVER_BLOCKED: positions.qty={row['qty']!r}")
    cash = row["cash"]
    if cash is not None and float(cash) != float(expected_cash):
        raise CutoverRefused("CUTOVER_BLOCKED: positions.cash != engine_state.equity")


def apply_legacy_synthetic_carry_cutover(
    database: str | Path,
    *,
    expected_state_sha256: str,
    git_sha: str,
    operator: str | None = None,
    acquire_lock: bool = True,
) -> CutoverResult:
    """Applique le cutover ou confirme l'idempotence dans une transaction unique."""

    if not _SHA256_RE.fullmatch(expected_state_sha256):
        raise CutoverRefused("CUTOVER_BLOCKED: --expected-state-sha256 invalide")
    if not _FULL_SHA_RE.fullmatch(git_sha):
        raise CutoverRefused("CUTOVER_BLOCKED: --git-sha doit être un SHA git 40 hex")

    require_schema_6(database)
    try:
        assert_writer_recovery_clear(Path(database).parent)
    except RecoveryRequired as error:
        raise CutoverRefused(f"CUTOVER_BLOCKED: recovery marker actif ({error})") from error

    lock: EngineInstanceLock | None = None
    if acquire_lock:
        lock = EngineInstanceLock(database, "carry")
        try:
            lock.acquire()
        except EngineInstanceAlreadyRunning as error:
            raise CutoverRefused(f"CUTOVER_BLOCKED: writer carry actif ({error})") from error

    try:
        store = StateStore(database, allow_migration=False)
        return _apply_in_transaction(
            store,
            expected_state_sha256=expected_state_sha256,
            git_sha=git_sha,
            operator=operator or getpass.getuser() or os.environ.get("USER") or "unknown",
        )
    except RecoveryRequired as error:
        raise CutoverRefused(f"CUTOVER_BLOCKED: recovery marker actif ({error})") from error
    finally:
        if lock is not None:
            lock.release()


def apply_cutover_on_connection(
    store: StateStore,
    connection: Any,
    *,
    expected_state_sha256: str,
    git_sha: str,
    operator: str,
) -> CutoverResult:
    """Cœur transactionnel : l'appelant détient déjà BEGIN IMMEDIATE."""

    schema_row = connection.execute(
        "SELECT value FROM metadata WHERE key = 'schema_version'"
    ).fetchone()
    if schema_row is None or int(schema_row[0]) != SCHEMA_VERSION:
        raise CutoverRefused("CUTOVER_BLOCKED: schéma != 6 dans la transaction")

    row = connection.execute("SELECT payload FROM engine_state WHERE engine = 'carry'").fetchone()
    if row is None:
        raise CutoverRefused("CUTOVER_BLOCKED: engine_state carry absent")
    payload = json.loads(row["payload"])
    current_sha = canonical_carry_state_sha256(payload)

    events = [
        dict(event)
        for event in connection.execute(
            "SELECT id, event_type, payload FROM events WHERE engine = 'carry' ORDER BY id"
        ).fetchall()
    ]
    already = _matching_cutover_event(events, current_payload=payload, current_sha=current_sha)
    if already is not None:
        if current_sha != expected_state_sha256:
            raise CutoverRefused("CUTOVER_BLOCKED: hash attendu différent de l'état déjà cutover")
        return CutoverResult(
            status="NO_OP_ALREADY_CUT_OVER",
            old_state_sha256=str(already.get("old_state_sha256") or current_sha),
            new_state_sha256=current_sha,
            equity=float(payload["equity"]),
            cutover_timestamp_utc=str(already.get("cutover_timestamp_utc") or ""),
        )

    if current_sha != expected_state_sha256:
        raise CutoverRefused(
            "CUTOVER_BLOCKED: hash d'état différent "
            f"(lu={current_sha}, attendu={expected_state_sha256})"
        )

    pattern_failures = diagnose_legacy_synthetic_pattern(payload)
    if pattern_failures:
        raise CutoverRefused(
            "CUTOVER_BLOCKED: motif legacy non satisfait: " + "; ".join(pattern_failures)
        )

    order_count, unresolved = _count_carry_orders(connection)
    if order_count:
        raise CutoverRefused(f"CUTOVER_BLOCKED: {order_count} ordre(s) carry présent(s)")
    if unresolved:
        raise CutoverRefused(f"CUTOVER_BLOCKED: {unresolved} ordre(s) carry non résolu(s)")

    ledger_count = _count_carry_funding_ledger(connection)
    if ledger_count:
        raise CutoverRefused(f"CUTOVER_BLOCKED: funding_ledger non vide ({ledger_count} ligne(s))")

    incidents = _open_critical_carry_incidents(connection)
    if incidents:
        raise CutoverRefused(
            "CUTOVER_BLOCKED: incident CRITICAL carry OPEN: " + ", ".join(incidents)
        )

    _require_consistent_position_row(
        connection,
        expected_status="OPEN",
        expected_qty=0.0,
        expected_cash=float(payload["equity"]),
    )

    cutover_ts = utc_now()
    new_payload = build_flat_payload(payload, last_funding_ts=cutover_ts)
    new_sha = canonical_carry_state_sha256(new_payload)
    event_payload = {
        "schema_version": SCHEMA_VERSION,
        "cutover_timestamp_utc": cutover_ts,
        "reason": CUTOVER_REASON,
        "old_state_sha256": current_sha,
        "new_state_sha256": new_sha,
        "git_sha": git_sha,
        "operator": operator,
        "equity_before": payload["equity"],
        "equity_after": new_payload["equity"],
        "old_execution_state": payload.get("execution_state"),
        "new_execution_state": new_payload["execution_state"],
        "old_qty": payload.get("qty", 0.0),
        "new_qty": new_payload["qty"],
        "funding_ledger_rows_before": ledger_count,
        "carry_orders_before": order_count,
    }
    connection.execute(
        """
        INSERT INTO engine_state(engine, payload, updated_at) VALUES(?, ?, ?)
        ON CONFLICT(engine) DO UPDATE SET
            payload=excluded.payload, updated_at=excluded.updated_at
        """,
        ("carry", store._json(new_payload), cutover_ts),
    )
    store._sync_positions(connection, "carry", new_payload, cutover_ts)
    store._insert_event(
        connection,
        "carry",
        CUTOVER_EVENT_TYPE,
        store._state_event(new_payload, event_payload),
        aggregate_type="engine",
        aggregate_id="carry",
    )
    _require_consistent_position_row(
        connection,
        expected_status="FLAT",
        expected_qty=0.0,
        expected_cash=float(new_payload["equity"]),
    )
    return CutoverResult(
        status="CUTOVER_APPLIED",
        old_state_sha256=current_sha,
        new_state_sha256=new_sha,
        equity=float(new_payload["equity"]),
        cutover_timestamp_utc=cutover_ts,
    )


def _apply_in_transaction(
    store: StateStore,
    *,
    expected_state_sha256: str,
    git_sha: str,
    operator: str,
) -> CutoverResult:
    with store._transaction() as connection:
        return apply_cutover_on_connection(
            store,
            connection,
            expected_state_sha256=expected_state_sha256,
            git_sha=git_sha,
            operator=operator,
        )


def read_carry_state_sha256(database: str | Path) -> str:
    require_schema_6(database)
    store = StateStore(database, allow_migration=False, read_only=True)
    payload = store.load_engine_state("carry")
    if payload is None:
        raise CutoverRefused("CUTOVER_BLOCKED: engine_state carry absent")
    return canonical_carry_state_sha256(payload)
