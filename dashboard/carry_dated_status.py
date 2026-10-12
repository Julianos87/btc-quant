"""Strict, read-only projection for the isolated dated-carry PAPER runner."""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


STATUS_ENV = "BTCQUANT_CARRY_DATED_STATUS_PATH"
DEFAULT_STATUS_RELATIVE_PATH = Path("carry-dated-experiment/carry_dated_status.json")
DEFAULT_COLLECTOR_STATUS_RELATIVE_PATH = Path("carry-dated-experiment/collector_status.json")
STATUS_SCHEMA_VERSION = 1
MAX_STATUS_BYTES = 256 * 1024
MAX_COLLECTOR_STATUS_BYTES = 256 * 1024
# This matches the existing Carry PAPER stale-state limit. The dated runner's
# service/collector liveness is reported separately and is not inferred here.
MAX_SNAPSHOT_AGE_SECONDS = 20 * 60
MAX_COLLECTOR_AGE_SECONDS = 3 * 60
ALLOWED_STATES = frozenset(
    {
        "FLAT",
        "OPENING",
        "OPEN",
        "SETTLEMENT_PENDING",
        "CLOSING",
        "UNBALANCED",
        "ACCOUNTING_UNCERTAIN",
    }
)

REQUIRED_KEYS = frozenset(
    {
        "strategy",
        "schema_version",
        "paper_only",
        "status",
        "instrument",
        "expiry_utc",
        "capital",
        "cash_total",
        "free_cash",
        "reserved_margin",
        "stress_cash_buffer",
        "spot_qty",
        "future_qty",
        "hedge_residual",
        "spot_avg_entry",
        "future_avg_entry",
        "last_settlement_key",
        "last_settlement_index",
        "last_settlement_period_end",
        "settlements_applied",
        "pnl_realized",
        "pnl_unrealized",
        "fees_paid",
        "decision",
        "decision_at",
        "reason_code",
        "reason_message",
        "market",
        "accounting_uncertain",
        "incident",
        "pending_saga_ids",
        "last_updated_at",
    }
)
DECIMAL_KEYS = frozenset(
    {
        "capital",
        "cash_total",
        "free_cash",
        "reserved_margin",
        "stress_cash_buffer",
        "spot_qty",
        "future_qty",
        "hedge_residual",
        "spot_avg_entry",
        "future_avg_entry",
        "last_settlement_index",
        "pnl_realized",
        "pnl_unrealized",
        "fees_paid",
    }
)
TIMESTAMP_KEYS = frozenset(
    {
        "expiry_utc",
        "last_settlement_period_end",
        "decision_at",
        "last_updated_at",
    }
)


class CarryDatedStatusError(ValueError):
    """The dated carry snapshot does not satisfy the published read contract."""


