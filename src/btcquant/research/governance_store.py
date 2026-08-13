"""Durable, crash-safe persistence for quantitative governance.

This database is deliberately separate from the trading StateStore.  It is a
research control plane: reservations and audit events survive a process crash
and cannot be removed by a normal API.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from .governance import (
    DatasetProvenance,
    DatasetRole,
    ExperimentInvalidated,
    ExperimentSpec,
    GovernanceError,
    HoldoutInvalidated,
    TrialBudgetExceeded,
    canonical_json,
    cost_model_fingerprint,
    parameter_fingerprint,
    sha256_canonical,
)
from .search_gates import governance_missing_fields, validate_search_ready


GOVERNANCE_SCHEMA_VERSION = 1
DEFAULT_GOVERNANCE_DB = Path("state/research/governance.sqlite3")
_TRADING_DB_NAMES = {"btcquant.db", "btcquant-testnet.db", "execution-shadow.db"}


class GovernanceStoreError(GovernanceError):
    """Base error for durable governance state."""


class DuplicateTrial(GovernanceStoreError):
    """The same semantic search trial is already registered."""


class DatasetRoleConflict(GovernanceStoreError):
    """A dataset or interval cannot be assigned the requested role."""


class HoldoutConflict(GovernanceStoreError):
    """A holdout identity or state does not match the requested operation."""


_T = TypeVar("_T")


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _json_load(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    loaded = json.loads(value)
    if not isinstance(loaded, dict):
        raise GovernanceStoreError("JSON de gouvernance inattendu")
    return loaded


def _trial_fingerprint(
    *,
    experiment_fingerprint: str,
    parameters: Mapping[str, Any],
    dataset_fingerprint: str,
    split_fingerprint: str,
    seed: int | None,
    cost_fingerprint: str,
) -> str:
    return sha256_canonical(
        {
            "experiment_fingerprint": experiment_fingerprint,
            "parameter_fingerprint": parameter_fingerprint(parameters),
            "dataset_fingerprint": dataset_fingerprint,
            "split_fingerprint": split_fingerprint,
            "seed": seed,
            "cost_model_fingerprint": cost_fingerprint,
        }
    )


def protocol_fingerprint(spec: ExperimentSpec) -> str:
    return spec.fingerprint


def candidate_fingerprint(parameters: Mapping[str, Any]) -> str:
    return parameter_fingerprint(parameters)


def _without_volatile(value: Any) -> Any:
    volatile = {
        "generated_at",
        "runtime_seconds",
        "wall_clock_timestamp",
        "started_at",
        "finished_at",
    }
    if isinstance(value, Mapping):
        return {key: _without_volatile(item) for key, item in value.items() if key not in volatile}
    if isinstance(value, list):
        return [_without_volatile(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_without_volatile(item) for item in value)
    return value


def result_fingerprint(result: Mapping[str, Any]) -> str:
    return sha256_canonical(_without_volatile(dict(result)))


@dataclass(frozen=True)
class TrialReservation:
    trial_id: str
    trial_fingerprint: str
    sequence: int
    is_new: bool
    attempt_id: int


@dataclass(frozen=True)
class HoldoutReservation:
    holdout_id: str
    status: str
    evaluation_token: str | None


class GovernanceStore:
    """SQLite store with a separate schema and transaction boundary."""

    def __init__(self, path: str | Path | None = None) -> None:
        configured = path or os.environ.get("BTCQUANT_GOVERNANCE_DB")
        self.path = Path(configured or DEFAULT_GOVERNANCE_DB)
        resolved = self.path.expanduser().resolve()
        if resolved.name in _TRADING_DB_NAMES:
            raise GovernanceStoreError("le governance store ne peut pas utiliser une DB de trading")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            str(self.path), timeout=15.0, isolation_level=None, check_same_thread=False
        )
        self._connection.row_factory = sqlite3.Row
        self._configure_connection()
        self._initialize_schema()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> GovernanceStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _configure_connection(self) -> None:
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute("PRAGMA busy_timeout=15000")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute("PRAGMA journal_mode=WAL")

    def _initialize_schema(self) -> None:
        with self._transaction():
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS governance_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS experiments (
                    experiment_id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL UNIQUE,
                    spec_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    registered_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS candidates (
                    candidate_id TEXT PRIMARY KEY,
                    experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id),
                    fingerprint TEXT NOT NULL,
                    parameters_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(experiment_id, fingerprint)
                );
                CREATE TABLE IF NOT EXISTS trials (
                    trial_id TEXT PRIMARY KEY,
                    experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id),
                    experiment_fingerprint TEXT NOT NULL,
                    trial_fingerprint TEXT NOT NULL,
                    parameter_fingerprint TEXT NOT NULL,
                    parameters_json TEXT NOT NULL,
                    dataset_fingerprint TEXT NOT NULL,
                    split_fingerprint TEXT NOT NULL,
                    seed INTEGER,
                    cost_model_fingerprint TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    trial_kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reserved_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    result_json TEXT,
                    metrics_json TEXT,
                    failure_reason TEXT,
                    UNIQUE(experiment_id, trial_fingerprint)
                );
                CREATE TABLE IF NOT EXISTS trial_attempts (
                    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trial_id TEXT NOT NULL REFERENCES trials(trial_id),
                    attempt_no INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    details_json TEXT,
                    UNIQUE(trial_id, attempt_no)
                );
                CREATE TABLE IF NOT EXISTS dataset_usage (
                    usage_id TEXT PRIMARY KEY,
                    dataset_id TEXT NOT NULL,
                    venue TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    start_ts TEXT NOT NULL,
                    end_ts TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    role TEXT NOT NULL,
                    status TEXT NOT NULL,
                    experiment_id TEXT,
                    purpose TEXT NOT NULL,
                    first_registered_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_dataset_usage_interval
                    ON dataset_usage(venue, symbol, start_ts, end_ts);
                CREATE TABLE IF NOT EXISTS holdouts (
                    holdout_id TEXT PRIMARY KEY,
                    identity_fingerprint TEXT NOT NULL UNIQUE,
                    candidate_fingerprint TEXT NOT NULL,
                    experiment_fingerprint TEXT NOT NULL,
                    code_sha TEXT NOT NULL,
                    venue TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    start_rule TEXT NOT NULL,
                    end_rule TEXT NOT NULL,
                    cost_json TEXT NOT NULL,
                    metrics_json TEXT NOT NULL,
                    gates_json TEXT NOT NULL,
                    sample_rule_json TEXT NOT NULL,
                    dataset_usage_id TEXT,
                    status TEXT NOT NULL,
                    evaluation_token TEXT,
                    result_json TEXT,
                    result_fingerprint TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS governance_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    previous_event_hash TEXT,
                    event_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                """
            )
            current = self._connection.execute(
                "SELECT value FROM governance_metadata WHERE key='schema_version'"
            ).fetchone()
            if current is None:
                self._connection.execute(
                    "INSERT INTO governance_metadata(key, value) VALUES ('schema_version', ?)",
                    (str(GOVERNANCE_SCHEMA_VERSION),),
                )
            elif int(current[0]) != GOVERNANCE_SCHEMA_VERSION:
                raise GovernanceStoreError("version de governance store incompatible")

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    def _event(
        self, event_type: str, entity_type: str, entity_id: str, payload: Mapping[str, Any]
    ) -> None:
        previous = self._connection.execute(
            "SELECT event_hash FROM governance_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = str(previous[0]) if previous else None
        body = {
            "event_type": event_type,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "payload": dict(payload),
            "previous_event_hash": previous_hash,
        }
        event_hash = sha256_canonical(body)
        self._connection.execute(
            """
            INSERT INTO governance_events(
                event_type, entity_type, entity_id, payload_json,
                previous_event_hash, event_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_type,
                entity_type,
                entity_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                _now(),
            ),
        )

    def get_experiment_spec(self, experiment_id: str) -> ExperimentSpec:
        row = self._connection.execute(
            "SELECT spec_json FROM experiments WHERE experiment_id=?",
            (experiment_id,),
        ).fetchone()
        if row is None:
            raise GovernanceStoreError("experiment not registered")
        value = json.loads(str(row[0]))
        if not isinstance(value, dict):
            raise GovernanceStoreError("invalid experiment specification")
        return ExperimentSpec.from_dict(value)

    def record_policy_fingerprint(
        self, *, policy_version: str, fingerprint: str, status: str
    ) -> None:
        """Seal one policy version in the durable governance metadata."""

        key = f"policy:{policy_version}"
        with self._transaction():
            row = self._connection.execute(
                "SELECT value FROM governance_metadata WHERE key=?", (key,)
            ).fetchone()
            if row is not None:
                recorded = json.loads(str(row[0]))
                if recorded.get("fingerprint") != fingerprint:
                    raise GovernanceStoreError("policy fingerprint changed in place")
                if recorded.get("status") != status:
                    raise GovernanceStoreError("policy status changed after sealing")
                return
            self._connection.execute(
                "INSERT INTO governance_metadata(key, value) VALUES (?, ?)",
                (key, canonical_json({"fingerprint": fingerprint, "status": status})),
            )
            self._event(
                "POLICY_FINGERPRINT_RECORDED",
                "policy",
                policy_version,
                {"fingerprint": fingerprint, "status": status},
            )

    def record_policy_transition(
        self,
        *,
        policy_version: str,
        trend_fingerprint: str,
        carry_fingerprint: str,
        combined_fingerprint: str,
        previous_state: str,
        new_state: str,
        base_git_sha: str,
    ) -> None:
        """Persist an auditable policy lifecycle transition."""

        if not base_git_sha or len(base_git_sha) != 40:
            raise GovernanceStoreError("base_git_sha must be a full Git SHA")
        if previous_state == new_state:
            raise GovernanceStoreError("policy lifecycle state must change")
        payload = {
            "policy_version": policy_version,
            "trend_fingerprint": trend_fingerprint,
            "carry_fingerprint": carry_fingerprint,
            "combined_fingerprint": combined_fingerprint,
            "previous_state": previous_state,
            "new_state": new_state,
            "transitioned_at": _now(),
            "base_git_sha": base_git_sha,
        }
        with self._transaction():
            self._event("POLICY_LIFECYCLE_TRANSITION", "policy", policy_version, payload)

    def get_policy_fingerprint(self, policy_version: str) -> dict[str, str] | None:
        row = self._connection.execute(
            "SELECT value FROM governance_metadata WHERE key=?",
            (f"policy:{policy_version}",),
        ).fetchone()
        if row is None:
            return None
        value = json.loads(str(row[0]))
        if not isinstance(value, dict):
            raise GovernanceStoreError("invalid policy fingerprint record")
        return {str(key): str(item) for key, item in value.items()}

    def register_experiment(self, spec: ExperimentSpec) -> str:
        fingerprint = protocol_fingerprint(spec)
        now = _now()
        missing = governance_missing_fields(spec)
        status = "GOVERNANCE_INCOMPLETE" if missing else "REGISTERED"
        with self._transaction():
            row = self._connection.execute(
                "SELECT fingerprint FROM experiments WHERE experiment_id=?",
                (spec.experiment_id,),
            ).fetchone()
            if row is not None:
                if row[0] != fingerprint:
                    raise ExperimentInvalidated(
                        "experiment fingerprint modifié; nouvel experiment_id requis"
                    )
                return fingerprint
            self._connection.execute(
                """
                INSERT INTO experiments(
                    experiment_id, fingerprint, spec_json, status, created_at, registered_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    spec.experiment_id,
                    fingerprint,
                    canonical_json(spec.to_dict()),
                    status,
                    spec.created_at,
                    now,
                ),
            )
            self._event(
                "EXPERIMENT_REGISTERED",
                "experiment",
                spec.experiment_id,
                {"fingerprint": fingerprint, "status": status, "missing": missing},
            )
        return fingerprint

    def register_seen_dataset(
        self,
        dataset: DatasetProvenance,
        *,
        experiment_id: str | None = None,
        purpose: str = "development",
    ) -> str:
        if dataset.role is DatasetRole.BLIND_FORWARD_OOS:
            raise DatasetRoleConflict("un dataset vu ne peut pas être enregistré comme blind")
        usage_id = sha256_canonical(
            {
                "dataset_id": dataset.dataset_id,
                "venue": dataset.venue,
                "symbol": dataset.symbol,
                "start": dataset.start,
                "end": dataset.end,
                "sha256": dataset.sha256,
            }
        )
        now = _now()
        with self._transaction():
            existing = self._connection.execute(
                "SELECT role, sha256 FROM dataset_usage WHERE usage_id=?", (usage_id,)
            ).fetchone()
            if existing is not None:
                if existing[1] != dataset.sha256:
                    raise DatasetRoleConflict("identité dataset contradictoire")
                return usage_id
            self._connection.execute(
                """
                INSERT INTO dataset_usage(
                    usage_id, dataset_id, venue, symbol, start_ts, end_ts, sha256,
                    role, status, experiment_id, purpose, first_registered_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'SEEN', ?, ?, ?)
                """,
                (
                    usage_id,
                    dataset.dataset_id,
                    dataset.venue,
                    dataset.symbol,
                    dataset.start,
                    dataset.end,
                    dataset.sha256,
                    dataset.role.value,
                    experiment_id,
                    purpose,
                    now,
                ),
            )
            self._event(
                "DATASET_SEEN",
                "dataset",
                usage_id,
                {"role": dataset.role.value, "purpose": purpose},
            )
        return usage_id

    def reserve_blind_dataset(
        self,
        dataset: DatasetProvenance,
        *,
        experiment_id: str,
        purpose: str = "blind_holdout",
    ) -> str:
        if dataset.role is not DatasetRole.BLIND_FORWARD_OOS or dataset.already_seen:
            raise DatasetRoleConflict("un blind doit être réellement non vu")
        if purpose == "HYPERLIQUID_FINAL_OOS" and dataset.venue.lower() != "hyperliquid":
            raise DatasetRoleConflict("HYPERLIQUID final OOS exige Hyperliquid")
        usage_id = sha256_canonical(
            {
                "dataset_id": dataset.dataset_id,
                "venue": dataset.venue,
                "symbol": dataset.symbol,
                "start": dataset.start,
                "end": dataset.end,
                "sha256": dataset.sha256,
            }
        )
        with self._transaction():
            overlap = self._connection.execute(
                """
                SELECT usage_id, role, status FROM dataset_usage
                WHERE lower(venue)=lower(?) AND symbol=?
                  AND start_ts <= ? AND end_ts >= ?
                """,
                (dataset.venue, dataset.symbol, dataset.end, dataset.start),
            ).fetchone()
            if overlap is not None:
                raise DatasetRoleConflict(
                    "intervalle déjà enregistré et ne pouvant pas devenir blind"
                )
            self._connection.execute(
                """
                INSERT INTO dataset_usage(
                    usage_id, dataset_id, venue, symbol, start_ts, end_ts, sha256,
                    role, status, experiment_id, purpose, first_registered_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'BLIND_RESERVED', ?, ?, ?)
                """,
                (
                    usage_id,
                    dataset.dataset_id,
                    dataset.venue,
                    dataset.symbol,
                    dataset.start,
                    dataset.end,
                    dataset.sha256,
                    DatasetRole.BLIND_FORWARD_OOS.value,
                    experiment_id,
                    purpose,
                    _now(),
                ),
            )
            self._event(
                "BLIND_DATASET_RESERVED",
                "dataset",
                usage_id,
                {"experiment_id": experiment_id},
            )
        return usage_id

    def list_dataset_usage(self) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT * FROM dataset_usage ORDER BY first_registered_at, usage_id"
        ).fetchall()
        return [dict(row) for row in rows]

    def reserve_trial(
        self,
        spec: ExperimentSpec,
        parameters: Mapping[str, Any],
        *,
        dataset_fingerprint: str,
        split_fingerprint: str,
        seed: int | None = None,
        cost_assumptions: Mapping[str, Any] | None = None,
        kind: str = "SEARCH",
    ) -> TrialReservation:
        if kind == "SEARCH":
            validate_search_ready(spec)
        elif kind != "REPRODUCTION":
            raise GovernanceStoreError("trial kind inconnu")
        experiment_fp = self.register_experiment(spec)
        costs = cost_model_fingerprint(cost_assumptions or spec.cost_assumptions)
        trial_fp = _trial_fingerprint(
            experiment_fingerprint=experiment_fp,
            parameters=parameters,
            dataset_fingerprint=dataset_fingerprint,
            split_fingerprint=split_fingerprint,
            seed=seed if seed is not None else spec.random_seed,
            cost_fingerprint=costs,
        )
        with self._transaction():
            existing = self._connection.execute(
                "SELECT trial_id, sequence FROM trials WHERE experiment_id=? AND trial_fingerprint=?",
                (spec.experiment_id, trial_fp),
            ).fetchone()
            if existing is not None:
                if kind != "REPRODUCTION":
                    raise DuplicateTrial("trial sémantiquement identique déjà réservé")
                attempt_id = self._insert_attempt(
                    str(existing[0]), "REPRODUCTION", "REPRODUCTION_REQUESTED", {}
                )
                self._event(
                    "TRIAL_REPRODUCTION_REQUESTED",
                    "trial",
                    str(existing[0]),
                    {"trial_fingerprint": trial_fp},
                )
                return TrialReservation(
                    str(existing[0]), trial_fp, int(existing[1]), False, attempt_id
                )
            if kind == "REPRODUCTION":
                raise DuplicateTrial(
                    "reproduction explicite exige un trial de recherche déjà enregistré"
                )
            count = int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM trials WHERE experiment_id=? AND trial_kind='SEARCH'",
                    (spec.experiment_id,),
                ).fetchone()[0]
            )
            if count >= spec.maximum_trial_budget:
                raise TrialBudgetExceeded(
                    f"TRIAL_BUDGET_EXHAUSTED: {count + 1} > {spec.maximum_trial_budget}"
                )
            sequence = count + 1
            trial_id = f"{spec.experiment_id}:trial:{sequence:06d}"
            self._connection.execute(
                """
                INSERT INTO trials(
                    trial_id, experiment_id, experiment_fingerprint, trial_fingerprint,
                    parameter_fingerprint, parameters_json, dataset_fingerprint,
                    split_fingerprint, seed, cost_model_fingerprint, sequence, trial_kind,
                    status, reserved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'SEARCH', 'RESERVED', ?)
                """,
                (
                    trial_id,
                    spec.experiment_id,
                    experiment_fp,
                    trial_fp,
                    parameter_fingerprint(parameters),
                    canonical_json(dict(parameters)),
                    dataset_fingerprint,
                    split_fingerprint,
                    seed if seed is not None else spec.random_seed,
                    costs,
                    sequence,
                    _now(),
                ),
            )
            attempt_id = self._insert_attempt(trial_id, "SEARCH", "RESERVED", {})
            self._event(
                "TRIAL_RESERVED",
                "trial",
                trial_id,
                {"sequence": sequence, "trial_fingerprint": trial_fp},
            )
        return TrialReservation(trial_id, trial_fp, sequence, True, attempt_id)

    def _insert_attempt(
        self, trial_id: str, kind: str, status: str, details: Mapping[str, Any]
    ) -> int:
        current = self._connection.execute(
            "SELECT COALESCE(MAX(attempt_no), 0) FROM trial_attempts WHERE trial_id=?",
            (trial_id,),
        ).fetchone()
        attempt_no = int(current[0]) + 1
        cursor = self._connection.execute(
            """
            INSERT INTO trial_attempts(
                trial_id, attempt_no, kind, status, started_at, details_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (trial_id, attempt_no, kind, status, _now(), canonical_json(details)),
        )
        return int(cursor.lastrowid or 0)

    def execute_trial(
        self,
        spec: ExperimentSpec,
        parameters: Mapping[str, Any],
        *,
        dataset_fingerprint: str,
        split_fingerprint: str,
        evaluator: Callable[[TrialReservation], Mapping[str, Any]],
        seed: int | None = None,
        cost_assumptions: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Reserve, then execute; a crash leaves the reservation durable."""

        reservation = self.reserve_trial(
            spec,
            parameters,
            dataset_fingerprint=dataset_fingerprint,
            split_fingerprint=split_fingerprint,
            seed=seed,
            cost_assumptions=cost_assumptions,
        )
        if not reservation.is_new:
            row = self.get_trial(reservation.trial_id)
            if row is None or not row.get("result_json"):
                raise GovernanceStoreError("reproduction sans résultat durable")
            return _json_load(row["result_json"])
        self.start_trial(reservation.trial_id)
        try:
            result = dict(evaluator(reservation))
        except Exception as exc:
            self.finish_trial(reservation.trial_id, status="FAILED", failure_reason=str(exc))
            raise
        self.finish_trial(
            reservation.trial_id,
            status="SUCCEEDED",
            result=result,
            metrics=result.get("metrics", {})
            if isinstance(result.get("metrics", {}), Mapping)
            else {},
        )
        return result

    def start_trial(self, trial_id: str) -> None:
        with self._transaction():
            row = self._connection.execute(
                "SELECT status FROM trials WHERE trial_id=?", (trial_id,)
            ).fetchone()
            if row is None or row[0] != "RESERVED":
                raise GovernanceStoreError("trial non réservable ou déjà terminal")
            self._connection.execute(
                "UPDATE trials SET status='RUNNING', started_at=? WHERE trial_id=?",
                (_now(), trial_id),
            )
            self._connection.execute(
                "UPDATE trial_attempts SET status='RUNNING' WHERE trial_id=? AND status='RESERVED'",
                (trial_id,),
            )
            self._event("TRIAL_STARTED", "trial", trial_id, {})

    def finish_trial(
        self,
        trial_id: str,
        *,
        status: str,
        result: Mapping[str, Any] | None = None,
        metrics: Mapping[str, Any] | None = None,
        failure_reason: str | None = None,
    ) -> None:
        if status not in {"SUCCEEDED", "FAILED", "INVALID_RESULT", "ABORTED"}:
            raise GovernanceStoreError("état terminal de trial inconnu")
        with self._transaction():
            row = self._connection.execute(
                "SELECT status FROM trials WHERE trial_id=?", (trial_id,)
            ).fetchone()
            if row is None or row[0] not in {"RESERVED", "RUNNING"}:
                raise GovernanceStoreError("trial non finalisable")
            self._connection.execute(
                """
                UPDATE trials SET status=?, finished_at=?, result_json=?, metrics_json=?,
                    failure_reason=? WHERE trial_id=?
                """,
                (
                    status,
                    _now(),
                    canonical_json(dict(result or {})),
                    canonical_json(dict(metrics or {})),
                    failure_reason,
                    trial_id,
                ),
            )
            self._connection.execute(
                """
                UPDATE trial_attempts SET status=?, finished_at=?, details_json=?
                WHERE trial_id=? AND finished_at IS NULL
                """,
                (status, _now(), canonical_json(dict(result or {})), trial_id),
            )
            self._event("TRIAL_FINISHED", "trial", trial_id, {"status": status})

    def abort_trial(self, trial_id: str, reason: str) -> None:
        self.finish_trial(trial_id, status="ABORTED", failure_reason=reason)

    def get_trial(self, trial_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT * FROM trials WHERE trial_id=?", (trial_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def trial_count(self, experiment_id: str) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) FROM trials WHERE experiment_id=? AND trial_kind='SEARCH'",
            (experiment_id,),
        ).fetchone()
        return int(row[0])

    def begin_reproduction(
        self,
        spec: ExperimentSpec,
        parameters: Mapping[str, Any],
        *,
        dataset_fingerprint: str,
        split_fingerprint: str,
        seed: int | None = None,
        cost_assumptions: Mapping[str, Any] | None = None,
    ) -> TrialReservation:
        return self.reserve_trial(
            spec,
            parameters,
            dataset_fingerprint=dataset_fingerprint,
            split_fingerprint=split_fingerprint,
            seed=seed,
            cost_assumptions=cost_assumptions,
            kind="REPRODUCTION",
        )

    def reserve_holdout(
        self,
        *,
        holdout_id: str,
        candidate_fingerprint: str,
        experiment_fingerprint: str,
        code_sha: str,
        venue: str,
        symbol: str,
        start_rule: str,
        end_rule: str,
        cost_assumptions: Mapping[str, Any],
        metrics: Mapping[str, Any],
        promotion_gates: Mapping[str, Any],
        sample_sufficiency_rule: Mapping[str, Any],
        dataset_usage_id: str,
    ) -> HoldoutReservation:
        identity = sha256_canonical(
            {
                "candidate_fingerprint": candidate_fingerprint,
                "experiment_fingerprint": experiment_fingerprint,
                "code_sha": code_sha,
                "venue": venue,
                "symbol": symbol,
                "start_rule": start_rule,
                "end_rule": end_rule,
                "cost_assumptions": dict(cost_assumptions),
                "metrics": dict(metrics),
                "promotion_gates": dict(promotion_gates),
                "sample_sufficiency_rule": dict(sample_sufficiency_rule),
                "dataset_usage_id": dataset_usage_id,
            }
        )
        with self._transaction():
            existing = self._connection.execute(
                "SELECT * FROM holdouts WHERE holdout_id=?", (holdout_id,)
            ).fetchone()
            if existing is not None:
                if existing["identity_fingerprint"] != identity:
                    raise HoldoutConflict("holdout identity changed")
                return HoldoutReservation(
                    holdout_id, str(existing["status"]), existing["evaluation_token"]
                )
            if dataset_usage_id:
                usage = self._connection.execute(
                    "SELECT role, status FROM dataset_usage WHERE usage_id=?",
                    (dataset_usage_id,),
                ).fetchone()
                if usage is None or usage[0] != DatasetRole.BLIND_FORWARD_OOS.value:
                    raise HoldoutConflict("holdout dataset is not a reserved blind dataset")
                if usage[1] != "BLIND_RESERVED":
                    raise HoldoutConflict("holdout dataset is not reserved")
            self._connection.execute(
                """
                INSERT INTO holdouts(
                    holdout_id, identity_fingerprint, candidate_fingerprint,
                    experiment_fingerprint, code_sha, venue, symbol, start_rule, end_rule,
                    cost_json, metrics_json, gates_json, sample_rule_json, dataset_usage_id,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'BLIND_RESERVED', ?, ?)
                """,
                (
                    holdout_id,
                    identity,
                    candidate_fingerprint,
                    experiment_fingerprint,
                    code_sha,
                    venue,
                    symbol,
                    start_rule,
                    end_rule,
                    canonical_json(dict(cost_assumptions)),
                    canonical_json(dict(metrics)),
                    canonical_json(dict(promotion_gates)),
                    canonical_json(dict(sample_sufficiency_rule)),
                    dataset_usage_id,
                    _now(),
                    _now(),
                ),
            )
            self._event("HOLDOUT_RESERVED", "holdout", holdout_id, {"identity": identity})
        return HoldoutReservation(holdout_id, "BLIND_RESERVED", None)

    def _holdout_identity_matches(self, row: sqlite3.Row, identity_fingerprint: str) -> bool:
        return str(row["identity_fingerprint"]) == identity_fingerprint

    def _holdout_identity(self, holdout_id: str, identity: Mapping[str, Any]) -> str:
        row = self._connection.execute(
            "SELECT * FROM holdouts WHERE holdout_id=?", (holdout_id,)
        ).fetchone()
        if row is None:
            raise HoldoutConflict("holdout inconnu")
        computed = sha256_canonical(dict(identity))
        if not self._holdout_identity_matches(row, computed):
            raise HoldoutInvalidated("identité du holdout modifiée")
        return computed

    def begin_holdout_evaluation(
        self, holdout_id: str, *, identity: Mapping[str, Any]
    ) -> HoldoutReservation:
        identity_fp = sha256_canonical(dict(identity))
        with self._transaction():
            row = self._connection.execute(
                "SELECT * FROM holdouts WHERE holdout_id=?", (holdout_id,)
            ).fetchone()
            if row is None or not self._holdout_identity_matches(row, identity_fp):
                raise HoldoutInvalidated("identité du holdout modifiée ou inconnue")
            status = str(row["status"])
            if status == "SPENT":
                return HoldoutReservation(holdout_id, status, None)
            if status == "INVALIDATED":
                raise HoldoutInvalidated("holdout invalidé")
            token = row["evaluation_token"] or secrets.token_hex(16)
            if status not in {"BLIND_RESERVED", "PENDING"}:
                raise HoldoutConflict("état holdout non évaluable")
            self._connection.execute(
                "UPDATE holdouts SET status='PENDING', evaluation_token=?, updated_at=? WHERE holdout_id=?",
                (token, _now(), holdout_id),
            )
            self._event("HOLDOUT_PENDING", "holdout", holdout_id, {"identity": identity_fp})
        return HoldoutReservation(holdout_id, "PENDING", str(token))

    def evaluate_holdout(
        self,
        holdout_id: str,
        *,
        identity: Mapping[str, Any],
        evaluator: Callable[[], Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Evaluate without exposing the result until result+SPENT commit."""

        reservation = self.begin_holdout_evaluation(holdout_id, identity=identity)
        if reservation.status == "SPENT":
            row = self._connection.execute(
                "SELECT result_json FROM holdouts WHERE holdout_id=?", (holdout_id,)
            ).fetchone()
            return _json_load(row[0] if row else None)
        # Exceptions leave the durable state PENDING; they never reset it.
        computed = dict(evaluator())
        result_json = canonical_json(computed)
        result_fp = result_fingerprint(computed)
        with self._transaction():
            row = self._connection.execute(
                "SELECT status, identity_fingerprint FROM holdouts WHERE holdout_id=?",
                (holdout_id,),
            ).fetchone()
            if row is None or row[0] != "PENDING" or row[1] != sha256_canonical(dict(identity)):
                raise HoldoutConflict("holdout non disponible pour le commit du résultat")
            self._connection.execute(
                """
                UPDATE holdouts SET status='SPENT', result_json=?, result_fingerprint=?,
                    updated_at=? WHERE holdout_id=?
                """,
                (result_json, result_fp, _now(), holdout_id),
            )
            self._event(
                "HOLDOUT_SPENT",
                "holdout",
                holdout_id,
                {"result_fingerprint": result_fp},
            )
        return computed

    def invalidate_holdout(self, holdout_id: str, reason: str) -> None:
        with self._transaction():
            row = self._connection.execute(
                "SELECT status FROM holdouts WHERE holdout_id=?", (holdout_id,)
            ).fetchone()
            if row is None or row[0] == "SPENT":
                raise HoldoutConflict("holdout absent ou déjà dépensé")
            self._connection.execute(
                "UPDATE holdouts SET status='INVALIDATED', updated_at=? WHERE holdout_id=?",
                (_now(), holdout_id),
            )
            self._event("HOLDOUT_INVALIDATED", "holdout", holdout_id, {"reason": reason})

    def get_holdout(self, holdout_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT * FROM holdouts WHERE holdout_id=?", (holdout_id,)
        ).fetchone()
        if row is None:
            return None
        output = dict(row)
        output["result"] = _json_load(output.pop("result_json"))
        output["cost_assumptions"] = _json_load(output.pop("cost_json"))
        output["metrics"] = _json_load(output.pop("metrics_json"))
        output["promotion_gates"] = _json_load(output.pop("gates_json"))
        output["sample_sufficiency_rule"] = _json_load(output.pop("sample_rule_json"))
        return output
