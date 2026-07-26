"""Protocole versionné de qualification paper avant tout accès testnet.

Les seuils sont figés dans le code puis copiés dans SQLite au démarrage d'une
campagne. Une modification ultérieure ne réécrit donc jamais les règles d'une
observation déjà commencée.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from typing import Any

from .state_store import StateStore

PROTOCOL_VERSION = 1
TERMINAL_STATUSES = {"FILLED", "PARTIAL", "REJECTED", "FAILED", "CANCELED"}
UNRESOLVED_STATUSES = {"PENDING", "OPEN", "UNBALANCED"}


@dataclass(frozen=True)
class ReadinessPolicy:
    min_observation_days: int = 90
    min_equity_coverage: float = 0.95
    min_closed_trades: int = 30
    min_terminal_orders: int = 50
    min_terminal_orders_per_engine: int = 5
    max_rejection_rate: float = 0.02
    max_partial_rate: float = 0.10
    max_p95_slippage_bps: float = 20.0
    max_drawdown: float = -0.45
    max_trend_state_age_seconds: float = 6 * 3600
    max_carry_state_age_seconds: float = 3 * 3600
    qualification_valid_days: int = 7

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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
            if age_days > policy.qualification_valid_days:
                checks = list(final_report["checks"])
                checks.append(
                    ReadinessCheck(
                        "qualification_age",
                        "Validité de la qualification",
                        False,
                        f"{age_days:.1f} j",
                        f"≤ {policy.qualification_valid_days} j",
                    ).to_dict()
                )
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
    orders = [
        item for item in store.read_orders() if _parse_datetime(item["created_at"]) >= started
    ]
    terminal = [item for item in orders if item["status"] in TERMINAL_STATUSES]
    unresolved = [item for item in orders if item["status"] in UNRESOLVED_STATUSES]
    trades = [item for item in store.read_trades() if _parse_datetime(item["exit_ts"]) >= started]
    incidents = store.read_incidents(open_only=True)

    elapsed_days = max(0.0, (current - started).total_seconds() / 86400)
    covered_days = _covered_equity_days(store, started.date(), current.date())
    expected_days = max(1, (current.date() - started.date()).days + 1)
    coverage = len(covered_days) / expected_days

    rejection_count = sum(item["status"] in ("REJECTED", "FAILED") for item in terminal)
    partial_count = sum(item["status"] == "PARTIAL" for item in terminal)
    rejection_rate = rejection_count / len(terminal) if terminal else None
    partial_rate = partial_count / len(terminal) if terminal else None
    slippages = _slippages(terminal)
    p95_slippage = _percentile(slippages, 0.95)
    max_drawdown = _max_portfolio_drawdown(store, started.date(), current.date())

    trend_age = store.engine_age_seconds("trend", now=current)
    carry_age = store.engine_age_seconds("carry", now=current)
    halted = any(
        bool((store.load_engine_state(engine) or {}).get("halted")) for engine in ("trend", "carry")
    )

    integrity_ok = store.integrity_check()
    checks = [
        ReadinessCheck(
            "campaign",
            "Campagne de qualification",
            True,
            f"#{campaign['id']} / v{campaign['protocol_version']}",
            "RUNNING",
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
            "Présence quotidienne des deux moteurs",
            coverage >= policy.min_equity_coverage,
            f"{coverage:.1%}",
            f"≥ {policy.min_equity_coverage:.0%}",
        ),
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
            for engine in ("trend", "carry")
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
        _freshness_check(
            "trend_freshness",
            "Fraîcheur moteur trend",
            trend_age,
            policy.max_trend_state_age_seconds,
        ),
        _freshness_check(
            "carry_freshness",
            "Fraîcheur moteur carry",
            carry_age,
            policy.max_carry_state_age_seconds,
        ),
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


def _covered_equity_days(store: StateStore, start: date, end: date) -> set[date]:
    engine_days = []
    for engine in ("trend", "carry"):
        engine_days.append(
            {
                _parse_datetime(row["ts"]).date()
                for row in store.read_equity(engine)
                if start <= _parse_datetime(row["ts"]).date() <= end
            }
        )
    return set.intersection(*engine_days) if engine_days else set()


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
    start: date,
    end: date,
) -> float | None:
    daily: list[dict[date, tuple[datetime, float]]] = []
    for engine in ("trend", "carry"):
        values: dict[date, tuple[datetime, float]] = {}
        for row in store.read_equity(engine):
            timestamp = _parse_datetime(row["ts"])
            day = timestamp.date()
            if start <= day <= end:
                values[day] = (timestamp, float(row["equity"]))
        daily.append(values)
    common = sorted(set(daily[0]).intersection(daily[1])) if len(daily) == 2 else []
    if len(common) < 2:
        return None
    flows = sorted(
        (
            _parse_datetime(flow["ts"]),
            float(flow["trend_flow"]) + float(flow["carry_flow"]),
        )
        for flow in store.read_flows()
        if start <= _parse_datetime(flow["ts"]).date() <= end
    )
    equities = []
    flow_index = 0
    cumulative_flow = 0.0
    for day in common:
        sample_time = min(daily[0][day][0], daily[1][day][0])
        while flow_index < len(flows) and flows[flow_index][0] <= sample_time:
            cumulative_flow += flows[flow_index][1]
            flow_index += 1
        equities.append(daily[0][day][1] + daily[1][day][1] - cumulative_flow)
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