def configured_status_path(state_dir: Path, environ: dict[str, str] | None = None) -> Path:
    """Resolve the configured status snapshot without consulting any DB state."""

    env = os.environ if environ is None else environ
    configured = env.get(STATUS_ENV)
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            raise CarryDatedStatusError("STATUS_PATH_MUST_BE_ABSOLUTE")
        return path
    return state_dir / DEFAULT_STATUS_RELATIVE_PATH


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CarryDatedStatusError("DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _collector_projection(state_dir: Path, *, now: datetime) -> dict[str, Any]:
    """Project the collector heartbeat without claiming systemd/process health."""

    path = state_dir / DEFAULT_COLLECTOR_STATUS_RELATIVE_PATH
    base = {
        "unit": "btcquant-carry-dated-collector.service",
        "status": "UNKNOWN",
        "attempted_at": None,
        "age_seconds": None,
        "state": None,
        "market_data": {"status": "UNKNOWN", "freshest_market_timestamp": None},
        "disk": {"status": "UNKNOWN", "used_bytes": None, "free_bytes": None, "quota_bytes": None},
        "coverage": {"start_utc": None, "end_utc": None, "archive_segments": None},
        "error_counts": {"book": None, "settlement": None},
    }
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_COLLECTOR_STATUS_BYTES:
            return {**base, "reason_code": "COLLECTOR_STATUS_INVALID_SIZE_OR_TYPE"}
        with path.open("rb") as handle:
            raw = handle.read(MAX_COLLECTOR_STATUS_BYTES + 1)
        if len(raw) > MAX_COLLECTOR_STATUS_BYTES:
            return {**base, "reason_code": "COLLECTOR_STATUS_INVALID_SIZE_OR_TYPE"}
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != 1
            or payload.get("service") != "btcquant-carry-dated-collector"
            or payload.get("paper_only") is not True
            or not isinstance(payload.get("details"), dict)
        ):
            return {**base, "reason_code": "COLLECTOR_STATUS_CONTRACT_INVALID"}
        attempted = _timestamp_or_none(payload.get("attempted_at"), "collector.attempted_at")
        if attempted is None:
            return {**base, "reason_code": "COLLECTOR_STATUS_TIMESTAMP_MISSING"}
        age = (now - attempted).total_seconds()
        if age < -30:
            return {**base, "reason_code": "COLLECTOR_STATUS_CLOCK_SKEW"}
        details = payload["details"]
        state = payload.get("state")
        if not isinstance(state, str):
            return {**base, "reason_code": "COLLECTOR_STATUS_STATE_INVALID"}
        is_fresh = age <= MAX_COLLECTOR_AGE_SECONDS
        archive_state = details.get("archive_integrity_status", "UNKNOWN")
        status = (
            "HEALTHY"
            if is_fresh and state == "OK" and archive_state in {"VERIFIED", "NO_ARCHIVES"}
            else "STALE"
            if not is_fresh
            else "DEGRADED"
        )
        book_errors = details.get("book_errors")
        settlement_errors = details.get("settlement_errors")
        book_quality = details.get("book_quality")
        freshness = _timestamp_or_none(
            details.get("freshest_market_timestamp"), "collector.freshest_market_timestamp"
        )
        max_bytes = payload.get("max_total_bytes")
        total_bytes = details.get("total_bytes")
        free_bytes = details.get("free_bytes")
        min_free_bytes = payload.get("min_free_bytes")
        numeric = (max_bytes, total_bytes, free_bytes, min_free_bytes)
        valid_storage = all(type(item) is int and item >= 0 for item in numeric)
        disk_status = (
            "OK"
            if valid_storage and total_bytes < max_bytes and free_bytes >= min_free_bytes
            else "LIMIT_REACHED"
            if valid_storage
            else "UNKNOWN"
        )
        market_status = (
            "HEALTHY"
            if status == "HEALTHY"
            and isinstance(book_errors, list)
            and not book_errors
            and isinstance(book_quality, dict)
            and bool(book_quality)
            and all(isinstance(reasons, list) and not reasons for reasons in book_quality.values())
            and freshness is not None
            else "DEGRADED"
            if status in {"HEALTHY", "DEGRADED"}
            else status
        )
        return {
            **base,
            "status": status,
            "attempted_at": attempted.isoformat().replace("+00:00", "Z"),
            "age_seconds": max(0.0, age),
            "state": state,
            "reason_code": details.get("reason"),
            "market_data": {
                "status": market_status,
                "freshest_market_timestamp": (
                    freshness.isoformat().replace("+00:00", "Z") if freshness else None
                ),
            },
            "disk": {
                "status": disk_status,
                "used_bytes": total_bytes if type(total_bytes) is int else None,
                "free_bytes": free_bytes if type(free_bytes) is int else None,
                "quota_bytes": max_bytes if type(max_bytes) is int else None,
                "minimum_free_bytes": min_free_bytes if type(min_free_bytes) is int else None,
            },
            "coverage": {
                "start_utc": details.get("observed_coverage_start_utc"),
                "end_utc": details.get("observed_coverage_end_utc"),
                "archive_segments": details.get("archive_segment_count"),
                "invalid_archive_manifests": details.get("invalid_archive_manifest_count"),
            },
            "error_counts": {
                "book": len(book_errors) if isinstance(book_errors, list) else None,
                "settlement": len(settlement_errors)
                if isinstance(settlement_errors, list)
                else None,
            },
            "archive_integrity": {
                "status": archive_state,
                "verified_segments": details.get("archive_verified_segment_count"),
                "unverified_segments": details.get("archive_unverified_segment_count"),
                "failed_segments": details.get("archive_failed_segment_count"),
                "last_verified_at": details.get("archive_last_verification_at"),
            },
            "retention_goal_days": payload.get("retention_goal_days"),
            "retention_policy": payload.get("retention_policy"),
        }
    except (OSError, UnicodeError, json.JSONDecodeError, CarryDatedStatusError):
        return {**base, "reason_code": "COLLECTOR_STATUS_UNREADABLE"}


