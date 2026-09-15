"""Deterministic, side-effect-free alerts derived from an ops status snapshot."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

PASS = "PASS"
WATCH = "WATCH"
FAIL = "FAIL"
UNKNOWN = "UNKNOWN"


def _status(value: object) -> str:
    return str(value) if value in {PASS, WATCH, FAIL, UNKNOWN} else UNKNOWN


def evaluate_alerts(
    report: Mapping[str, Any], *, previous_keys: Iterable[str] = ()
) -> dict[str, Any]:
    """Evaluate conditions without notifying or mutating any state.

    ``previous_keys`` is an optional caller-owned snapshot. Supplying it adds
    deterministic ``new``/``ongoing``/``resolved`` classification without
    creating persistence in the financial database.
    """

    domains = report.get("domains", {})
    if not isinstance(domains, Mapping):
        domains = {}
    previous = set(previous_keys)
    active: list[dict[str, Any]] = []

    def add(code: str, severity: str, subject: str, message: str, action: str) -> None:
        key = f"{code}:{subject}"
        active.append(
            {
                "code": code,
                "severity": severity,
                "dedup_key": key,
                "state": "ongoing" if key in previous else "new",
                "message": message,
                "operator_action": action,
            }
        )

    release = domains.get("release", {})
    qualification = domains.get("qualification", {})
    database = domains.get("database", {})
    health = domains.get("health", {})
    services = domains.get("services", {})
    backup = domains.get("backup", {})
    capacity = domains.get("capacity", {})
    safety = domains.get("testnet_safety", {})
    maturity = domains.get("maturity", {})

    if isinstance(release, Mapping):
        state = _status(release.get("status"))
        if state == FAIL:
            add(
                "RELEASE_IDENTITY_FAILED",
                "CRITICAL",
                "paper",
                "Active release identity failed",
                "Stop promotion and inspect the manifest",
            )
        elif state == UNKNOWN:
            add(
                "RELEASE_IDENTITY_UNKNOWN",
                "CRITICAL",
                "paper",
                "Active release identity is unavailable",
                "Inspect the immutable release and manifest",
            )
        source = release.get("source", {})
        if isinstance(source, Mapping) and source.get("runtime_drift"):
            add(
                "SOURCE_RUNTIME_DRIFT",
                "INFO",
                "paper",
                "Source checkout differs from active release",
                "Use the immutable release identity for operations",
            )

    if isinstance(qualification, Mapping):
        state = _status(qualification.get("status"))
        if state == FAIL:
            add(
                "TECHNICAL_QUALIFICATION_MISMATCH",
                "CRITICAL",
                "paper",
                "Technical qualification does not match active PAPER",
                "Do not promote; requalify the active release",
            )
        elif state == UNKNOWN:
            add(
                "TECHNICAL_QUALIFICATION_UNKNOWN",
                "WARNING",
                "paper",
                "Technical qualification evidence is unavailable",
                "Inspect qualification evidence before promotion",
            )

    if isinstance(maturity, Mapping):
        binding = maturity.get("binding_status")
        if binding in {"FAIL", "MISMATCH"}:
            add(
                "MATURITY_BINDING_FAILED",
                "CRITICAL",
                "paper",
                "Maturity binding does not match the active release",
                "Freeze promotion and follow the maturity runbook",
            )
        elif binding == UNKNOWN:
            add(
                "MATURITY_BINDING_UNKNOWN",
                "WARNING",
                "paper",
                "Maturity binding could not be established",
                "Inspect canonical configuration and release identity",
            )

    if isinstance(database, Mapping):
        state = _status(database.get("status"))
        if state == FAIL:
            add(
                "DB_SAFETY_FAILED",
                "CRITICAL",
                "paper",
                "PAPER database safety gate failed",
                "Stop writers or follow the approved incident procedure",
            )
        elif state == UNKNOWN:
            add(
                "DB_SAFETY_UNKNOWN",
                "CRITICAL",
                "paper",
                "PAPER database safety evidence is unavailable",
                "Do not assume the database is safe",
            )
        for field, code, label in (
            ("unresolved_orders", "UNRESOLVED_ORDERS", "unresolved orders"),
            ("reconciliation_required", "RECONCILIATION_REQUIRED", "reconciliation required"),
            ("open_critical_incidents", "OPEN_CRITICAL_INCIDENT", "open critical incidents"),
        ):
            try:
                count = int(database.get(field, 0))
            except (TypeError, ValueError):
                count = -1
            if count != 0:
                add(
                    code,
                    "CRITICAL",
                    "paper",
                    f"PAPER has {label}",
                    "Follow the corresponding operator runbook",
                )

    if isinstance(health, Mapping):
        endpoint_keys = {"healthz", "readyz"}
        if endpoint_keys.intersection(health):
            for endpoint, code in (("healthz", "HEALTH_FAILED"), ("readyz", "READINESS_FAILED")):
                item = health.get(endpoint, {})
                state = _status(item.get("status")) if isinstance(item, Mapping) else UNKNOWN
                if state == FAIL:
                    add(
                        code,
                        "CRITICAL",
                        endpoint,
                        f"{endpoint} is failing",
                        "Inspect the PAPER service and dependency health",
                    )
                elif state == UNKNOWN:
                    add(
                        f"{code}_UNKNOWN",
                        "CRITICAL",
                        endpoint,
                        f"{endpoint} is unavailable",
                        "Do not treat an unavailable probe as PASS",
                    )
        else:
            state = _status(health.get("status"))
            if state == FAIL:
                add(
                    "HEALTH_DOMAIN_FAILED",
                    "CRITICAL",
                    "paper",
                    "PAPER health evidence failed",
                    "Inspect the PAPER service and dependency health",
                )
            elif state == UNKNOWN:
                add(
                    "HEALTH_DOMAIN_UNKNOWN",
                    "CRITICAL",
                    "paper",
                    "PAPER health evidence is unavailable",
                    "Do not treat unavailable health evidence as PASS",
                )

    if isinstance(services, Mapping):
        components = services.get("components", {})
        if isinstance(components, Mapping):
            for name, component in components.items():
                state = (
                    _status(component.get("status")) if isinstance(component, Mapping) else UNKNOWN
                )
                if state == FAIL:
                    add(
                        "SERVICE_INACTIVE",
                        "CRITICAL",
                        str(name),
                        f"Required PAPER service {name} is inactive",
                        "Inspect the unit and crash/restart history",
                    )
                elif state == UNKNOWN:
                    add(
                        "SERVICE_STATE_UNKNOWN",
                        "WARNING",
                        str(name),
                        f"Required PAPER service {name} state is unknown",
                        "Inspect systemd status",
                    )

    if isinstance(backup, Mapping):
        state = _status(backup.get("status"))
        if state == FAIL:
            add(
                "BACKUP_VERIFICATION_FAILED",
                "CRITICAL",
                "paper",
                "Latest PAPER backup is not verified",
                "Do not rely on the backup until it is verified",
            )
        elif state in {WATCH, UNKNOWN}:
            add(
                "BACKUP_FRESHNESS_WARNING",
                "WARNING",
                "paper",
                "PAPER backup freshness or verification needs attention",
                "Verify a fresh encrypted backup",
            )

    if isinstance(capacity, Mapping):
        state = _status(capacity.get("status"))
        if state == FAIL:
            add(
                "DISK_CAPACITY_FAILED",
                "CRITICAL",
                "root",
                "Filesystem capacity is below the deployment hard gate",
                "Stop growth and follow the retention plan",
            )
        elif state == WATCH:
            add(
                "DISK_CAPACITY_WARNING",
                "WARNING",
                "root",
                "Filesystem capacity is in the advisory band",
                "Review the dry-run retention plan",
            )

    if isinstance(safety, Mapping):
        state = _status(safety.get("status"))
        if state == FAIL:
            add(
                "TESTNET_SAFETY_FAILED",
                "CRITICAL",
                "testnet",
                "TESTNET safety boundary is not satisfied",
                "Do not activate TESTNET; investigate markers and service state",
            )
        elif state == UNKNOWN:
            add(
                "TESTNET_SAFETY_UNKNOWN",
                "CRITICAL",
                "testnet",
                "TESTNET safety evidence is unavailable",
                "Do not assume TESTNET is inactive",
            )

    current_keys = {str(item["dedup_key"]) for item in active}
    resolved = [
        {
            "dedup_key": key,
            "severity": "INFO",
            "state": "resolved",
            "message": "Previously observed operational condition is no longer active",
        }
        for key in sorted(previous - current_keys)
    ]
    severities = {item["severity"] for item in active}
    unknown_evidence = any(str(item["code"]).endswith("_UNKNOWN") for item in active)
    alert_status = (
        UNKNOWN
        if unknown_evidence
        else FAIL
        if "CRITICAL" in severities
        else WATCH
        if "WARNING" in severities
        else PASS
    )
    return {
        "status": alert_status,
        "read_only": True,
        "deduplication": "deterministic_key",
        "active": active,
        "resolved": resolved,
        "real_notifications_sent": 0,
    }
