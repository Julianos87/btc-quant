"""Persistence boundary for PAPER qualification and maturity campaigns.

The repository owns only SQLite persistence.  Runtime evidence collection,
release inspection, health probes and policy evaluation remain outside this
module.  A caller may provide an existing connection for operations that are
already part of a larger transaction; the v3 maturity start path uses one
connection for every DB-local check and its INSERT.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Any


class QualificationRepository:
    """Store qualification, readiness and maturity campaign records."""

    def __init__(
        self,
        *,
        connect: Callable[[], sqlite3.Connection],
        transaction: Callable[[], AbstractContextManager[sqlite3.Connection]],
        encode_json: Callable[[Any], str],
        schema_version: int,
        now: Callable[[], str],
    ) -> None:
        self._connect_factory = connect
        self._transaction_factory = transaction
        self._encode_json = encode_json
        self._schema_version = schema_version
        self._now = now

    def _connect(self) -> sqlite3.Connection:
        return self._connect_factory()

    def _transaction(self) -> AbstractContextManager[sqlite3.Connection]:
        return self._transaction_factory()

    def start_qualification_campaign(
        self,
        *,
        protocol_version: int,
        policy: dict[str, Any],
        started_at: str | None = None,
    ) -> dict[str, Any]:
        """Start only a legacy fixture campaign; PAPER v3 uses the bound API."""

        if int(protocol_version) >= 3:
            raise RuntimeError("Le protocole PAPER v3 exige start_bound_paper_maturity_campaign")
        now = started_at or self._now()
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
                (protocol_version, self._encode_json(policy), now),
            )
            campaign_id = cursor.lastrowid
        assert campaign_id is not None
        return self.read_qualification_campaign(int(campaign_id))

    def start_bound_paper_maturity_campaign(
        self,
        *,
        policy: Mapping[str, Any],
        binding: Mapping[str, Any],
        required_engines: Sequence[str],
        qualification_id: int,
        now_factory: Callable[[], Any],
    ) -> dict[str, Any]:
        """Atomically insert one fully bound PAPER maturity campaign.

        The repository owns one ``BEGIN IMMEDIATE`` transaction.  The exact
        technical qualification row is read using that same connection before
        the campaign row is inserted; this prevents a second connection from
        weakening the start boundary.
        """

        if binding.get("environment") != "paper":
            raise ValueError("Une campagne PAPER doit être liée à l'environnement paper")
        if not isinstance(qualification_id, int) or isinstance(qualification_id, bool):
            raise ValueError("technical_qualification_id invalide")
        if int(binding.get("technical_qualification_id", -1)) != qualification_id:
            raise ValueError("Le binding de qualification est incohérent")
        engines = tuple(dict.fromkeys(str(engine) for engine in required_engines))
        if not engines or tuple(binding.get("required_engines", ())) != engines:
            raise ValueError("Le binding des moteurs PAPER est incohérent")
        if (
            not isinstance(binding.get("release_sha"), str)
            or re.fullmatch(r"[0-9a-f]{40}", binding["release_sha"]) is None
            or not isinstance(binding.get("release_tree"), str)
            or re.fullmatch(r"[0-9a-f]{40}", binding["release_tree"]) is None
            or binding.get("schema_version") != self._schema_version
            or binding.get("config_identity_version") != 1
            or re.fullmatch(r"[0-9a-f]{64}", str(binding.get("config_identity", ""))) is None
        ):
            raise ValueError("Le binding PAPER contient une identité invalide")

        with self._transaction() as connection:
            active = connection.execute(
                "SELECT id FROM qualification_campaigns WHERE status='RUNNING'"
            ).fetchone()
            if active is not None:
                raise RuntimeError(f"La campagne de qualification {active['id']} est déjà active")

            schema_row = connection.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()
            if schema_row is None or int(schema_row["value"]) != self._schema_version:
                raise RuntimeError("Le schéma PAPER n'est pas compatible")
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or integrity[0] != "ok":
                raise RuntimeError("L'intégrité SQLite PAPER n'est pas démontrée")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise RuntimeError("Les contraintes FK PAPER ne sont pas satisfaites")
            unresolved = connection.execute(
                "SELECT COUNT(*) FROM orders "
                "WHERE local_state != 'TERMINAL' "
                "AND NOT (order_type='STOP' AND status='OPEN')"
            ).fetchone()[0]
            if int(unresolved) != 0:
                raise RuntimeError("Des ordres PAPER restent non résolus")
            critical = connection.execute(
                "SELECT COUNT(*) FROM incidents WHERE status='OPEN' AND severity='CRITICAL'"
            ).fetchone()[0]
            if int(critical) != 0:
                raise RuntimeError("Un incident critique PAPER est ouvert")

            state_rows = {
                str(row["engine"]): row["payload"]
                for row in connection.execute("SELECT engine, payload FROM engine_state")
            }
            for engine in engines:
                raw = state_rows.get(engine)
                if raw is None:
                    raise RuntimeError(f"État moteur absent: {engine}")
                try:
                    state = json.loads(str(raw))
                except json.JSONDecodeError as error:
                    raise RuntimeError(f"État moteur malformé: {engine}") from error
                if not isinstance(state, dict) or state.get("reconciliation_required") is True:
                    raise RuntimeError(f"Réconciliation requise pour {engine}")
                if engine == "trend":
                    slots = state.get("slots")
                    if not isinstance(slots, dict) or any(
                        not isinstance(slot, dict)
                        or "position" not in slot
                        or slot.get("position") is not None
                        for slot in slots.values()
                    ):
                        raise RuntimeError("Le moteur Trend n'est pas FLAT")
                elif engine == "carry":
                    if (
                        state.get("execution_state") not in (None, "FLAT")
                        or state.get("in_position", False) is not False
                    ):
                        raise RuntimeError("Le moteur Carry n'est pas FLAT")
                else:
                    raise RuntimeError(f"Moteur requis non supporté: {engine}")

            placeholders = ",".join("?" for _ in engines)
            position_rows = connection.execute(
                f"SELECT engine, status FROM positions WHERE engine IN ({placeholders})",
                engines,
            ).fetchall()
            if any(row["status"] != "FLAT" for row in position_rows):
                raise RuntimeError("Une position PAPER n'est pas FLAT")

            qualification_record = self.paper_technical_qualification_record(
                qualification_id,
                connection=connection,
            )
            if qualification_record is None:
                raise RuntimeError("La qualification technique PAPER courante est absente")
            qualification_payload = qualification_record["payload"]
            for key in ("release_sha", "release_tree", "schema_version"):
                if qualification_payload.get(key) != binding.get(key):
                    raise RuntimeError(f"Binding de qualification incohérent: {key}")

            started_at = str(now_factory())
            try:
                parsed_started_at = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            except (TypeError, ValueError) as error:
                raise ValueError("started_at doit être ISO-8601") from error
            if parsed_started_at.tzinfo is None:
                raise ValueError("started_at doit inclure un fuseau explicite")
            payload = {"policy": dict(policy), "binding": dict(binding)}
            cursor = connection.execute(
                "INSERT INTO qualification_campaigns("
                "protocol_version, status, policy, started_at) "
                "VALUES(3, 'RUNNING', ?, ?)",
                (self._encode_json(payload), started_at),
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
                    self._encode_json(report),
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

    def record_paper_technical_qualification(
        self,
        *,
        release_sha: str,
        release_tree: str,
        schema_version: int,
        full_test_results: Mapping[str, Any],
        staging_run: Mapping[str, Any],
        migration: Mapping[str, Any],
        rollback_rehearsal: Mapping[str, Any],
        production_health: Mapping[str, Any],
        backup_verification: Mapping[str, Any],
        qualified_at: str | None = None,
    ) -> int:
        """Persist a complete PAPER technical qualification evidence bundle."""

        for name, value in (("release_sha", release_sha), ("release_tree", release_tree)):
            if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None:
                raise ValueError(f"{name} must be a lowercase Git SHA")
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version != self._schema_version
        ):
            raise ValueError("technical qualification schema does not match the running schema")
        evidence = {
            "full_test_results": full_test_results,
            "staging_run": staging_run,
            "migration": migration,
            "rollback_rehearsal": rollback_rehearsal,
            "production_health": production_health,
            "backup_verification": backup_verification,
        }
        for evidence_name, evidence_value in evidence.items():
            if not isinstance(evidence_value, Mapping) or evidence_value.get("status") != "PASS":
                raise ValueError(f"{evidence_name} must be a structured PASS record")
        timestamp = qualified_at or self._now()
        try:
            parsed_timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError) as error:
            raise ValueError("qualified_at must be an ISO-8601 timestamp") from error
        if parsed_timestamp.tzinfo is None:
            raise ValueError("qualified_at must include an explicit timezone")
        payload = {
            "kind": "PAPER_TECHNICAL_QUALIFICATION",
            "qualification_version": 1,
            "release_sha": release_sha,
            "release_tree": release_tree,
            "schema_version": schema_version,
            "full_test_results": dict(full_test_results),
            "staging_run": dict(staging_run),
            "migration": dict(migration),
            "rollback_rehearsal": dict(rollback_rehearsal),
            "production_health": dict(production_health),
            "backup_verification": dict(backup_verification),
            "status": "PAPER_TECHNICAL_QUALIFIED",
            "qualified_at": parsed_timestamp.astimezone(UTC).isoformat(),
            "protocol_version": 1,
        }
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO readiness_reports(
                    campaign_id, protocol_version, status, generated_at, payload
                ) VALUES(NULL, ?, 'PASS', ?, ?)
                """,
                (1, payload["qualified_at"], self._encode_json(payload)),
            )
        assert cursor.lastrowid is not None
        return int(cursor.lastrowid)

    def latest_paper_technical_qualification(self) -> dict[str, Any] | None:
        record = self.latest_paper_technical_qualification_record()
        return record["payload"] if record else None

    def latest_paper_technical_qualification_record(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, status, generated_at, payload "
                "FROM readiness_reports WHERE status='PASS' ORDER BY id DESC"
            ).fetchall()
        for row in rows:
            try:
                payload = json.loads(str(row["payload"]))
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict) and payload.get("kind") == (
                "PAPER_TECHNICAL_QUALIFICATION"
            ):
                return {
                    "id": int(row["id"]),
                    "status": str(row["status"]),
                    "generated_at": str(row["generated_at"]),
                    "payload": payload,
                }
        return None

    def paper_technical_qualification_record(
        self,
        qualification_id: int,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        def read(active_connection: sqlite3.Connection) -> dict[str, Any] | None:
            row = active_connection.execute(
                "SELECT id, status, generated_at, payload FROM readiness_reports WHERE id = ?",
                (qualification_id,),
            ).fetchone()
            if row is None or str(row["status"]) != "PASS":
                return None
            try:
                payload = json.loads(str(row["payload"]))
            except (TypeError, json.JSONDecodeError):
                return None
            if not isinstance(payload, dict) or payload.get("kind") != (
                "PAPER_TECHNICAL_QUALIFICATION"
            ):
                return None
            return {
                "id": int(row["id"]),
                "status": str(row["status"]),
                "generated_at": str(row["generated_at"]),
                "payload": payload,
            }

        if connection is not None:
            return read(connection)
        with self._connect() as owned_connection:
            return read(owned_connection)

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
                    ended_at or self._now(),
                    self._encode_json(final_report) if final_report else None,
                    campaign_id,
                ),
            )
        if cursor.rowcount != 1:
            raise RuntimeError("La campagne n'est plus active")