def _decimal_or_none(value: Any, name: str) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        raise CarryDatedStatusError(f"{name.upper()}_NOT_DECIMAL_STRING")
    try:
        number = Decimal(value)
    except InvalidOperation as error:
        raise CarryDatedStatusError(f"{name.upper()}_INVALID_DECIMAL") from error
    if not number.is_finite():
        raise CarryDatedStatusError(f"{name.upper()}_NONFINITE_DECIMAL")


def _timestamp_or_none(value: Any, name: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise CarryDatedStatusError(f"{name.upper()}_NOT_ISO_TIMESTAMP")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise CarryDatedStatusError(f"{name.upper()}_INVALID_TIMESTAMP") from error
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise CarryDatedStatusError(f"{name.upper()}_NOT_UTC")
    return parsed.astimezone(UTC)


def validate_snapshot(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise CarryDatedStatusError("ROOT_NOT_OBJECT")
    missing = REQUIRED_KEYS - payload.keys()
    if missing:
        raise CarryDatedStatusError("REQUIRED_KEYS_MISSING")
    if (
        type(payload.get("schema_version")) is not int
        or payload.get("schema_version") != STATUS_SCHEMA_VERSION
    ):
        raise CarryDatedStatusError("UNSUPPORTED_SCHEMA_VERSION")
    if payload.get("strategy") != "carry_btc_dated" or payload.get("paper_only") is not True:
        raise CarryDatedStatusError("STRATEGY_OR_MODE_MISMATCH")
    if not isinstance(payload.get("status"), str) or payload.get("status") not in ALLOWED_STATES:
        raise CarryDatedStatusError("UNKNOWN_STATE")
    if not isinstance(payload.get("accounting_uncertain"), bool):
        raise CarryDatedStatusError("ACCOUNTING_FLAG_INVALID")
    if not isinstance(payload.get("settlements_applied"), int) or isinstance(
        payload.get("settlements_applied"), bool
    ):
        raise CarryDatedStatusError("SETTLEMENT_COUNT_INVALID")
    if payload["settlements_applied"] < 0:
        raise CarryDatedStatusError("SETTLEMENT_COUNT_INVALID")
    if not isinstance(payload.get("pending_saga_ids"), list) or any(
        not isinstance(item, str) for item in payload["pending_saga_ids"]
    ):
        raise CarryDatedStatusError("PENDING_SAGA_IDS_INVALID")
    if payload.get("instrument") is not None and not isinstance(payload["instrument"], dict):
        raise CarryDatedStatusError("INSTRUMENT_INVALID")
    if not isinstance(payload.get("market"), dict) or not {
        "spot_timestamp",
        "future_timestamp",
    }.issubset(payload["market"]):
        raise CarryDatedStatusError("MARKET_INVALID")
    for key in DECIMAL_KEYS:
        _decimal_or_none(payload.get(key), key)
    for key in TIMESTAMP_KEYS:
        _timestamp_or_none(payload.get(key), key)
    for key in ("spot_timestamp", "future_timestamp"):
        _timestamp_or_none(payload["market"].get(key), f"market.{key}")
    decision = payload.get("decision")
    if decision is not None and not isinstance(decision, dict):
        raise CarryDatedStatusError("DECISION_INVALID")
    if isinstance(decision, dict) and not {
        "action",
        "reason_code",
        "reason_message",
        "gross_premium",
        "net_before_reserve",
        "risk_reserve",
        "net_after_reserve",
    }.issubset(decision):
        raise CarryDatedStatusError("DECISION_KEYS_MISSING")
    incident = payload.get("incident")
    if incident is not None and not isinstance(incident, (dict, str)):
        raise CarryDatedStatusError("INCIDENT_INVALID")
    return payload


def project_status(
    state_dir: Path,
    *,
    now: datetime | None = None,
    environ: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Return an explicit health envelope; errors never turn into a flat state."""

    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() != UTC.utcoffset(current):
        raise ValueError("now must be timezone-aware UTC")
    try:
        path = configured_status_path(state_dir, environ)
    except CarryDatedStatusError as error:
        return _unavailable("UNKNOWN", str(error), state_dir=state_dir, now=current)
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_STATUS_BYTES:
            return _unavailable("UNKNOWN", "STATUS_SNAPSHOT_INVALID_SIZE_OR_TYPE")
        with path.open("rb") as handle:
            raw = handle.read(MAX_STATUS_BYTES + 1)
        if len(raw) > MAX_STATUS_BYTES:
            return _unavailable("UNKNOWN", "STATUS_SNAPSHOT_INVALID_SIZE_OR_TYPE")
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
        snapshot = validate_snapshot(payload)
    except (OSError, UnicodeError, json.JSONDecodeError, CarryDatedStatusError) as error:
        if isinstance(error, FileNotFoundError):
            return _unavailable(
                "NOT_DEPLOYED", "STATUS_SNAPSHOT_MISSING", state_dir=state_dir, now=current
            )
        reason = (
            str(error) if isinstance(error, CarryDatedStatusError) else "STATUS_SNAPSHOT_UNREADABLE"
        )
        return _unavailable("UNKNOWN", reason, state_dir=state_dir, now=current)

    updated = _timestamp_or_none(snapshot["last_updated_at"], "last_updated_at")
    if updated is None:
        return _unavailable("UNKNOWN", "STATUS_TIMESTAMP_MISSING", state_dir=state_dir, now=current)
    age = (current - updated).total_seconds()
    if age < -30:
        return _unavailable("UNKNOWN", "STATUS_CLOCK_SKEW", state_dir=state_dir, now=current)
    if age > MAX_SNAPSHOT_AGE_SECONDS:
        envelope_status = "STALE"
    elif snapshot["accounting_uncertain"] or snapshot["status"] == "ACCOUNTING_UNCERTAIN":
        envelope_status = "ACCOUNTING_UNCERTAIN"
    elif snapshot["incident"] is not None:
        envelope_status = "INCIDENT_OPEN"
    elif snapshot["status"] == "UNBALANCED":
        envelope_status = "UNBALANCED"
    else:
        envelope_status = "AVAILABLE"
    collector = _collector_projection(state_dir, now=current)
    return {
        "api_schema_version": 1,
        "kind": "CARRY_DATED_PAPER_STATUS",
        "status": envelope_status,
        "reason_code": None if envelope_status == "AVAILABLE" else envelope_status,
        "snapshot_age_seconds": max(0.0, age),
        "snapshot_max_age_seconds": MAX_SNAPSHOT_AGE_SECONDS,
        # The status snapshot does not prove systemd liveness, collector health,
        # database integrity, or economics. Keep overall readiness explicit.
        "readiness_status": "UNKNOWN",
        "readiness_reason_codes": [
            "COLLECTOR_SYSTEMD_LIVENESS_NOT_PROBED",
            "RUNNER_UNIT_PROBE_NOT_BOUND",
            "DATABASE_INTEGRITY_NOT_BOUND",
            "BACKUP_RESTORE_NOT_BOUND",
        ],
        "components": {
            "runner": {
                "unit": "btcquant-carry-dated-experimental.service",
                "status": "UNKNOWN",
            },
            "collector": collector,
            "database": {"integrity": "UNKNOWN"},
            "disk": collector["disk"],
            "market_data": {
                **collector["market_data"],
                "spot_timestamp": snapshot["market"].get("spot_timestamp"),
                "future_timestamp": snapshot["market"].get("future_timestamp"),
            },
            "settlement_backlog": {"status": "UNKNOWN", "pending_count": None},
        },
        "economic_evidence": "INSUFFICIENT_EVIDENCE",
        "snapshot": snapshot,
    }


def _unavailable(
    status: str, reason: str, *, state_dir: Path, now: datetime
) -> dict[str, Any]:
    collector = _collector_projection(state_dir, now=now)
    return {
        "api_schema_version": 1,
        "kind": "CARRY_DATED_PAPER_STATUS",
        "status": status,
        "reason_code": reason,
        "snapshot_age_seconds": None,
        "snapshot_max_age_seconds": MAX_SNAPSHOT_AGE_SECONDS,
        "readiness_status": "UNKNOWN",
        "readiness_reason_codes": [reason],
        "components": {
            "runner": {
                "unit": "btcquant-carry-dated-experimental.service",
                "status": "UNKNOWN",
            },
            "collector": collector,
            "database": {"integrity": "UNKNOWN"},
            "disk": collector["disk"],
            "market_data": {
                **collector["market_data"],
                "spot_timestamp": None,
                "future_timestamp": None,
            },
            "settlement_backlog": {"status": "UNKNOWN", "pending_count": None},
        },
        "economic_evidence": "INSUFFICIENT_EVIDENCE",
        "snapshot": None,
    }


__all__ = [
    "ALLOWED_STATES",
    "DEFAULT_STATUS_RELATIVE_PATH",
    "DEFAULT_COLLECTOR_STATUS_RELATIVE_PATH",
    "MAX_SNAPSHOT_AGE_SECONDS",
    "STATUS_ENV",
    "CarryDatedStatusError",
    "configured_status_path",
    "project_status",
    "validate_snapshot",
]
