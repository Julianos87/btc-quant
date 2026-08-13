"""Dashboard local du portefeuille 60/40 (paper trading).

Lit les états et journaux réels des runners — aucune donnée simulée.
Usage local : python -m dashboard.app  (puis http://localhost:8666)
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import re
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import ccxt
import pandas as pd
from flask import Flask, Response, jsonify, make_response, redirect, request
from dashboard.auth import DashboardAuthenticator
from dashboard.web import web
from btcquant.config import carry_policy_from_config, load_config, portfolio_from_config
from btcquant.reporting.analytics import (
    best_and_worst_day,
    carry_funding_curve,
    combined_equity,
    deposits_total,
    live_metrics,
    net_of_flows,
    trade_analytics,
)
from btcquant.reporting.repository import ReportingReadError, ReportingRepository
from btcquant.reporting.prometheus import render_prometheus
from btcquant.execution.health import execution_health
from btcquant.execution.readiness import (
    SERVICE_ENGINE_MAX_AGE_SECONDS,
    SERVICE_SHADOW_MAX_AGE_SECONDS,
    evaluate_readiness,
    evaluate_service_readiness,
)
from btcquant.execution.shadow import ShadowStore
from btcquant.execution.state_store import StateStore
from btcquant.observability import (
    BoundedReadCache,
    CachePolicy,
    Freshness,
    SourceSnapshot,
    temporal_skew,
)
from werkzeug.middleware.proxy_fix import ProxyFix

ROOT = Path(__file__).resolve().parents[1]
log = logging.getLogger(__name__)
STATE = ROOT / "state"
repository = ReportingRepository(STATE)
START_TIME = time.monotonic()  # uptime: horloge monotone, jamais l'horloge murale

app = Flask(__name__)
app.register_blueprint(web)
# Derrière un reverse proxy TLS (nginx, Caddy...) : Flask n'écoute qu'en
# 127.0.0.1 (DASHBOARD_HOST), donc le seul appelant possible de X-Forwarded-*
# est ce proxy lui-même — faire confiance à UN SEUL hop est donc sûr (x_proto=1
# rend request.is_secure correct, ce dont dépend le cookie `secure=` posé plus
# bas). Sans reverse proxy (dev local), ces en-têtes sont simplement absents
# et ProxyFix ne change rien.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# ── session dashboard ───────────────────────────────────────────────────────
# DASHBOARD_TOKEN est saisi par POST sur /login : il n'apparaît donc ni dans
# l'URL, ni dans l'historique, ni dans les logs du proxy ou le manifest.
# Le navigateur reçoit seulement une preuve signée à durée limitée.
#
# Toutes les routes sont en lecture seule (aucune ne passe d'ordre ni ne modifie
# l'état) : le jeton protège la confidentialité, pas l'intégrité.
#
# Sans DASHBOARD_TOKEN défini, seul localhost est servi (usage en dev).
# Révocation : changer DASHBOARD_TOKEN dans .env et redémarrer le service.
AUTH_TOKEN = os.environ.get("DASHBOARD_TOKEN")
COOKIE_NAME = "tandem_session"
COOKIE_MAX_AGE = 12 * 3600
ENGINE_MAX_AGE_SECONDS = SERVICE_ENGINE_MAX_AGE_SECONDS
SHADOW_MAX_AGE_SECONDS = SERVICE_SHADOW_MAX_AGE_SECONDS
TEMPORAL_SKEW_MAX_SECONDS = max(*ENGINE_MAX_AGE_SECONDS.values(), SHADOW_MAX_AGE_SECONDS)
authenticator = DashboardAuthenticator(
    lambda: AUTH_TOKEN,
    cookie_name=COOKIE_NAME,
    max_age=COOKIE_MAX_AGE,
)


@app.errorhandler(ReportingReadError)
def _reporting_unavailable(error: ReportingReadError):
    app.logger.error("Source de reporting indisponible: %s", error)
    return jsonify({"error": "reporting_unavailable"}), 503


@app.before_request
def _guard():
    if request.path in ("/login", "/healthz", "/readyz"):
        return None
    if request.path == "/metrics/prometheus":
        if request.remote_addr not in ("127.0.0.1", "::1"):
            return Response("Accès refusé : métriques disponibles uniquement en local.", 403)
        return None
    if not authenticator.configured:
        if request.remote_addr not in ("127.0.0.1", "::1"):
            return Response("Accès refusé : définir DASHBOARD_TOKEN pour l'accès distant.", 403)
        return None
    if not authenticator.valid_session(request.cookies.get(COOKIE_NAME) or ""):
        return redirect("/login", code=303)
    return None


@app.get("/healthz")
def healthz():
    # Process liveness only: no database, cache or exchange dependency.
    return jsonify({"api_schema_version": 2, "kind": "PROCESS_LIVENESS", "status": "ok"})


def _readiness_snapshot() -> tuple[dict, int]:
    # Operational readiness with one read-only, profile-driven contract.
    payload = evaluate_service_readiness(
        str(STATE / "btcquant.db"),
        str(STATE / "execution-shadow.db"),
    )
    return payload, (200 if payload["ready"] else 503)


@app.get("/readyz")
def readyz():
    """Public minimal probe; details remain authenticated/local."""

    payload, status = _readiness_snapshot()
    return jsonify(
        {
            "api_schema_version": 2,
            "kind": "SERVICE_READINESS",
            "status": payload["status"],
            "ready": payload["ready"],
        }
    ), status


@app.get("/api/operational-health")
def operational_health():
    # Detailed service readiness; liveness remains intentionally separate.
    payload, status = _readiness_snapshot()
    return jsonify(payload), status


@app.get("/metrics/prometheus")
def prometheus_metrics():
    # Metrics are a local read surface. Source failures become explicit
    # UNKNOWN/availability metrics; they never become a false zero.
    metrics: dict[str, int | float | None] = {
        "btcquant_dashboard_up": 1,
        "btcquant_dashboard_uptime_seconds": max(0.0, time.monotonic() - START_TIME),
    }
    safety_unknown = False
    try:
        combined = _combined_equity(net_of_flows=True)
        metrics["btcquant_portfolio_equity"] = float(combined.iloc[-1]) if len(combined) else None
        for key, value in _live_metrics().items():
            metrics[f"btcquant_portfolio_{key}"] = value
    except Exception:
        app.logger.exception("Portfolio metrics source unavailable")
        metrics["btcquant_portfolio_source_available"] = 0
        safety_unknown = True

    for engine, legacy_name in (
        ("trend", "live_state_4x.json"),
        ("carry", "carry_state.json"),
    ):
        try:
            metrics[f"btcquant_{engine}_state_age_seconds"] = _engine_age_seconds(
                engine, legacy_name
            )
        except Exception:
            app.logger.exception("%s state age source unavailable", engine)
            metrics[f"btcquant_{engine}_state_age_seconds"] = None
            safety_unknown = True
    database = STATE / "btcquant.db"
    metrics["btcquant_trading_db_available"] = int(database.exists())
    if database.exists():
        try:
            store = StateStore(database, initialize=False, read_only=True)
            metrics["btcquant_trading_db_available"] = int(store.integrity_check())
            incidents = store.read_incidents(open_only=True)
            metrics["btcquant_open_incidents"] = len(incidents)
            metrics["btcquant_open_critical_incidents"] = sum(
                item["severity"] == "CRITICAL" for item in incidents
            )
            pending_deposits = store.read_deposits(status="PENDING")
            metrics["btcquant_pending_deposit_count"] = len(pending_deposits)
            metrics["btcquant_pending_deposit_amount"] = sum(
                float(item["amount"]) for item in pending_deposits
            )
            for engine in ("trend", "carry"):
                health = execution_health(store, engine)
                prefix = f"btcquant_execution_{engine}"
                metrics.update(
                    {
                        f"{prefix}_orders_analyzed": health.orders_analyzed,
                        f"{prefix}_unresolved_orders": len(health.unresolved_order_ids),
                        f"{prefix}_stale_pending_orders": len(health.stale_pending_order_ids),
                        f"{prefix}_unbalanced_orders": len(health.unbalanced_order_ids),
                        f"{prefix}_fill_ratio": health.fill_ratio,
                        f"{prefix}_rejection_rate": health.rejection_rate,
                        f"{prefix}_partial_rate": health.partial_rate,
                        f"{prefix}_average_slippage_bps": health.average_slippage_bps,
                        f"{prefix}_p95_slippage_bps": health.p95_slippage_bps,
                    }
                )
            if not metrics["btcquant_trading_db_available"]:
                safety_unknown = True
        except Exception:
            app.logger.exception("Trading metrics source unavailable")
            metrics["btcquant_trading_db_available"] = 0
            safety_unknown = True
    else:
        safety_unknown = True

    shadow_database = STATE / "execution-shadow.db"
    metrics["btcquant_shadow_db_available"] = int(shadow_database.exists())
    if shadow_database.exists():
        try:
            shadow = ShadowStore(shadow_database, read_only=True).summary()
            evidence = shadow["evidence"]
            runtime = shadow["runtime"]
            metrics.update(
                {
                    "btcquant_shadow_observation_days": evidence["observation_days"],
                    "btcquant_shadow_eligible_intents": evidence["eligible_intents"],
                    "btcquant_shadow_pending_quotes": shadow["pending_quotes"],
                    "btcquant_shadow_touch_proxy_rate": shadow["touch_proxy_rate"],
                    "btcquant_shadow_fallback_rate": shadow["fallback_rate"],
                    "btcquant_shadow_p95_touch_seconds": evidence["p95_fill_seconds"],
                    "btcquant_shadow_mean_all_in_cost_bps": evidence["mean_all_in_cost_bps"],
                    "btcquant_shadow_p95_all_in_cost_bps": evidence["p95_slippage_bps"],
                    "btcquant_shadow_mean_markout_bps": shadow["mean_markout_bps"],
                    "btcquant_shadow_proxy_qualified": int(shadow["proxy_qualification"]["passed"]),
                    "btcquant_shadow_last_success_age_seconds": runtime["last_success_age_seconds"],
                    "btcquant_shadow_consecutive_failures": runtime["consecutive_failures"],
                    "btcquant_shadow_total_failures": runtime["total_failures"],
                    "btcquant_shadow_outage_age_seconds": runtime["outage_age_seconds"],
                }
            )
        except Exception:
            app.logger.exception("Shadow metrics source unavailable")
            metrics["btcquant_shadow_db_available"] = 0
            safety_unknown = True

    for key, snapshot in _cache_snapshots.items():
        prefix = f"btcquant_source_{key}"
        metrics[f"{prefix}_available"] = int(snapshot.freshness is not Freshness.UNAVAILABLE)
        metrics[f"{prefix}_fresh"] = int(snapshot.freshness is Freshness.FRESH)
        if snapshot.age_seconds is not None:
            metrics[f"{prefix}_age_seconds"] = snapshot.age_seconds
        if snapshot.freshness in {Freshness.UNAVAILABLE, Freshness.UNKNOWN}:
            safety_unknown = True

    readiness, _status = _readiness_snapshot()
    metrics["btcquant_ready"] = int(readiness["status"] == "ready")
    metrics["btcquant_execution_safety_unknown"] = int(safety_unknown)
    return Response(
        render_prometheus(metrics),
        content_type="text/plain; version=0.0.4; charset=utf-8",
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    if not authenticator.configured:
        return redirect("/")
    error = ""
    if request.method == "POST":
        supplied = request.form.get("token") or ""
        if authenticator.verify_token(supplied):
            response = make_response(redirect("/", code=303))
            authenticator.set_session_cookie(response, secure=request.is_secure)
            return response
        error = "Accès refusé"
    return Response(
        """<!doctype html><html lang="fr"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Connexion TANDEM</title>
