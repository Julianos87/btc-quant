"""Read-only gate for a deliberately inactive Hyperliquid testnet profile.

This module does not start a service, load execution credentials, or call an
exchange.  A failing external-evidence gate is intentional until venue fill
identity and the external reconciliation/application path are formally
qualified.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import load_config
from .external_capability_profile import hyperliquid_testnet_trend_ioc_v1
from .readiness import paper_maturity_status, require_passed_qualification
from .state_store import SCHEMA_VERSION, StateStore

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_WALLET_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_PRIVATE_KEY_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")

# These are explicit gates, not optimistic defaults.  They remain false until
# the corresponding external execution contracts are separately qualified.
EXTERNAL_FILL_IDENTITY_PROVEN = False
EXTERNAL_RECONCILIATION_COORDINATOR_PROVEN = False
EXTERNAL_FINANCIAL_APPLICATION_PROVEN = False
EXTERNAL_FINALIZATION_PROVEN = False
EXTERNAL_ZERO_EFFECT_PROVEN = False
EXTERNAL_AUTOMATIC_RETRY_ENABLED = False


@dataclass(frozen=True)
class PreflightCheck:
    key: str
    passed: bool
    value: str
    required: str
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "key": self.key,
            "passed": self.passed,
            "value": self.value,
            "required": self.required,
        }
        if self.reason is not None:
            payload["reason"] = self.reason
        return payload


def _check(
    key: str,
    passed: bool,
    value: object,
    required: str,
    reason: str | None = None,
) -> PreflightCheck:
    return PreflightCheck(key, passed, str(value), required, reason)


def _gate_summary(
    checks: list[PreflightCheck],
    *,
    inspect_systemd: bool,
) -> list[dict[str, Any]]:
    """Project checks into separate technical and activation gates."""

    by_key = {item.key: item for item in checks}
    summary: list[dict[str, Any]] = []

    def add(name: str, keys: tuple[str, ...], *, required: str) -> None:
        selected = [by_key[key] for key in keys if key in by_key]
        passed = bool(selected) and all(item.passed for item in selected)
        reason = next((item.reason for item in selected if item.reason), None)
        summary.append(
            {
                "name": name,
                "status": "PASS" if passed else "FAIL",
                "required": required,
                "checks": [item.key for item in selected],
                **({"reason": reason} if reason else {}),
            }
        )

    add("CODE", ("qualified_code_sha", "qualified_code_tree"), required="qualified code")
    add(
        "PAPER_HEALTH",
        ("paper_schema", "paper_integrity", "paper_health"),
        required="healthy PAPER",
    )
    add(
        "PAPER_TECHNICAL_QUALIFICATION",
        ("paper_technical_qualification",),
        required="durable technical qualification",
    )
    add(
        "EXTERNAL_CAPABILITY_PROFILE",
        ("external_capability_profile",),
        required="qualified profile",
    )
    add("SUBMISSION_COMMITMENT", ("submission_commitment",), required="durable submission evidence")
    add("ORDER_EVIDENCE", ("order_evidence",), required="read-only order evidence")
    add("FILL_EVIDENCE", ("fill_evidence",), required="read-only fill evidence")
    add("COORDINATOR", ("coordinator",), required="external settlement coordinator")
    add("STATUS_TIMESTAMP", ("status_timestamp",), required="persisted venue status timestamp")
    add("SETTLEMENT_COMPLETENESS", ("settlement_completeness",), required="qualified completeness")
    add(
        "SETTLEMENT_PERSISTENCE",
        ("settlement_persistence",),
        required="durable settlement persistence",
    )
    add(
        "SETTLEMENT_APPLICATION",
        ("settlement_application",),
        required="atomic settlement application",
    )
    add("EXTERNAL_FINALIZATION", ("external_finalization",), required="atomic finalization")
    add("STARTUP_RECOVERY", ("startup_recovery",), required="settlement-aware startup recovery")
    add("STOP_RECOVERY", ("stop_recovery",), required="protective stop recovery")
    add("SAFE_RETRY", ("external_automatic_retry",), required="disabled")
    add(
        "TESTNET_DB",
        ("testnet_schema", "testnet_integrity", "testnet_recovery_state"),
        required="isolated clean DB",
    )
    add("TESTNET_ENDPOINT", ("testnet_config_isolation",), required="testnet-only endpoint")
    add("MAINNET_LOCK", ("mainnet_endpoint_lock",), required="explicit mainnet exclusion")
    add("BACKUP", ("encrypted_backup",), required="encrypted backup")
    add("SERVICE_DEFINITION", ("testnet_unit_definition",), required="gated testnet service")
    add(
        "SERVICE_STATE",
        ("testnet_service_active", "testnet_service_enabled"),
        required="inactive and disabled",
    )
    add("PAPER_MATURITY", ("paper_maturity",), required="independent PAPER maturity")
    add("TESTNET_SECRET", ("secret_format",), required="validated secret format")
    add("ACTIVATION_MARKER", ("activation_marker",), required="explicit activation approval")
    return summary


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _schema_and_integrity(path: Path) -> tuple[int | None, bool]:
    if not path.exists():
        return None, False
    uri = f"file:{path.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.execute("PRAGMA query_only = ON")
        row = connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()
        schema = int(row[0]) if row is not None else None
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
    return schema, bool(integrity and integrity[0] == "ok")


def _systemd_state(unit: str, action: str) -> str:
    result = subprocess.run(
        ["systemctl", action, unit],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return result.stdout.strip() or result.stderr.strip() or "unknown"


def _secret_format(env_path: Path) -> tuple[bool, str]:
    if not env_path.is_file():
        return False, "missing"
    values: dict[str, str] = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    wallet = values.get("HYPERLIQUID_WALLET_ADDRESS", "")
    private_key = values.get("HYPERLIQUID_PRIVATE_KEY", "")
    telegram_token = values.get("TELEGRAM_BOT_TOKEN", "")
    telegram_chat = values.get("TELEGRAM_CHAT_ID", "")
    valid = bool(
        _WALLET_RE.fullmatch(wallet)
        and _PRIVATE_KEY_RE.fullmatch(private_key)
        and telegram_token
        and telegram_chat
    )
    # Never include a secret value in the result.
    return valid, "present_and_format_valid" if valid else "missing_or_invalid_format"


def _testnet_config_check(config_path: Path, root: Path) -> tuple[bool, str]:
    config = load_config(config_path)
    execution = config["execution"]
    state_file = Path(str(execution["state_file"]))
    state_path = state_file if state_file.is_absolute() else root / state_file
    paper_path = root / "state" / "btcquant.db"
    valid = (
        config["environment"] == "testnet"
        and config["exchange"] == "hyperliquid"
        and execution["mode"] == "testnet"
        and execution["testnet"] is True
        and execution["live_exchange"] == "hyperliquid"
        and execution["api_url"] == "https://api.hyperliquid-testnet.xyz"
        and state_path.resolve() != paper_path.resolve()
        and state_path.resolve().parent == (root / "state").resolve()
    )
    return valid, str(state_path)


def _testnet_order_state(path: Path) -> tuple[bool, str]:
    if not path.exists():
        return True, "database_absent_before_activation"
    store = StateStore(path, initialize=False, read_only=True)
    unresolved = store.unresolved_orders("trend")
    critical = [
        item for item in store.read_incidents(open_only=True) if item.get("severity") == "CRITICAL"
    ]
    return not unresolved and not critical, f"orders={len(unresolved)},critical={len(critical)}"


def _paper_health(path: Path) -> tuple[bool, str]:
    if not path.exists():
        return False, "database_missing"
    try:
        store = StateStore(path, initialize=False, read_only=True)
        unresolved = store.unresolved_orders("trend")
        critical = [
            item
            for item in store.read_incidents(open_only=True)
            if item.get("severity") == "CRITICAL"
        ]
        healthy = store.integrity_check() and not unresolved and not critical
        return healthy, f"unresolved={len(unresolved)},critical={len(critical)}"
    except (FileNotFoundError, sqlite3.Error, ValueError):
        return False, "database_unavailable"


def _technical_qualification(
    path: Path,
    *,
    expected_git_sha: str | None,
    expected_tree: str | None,
) -> tuple[bool, str, str | None]:
    if not path.exists():
        return False, "missing", "PAPER_TECHNICAL_QUALIFICATION_MISSING"
    try:
        payload = StateStore(
            path, initialize=False, read_only=True
        ).latest_paper_technical_qualification()
    except (FileNotFoundError, sqlite3.Error, ValueError):
        return False, "unavailable", "PAPER_TECHNICAL_QUALIFICATION_UNAVAILABLE"
    if payload is None:
        return False, "missing", "PAPER_TECHNICAL_QUALIFICATION_MISSING"
    if payload.get("status") != "PAPER_TECHNICAL_QUALIFIED":
        return False, "invalid_status", "PAPER_TECHNICAL_QUALIFICATION_INVALID"
    if payload.get("schema_version") != SCHEMA_VERSION:
        return False, "schema_mismatch", "PAPER_TECHNICAL_SCHEMA_MISMATCH"
    for key, expected in (("release_sha", expected_git_sha), ("release_tree", expected_tree)):
        value = payload.get(key)
        if not isinstance(value, str) or not _SHA_RE.fullmatch(value):
            return False, f"{key}_invalid", "PAPER_TECHNICAL_RELEASE_INVALID"
        if expected is not None and value != expected:
            return False, f"{key}_mismatch", "PAPER_TECHNICAL_RELEASE_MISMATCH"
    try:
        qualified_at = datetime.fromisoformat(str(payload["qualified_at"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return False, "timestamp_invalid", "PAPER_TECHNICAL_TIMESTAMP_INVALID"
    if qualified_at.tzinfo is None:
        return False, "timestamp_naive", "PAPER_TECHNICAL_TIMESTAMP_INVALID"
    evidence_names = (
        "full_test_results",
        "staging_run",
        "migration",
        "rollback_rehearsal",
        "production_health",
        "backup_verification",
    )
    if any(
        not isinstance(payload.get(name), dict) or payload[name].get("status") != "PASS"
        for name in evidence_names
    ):
        return False, "evidence_incomplete", "PAPER_TECHNICAL_EVIDENCE_INCOMPLETE"
    return True, "PAPER_TECHNICAL_QUALIFIED", None


def _runtime_capability_checks() -> dict[str, tuple[bool, str | None]]:
    """Check passive qualified boundaries without invoking any of them."""

    from .external_evidence import ExternalOrderObservation
    from .external_evidence_reader import CcxtExternalEvidenceReader, ExternalEvidencePersistence
    from .external_fill_evidence_reader import (
        CcxtExternalFillEvidenceReader,
        ExternalFillEvidencePersistence,
    )
    from .external_settlement_acquisition import (
        CcxtExternalSettlementAcquirer,
        ExternalSettlementEvidenceAcquirer,
    )
    from .external_settlement_coordinator import ExternalSettlementCoordinator
    from .external_settlement_finalization import ExternalSettlementFinalizer
    from .external_settlement_recovery import ExternalSettlementStartupRecovery

    checks: dict[str, tuple[bool, str | None]] = {
        "submission_commitment": (
            callable(getattr(StateStore, "append_external_submission_response", None))
            and callable(getattr(StateStore, "read_external_submission_responses", None)),
            "SUBMISSION_COMMITMENT_BOUNDARY_NOT_AVAILABLE",
        ),
        "order_evidence": (
            callable(CcxtExternalEvidenceReader)
            and callable(ExternalEvidencePersistence)
            and callable(getattr(StateStore, "persist_external_order_lookup_evidence", None)),
            "ORDER_EVIDENCE_BOUNDARY_NOT_AVAILABLE",
        ),
        "status_timestamp": (
            "status_event_at" in ExternalOrderObservation.__dataclass_fields__,
            "STATUS_TIMESTAMP_NOT_PERSISTED",
        ),
        "settlement_completeness": (
            callable(CcxtExternalSettlementAcquirer)
            and callable(ExternalSettlementEvidenceAcquirer)
            and callable(getattr(CcxtExternalSettlementAcquirer, "acquire", None)),
            "SETTLEMENT_COMPLETENESS_BOUNDARY_NOT_AVAILABLE",
        ),
        "settlement_persistence": (
            callable(getattr(StateStore, "persist_external_order_settlement", None)),
            "SETTLEMENT_PERSISTENCE_NOT_AVAILABLE",
        ),
        "settlement_application": (
            callable(getattr(StateStore, "apply_external_settlement_atomically", None)),
            "SETTLEMENT_APPLICATION_NOT_AVAILABLE",
        ),
        "external_finalization": (
            callable(ExternalSettlementFinalizer)
            and callable(getattr(StateStore, "finalize_external_order_atomically", None)),
            "EXTERNAL_FINALIZATION_NOT_AVAILABLE",
        ),
        "startup_recovery": (
            callable(ExternalSettlementStartupRecovery)
            and callable(getattr(ExternalSettlementStartupRecovery, "recover", None)),
            "STARTUP_RECOVERY_NOT_AVAILABLE",
        ),
        "fill_evidence": (
            callable(CcxtExternalFillEvidenceReader) and callable(ExternalFillEvidencePersistence),
            "FILL_EVIDENCE_BOUNDARY_NOT_AVAILABLE",
        ),
        "coordinator": (
            callable(ExternalSettlementCoordinator),
            "EXTERNAL_COORDINATOR_NOT_AVAILABLE",
        ),
        "stop_recovery": (False, "STOP_RECOVERY_NOT_INSPECTED"),
    }
    try:
        from .runner import LiveRunner

        checks["stop_recovery"] = (
            callable(getattr(LiveRunner, "_recover_protective_stop_transitions", None)),
            "STOP_RECOVERY_NOT_AVAILABLE",
        )
    except (ImportError, AttributeError):
        pass
    return checks


def _legacy_evaluate_testnet_preflight(
    root: str | Path = "/opt/btcquant",
    *,
    expected_git_sha: str | None = None,
    expected_tree: str | None = None,
    inspect_systemd: bool = False,
) -> dict[str, Any]:
    """Return a read-only, fail-closed testnet activation preflight report."""

    root_path = Path(root).resolve()
    current = root_path / "current"
    checks: list[PreflightCheck] = []
    reasons: list[str] = []

    manifest_path = current / "release-manifest.json"
    try:
        manifest = _read_json(manifest_path)
        git_sha = manifest.get("git_sha")
        tree = manifest.get("git_tree")
        checks.append(
            _check(
                "qualified_code_sha",
                bool(_SHA_RE.fullmatch(str(git_sha or "")))
                and (expected_git_sha is None or git_sha == expected_git_sha),
                git_sha or "missing",
                expected_git_sha or "40 lowercase hexadecimal characters",
                "QUALIFIED_CODE_SHA_MISMATCH"
                if expected_git_sha and git_sha != expected_git_sha
                else None,
            )
        )
        checks.append(
            _check(
                "qualified_code_tree",
                bool(_SHA_RE.fullmatch(str(tree or "")))
                and (expected_tree is None or tree == expected_tree),
                tree or "missing",
                expected_tree or "40 lowercase hexadecimal characters",
                "QUALIFIED_CODE_TREE_MISMATCH" if expected_tree and tree != expected_tree else None,
            )
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        checks.append(
            _check("release_manifest", False, "unreadable", "valid JSON manifest", str(error))
        )

    paper_db = root_path / "state" / "btcquant.db"
    paper_schema, paper_integrity = _schema_and_integrity(paper_db)
    checks.append(
        _check(
            "paper_schema",
            paper_schema == SCHEMA_VERSION,
            paper_schema or "missing",
            str(SCHEMA_VERSION),
        )
    )
    checks.append(
        _check("paper_integrity", paper_integrity, "ok" if paper_integrity else "failed", "ok")
    )
    try:
        paper_store = StateStore(paper_db, initialize=False, read_only=True)
        require_passed_qualification(paper_store)
        qualification_ok = True
        qualification_value = "PASS"
    except (FileNotFoundError, RuntimeError, ValueError, sqlite3.Error) as error:
        qualification_ok = False
        qualification_value = type(error).__name__
    checks.append(
        _check("paper_qualification", qualification_ok, qualification_value, "passed qualification")
    )

    external_gates = {
        "external_fill_identity": EXTERNAL_FILL_IDENTITY_PROVEN,
        "external_reconciliation": EXTERNAL_RECONCILIATION_COORDINATOR_PROVEN,
        "external_financial_application": EXTERNAL_FINANCIAL_APPLICATION_PROVEN,
        "external_finalization": EXTERNAL_FINALIZATION_PROVEN,
        "external_zero_effect": EXTERNAL_ZERO_EFFECT_PROVEN,
    }
    for key, passed in external_gates.items():
        checks.append(
            _check(
                key,
                passed,
                "proven" if passed else "not_proven",
                "proven",
                key.upper() + "_NOT_PROVEN" if not passed else None,
            )
        )
        if not passed:
            reasons.append(key.upper() + "_NOT_PROVEN")
    checks.append(
        _check(
            "external_automatic_retry",
            not EXTERNAL_AUTOMATIC_RETRY_ENABLED,
            "disabled" if not EXTERNAL_AUTOMATIC_RETRY_ENABLED else "enabled",
            "disabled",
        )
    )

    testnet_config = current / "environments" / "testnet" / "config.yaml"
    try:
        config_ok, config_value = _testnet_config_check(testnet_config, root_path)
    except (OSError, KeyError, TypeError, ValueError) as error:
        config_ok, config_value = False, type(error).__name__
    checks.append(
        _check(
            "testnet_config_isolation",
            config_ok,
            config_value,
            "dedicated testnet DB and Hyperliquid testnet",
        )
    )
    checks.append(
        _check(
            "mainnet_endpoint_lock",
            config_ok,
            config_value,
            "explicit Hyperliquid testnet API endpoint",
            "MAINNET_ENDPOINT_LOCK_INVALID" if not config_ok else None,
        )
    )

    testnet_db = root_path / "state" / "btcquant-testnet.db"
    testnet_schema, testnet_integrity = _schema_and_integrity(testnet_db)
    checks.append(
        _check(
            "testnet_schema",
            not testnet_db.exists() or testnet_schema == SCHEMA_VERSION,
            testnet_schema or "absent",
            str(SCHEMA_VERSION),
        )
    )
    checks.append(
        _check(
            "testnet_integrity",
            not testnet_db.exists() or testnet_integrity,
            "ok" if testnet_integrity else "absent_or_failed",
            "ok or absent",
        )
    )
    try:
        order_state_ok, order_state_value = _testnet_order_state(testnet_db)
    except (FileNotFoundError, sqlite3.Error, ValueError) as error:
        order_state_ok, order_state_value = False, type(error).__name__
    checks.append(
        _check(
            "testnet_recovery_state",
            order_state_ok,
            order_state_value,
            "no unresolved orders or critical incidents",
        )
    )

    secret_ok, secret_value = _secret_format(root_path / ".env")
    if not secret_ok:
        reasons.append("TESTNET_SECRET_PROVISIONING_REQUIRED")
    checks.append(
        _check(
            "secret_format",
            secret_ok,
            secret_value,
            "present and valid without printing values",
            "TESTNET_SECRET_PROVISIONING_REQUIRED" if not secret_ok else None,
        )
    )

    unit_path = current / "deploy" / "btcquant-hyperliquid-testnet.service"
    try:
        unit = unit_path.read_text(encoding="utf-8")
        unit_ok = (
            "ConditionPathExists=/opt/btcquant/state/HYPERLIQUID_TESTNET_APPROVED" in unit
            and "Environment=BTCQUANT_ENABLE_TESTNET=I_ACCEPT_TESTNET_ORDERS" in unit
            and "btcquant-trend.service" in unit
            and "mainnet" not in unit.lower()
        )
        unit_value = "static_gate_present" if unit_ok else "static_gate_invalid"
    except OSError:
        unit_ok, unit_value = False, "missing"
    checks.append(
        _check(
            "testnet_unit_definition",
            unit_ok,
            unit_value,
            "explicit approval and testnet-only unit",
        )
    )

    if inspect_systemd:
        active = _systemd_state("btcquant-hyperliquid-testnet.service", "is-active")
        enabled = _systemd_state("btcquant-hyperliquid-testnet.service", "is-enabled")
        checks.append(_check("testnet_service_active", active == "inactive", active, "inactive"))
        checks.append(_check("testnet_service_enabled", enabled == "disabled", enabled, "disabled"))

    backups = sorted((root_path / "backups").glob("*.tar.gz.enc"))
    checks.append(
        _check("encrypted_backup", bool(backups), len(backups), "at least one encrypted backup")
    )

    passed = all(item.passed for item in checks)
    return {
        "status": "PASS" if passed else "FAIL",
        "ready": passed,
        "root": str(root_path),
        "checks": [item.to_dict() for item in checks],
        "gate_summary": _gate_summary(checks, inspect_systemd=inspect_systemd),
        "reason_codes": sorted(set(reasons)),
        "activation_performed": False,
        "authenticated_exchange_call": False,
    }


def evaluate_testnet_preflight(
    root: str | Path = "/opt/btcquant",
    *,
    expected_git_sha: str | None = None,
    expected_tree: str | None = None,
    inspect_systemd: bool = False,
) -> dict[str, Any]:
    """Return architecture-aware, read-only preflight V3.

    Technical readiness describes qualified software and isolated state.  It
    intentionally does not require a private key, a 90-day PAPER campaign, or
    an activation marker.  Activation readiness retains those operational
    gates and must remain false until an explicit later approval.
    """

    root_path = Path(root).resolve()
    current = root_path / "current"
    checks: list[PreflightCheck] = []
    technical_reasons: list[str] = []
    activation_blockers: list[str] = []

    def add_check(
        key: str,
        passed: bool,
        value: object,
        required: str,
        reason: str | None = None,
        *,
        technical: bool = True,
    ) -> None:
        checks.append(_check(key, passed, value, required, reason))
        if not passed and reason:
            (technical_reasons if technical else activation_blockers).append(reason)

    manifest_path = current / "release-manifest.json"
    manifest_sha: str | None = None
    manifest_tree: str | None = None
    try:
        manifest = _read_json(manifest_path)
        manifest_sha = manifest.get("git_sha")
        manifest_tree = manifest.get("git_tree")
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        pass
    add_check(
        "qualified_code_sha",
        bool(isinstance(manifest_sha, str) and _SHA_RE.fullmatch(manifest_sha))
        and (expected_git_sha is None or manifest_sha == expected_git_sha),
        manifest_sha or "missing",
        expected_git_sha or "40 lowercase hexadecimal characters",
        "QUALIFIED_CODE_SHA_MISMATCH",
    )
    add_check(
        "qualified_code_tree",
        bool(isinstance(manifest_tree, str) and _SHA_RE.fullmatch(manifest_tree))
        and (expected_tree is None or manifest_tree == expected_tree),
        manifest_tree or "missing",
        expected_tree or "40 lowercase hexadecimal characters",
        "QUALIFIED_CODE_TREE_MISMATCH",
    )

    paper_db = root_path / "state" / "btcquant.db"
    try:
        paper_schema, paper_integrity = _schema_and_integrity(paper_db)
    except (OSError, sqlite3.Error, TypeError, ValueError):
        paper_schema, paper_integrity = None, False
    add_check(
        "paper_schema",
        paper_schema == SCHEMA_VERSION,
        paper_schema or "missing",
        str(SCHEMA_VERSION),
    )
    add_check("paper_integrity", paper_integrity, "ok" if paper_integrity else "failed", "ok")
    paper_health, paper_health_value = _paper_health(paper_db)
    add_check(
        "paper_health",
        paper_health,
        paper_health_value,
        "no unresolved PAPER order or critical incident",
        "PAPER_HEALTH_NOT_PASSING" if not paper_health else None,
    )
    qualification_ok, qualification_value, qualification_reason = _technical_qualification(
        paper_db,
        expected_git_sha=expected_git_sha or manifest_sha,
        expected_tree=expected_tree or manifest_tree,
    )
    add_check(
        "paper_technical_qualification",
        qualification_ok,
        qualification_value,
        "durable PAPER technical qualification",
        qualification_reason,
    )
    try:
        maturity = paper_maturity_status(StateStore(paper_db, initialize=False, read_only=True))
    except (FileNotFoundError, RuntimeError, ValueError, sqlite3.Error):
        maturity = {
            "kind": "PAPER_MATURITY_STATUS",
            "status": "NOT_STARTED",
            "qualified": False,
            "campaign_id": None,
        }
    maturity_ok = bool(maturity.get("qualified"))
    add_check(
        "paper_maturity",
        maturity_ok,
        maturity.get("status", "NOT_STARTED"),
        "independent PAPER maturity",
        "PAPER_MATURITY_IN_PROGRESS" if not maturity_ok else None,
        technical=False,
    )

    profile = hyperliquid_testnet_trend_ioc_v1()
    profile_ok = profile.technical_contract_passed and profile.protective_stop_qualified
    add_check(
        "external_capability_profile",
        profile_ok,
        profile.name,
        "HYPERLIQUID_TESTNET_TREND_IOC_V1",
        "EXTERNAL_CAPABILITY_PROFILE_INVALID" if not profile_ok else None,
    )
    for key, (passed, reason) in _runtime_capability_checks().items():
        add_check(
            key,
            passed,
            "available" if passed else "unavailable",
            "qualified passive boundary",
            reason,
        )
    add_check(
        "external_automatic_retry",
        not EXTERNAL_AUTOMATIC_RETRY_ENABLED,
        "disabled" if not EXTERNAL_AUTOMATIC_RETRY_ENABLED else "enabled",
        "disabled",
    )

    try:
        config_ok, config_value = _testnet_config_check(
            current / "environments" / "testnet" / "config.yaml",
            root_path,
        )
    except (OSError, KeyError, TypeError, ValueError):
        config_ok, config_value = False, "invalid_or_missing"
    add_check(
        "testnet_config_isolation",
        config_ok,
        config_value,
        "dedicated testnet DB and Hyperliquid testnet",
        "TESTNET_CONFIG_ISOLATION_INVALID" if not config_ok else None,
    )
    add_check(
        "mainnet_endpoint_lock",
        config_ok,
        config_value,
        "explicit Hyperliquid testnet API endpoint",
        "MAINNET_ENDPOINT_LOCK_INVALID" if not config_ok else None,
    )

    testnet_db = root_path / "state" / "btcquant-testnet.db"
    try:
        testnet_schema, testnet_integrity = _schema_and_integrity(testnet_db)
    except (OSError, sqlite3.Error, TypeError, ValueError):
        testnet_schema, testnet_integrity = None, False
    add_check(
        "testnet_schema",
        testnet_db.exists() and testnet_schema == SCHEMA_VERSION,
        testnet_schema or "missing",
        str(SCHEMA_VERSION),
        "TESTNET_SCHEMA_INVALID"
        if not testnet_db.exists() or testnet_schema != SCHEMA_VERSION
        else None,
    )
    add_check(
        "testnet_integrity",
        testnet_db.exists() and testnet_integrity,
        "ok" if testnet_integrity else "missing_or_failed",
        "ok",
        "TESTNET_DATABASE_INVALID" if not testnet_db.exists() or not testnet_integrity else None,
    )
    try:
        order_state_ok, order_state_value = _testnet_order_state(testnet_db)
    except (FileNotFoundError, sqlite3.Error, ValueError):
        order_state_ok, order_state_value = False, "unavailable"
    add_check(
        "testnet_recovery_state",
        order_state_ok,
        order_state_value,
        "no unresolved orders or critical incidents",
        "TESTNET_RECOVERY_STATE_INVALID" if not order_state_ok else None,
    )

    secret_ok, secret_value = _secret_format(root_path / ".env")
    add_check(
        "secret_format",
        secret_ok,
        secret_value,
        "present and valid without printing values",
        "TESTNET_SECRET_PROVISIONING_REQUIRED" if not secret_ok else None,
        technical=False,
    )

    unit_path = current / "deploy" / "btcquant-hyperliquid-testnet.service"
    try:
        unit = unit_path.read_text(encoding="utf-8")
        unit_ok = (
            "ConditionPathExists=/opt/btcquant/state/HYPERLIQUID_TESTNET_APPROVED" in unit
            and "Environment=BTCQUANT_ENABLE_TESTNET=I_ACCEPT_TESTNET_ORDERS" in unit
            and "btcquant-trend.service" in unit
            and "mainnet" not in unit.lower()
        )
        unit_value = "static_gate_present" if unit_ok else "static_gate_invalid"
    except OSError:
        unit_ok, unit_value = False, "missing"
    add_check(
        "testnet_unit_definition",
        unit_ok,
        unit_value,
        "explicit approval and testnet-only unit",
        "TESTNET_UNIT_DEFINITION_INVALID" if not unit_ok else None,
    )

    if inspect_systemd:
        active = _systemd_state("btcquant-hyperliquid-testnet.service", "is-active")
        enabled = _systemd_state("btcquant-hyperliquid-testnet.service", "is-enabled")
    else:
        active, enabled = "not_inspected", "not_inspected"
    add_check(
        "testnet_service_active",
        active == "inactive",
        active,
        "inactive",
        "SERVICE_STATE_NOT_INSPECTED" if not inspect_systemd else "TESTNET_SERVICE_ACTIVE",
    )
    add_check(
        "testnet_service_enabled",
        enabled == "disabled",
        enabled,
        "disabled",
        "SERVICE_STATE_NOT_INSPECTED" if not inspect_systemd else "TESTNET_SERVICE_ENABLED",
    )

    backups = sorted((root_path / "backups").glob("*.tar.gz.enc"))
    add_check(
        "encrypted_backup",
        bool(backups),
        len(backups),
        "at least one encrypted backup",
        "ENCRYPTED_BACKUP_MISSING" if not backups else None,
    )
    marker = root_path / "state" / "HYPERLIQUID_TESTNET_APPROVED"
    add_check(
        "activation_marker",
        marker.is_file(),
        "present" if marker.is_file() else "absent",
        "explicit approval marker",
        "ACTIVATION_MARKER_ABSENT" if not marker.is_file() else None,
        technical=False,
    )

    technical_keys = (
        "qualified_code_sha",
        "qualified_code_tree",
        "paper_schema",
        "paper_integrity",
        "paper_health",
        "paper_technical_qualification",
        "external_capability_profile",
        "submission_commitment",
        "order_evidence",
        "fill_evidence",
        "coordinator",
        "status_timestamp",
        "settlement_completeness",
        "settlement_persistence",
        "settlement_application",
        "external_finalization",
        "startup_recovery",
        "stop_recovery",
        "external_automatic_retry",
        "testnet_config_isolation",
        "mainnet_endpoint_lock",
        "testnet_schema",
        "testnet_integrity",
        "testnet_recovery_state",
        "testnet_unit_definition",
        "testnet_service_active",
        "testnet_service_enabled",
        "encrypted_backup",
    )
    checks_by_key = {item.key: item for item in checks}
    technical_ready = all(checks_by_key[key].passed for key in technical_keys)
    if not maturity_ok:
        activation_blockers.append("PAPER_MATURITY_IN_PROGRESS")
    if not secret_ok:
        activation_blockers.append("TESTNET_SECRET_PROVISIONING_REQUIRED")
    if not marker.is_file():
        activation_blockers.append("ACTIVATION_MARKER_ABSENT")
    activation_ready = technical_ready and not activation_blockers
    historical_gates = {
        "TID_CANDIDATE_NOT_FULLY_PROVEN": not EXTERNAL_FILL_IDENTITY_PROVEN,
        "ZERO_FILL_PROOF_NOT_YET_SUFFICIENTLY_ESTABLISHED": not EXTERNAL_ZERO_EFFECT_PROVEN,
    }
    return {
        "status": "PASS" if activation_ready else "FAIL",
        "ready": activation_ready,
        "technical_ready": technical_ready,
        "activation_ready": activation_ready,
        "root": str(root_path),
        "capability_profile": profile.to_dict(),
        "maturity": maturity,
        "checks": [item.to_dict() for item in checks],
        "gate_summary": _gate_summary(checks, inspect_systemd=inspect_systemd),
        "reason_codes": sorted(set(technical_reasons + activation_blockers)),
        "technical_reason_codes": sorted(set(technical_reasons)),
        "activation_blockers": sorted(set(activation_blockers)),
        "historical_gates": historical_gates,
        "activation_performed": False,
        "authenticated_exchange_call": False,
    }


__all__ = ["PreflightCheck", "evaluate_testnet_preflight", "paper_maturity_status"]
