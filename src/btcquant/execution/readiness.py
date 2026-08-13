"""Protocole versionné de qualification paper avant tout accès testnet.

Les seuils sont figés dans le code puis copiés dans SQLite au démarrage d'une
campagne. Une modification ultérieure ne réécrit donc jamais les règles d'une
observation déjà commencée.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from .quality_metrics import percentile, slippages_bps
from .state_store import StateStore

PROTOCOL_VERSION = 2
TERMINAL_STATUSES = {"FILLED", "PARTIAL", "REJECTED", "FAILED", "CANCELED"}
UNRESOLVED_STATUSES = {"PENDING", "OPEN", "UNBALANCED"}


@dataclass(frozen=True)
class ReadinessPolicy:
    required_engines: tuple[str, ...] = ("trend",)
    min_observation_days: int = 90
    min_engine_uptime: float = 0.995
    min_daily_sample_coverage: float = 0.95
    min_equity_coverage: float = 0.95
    min_closed_trades: int = 30
    min_terminal_orders: int = 50
    min_terminal_orders_per_engine: int = 5
    max_rejection_rate: float = 0.05
    max_partial_rate: float = 0.10
    max_p95_slippage_bps: float = 20.0
    max_drawdown: float = -0.45
    max_trend_state_age_seconds: float = 10 * 60
    max_carry_state_age_seconds: float = 20 * 60
    qualification_valid_days: int = 7

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ServiceComponentProfile:
    """Operational components required by the configured service profile."""

    required: tuple[str, ...] = ("trend",)
    optional: tuple[str, ...] = ("carry", "shadow")
    reason_codes: tuple[str, ...] = ()


def service_component_profile() -> ServiceComponentProfile:
    """Resolve one readiness profile for dashboard and operational probes.

    The default follows the documented standard campaign: trend is required;
    carry and shadow are observable but optional until an explicit profile
    enables them.  A deployment may opt in with a comma-separated environment
    variable, which is configuration rather than a dashboard-local threshold.
    """

    configured = os.environ.get("BTCQUANT_REQUIRED_ENGINES")
    if not configured:
        return ServiceComponentProfile()
    names = tuple(
        dict.fromkeys(item.strip().lower() for item in configured.split(",") if item.strip())
    )
    allowed = {"trend", "carry", "shadow"}
    if not names or any(item not in allowed for item in names):
        return ServiceComponentProfile(reason_codes=("INVALID_REQUIRED_ENGINE_PROFILE",))
    optional = tuple(item for item in ("trend", "carry", "shadow") if item not in names)
    return ServiceComponentProfile(required=names, optional=optional)


SERVICE_ENGINE_MAX_AGE_SECONDS = {"trend": 600.0, "carry": 1200.0}
SERVICE_SHADOW_MAX_AGE_SECONDS = 300.0


def evaluate_service_readiness(
    database: str,
    shadow_database: str | None = None,
    *,
    now: datetime | None = None,
    profile: ServiceComponentProfile | None = None,
) -> dict[str, Any]:
    """Read-only service readiness, distinct from campaign qualification."""

    from pathlib import Path

    from ..observability import Freshness
    from .health import execution_health
    from .shadow import ShadowStore

    current = now or datetime.now(UTC)
    cfg = profile or service_component_profile()
    database_path = Path(database)
    shadow_path = Path(shadow_database) if shadow_database is not None else None
    components: list[dict[str, Any]] = []
    checks: dict[str, bool] = {}
    details: dict[str, Any] = {}
    reasons = list(cfg.reason_codes)

    if not database_path.exists():
        checks = {"database": False, "shadow_database": bool(shadow_path and shadow_path.exists())}
        reasons.append("DATABASE_UNAVAILABLE")
        return {
            "api_schema_version": 2,
            "kind": "SERVICE_READINESS",
            "status": "not_ready",
            "ready": False,
            "generated_at": current.isoformat(),
            "required_components": list(cfg.required),
            "optional_components": list(cfg.optional),
            "checks": checks,
            "details": details,
            "components": components,
            "reason_codes": reasons,
            "campaign_qualification": "SEPARATE",
        }

    try:
        store = StateStore(database_path, initialize=False, read_only=True)
        checks["database"] = bool(store.integrity_check())
        if not checks["database"]:
            reasons.append("DATABASE_CORRUPT")
        incidents = store.read_incidents(open_only=True)
        critical_count = sum(item["severity"] == "CRITICAL" for item in incidents)
        details["open_incidents"] = len(incidents)
        details["open_critical_incidents"] = critical_count
        checks["no_critical_incident"] = critical_count == 0
        if critical_count:
            reasons.append("CRITICAL_INCIDENT_OPEN")
        safety: dict[str, Any] = {}
        safety_pass = True
        try:
            for engine in ("trend", "carry"):
                health = execution_health(store, engine)
                safety[engine] = {
                    "status": "PASS"
                    if not (
                        health.unresolved_order_ids
                        or health.unbalanced_order_ids
                        or health.unprotected_slots
                        or health.stop_transition_slots
                        or health.reconciliation_required
                    )
                    else "FAIL",
                    "unresolved_orders": len(health.unresolved_order_ids),
                    "unbalanced_orders": len(health.unbalanced_order_ids),
                    "unprotected_slots": len(health.unprotected_slots),
                    "stop_transition_slots": len(health.stop_transition_slots),
                    "reconciliation_required": health.reconciliation_required,
                    "orders_analyzed": health.orders_analyzed,
                }
                if engine in cfg.required and safety[engine]["status"] != "PASS":
                    safety_pass = False
        except Exception:
            safety = {"status": "UNKNOWN"}
            safety_pass = False
            reasons.append("EXECUTION_SAFETY_UNKNOWN")
        details["execution_safety"] = safety
        checks["execution_safety"] = safety_pass
        for engine in ("trend", "carry"):
            age = store.engine_age_seconds(engine, now=current)
            observed_at = store.engine_updated_at(engine)
            limit = SERVICE_ENGINE_MAX_AGE_SECONDS[engine]
            status = (
                Freshness.UNAVAILABLE.value
                if age is None
                else Freshness.FRESH.value
                if age <= limit
                else Freshness.STALE.value
            )
            item = {
                "name": engine,
                "required": engine in cfg.required,
                "status": status,
                "age_seconds": age,
                "observed_at": observed_at.isoformat() if observed_at else None,
                "max_age_seconds": limit,
            }
            components.append(item)
            details[f"{engine}_age_seconds"] = age
            if engine in cfg.required:
                checks[f"{engine}_fresh"] = status == Freshness.FRESH.value
                if status != Freshness.FRESH.value:
                    reasons.append(f"REQUIRED_{engine.upper()}_{status}")
    except Exception:
        checks["database"] = False
        reasons.append("DATABASE_UNAVAILABLE")
        return {
            "api_schema_version": 2,
            "kind": "SERVICE_READINESS",
            "status": "not_ready",
            "ready": False,
            "generated_at": current.isoformat(),
            "required_components": list(cfg.required),
            "optional_components": list(cfg.optional),
            "checks": checks,
            "details": details,
            "components": components,
            "reason_codes": sorted(set(reasons)),
            "campaign_qualification": "SEPARATE",
        }

    shadow_exists = bool(shadow_path and shadow_path.exists())
    checks["shadow_database"] = shadow_exists
    shadow_item: dict[str, Any] = {
        "name": "shadow",
        "required": "shadow" in cfg.required,
        "status": Freshness.UNAVAILABLE.value,
        "age_seconds": None,
        "max_age_seconds": SERVICE_SHADOW_MAX_AGE_SECONDS,
    }
    if shadow_exists and shadow_path is not None:
        try:
            runtime = ShadowStore(shadow_path, read_only=True).runtime_health(now=current)
            age = runtime["last_success_age_seconds"]
            status = (
                Freshness.UNAVAILABLE.value
                if age is None
                else Freshness.FRESH.value
                if age <= SERVICE_SHADOW_MAX_AGE_SECONDS
                else Freshness.STALE.value
            )
            shadow_item.update(
                status=status,
                age_seconds=age,
                observed_at=runtime.get("last_success_at"),
                consecutive_failures=runtime.get("consecutive_failures"),
            )
            details["shadow_age_seconds"] = age
        except Exception:
            reasons.append("SHADOW_SOURCE_UNAVAILABLE")
    components.append(shadow_item)
    if shadow_item["required"] and shadow_item["status"] != Freshness.FRESH.value:
        reasons.append(f"REQUIRED_SHADOW_{shadow_item['status']}")

    required_checks = ["database", "no_critical_incident", "execution_safety"]
    required_checks.extend(f"{engine}_fresh" for engine in cfg.required if engine != "shadow")
    if "shadow" in cfg.required:
        required_checks.append("shadow_fresh")
    ready = (
        bool(required_checks)
        and all(checks.get(key, False) for key in required_checks)
        and not cfg.reason_codes
        and not any(
            reason.startswith("REQUIRED_")
            or reason in {"DATABASE_CORRUPT", "DATABASE_UNAVAILABLE", "CRITICAL_INCIDENT_OPEN"}
            for reason in reasons
        )
    )
    return {
        "api_schema_version": 2,
        "kind": "SERVICE_READINESS",
        "status": "ready" if ready else "not_ready",
        "ready": ready,
        "generated_at": current.isoformat(),
        "required_components": list(cfg.required),
        "optional_components": list(cfg.optional),
        "checks": checks,
        "details": details,
        "components": components,
        "reason_codes": sorted(set(reasons)),
        "campaign_qualification": "SEPARATE",
    }


def testnet_p1_policy() -> ReadinessPolicy:
    """Politique d'observation opérationnelle après le portail paper.

    Le smoke test journalise deux ordres terminaux. La campagne reste surtout
    bloquée par 30 jours de disponibilité, l'absence d'incident et les SLO ;
    elle ne dépend pas de l'apparition aléatoire de 30 signaux trend.
    """

    return ReadinessPolicy(
        min_observation_days=30,
        min_closed_trades=0,
        min_terminal_orders=2,
        min_terminal_orders_per_engine=2,
        qualification_valid_days=30,
    )


@dataclass(frozen=True)
class ReadinessCheck:
    key: str
    label: str
    passed: bool
    value: str
    target: str
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "status": "ok" if self.passed else "warn",
        }


def start_campaign(
    store: StateStore,
    policy: ReadinessPolicy | None = None,
    *,
    started_at: str | None = None,
) -> dict[str, Any]:
    cfg = policy or ReadinessPolicy()
    return store.start_qualification_campaign(
        protocol_version=PROTOCOL_VERSION,
        policy=cfg.to_dict(),
        started_at=started_at,
    )


def _inactive_campaign_report(
    store: StateStore,
    current: datetime,
    *,
    persist: bool,
) -> dict[str, Any]:
    passed_campaign = store.latest_passed_qualification()
    if passed_campaign is not None and isinstance(passed_campaign.get("final_report"), dict):
        final_report = dict(passed_campaign["final_report"])
        final_report["campaign_status"] = "PASSED"
        policy = ReadinessPolicy(**passed_campaign["policy"])
        ended = _parse_datetime(passed_campaign["ended_at"])
        age_days = max(0.0, (current - ended).total_seconds() / 86400)
        checks = list(final_report["checks"])
        if int(passed_campaign["protocol_version"]) != PROTOCOL_VERSION:
            checks.append(
                ReadinessCheck(
                    "protocol_version",
                    "Version du protocole",
                    False,
                    f"v{passed_campaign['protocol_version']}",
                    f"v{PROTOCOL_VERSION}",
                    "Une nouvelle campagne complète est requise.",
                ).to_dict()
            )
        if age_days > policy.qualification_valid_days:
            checks.append(
                ReadinessCheck(
                    "qualification_age",
                    "Validité de la qualification",
                    False,
                    f"{age_days:.1f} j",
                    f"≤ {policy.qualification_valid_days} j",
                ).to_dict()
            )
        if len(checks) != len(final_report["checks"]):
            final_report.update(
                status="FAIL",
                ready=False,
                n_total=len(checks),
                n_ok=sum(bool(item["passed"]) for item in checks),
                checks=checks,
            )
        return final_report
    report = _report(
        current,
        None,
        [
            ReadinessCheck(
                "campaign",
                "Campagne de qualification",
                False,
                "non démarrée",
                "RUNNING",
                "Lancer scripts/readiness.py start.",
            )
        ],
        ReadinessPolicy(),
    )
    if persist:
        store.save_readiness_report(report, campaign_id=None)
    return report


def _build_active_checks(
    store: StateStore,
    current: datetime,
    campaign: dict[str, Any],
    policy: ReadinessPolicy,
    required_engines: tuple[str, ...],
    *,
    integrity_ok: bool,
    elapsed_days: float,
    coverage: float,
    coverage_by_engine: dict[str, float],
    trades: list[dict[str, Any]],
    terminal: list[dict[str, Any]],
    unresolved: list[dict[str, Any]],
    incidents: list[dict[str, Any]],
    rejection_rate: float | None,
    partial_rate: float | None,
    p95_slippage: float | None,
    max_drawdown: float | None,
    halted: bool,
) -> list[ReadinessCheck]:
    return [
        ReadinessCheck(
            "campaign",
            "Campagne de qualification",
            int(campaign["protocol_version"]) == PROTOCOL_VERSION,
            f"#{campaign['id']} / v{campaign['protocol_version']}",
            f"RUNNING / v{PROTOCOL_VERSION}",
            (
                ""
                if int(campaign["protocol_version"]) == PROTOCOL_VERSION
                else "Annuler cette campagne obsolète et en démarrer une nouvelle."
            ),
        ),
        ReadinessCheck(
            "integrity",
            "Intégrité SQLite",
            integrity_ok,
            "ok" if integrity_ok else "échec",
            "ok",
        ),
        ReadinessCheck(
            "days",
            "Durée observée",
            elapsed_days >= policy.min_observation_days,
            f"{int(elapsed_days)} j",
            f"≥ {policy.min_observation_days} j",
        ),
        ReadinessCheck(
            "uptime",
            "Jours avec couverture equity suffisante",
            coverage >= policy.min_equity_coverage,
            f"{coverage:.1%}",
            f"≥ {policy.min_equity_coverage:.0%}",
        ),
        *[
            ReadinessCheck(
                f"{engine}_uptime",
                f"Disponibilité temporelle {engine}",
                coverage_by_engine[engine] >= policy.min_engine_uptime,
                f"{coverage_by_engine[engine]:.3%}",
                f"≥ {policy.min_engine_uptime:.1%}",
            )
            for engine in required_engines
        ],
        ReadinessCheck(
            "trades",
            "Trades clôturés",
            len(trades) >= policy.min_closed_trades,
            str(len(trades)),
            f"≥ {policy.min_closed_trades}",
        ),
        ReadinessCheck(
            "orders",
            "Ordres terminaux analysés",
            len(terminal) >= policy.min_terminal_orders,
            str(len(terminal)),
            f"≥ {policy.min_terminal_orders}",
        ),
        *[
            ReadinessCheck(
                f"orders_{engine}",
                f"Ordres terminaux {engine}",
                sum(item["engine"] == engine for item in terminal)
                >= policy.min_terminal_orders_per_engine,
                str(sum(item["engine"] == engine for item in terminal)),
                f"≥ {policy.min_terminal_orders_per_engine}",
            )
            for engine in required_engines
        ],
        ReadinessCheck(
            "unresolved",
            "Ordres non résolus",
            not unresolved,
            str(len(unresolved)),
            "0",
        ),
        ReadinessCheck(
            "incidents",
            "Incidents ouverts",
            not incidents,
            str(len(incidents)),
            "0",
        ),
        _rate_check(
            "rejections",
            "Taux de rejet",
            rejection_rate,
            policy.max_rejection_rate,
        ),
        _rate_check(
            "partials",
            "Taux de fills partiels",
            partial_rate,
            policy.max_partial_rate,
        ),
        ReadinessCheck(
            "slippage",
            "Slippage p95",
            p95_slippage is not None and p95_slippage <= policy.max_p95_slippage_bps,
            "—" if p95_slippage is None else f"{p95_slippage:.1f} bps",
            f"≤ {policy.max_p95_slippage_bps:.1f} bps",
        ),
        ReadinessCheck(
            "drawdown",
            "Drawdown maximal paper",
            max_drawdown is not None and max_drawdown >= policy.max_drawdown,
            "—" if max_drawdown is None else f"{max_drawdown:.1%}",
            f"≥ {policy.max_drawdown:.0%}",
        ),
        *[
            _freshness_check(
                f"{engine}_freshness",
                f"Fraîcheur moteur {engine}",
                store.engine_age_seconds(engine, now=current),
                _freshness_limit(policy, engine),
            )
            for engine in required_engines
        ],
        ReadinessCheck(
            "killswitch",
            "Kill-switch inactif",
            not halted,
            "déclenché" if halted else "non",
            "non",
        ),
    ]


def evaluate_readiness(
    store: StateStore,
    *,
    now: datetime | None = None,
    persist: bool = False,
) -> dict[str, Any]:
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    campaign = store.active_qualification_campaign()
    if campaign is None:
        return _inactive_campaign_report(store, current, persist=persist)

    policy = ReadinessPolicy(**campaign["policy"])
    started = _parse_datetime(campaign["started_at"])
    required_engines = tuple(policy.required_engines)
    if not required_engines:
        raise ValueError("La qualification doit exiger au moins un moteur")
    orders = [
        item for item in store.read_orders() if _parse_datetime(item["created_at"]) >= started
    ]
    scoped_orders = [item for item in orders if item["engine"] in required_engines]
    terminal = [
        item
        for item in scoped_orders
        if item["order_type"] != "STOP" and item["status"] in TERMINAL_STATUSES
    ]
    unresolved = [
        item
        for item in scoped_orders
        if item["status"] in ("PENDING", "UNBALANCED")
        or (item["status"] == "OPEN" and item["order_type"] != "STOP")
    ]
    trades = [item for item in store.read_trades() if _parse_datetime(item["exit_ts"]) >= started]
    incidents = [
        item
        for item in store.read_incidents(open_only=True)
        if item.get("engine") is None or item.get("engine") in required_engines
    ]

    elapsed_days = max(0.0, (current - started).total_seconds() / 86400)
    equity_timestamps = {
        engine: _equity_timestamps(
            store,
            engine,
            started,
            current,
            _freshness_limit(policy, engine),
        )
        for engine in required_engines
    }
    coverage_by_engine = {
        engine: _availability_from_timestamps(
            equity_timestamps[engine],
            started,
            current,
            _freshness_limit(policy, engine),
        )
        for engine in required_engines
    }
    covered_days = _covered_equity_days(
        started,
        current,
        required_engines,
        policy,
        equity_timestamps,
    )
    expected_days = max(1, (current.date() - started.date()).days + 1)
    coverage = len(covered_days) / expected_days

    rejection_count = sum(item["status"] in ("REJECTED", "FAILED") for item in terminal)
    partial_count = sum(item["status"] == "PARTIAL" for item in terminal)
    rejection_rate = rejection_count / len(terminal) if terminal else None
    partial_rate = partial_count / len(terminal) if terminal else None
    p95_slippage = percentile(slippages_bps(terminal), 0.95)
    max_drawdown = _max_portfolio_drawdown(
        store,
        started,
        current,
        required_engines,
        policy,
    )

    halted = any(
        bool((store.load_engine_state(engine) or {}).get("halted")) for engine in required_engines
    )

    integrity_ok = store.integrity_check()
    checks = _build_active_checks(
        store,
        current,
        campaign,
        policy,
        required_engines,
        integrity_ok=integrity_ok,
        elapsed_days=elapsed_days,
        coverage=coverage,
        coverage_by_engine=coverage_by_engine,
        trades=trades,
        terminal=terminal,
        unresolved=unresolved,
        incidents=incidents,
        rejection_rate=rejection_rate,
        partial_rate=partial_rate,
        p95_slippage=p95_slippage,
        max_drawdown=max_drawdown,
        halted=halted,
    )
    report = _report(current, campaign, checks, policy)
    if persist:
        store.save_readiness_report(report, campaign_id=int(campaign["id"]))
    return report


def finalize_campaign(store: StateStore, *, now: datetime | None = None) -> dict[str, Any]:
    campaign = store.active_qualification_campaign()
    if campaign is None:
        raise RuntimeError("Aucune campagne de qualification active")
    report = evaluate_readiness(store, now=now, persist=True)
    if report["status"] != "PASS":
        raise RuntimeError("Qualification refusée : certains critères sont en échec")
    store.finish_qualification_campaign(
        int(campaign["id"]),
        status="PASSED",
        final_report=report,
        ended_at=report["generated_at"],
    )
    return report


def require_passed_qualification(store: StateStore) -> dict[str, Any]:
    if store.active_qualification_campaign() is not None:
        raise RuntimeError("SÉCURITÉ : une nouvelle campagne est en cours ; testnet interdit")
    campaign = store.latest_passed_qualification()
    if campaign is None:
        raise RuntimeError("SÉCURITÉ : aucune campagne paper finalisée PASS ; testnet interdit")
    if int(campaign["protocol_version"]) != PROTOCOL_VERSION:
        raise RuntimeError("SÉCURITÉ : qualification réalisée avec un protocole obsolète")
    report = campaign.get("final_report")
    if not isinstance(report, dict) or report.get("status") != "PASS":
        raise RuntimeError("SÉCURITÉ : preuve de qualification PASS absente")
    policy = ReadinessPolicy(**campaign["policy"])
    ended_at = _parse_datetime(campaign["ended_at"])
    age_days = (datetime.now(UTC) - ended_at).total_seconds() / 86400
    if age_days > policy.qualification_valid_days:
        raise RuntimeError(
            "SÉCURITÉ : la qualification testnet a expiré ; nouvelle campagne requise"
        )
    return report


def _report(
    now: datetime,
    campaign: dict[str, Any] | None,
    checks: list[ReadinessCheck],
    policy: ReadinessPolicy,
) -> dict[str, Any]:
    payload = [item.to_dict() for item in checks]
    passed = all(item.passed for item in checks)
    return {
        "protocol_version": PROTOCOL_VERSION,
        "campaign_id": int(campaign["id"]) if campaign else None,
        "campaign_status": campaign["status"] if campaign else "NOT_STARTED",
        "generated_at": now.isoformat(),
        "status": "PASS" if passed else "FAIL",
        "ready": passed,
        "n_ok": sum(item.passed for item in checks),
        "n_total": len(checks),
        "checks": payload,
        "thresholds": policy.to_dict(),
    }


def _parse_datetime(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _freshness_limit(policy: ReadinessPolicy, engine: str) -> float:
    if engine == "trend":
        return policy.max_trend_state_age_seconds
    if engine == "carry":
        return policy.max_carry_state_age_seconds
    raise ValueError(f"Moteur non pris en charge par la readiness : {engine}")


def _equity_timestamps(
    store: StateStore,
    engine: str,
    start: datetime,
    end: datetime,
    freshness_seconds: float,
) -> list[datetime]:
    earliest = start - timedelta(seconds=freshness_seconds)
    return sorted(
        timestamp
        for row in store.read_equity(engine)
        if earliest <= (timestamp := _parse_datetime(row["ts"])) <= end
    )


def _availability_from_timestamps(
    timestamps: list[datetime],
    start: datetime,
    end: datetime,
    freshness_seconds: float,
) -> float:
    total = (end - start).total_seconds()
    if total <= 0:
        return 0.0
    intervals: list[tuple[datetime, datetime]] = []
    freshness = timedelta(seconds=freshness_seconds)
    for timestamp in timestamps:
        left = max(start, timestamp)
        right = min(end, timestamp + freshness)
        if right > left:
            intervals.append((left, right))
    if not intervals:
        return 0.0
    covered = 0.0
    left, right = intervals[0]
    for next_left, next_right in intervals[1:]:
        if next_left <= right:
            right = max(right, next_right)
        else:
            covered += (right - left).total_seconds()
            left, right = next_left, next_right
    covered += (right - left).total_seconds()
    return min(1.0, covered / total)


def _covered_equity_days(
    start: datetime,
    end: datetime,
    engines: tuple[str, ...],
    policy: ReadinessPolicy,
    timestamps: dict[str, list[datetime]],
) -> set[date]:
    covered: set[date] = set()
    day = start.date()
    while day <= end.date():
        day_start = max(start, datetime.combine(day, time.min, tzinfo=UTC))
        day_end = min(end, datetime.combine(day + timedelta(days=1), time.min, tzinfo=UTC))
        if day_end > day_start and all(
            _availability_from_timestamps(
                timestamps[engine],
                day_start,
                day_end,
                _freshness_limit(policy, engine),
            )
            >= policy.min_daily_sample_coverage
            for engine in engines
        ):
            covered.add(day)
        day += timedelta(days=1)
    return covered


def _max_portfolio_drawdown(
    store: StateStore,
    start: datetime,
    end: datetime,
    engines: tuple[str, ...],
    policy: ReadinessPolicy,
) -> float | None:
    samples: dict[str, list[tuple[datetime, float]]] = {}
    for engine in engines:
        values: list[tuple[datetime, float]] = []
        for row in store.read_equity(engine):
            timestamp = _parse_datetime(row["ts"])
            if start <= timestamp <= end:
                values.append((timestamp, float(row["equity"])))
        samples[engine] = sorted(values)
    timeline = sorted({timestamp for values in samples.values() for timestamp, _ in values})
    flows = sorted(
        (
            _parse_datetime(flow["ts"]),
            sum(float(flow[f"{engine}_flow"]) for engine in engines),
        )
        for flow in store.read_flows()
        if start <= _parse_datetime(flow["ts"]) <= end
    )
    equities: list[float] = []
    sample_indexes = {engine: 0 for engine in engines}
    latest: dict[str, tuple[datetime, float]] = {}
    flow_index = 0
    cumulative_flow = 0.0
    for timestamp in timeline:
        for engine in engines:
            values = samples[engine]
            while (
                sample_indexes[engine] < len(values)
                and values[sample_indexes[engine]][0] <= timestamp
            ):
                latest[engine] = values[sample_indexes[engine]]
                sample_indexes[engine] += 1
        while flow_index < len(flows) and flows[flow_index][0] <= timestamp:
            cumulative_flow += flows[flow_index][1]
            flow_index += 1
        if all(
            engine in latest
            and (timestamp - latest[engine][0]).total_seconds() <= _freshness_limit(policy, engine)
            for engine in engines
        ):
            equities.append(sum(latest[engine][1] for engine in engines) - cumulative_flow)
    if len(equities) < 2:
        return None
    peak = equities[0]
    worst = 0.0
    for equity in equities:
        peak = max(peak, equity)
        worst = min(worst, equity / peak - 1.0)
    return worst


def _rate_check(
    key: str,
    label: str,
    value: float | None,
    maximum: float,
) -> ReadinessCheck:
    return ReadinessCheck(
        key,
        label,
        value is not None and value <= maximum,
        "—" if value is None else f"{value:.1%}",
        f"≤ {maximum:.0%}",
    )


def _freshness_check(
    key: str,
    label: str,
    age: float | None,
    maximum: float,
) -> ReadinessCheck:
    return ReadinessCheck(
        key,
        label,
        age is not None and age <= maximum,
        "—" if age is None else f"{age / 3600:.1f} h",
        f"≤ {maximum / 3600:.0f} h",
    )