<style>body{font:16px system-ui;background:#0b1020;color:#eef2ff;display:grid;
place-items:center;height:100vh;margin:0}form{display:grid;gap:16px;width:min(360px,85vw);
padding:28px;background:#151c30;border-radius:14px}input,button{font:inherit;padding:12px;
border-radius:8px;border:1px solid #3b4664}button{cursor:pointer;font-weight:700}</style>
<form method="post"><h1>TANDEM</h1><label>Jeton d’accès
<input type="password" name="token" required autocomplete="current-password"></label>
<button type="submit">Se connecter</button>"""
        + (f"<p>{error}</p>" if error else "")
        + "</form></html>",
        status=403 if error else 200,
        mimetype="text/html",
    )


@app.after_request
def _security_headers(resp: Response) -> Response:
    """En-têtes défensifs pour un dashboard exposé sur Internet :
    - Referrer-Policy : évite de divulguer les chemins de navigation ;
    - X-Content-Type-Options / X-Frame-Options / CSP : durcissement standard
      contre le sniffing MIME, le clickjacking et l'injection de script."""
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Cache-Control"] = "no-store"
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; style-src 'self' 'unsafe-inline'; "
        "font-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; script-src 'self'",
    )
    return resp


@app.after_request
def _gzip_response(resp: Response) -> Response:
    """Compression des réponses JSON/CSV : l'équity et les bougies pèsent des
    dizaines de Ko — gzip divise par ~5-10 le transfert (net sur mobile)."""
    if (
        resp.status_code == 200
        and not resp.direct_passthrough
        and resp.mimetype
        in ("application/json", "text/csv", "application/javascript", "image/svg+xml", "text/html")
        and (resp.content_length or 0) > 512
        and "gzip" in request.headers.get("Accept-Encoding", "")
    ):
        data = gzip.compress(resp.get_data(), compresslevel=6)
        if len(data) < (resp.content_length or 0):
            resp.set_data(data)
            resp.headers["Content-Encoding"] = "gzip"
            resp.headers["Vary"] = "Accept-Encoding"
    return resp


