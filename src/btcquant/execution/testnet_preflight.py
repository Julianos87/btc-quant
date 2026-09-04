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
from pathlib import Path
from typing import Any

from ..config import load_config
from .readiness import require_passed_qualification
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


def evaluate_testnet_preflight(
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
        checks.append(_check(key, passed, "proven" if passed else "not_proven", "proven"))
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
    checks.append(
        _check(
            "secret_format", secret_ok, secret_value, "present and valid without printing values"
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
        "reason_codes": sorted(set(reasons)),
        "activation_performed": False,
        "authenticated_exchange_call": False,
    }


__all__ = ["PreflightCheck", "evaluate_testnet_preflight"]
