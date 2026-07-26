"""Protocole versionné de qualification paper avant tout accès testnet.

Les seuils sont figés dans le code puis copiés dans SQLite au démarrage d'une
campagne. Une modification ultérieure ne réécrit donc jamais les règles d'une
observation déjà commencée.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

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
    slippages = _slippages(terminal)
    p95_slippage = _percentile(slippages, 0.95)
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
    checks = [
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


def _slippages(orders: list[dict[str, Any]]) -> list[float]:
    values = []
    for order in orders:
        reference = order.get("reference_price")
        price = order.get("price")
        if reference is None or price is None or float(reference) <= 0:
            continue
        if float(order["filled_qty"]) <= 0:
            continue
        ratio = float(price) / float(reference)
        values.append((ratio - 1.0 if order["side"].upper() == "BUY" else 1.0 - ratio) * 10_000)
    return values


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * fraction + 0.5)))
    return ordered[index]


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