_cache: dict = {}
_cache_snapshots: dict[str, SourceSnapshot] = {}
_observation_cache: BoundedReadCache = BoundedReadCache()
_warm_thread: threading.Thread | None = None
_warm_thread_lock = threading.Lock()


def start_warm_loop() -> None:
    """Démarre une seule boucle de préchauffage par processus WSGI."""

    global _warm_thread
    with _warm_thread_lock:
        if _warm_thread is not None and _warm_thread.is_alive():
            return
        _warm_thread = threading.Thread(
            target=_warm_loop,
            name="dashboard-warm-cache",
            daemon=True,
        )
        _warm_thread.start()


# les objets ccxt ne sont pas thread-safe ; le serveur Flask sert les requêtes
# en parallèle (9 fetchs simultanés au chargement de la page) → on sérialise
# tous les appels exchange derrière un verrou pour éviter les races.
_ex_lock = threading.Lock()

# ── venue : Hyperliquid depuis le 17/07/2026 ─────────────────────────────────
# fetch_ticker y recharge le contexte de TOUS les marchés (~12 s mesurés) :
# on ne l'appelle JAMAIS — prix via la dernière bougie 1m (~0,5 s), variation
# 24 h via les bougies 1h, funding via l'historique des paiements (horaires).
SYMBOL = "BTC/USDC:USDC"
FOUR_HOURS_MS = 4 * 3_600_000
PROFILE_CONFIG = load_config(ROOT / "environments" / "paper" / "config.yaml")
PORTFOLIO = portfolio_from_config(PROFILE_CONFIG)
CARRY_POLICY = carry_policy_from_config(PROFILE_CONFIG)
INITIAL_CAPITAL = PORTFOLIO.total_capital


def _hl():
    if "hl_ex" not in _cache:
        # timeout 30 s : le premier appel paie un load_markets implicite
        # (~13 s mesurés) — 15 s le ferait échouer par intermittence
        _cache["hl_ex"] = ccxt.hyperliquid({"enableRateLimit": True, "timeout": 30_000})
    return _cache["hl_ex"]


def _fx_ex():
    # Binance spot UNIQUEMENT pour le taux EUR/USDT de l'affichage en euros
    # (Hyperliquid ne cote aucune paire EUR)
    if "fx_binance" not in _cache:
        _cache["fx_binance"] = ccxt.binance({"enableRateLimit": True, "timeout": 15_000})
    return _cache["fx_binance"]


_CACHE_MAX_STALE_SECONDS = {
    "price": 120.0,
    "ohlcv1h": 900.0,
    "ohlcv4h": 1_800.0,
    "funding": 1_800.0,
    "fx_eur": 7_200.0,
    "buyhold": 1_800.0,
}
_CACHE_MAX_AGE_SECONDS = {
    "price": 90.0,
    "ohlcv1h": 600.0,
    "ohlcv4h": 1_200.0,
    "funding": 900.0,
    "fx_eur": 3_600.0,
    "buyhold": 1_200.0,
}
_CACHE_SOURCES = {
    "price": "HYPERLIQUID",
    "ohlcv1h": "HYPERLIQUID_OHLCV_1H",
    "ohlcv4h": "HYPERLIQUID_OHLCV_4H",
    "funding": "HYPERLIQUID_FUNDING",
    "fx_eur": "BINANCE_FX_DISPLAY_ONLY",
    "buyhold": "HYPERLIQUID_OHLCV",
}


def _cache_observed_at(value):
    if isinstance(value, dict) and value.get("__dashboard_value__") is not None:
        raw = value.get("timestamp")
    elif isinstance(value, dict):
        raw = value.get("timestamp") or value.get("ts")
    elif isinstance(value, list) and value and isinstance(value[-1], (list, tuple)):
        raw = value[-1][0] if value[-1] else None
    else:
        raw = None
    if raw is None:
        return None
    try:
        numeric = float(raw)
        if numeric > 10_000_000_000:
            numeric /= 1000.0
        return datetime.fromtimestamp(numeric, tz=UTC)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _cache_value(value):
    if isinstance(value, dict) and "__dashboard_value__" in value:
        return value["__dashboard_value__"]
    return value


def _cached(key: str, ttl: float, fn):
    policy = CachePolicy(
        ttl_seconds=ttl,
        max_stale_seconds=_CACHE_MAX_STALE_SECONDS.get(key, max(ttl * 3.0, ttl + 60.0)),
        max_age_seconds=_CACHE_MAX_AGE_SECONDS.get(key, ttl),
    )
    try:
        with _ex_lock:
            snapshot = _observation_cache.get(
                key,
                policy,
                fn,
                source=_CACHE_SOURCES.get(key, key),
                observed_at=_cache_observed_at,
            )
        value = _cache_value(snapshot.value)
        if snapshot.value is not value:
            snapshot = SourceSnapshot(
                value=value,
                source=snapshot.source,
                observed_at=snapshot.observed_at,
                received_at=snapshot.received_at,
                age_seconds=snapshot.age_seconds,
                freshness=snapshot.freshness,
                error=snapshot.error,
                last_success_at=snapshot.last_success_at,
            )
        _cache_snapshots[key] = snapshot
        return value
    except Exception as error:
        log.warning("Source réseau dashboard indisponible pour %s (%s)", key, type(error).__name__)
        return None


def _cache_snapshot(key: str) -> SourceSnapshot:
    snapshot = _cache_snapshots.get(key)
    if snapshot is None:
        return SourceSnapshot.unavailable(source=_CACHE_SOURCES.get(key, key))
    max_age = _CACHE_MAX_AGE_SECONDS.get(key)
    max_stale = _CACHE_MAX_STALE_SECONDS.get(key)
    return snapshot.at(max_age_seconds=max_age, max_stale_seconds=max_stale)


# ── fetchers réseau (appelés via _cached ; préchauffés par _warm_loop) ──────
def _get_price() -> dict | None:
    c = _hl().fetch_ohlcv(SYMBOL, "1m", limit=1)
    return {"__dashboard_value__": float(c[-1][4]), "timestamp": c[-1][0]} if c else None


def _get_candles_1h() -> list[list]:
    raw = _hl().fetch_ohlcv(SYMBOL, "1h", limit=200)
    return [[r[0], r[1], r[2], r[3], r[4]] for r in raw]


def _get_candles_4h() -> list[list]:
    # 500 barres : 100 de lookback pour le canal D100 + ~300 de chauffe pour
    # que l'EMA200 (régime) soit fiable, le reste couvre la fenêtre affichée
    raw = _hl().fetch_ohlcv(SYMBOL, "4h", limit=500)
    return [[r[0], r[1], r[2], r[3], r[4]] for r in raw]


def _get_funding() -> dict | None:
    """Dernier paiement de funding (HORAIRE sur Hyperliquid) + annualisation."""
    since = int((time.time() - 3 * 3600) * 1000)
    hist = _hl().fetch_funding_rate_history(SYMBOL, since=since)
    if not hist:
        return None
    rate = float(hist[-1]["fundingRate"])
    return {
        "rate": rate,
        "annualized": rate * 24 * 365,
        "timestamp": hist[-1].get("timestamp"),
    }


def _get_fx_eur() -> dict | None:
    t = _fx_ex().fetch_ticker("EUR/USDT")
    if not t or not t.get("last") or not t.get("timestamp"):
        return None
    return {
        "__dashboard_value__": float(t["last"]),
        "timestamp": t["timestamp"],
    }


#     (clé cache, TTL endpoint, TTL préchauffage, fetch)
_WARM_JOBS = [
    ("price", 30, 20, lambda: _timed("api", _get_price)),
    ("ohlcv1h", 300, 240, _get_candles_1h),
    ("ohlcv4h", 300, 240, _get_candles_4h),
    ("funding", 300, 240, _get_funding),
    ("fx_eur", 3600, 3000, _get_fx_eur),
]


def _warm_loop() -> None:
    """Préchauffage du cache réseau : chaque donnée exchange est rafraîchie en
    arrière-plan JUSTE AVANT l'expiration de son TTL, si bien que les requêtes
    du navigateur ne paient jamais la latence Hyperliquid — elles lisent un
    cache toujours chaud. Démarré uniquement en exécution directe (pas à
    l'import : les tests appellent les endpoints sans réseau)."""
    while True:
        for key, _ttl, warm_ttl, fn in _WARM_JOBS:
            _cached(key, warm_ttl, fn)  # _cached avale les erreurs réseau
        _cached("buyhold", 480, _get_buyhold)
        time.sleep(10)


def _repository() -> ReportingRepository:
    """Suit le répertoire STATE actif (tests, staging ou production)."""

    global repository
    if repository.state_dir != STATE:
        repository = ReportingRepository(STATE)
    return repository


def _engine_state(engine: str, legacy_name: str) -> dict | None:
    return _repository().read_engine_state(engine, STATE / legacy_name)


def _read_json(path: Path) -> dict | None:
    """Lecture JSON générique, sans déduction implicite d'un moteur."""

    return _repository().read_json(path)


def _engine_age_seconds(engine: str, legacy_name: str) -> float | None:
    return _repository().engine_age_seconds(engine, STATE / legacy_name)


# ── cache de parsing des CSV d'état ──────────────────────────────────────────
# Un chargement de page = 9 endpoints, dont la plupart relisent les mêmes CSV
# (l'équity trend fait des centaines de Ko) : on ne reparse un fichier que
# lorsque (mtime, taille) a changé — le runner n'écrit qu'une fois par minute.
_parse_cache: dict = {}


def _file_key(path: Path) -> tuple | None:
    return _repository().file_key(path)


def _database_key() -> tuple:
    return _repository().database_key()


def _read_equity(engine: str, legacy_name: str) -> pd.Series:
    return _repository().read_engine_equity(engine, STATE / legacy_name)


def _read_trades() -> pd.DataFrame:
    return _repository().read_trades()


def _read_flows() -> pd.DataFrame:
    return _repository().read_flows()


def _deposits_total(flows: pd.DataFrame) -> float:
    return deposits_total(flows)


def _net_of_flows(s: pd.Series, flows: pd.DataFrame, col: str) -> pd.Series:
    return net_of_flows(s, flows, col)


def _timed(label: str, fn):
    """Exécute fn en mesurant sa latence (ms), stockée dans le cache."""
    t0 = time.time()
    try:
        return fn()
    finally:
        _cache[f"{label}_ms"] = round((time.time() - t0) * 1000, 1)


def _combined_equity(net_of_flows: bool = False) -> pd.Series:
    """Équity combinée trend + carry, alignée à la minute.

    net_of_flows=True : apports et transferts déduits (série « trading
    seul ») — c'est sur elle que se mesurent Sharpe, drawdowns et records,
    sans quoi chaque apport mensuel ressemblerait à un gain.

    Mise en cache sur les (mtime, taille) des trois CSV : cinq endpoints la
    recalculent à chaque chargement de page, le resampling minute est le
    poste de calcul le plus cher du dashboard."""
    key = (
        net_of_flows,
        _database_key(),
        _file_key(STATE / "equity_trend.csv"),
        _file_key(STATE / "equity_carry.csv"),
        _file_key(STATE / "flows.csv"),
    )
    hit = _parse_cache.get(f"combined_{net_of_flows}")
    if hit is not None and hit[0] == key:
        return hit[1]
    trend = _read_equity("trend", "equity_trend.csv")
    carry = _read_equity("carry", "equity_carry.csv")
    if not len(trend) or not len(carry):
        combined = pd.Series(dtype=float)
    else:
        if net_of_flows:
            flows = _read_flows()
            trend = _net_of_flows(trend, flows, "trend_flow")
            carry = _net_of_flows(carry, flows, "carry_flow")
        combined = combined_equity(trend, carry)
    _parse_cache[f"combined_{net_of_flows}"] = (key, combined)
    return combined


def _live_metrics() -> dict:
    return live_metrics(_combined_equity(net_of_flows=True), INITIAL_CAPITAL)


@app.route("/api/summary")
def summary():
    price = _cached("price", 30, lambda: _timed("api", _get_price))
    candles = _cached("ohlcv1h", 300, _get_candles_1h) or []
    funding = _cached("funding", 300, _get_funding)
    # taux EUR/USDT pour l'affichage optionnel en euros (1 EUR = X USDT)
    eur_usd = _cached("fx_eur", 3600, _get_fx_eur)
    price_snapshot = _cache_snapshot("price")
    candles_snapshot = _cache_snapshot("ohlcv1h")
    funding_snapshot = _cache_snapshot("funding")
    fx_snapshot = _cache_snapshot("fx_eur")

    # variation 24 h : clôture d'il y a 24 bougies 1h vs prix courant
    change_24h = None
    if price and len(candles) >= 25:
        ref = float(candles[-25][4])
        if ref > 0:
            change_24h = price / ref - 1.0

    trend_state = _engine_state("trend", "live_state_4x.json") or {}
    carry_state = _engine_state("carry", "carry_state.json") or {}
    trend_eq = _read_equity("trend", "equity_trend.csv")
    carry_eq = _read_equity("carry", "equity_carry.csv")

    trend_age = _engine_age_seconds("trend", "live_state_4x.json")
    carry_age = _engine_age_seconds("carry", "carry_state.json")

    def _engine_freshness(age: float | None, limit: float) -> str:
        if age is None:
            return Freshness.UNAVAILABLE.value
        return Freshness.FRESH.value if age <= limit else Freshness.STALE.value

    trend_freshness = _engine_freshness(trend_age, ENGINE_MAX_AGE_SECONDS["trend"])
    carry_freshness = _engine_freshness(carry_age, ENGINE_MAX_AGE_SECONDS["carry"])

    trend_equity = (
        float(trend_eq.iloc[-1])
        if len(trend_eq)
        else sum(s.get("cash", 0.0) for s in trend_state.get("slots", {}).values())
    )
    carry_equity = (
        float(carry_eq.iloc[-1]) if len(carry_eq) else float(carry_state.get("equity", 0.0))
    )
    total = trend_equity + carry_equity
    accounting_available = (
        bool(trend_state)
        and bool(carry_state)
        and (bool(len(trend_eq)) or bool(trend_state.get("slots")))
    )
    initial_total = INITIAL_CAPITAL
    database = STATE / "btcquant.db"
    store = StateStore(database, initialize=False, read_only=True) if database.exists() else None
    accounting_status = (
        "FRESH"
        if accounting_available
        else "EMPTY_BUT_VALID"
        if database.exists()
        else "SOURCE_UNAVAILABLE"
    )
    trend_observed_at = store.engine_updated_at("trend") if store is not None else None
    carry_observed_at = store.engine_updated_at("carry") if store is not None else None
    state_price_temporal = temporal_skew(
        {"state": trend_observed_at, "price": price_snapshot.observed_at},
        max_skew_seconds=TEMPORAL_SKEW_MAX_SECONDS,
    )
    if (
        price_snapshot.freshness in (Freshness.UNAVAILABLE, Freshness.UNKNOWN)
        or trend_freshness in (Freshness.UNAVAILABLE.value, Freshness.UNKNOWN.value)
        or state_price_temporal["freshness_status"] == Freshness.UNKNOWN.value
    ):
        market_valuation_status = "UNKNOWN"
    elif (
        price_snapshot.freshness is Freshness.STALE
        or trend_freshness == Freshness.STALE.value
        or state_price_temporal["freshness_status"] == Freshness.STALE.value
    ):
        market_valuation_status = "STALE_MARK_TO_MARKET_ESTIMATE"
    else:
        market_valuation_status = "MARK_TO_MARKET_ESTIMATE"
    pending_deposits = store.read_deposits(status="PENDING") if store is not None else []
    pending_deposit_amount = sum(float(item["amount"]) for item in pending_deposits)
    flows = _read_flows()
    deposits = _deposits_total(flows)
    invested = initial_total + deposits  # capital réellement engagé

    # PnL du jour (UTC) sur l'équity combinée, net des apports du jour
    # (sans quoi l'apport du 1er du mois s'afficherait comme un gain)
    day_pnl_pct = None
    if len(trend_eq) and len(carry_eq):
        today = pd.Timestamp.now(tz="UTC").normalize()
        t0 = trend_eq[trend_eq.index >= today]
        c0 = carry_eq[carry_eq.index >= today]
        if len(t0) and len(c0):
            start_day = float(t0.iloc[0]) + float(c0.iloc[0])
            start_ts = min(t0.index[0], c0.index[0])
            since_start = flows[flows["ts"] > start_ts]
            day_total = total - _deposits_total(since_start)
            if start_day > 0:
                day_pnl_pct = day_total / start_day - 1.0

    slots = []
    for name, s in trend_state.get("slots", {}).items():
        pos = s.get("position")
        row = {
            "name": name,
            "cash": s.get("cash"),
            "state": "FLAT",
            "last_bar": s.get("last_bar_ts"),
        }
        if pos:
            direction = pos.get("direction", 1)
            upnl = None
            if price and market_valuation_status != "UNKNOWN":
                upnl = direction * pos["qty"] * (price - pos["entry_price"])
            row.update(
                state="LONG" if direction == 1 else "SHORT",
                qty=pos["qty"],
                entry=pos["entry_price"],
                stop=pos["stop_price"],
                bars=pos.get("bars_held"),
                upnl=upnl,
                position_as_of=trend_observed_at.isoformat() if trend_observed_at else None,
                market_price_as_of=price_snapshot.observed_at.isoformat()
                if price_snapshot.observed_at
                else None,
                price_source=price_snapshot.source,
                state_age=trend_age,
                price_age=price_snapshot.age_seconds,
                source_skew=state_price_temporal["max_source_skew_seconds"],
                valuation_status=market_valuation_status,
            )
        slots.append(row)

    # funding horaire sur Hyperliquid : prochain paiement à l'heure pile
    next_funding = (int(time.time() // 3600) + 1) * 3600 * 1000

    # exposition brute / levier effectif (somme des notionnels / équity totale)
    trend_notional = 0.0
    for sl in slots:
        if sl.get("qty") and price and market_valuation_status != "UNKNOWN":
            trend_notional += abs(sl["qty"]) * price
    carry_notional = carry_equity * CARRY_POLICY.leverage if carry_state.get("in_position") else 0.0
    gross_notional = trend_notional + carry_notional
    leverage = gross_notional / total if total else 0.0

    # prochaine bougie 4h (le trend décide à la clôture des bougies 4h UTC)
    now_ts = pd.Timestamp.now(tz="UTC")
    next_bar = now_ts.floor("4h") + pd.Timedelta(hours=4)
    operational: dict = {"execution": {}, "open_incidents": []}
    if store is not None:
        operational["execution"] = {
            engine: execution_health(store, engine).to_dict() for engine in ("trend", "carry")
        }
        operational["open_incidents"] = store.read_incidents(open_only=True)

    source_observations = {
        "price": price_snapshot.observed_at,
        "candles_1h": candles_snapshot.observed_at,
        "funding": funding_snapshot.observed_at,
        "fx_eur": fx_snapshot.observed_at,
        "trend_state": trend_observed_at,
        "carry_state": carry_observed_at,
    }
    required_source_fresh = (
        price_snapshot.freshness is Freshness.FRESH
        and trend_freshness == Freshness.FRESH.value
        and state_price_temporal["freshness_status"] == Freshness.FRESH.value
    )
    return jsonify(
        {
            "api_schema_version": 2,
            "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
            "data_as_of": price_snapshot.observed_at.isoformat()
            if price_snapshot.observed_at
            else None,
            "mode": "PAPER",
            "sources": {
                "price": price_snapshot.to_dict(),
                "candles_1h": candles_snapshot.to_dict(),
                "funding": funding_snapshot.to_dict(),
                "fx_eur": fx_snapshot.to_dict(),
            },
            "temporal": temporal_skew(
                source_observations,
                max_skew_seconds=TEMPORAL_SKEW_MAX_SECONDS,
            ),
            "btc": {
                "price": price,
                "change24h": change_24h,
                "source": price_snapshot.source,
                "observed_at": price_snapshot.observed_at.isoformat()
                if price_snapshot.observed_at
                else None,
                "received_at": price_snapshot.received_at.isoformat(),
                "age_seconds": price_snapshot.age_seconds,
                "freshness": price_snapshot.freshness.value,
                "valuation_status": market_valuation_status,
            },
            "funding": {
                "rate": funding["rate"] if funding else None,
                "annualized": funding["annualized"] if funding else None,
                "next_ts": next_funding,
                "source": funding_snapshot.source,
                "observed_at": funding_snapshot.observed_at.isoformat()
                if funding_snapshot.observed_at
                else None,
                "age_seconds": funding_snapshot.age_seconds,
                "freshness": funding_snapshot.freshness.value,
            },
            "trend": {
                "alive": trend_age is not None and trend_age < 240,
                "freshness": trend_freshness,
                "observed_at": trend_observed_at.isoformat() if trend_observed_at else None,
                "age_s": trend_age,
                "equity": trend_equity if accounting_available else None,
                "initial": PORTFOLIO.trend_capital,
                "halted": trend_state.get("halted", False),
                "daily_lockout": trend_state.get("daily_lockout", False),
                "peak_equity": trend_state.get("peak_equity"),
                "slots": slots,
            },
            "carry": {
                "alive": carry_age is not None and carry_age < 900,
                "freshness": carry_freshness,
                "observed_at": carry_observed_at.isoformat() if carry_observed_at else None,
                "age_s": carry_age,
                "equity": carry_equity if accounting_available else None,
                "initial": PORTFOLIO.carry_capital,
                "in_position": carry_state.get("in_position", False),
                "last_funding_ts": carry_state.get("last_funding_ts"),
                # Le carry a des coupe-circuits depuis le 27/07/2026 ; les
                # exposer permet à la bannière d'alerte de les signaler comme
                # ceux du trend. Les états antérieurs n'ont pas ces clés.
                "halted": carry_state.get("halted", False),
                "daily_lockout": carry_state.get("daily_lockout", False),
                "peak_equity": carry_state.get("peak_equity"),
            },
            "totals": {
                "equity": total if accounting_available else None,
                "status": accounting_status,
                "initial": initial_total,
                "deposits": deposits,
                "pending_deposits": pending_deposit_amount,
                "pending_deposit_count": len(pending_deposits),
                "pnl": total - invested if accounting_available else None,
                "pnl_pct": total / invested - 1.0
                if accounting_available and invested > 0
                else None,
                "day_pnl_pct": day_pnl_pct,
                "allocation_trend": trend_equity / total
                if accounting_available and total
                else None,
                "gross_notional": gross_notional if accounting_available else None,
                "leverage": leverage if accounting_available else None,
            },
            "health": {
                "server_uptime_s": max(0.0, time.monotonic() - START_TIME),
                "api_latency_ms": _cache.get("api_ms"),
                "next_bar_ts": int(next_bar.timestamp() * 1000),
                "safety_status": (
                    "PASS" if required_source_fresh and accounting_status == "FRESH" else "UNKNOWN"
                ),
                "valuation_status": market_valuation_status,
                "source_skew": state_price_temporal["max_source_skew_seconds"],
                **operational,
            },
            "fx": {
                "eur_usd": eur_usd,
                "source": fx_snapshot.source,
                "observed_at": fx_snapshot.observed_at.isoformat()
                if fx_snapshot.observed_at
                else None,
                "age_seconds": fx_snapshot.age_seconds,
                "freshness": fx_snapshot.freshness.value,
                "display_only": True,
            },
        }
    )


@app.route("/api/operations")
def operations():
    database = STATE / "btcquant.db"
    if not database.exists():
        return jsonify(
            {
                "status": "SOURCE_UNAVAILABLE",
                "source": "trading_db",
                "execution": None,
                "incidents": None,
                "deposits": None,
            }
        ), 503
    try:
        store = StateStore(database, initialize=False, read_only=True)
        incidents = store.read_incidents()
        for incident in incidents:
            incident["context"] = json.loads(incident["context"])
        execution = {
            engine: execution_health(store, engine).to_dict() for engine in ("trend", "carry")
        }
        deposits = store.read_deposits()
    except Exception:
        app.logger.exception("Operational source unavailable")
        return jsonify(
            {
                "status": "SOURCE_CORRUPT",
                "source": "trading_db",
                "execution": None,
                "incidents": None,
                "deposits": None,
            }
        ), 503
    return jsonify(
        {
            "status": "OK",
            "source": "trading_db",
            "execution": execution,
            "incidents": incidents,
            "deposits": deposits,
        }
    )


@app.route("/api/execution-shadow")
def execution_shadow():
    """Résultats du proxy maker mainnet, sans ordres ni données de compte."""

    database = STATE / "execution-shadow.db"
    if not database.exists():
        return jsonify(
            {
                "schema_version": 1,
                "status": "NOT_STARTED",
                "source_status": "EMPTY_BUT_VALID",
                "warning": "aucune observation shadow disponible",
            }
        )
    try:
        return jsonify(ShadowStore(database, read_only=True).summary())
    except Exception:
        app.logger.exception("Shadow source unavailable")
        return jsonify(
            {
                "schema_version": 1,
                "status": "SOURCE_CORRUPT",
                "source_status": "SOURCE_CORRUPT",
            }
        ), 503


@app.route("/api/equity")
def equity():
    trend = _read_equity("trend", "equity_trend.csv")
    carry = _read_equity("carry", "equity_carry.csv")

    def pack(s: pd.Series, max_points: int = 1500):
        if len(s) > max_points:
            s = s.resample("5min").last().dropna()
        return [[int(ts.timestamp() * 1000), round(float(v), 2)] for ts, v in s.items()]

    combined = _combined_equity()
    if not (STATE / "btcquant.db").exists() and (not len(trend) or not len(carry)):
        return jsonify({"status": "SOURCE_UNAVAILABLE", "source": "reporting"}), 503
    buyhold = _cached("buyhold", 600, _get_buyhold) or []

    return jsonify(
        {
            "status": "OK" if len(trend) and len(carry) else "EMPTY_BUT_VALID",
            "source": "reporting",
            "trend": pack(trend),
            "carry": pack(carry),
            "combined": pack(combined),
            "buyhold": buyhold,
        }
    )


def _get_buyhold() -> list[list]:
    """Buy & hold : prix BTC sur la fenêtre de l'équity combinée (le front
    normalise en base 100). Timeframe adaptatif pour que TOUT l'historique
    tienne dans les 1000 bougies d'un seul appel (1h ≈ 41 j, 4h ≈ 166 j,
    1d au-delà)."""
    combined = _combined_equity()
    if not len(combined):
        return []
    start_ms = int(combined.index[0].timestamp() * 1000)
    span_h = (time.time() * 1000 - start_ms) / 3_600_000
    bh_tf = "1h" if span_h <= 950 else "4h" if span_h <= 3_800 else "1d"
    raw = _hl().fetch_ohlcv(SYMBOL, bh_tf, since=start_ms, limit=1000)
    return [[r[0], round(r[4], 2)] for r in raw]


#: horizons Donchian de l'ensemble trend (cf. environments/paper/config.yaml)
DONCHIAN_PERIODS = (20, 55, 100)


def _donchian_channels(
    candles_1h: list[list],
    candles_4h: list[list],
    *,
    now_ms: int | None = None,
) -> tuple[list[dict], bool | None]:
    """Canaux de Donchian 20/55/100 sur bougies 4h (le timeframe de décision du
    trend), rééchantillonnés sur les timestamps 1h du graphique.

    Convention identique au moteur (btcquant.indicators.donchian_high/low,
    reproduite ici car le dashboard n'importe pas le package) : la valeur d'une
    barre 4h = extrême des N barres PRÉCÉDENTES (barre courante exclue) — c'est
    le seuil que le runner comparera au close de cette barre. Retourne aussi le
    régime EMA50>EMA200 (4h) qui conditionne le sens des entrées."""
    current_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    current_bucket = current_ms // FOUR_HOURS_MS * FOUR_HOURS_MS
    # Le flux Hyperliquid contient la bougie 4 h en formation. Elle est utile
    # comme support temporel pour prolonger le seuil Donchian applicable à la
    # période courante, mais ne doit jamais influencer le régime EMA : le
    # moteur Trend ne décide qu'à partir de bougies entièrement clôturées.
    eligible = [candle for candle in candles_4h if int(candle[0]) <= current_bucket]
    closed = [candle for candle in eligible if int(candle[0]) + FOUR_HOURS_MS <= current_ms]
    if len(closed) < max(DONCHIAN_PERIODS) or not eligible:
        return [], None
    df = pd.DataFrame(eligible, columns=["ts", "open", "high", "low", "close"])
    closed_df = pd.DataFrame(closed, columns=["ts", "open", "high", "low", "close"])
    idx_of = {int(t): i for i, t in enumerate(df["ts"])}
    map_idx = [idx_of.get(int(c[0]) // FOUR_HOURS_MS * FOUR_HOURS_MS) for c in candles_1h]

    def _sample(series: pd.Series) -> list:
        return [
            None if i is None or pd.isna(series.iat[i]) else round(float(series.iat[i]), 1)
            for i in map_idx
        ]

    channels = [
        {
            "name": f"D{n}",
            "high": _sample(df["high"].rolling(n, min_periods=n).max().shift(1)),
            "low": _sample(df["low"].rolling(n, min_periods=n).min().shift(1)),
        }
        for n in DONCHIAN_PERIODS
    ]
    e50 = closed_df["close"].ewm(span=50, adjust=False, min_periods=50).mean().iat[-1]
    e200 = closed_df["close"].ewm(span=200, adjust=False, min_periods=200).mean().iat[-1]
    regime = None if pd.isna(e50) or pd.isna(e200) else bool(e50 > e200)
    return channels, regime


@app.route("/api/price")
def price_chart():
    """Dernières ~200 bougies 1h + canaux Donchian + positions ouvertes du trend."""
    candles = _cached("ohlcv1h", 300, _get_candles_1h) or []
    channels, regime_up = _donchian_channels(
        candles, _cached("ohlcv4h", 300, _get_candles_4h) or []
    )
    trend_state = _engine_state("trend", "live_state_4x.json") or {}
    positions = []
    for name, s in trend_state.get("slots", {}).items():
        pos = s.get("position")
        if pos:
            positions.append(
                {
                    "name": name.replace("trend_ls_", "D"),
                    "direction": pos.get("direction", 1),
                    "entry": pos["entry_price"],
                    "stop": pos["stop_price"],
                    "entry_ts": pos.get("entry_time"),
                }
            )
    return jsonify(
        {"candles": candles, "positions": positions, "channels": channels, "regime_up": regime_up}
    )


@app.route("/api/conformity")
def conformity():
    """Réalisé (paper) vs attendu (backtest) — la carte « Est-ce normal ? »."""
    ref_path = ROOT / "audit" / "baseline_reference.json"
    baseline = json.loads(ref_path.read_text(encoding="utf-8")) if ref_path.exists() else None
    ref = baseline["results"].get("conformity") if baseline else None
    if ref is not None:
        ref = {**ref, "provenance": baseline["provenance"]}

    out = {"reference": ref, "realized": None, "drawdown": None}
    df = _read_trades()
    if len(df):
        n = len(df)
        wins = int((df["pnl"] > 0).sum())
        # intervalle de Wilson à 95 % sur le win rate
        p, z = wins / n, 1.96
        denom = 1 + z * z / n
        center = (p + z * z / (2 * n)) / denom
        half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / denom
        out["realized"] = {
            "n": n,
            "wins": wins,
            "win_rate": p,
            "win_rate_ci": [max(0.0, center - half), min(1.0, center + half)],
            "avg_win": float(df.loc[df["pnl"] > 0, "pnl"].mean()) if wins else None,
            "avg_loss": float(df.loc[df["pnl"] <= 0, "pnl"].mean()) if wins < n else None,
            "current_loss_streak": int(
                (df["pnl"].iloc[::-1] <= 0).cummin().sum() if len(df) else 0
            ),
        }
    combined = _combined_equity(net_of_flows=True)  # drawdown hors apports
    if len(combined) > 2:
        dd_now = float(combined.iloc[-1] / combined.cummax().iloc[-1] - 1.0)
        time_deeper = None
        if ref:
            fracs = ref["dd_time_fraction"]
            # fraction du temps que le backtest a passée à un drawdown au moins aussi profond
            keys = sorted(int(k) for k in fracs)
            time_deeper = 1.0
            for k in keys:
                if dd_now < -k / 100:
                    time_deeper = fracs[str(k)]
        out["drawdown"] = {"current": dd_now, "backtest_time_at_least_as_deep": time_deeper}
    return jsonify(out)


@app.route("/api/readiness")
def readiness():
    # Campaign qualification is separate from operational service readiness.
    database = STATE / "btcquant.db"
    if not database.exists():
        return jsonify({"status": "SOURCE_UNAVAILABLE", "campaign_qualification": "UNKNOWN"}), 503
    try:
        store = StateStore(database, initialize=False, read_only=True)
        payload = evaluate_readiness(store, persist=False)
    except Exception:
        app.logger.exception("Campaign readiness source unavailable")
        return jsonify({"status": "SOURCE_UNAVAILABLE", "campaign_qualification": "UNKNOWN"}), 503
    payload["campaign_qualification"] = "SEPARATE"
    return jsonify(payload)


@app.route("/api/yearly")
def yearly():
    """Performances annuelles du backtest (générées par scripts/make_yearly_reference.py)."""
    path = Path(__file__).parent / "yearly_reference.json"
    data = _read_json(path)
    return jsonify(data if data else {"years": []})


@app.route("/api/metrics")
def metrics():
    """Métriques live (Sharpe/Sortino/Calmar glissants) sur l'équity réalisée."""
    return jsonify(_live_metrics())


@app.route("/api/trades")
def trades():
    """Trades clôturés, filtrables par date (from/to = ISO ou YYYY-MM-DD),
    par stratégie (strategy=trend_ls_20…) et paginables (limit, défaut 12)."""
    df = _read_trades()
    if not len(df):
        return jsonify({"stats": {"n": 0, "wins": 0, "pnl": 0.0}, "rows": []})
    if "exit_ts" in df.columns:
        et = pd.to_datetime(df["exit_ts"], utc=True, errors="coerce")
        frm, to = request.args.get("from"), request.args.get("to")
        if frm:
            df = df[et >= pd.Timestamp(frm, tz="UTC")]
        if to:
            df = df[et <= pd.Timestamp(to, tz="UTC") + pd.Timedelta(days=1)]
    strat = request.args.get("strategy")
    if strat and "strategy" in df.columns:
        df = df[df["strategy"].astype(str) == strat]
    stats = {
        "n": int(len(df)),
        "wins": int((df["pnl"] > 0).sum()) if len(df) else 0,
        "pnl": float(df["pnl"].sum()) if len(df) else 0.0,
    }
    try:
        limit = min(int(request.args.get("limit", 12)), 500)
    except ValueError:
        limit = 12
    rows = df.tail(limit).iloc[::-1].to_dict("records") if len(df) else []
    return jsonify({"stats": stats, "rows": rows})


@app.route("/api/strategy/<name>")
def strategy_detail(name: str):
    """Drill-down d'un sous-système : sa position courante + ses trades + stats."""
    trend_state = _engine_state("trend", "live_state_4x.json") or {}
    slot = trend_state.get("slots", {}).get(name, {})
    out = {
        "name": name,
        "position": slot.get("position"),
        "cash": slot.get("cash"),
        "last_bar": slot.get("last_bar_ts"),
        "trades": [],
        "stats": {},
    }
    df = _read_trades()
    if len(df):
        if "strategy" in df.columns:
            df = df[df["strategy"].astype(str) == name]
        if len(df):
            out["stats"] = {
                "n": int(len(df)),
                "wins": int((df["pnl"] > 0).sum()),
                "pnl": float(df["pnl"].sum()),
                "win_rate": float((df["pnl"] > 0).mean()),
                "avg_pnl": float(df["pnl"].mean()),
                "best": float(df["pnl"].max()),
                "worst": float(df["pnl"].min()),
            }
            out["trades"] = df.tail(20).iloc[::-1].to_dict("records")
    return jsonify(out)


@app.route("/api/trades.csv")
def trades_csv():
    """Export brut des trades clôturés (téléchargement)."""
    trades = _read_trades()
    if trades.empty:
        return Response("aucun trade\n", mimetype="text/csv")
    return Response(
        trades.to_csv(index=False),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=tandem_trades.csv"},
    )


@app.route("/api/analytics")
def analytics():
    """Répartition du PnL (sous-système, direction), records, funding cumulé."""
    out = {"by_strategy": [], "by_direction": [], "records": {}, "funding_cum": []}

    breakdown, records = trade_analytics(_read_trades())
    out.update(breakdown)
    out["records"] = records

    # PnL cumulé du carry = équity − capital initial (4000) − flux reçus par
    # la poche carry (apports 40 % et transferts de rééquilibrage : sans cette
    # soustraction, chaque apport apparaîtrait comme du funding gagné)
    carry = _read_equity("carry", "equity_carry.csv")
    flows = _read_flows()
    funding_total, funding_curve = carry_funding_curve(carry, flows, PORTFOLIO.carry_capital)
    if funding_total is not None:
        out["records"]["funding_total"] = funding_total
        out["funding_cum"] = funding_curve

    # meilleur / pire jour sur l'équity combinée (apports neutralisés)
    comb = _combined_equity(net_of_flows=True)
    best_day, worst_day = best_and_worst_day(comb)
    if best_day is not None:
        out["records"]["best_day"] = best_day
        out["records"]["worst_day"] = worst_day
    return jsonify(out)


LOG_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})[,.](\d+)\s+(\w+)\s+(.*)$")
KEEP = re.compile(
    r"Entrée|Sortie|ENTRÉE|SORTIE|Funding|stop|STOP|KILL|ERROR|WARNING|démarré|kill", re.I
)


@app.route("/api/events")
def events():
    out = []
    for fname, source in (("runner.log", "trend"), ("carry.log", "carry")):
        path = STATE / fname
        if not path.exists():
            continue
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-400:]
        for line in lines:
            m = LOG_RE.match(line)
            if not m or not KEEP.search(line):
                continue
            msg = m.group(4)
            msg = re.sub(r"^\S*(btcquant\S*)\s+", "", msg)  # retire le nom du logger
            # Les logs Python utilisent le fuseau local du VPS : mktime le
            # convertit correctement en epoch UTC, y compris avec l'heure d'été.
            ts_ms = int(time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")) * 1000)
            out.append(
                {
                    "ts": m.group(1),
                    "ts_ms": ts_ms,
                    "level": m.group(3),
                    "source": source,
                    "msg": msg.strip(),
                }
            )
    out.sort(key=lambda e: e["ts"], reverse=True)
    return jsonify(out[:60])


if __name__ == "__main__":
    # préchauffage réseau en arrière-plan : les requêtes ne paient jamais la
    # latence exchange (démarré ici et pas à l'import — les tests s'en passent)
    start_warm_loop()
    host = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
    app.run(
        host=host, port=int(os.environ.get("DASHBOARD_PORT", "8666")), debug=False, threaded=True
    )
