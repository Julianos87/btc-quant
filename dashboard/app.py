"""Dashboard local du portefeuille 60/40 (paper trading).

Lit les états et journaux réels des runners — aucune donnée simulée.
Usage : python dashboard/app.py  (puis http://localhost:8666)
"""

from __future__ import annotations

import gzip
import hmac
import json
import math
import os
import re
import threading
import time
from pathlib import Path

import ccxt
import pandas as pd
from flask import Flask, Response, jsonify, request, send_file

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "state"
START_TIME = time.time()  # démarrage du serveur dashboard (uptime)

app = Flask(__name__)

# ── accès par lien secret (« capability URL ») ───────────────────────────────
# Le dashboard est exposé sur Internet : sans garde, équity, positions, stops
# et logs seraient publics. Plutôt qu'un mot de passe à retaper, on utilise un
# jeton long et aléatoire (DASHBOARD_TOKEN) : la première visite avec ?k=<jeton>
# pose un cookie d'un an, et plus rien n'est jamais demandé ensuite.
#
# Toutes les routes sont en lecture seule (aucune ne passe d'ordre ni ne modifie
# l'état) : le jeton protège la confidentialité, pas l'intégrité.
#
# Sans DASHBOARD_TOKEN défini, seul localhost est servi (usage en dev).
# Révocation : changer DASHBOARD_TOKEN dans .env et redémarrer le service.
AUTH_TOKEN = os.environ.get("DASHBOARD_TOKEN")
COOKIE_NAME = "tandem_key"
COOKIE_MAX_AGE = 365 * 24 * 3600


@app.before_request
def _guard():
    if not AUTH_TOKEN:
        if request.remote_addr not in ("127.0.0.1", "::1"):
            return Response("Accès refusé : définir DASHBOARD_TOKEN pour l'accès distant.", 403)
        return None
    # jeton fourni dans l'URL (première visite / lien en favori) ou déjà en cookie
    supplied = request.args.get("k") or request.cookies.get(COOKIE_NAME) or ""
    if not hmac.compare_digest(supplied, AUTH_TOKEN):
        # 404 plutôt que 401 : ne révèle pas qu'il y a quelque chose à trouver ici
        return Response("Not Found", 404)
    return None


@app.after_request
def _gzip_response(resp: Response) -> Response:
    """Compression des réponses JSON/CSV : l'équity et les bougies pèsent des
    dizaines de Ko — gzip divise par ~5-10 le transfert (net sur mobile)."""
    if (
        resp.status_code == 200
        and not resp.direct_passthrough
        and resp.mimetype in ("application/json", "text/csv", "application/javascript",
                              "image/svg+xml", "text/html")
        and (resp.content_length or 0) > 512
        and "gzip" in request.headers.get("Accept-Encoding", "")
    ):
        data = gzip.compress(resp.get_data(), compresslevel=6)
        if len(data) < (resp.content_length or 0):
            resp.set_data(data)
            resp.headers["Content-Encoding"] = "gzip"
            resp.headers["Vary"] = "Accept-Encoding"
    return resp


@app.after_request
def _persist_token(resp: Response) -> Response:
    """Après une visite avec ?k=<jeton> valide, on mémorise le jeton en cookie :
    l'utilisateur n'a plus jamais à le fournir (y compris en PWA installée)."""
    if AUTH_TOKEN and request.args.get("k") and request.cookies.get(COOKIE_NAME) != AUTH_TOKEN:
        resp.set_cookie(
            COOKIE_NAME, AUTH_TOKEN, max_age=COOKIE_MAX_AGE,
            httponly=True, samesite="Lax",
        )
    return resp

_cache: dict = {}
# les objets ccxt ne sont pas thread-safe ; le serveur Flask sert les requêtes
# en parallèle (9 fetchs simultanés au chargement de la page) → on sérialise
# tous les appels exchange derrière un verrou pour éviter les races.
_ex_lock = threading.Lock()

# ── venue : Hyperliquid depuis le 17/07/2026 ─────────────────────────────────
# fetch_ticker y recharge le contexte de TOUS les marchés (~12 s mesurés) :
# on ne l'appelle JAMAIS — prix via la dernière bougie 1m (~0,5 s), variation
# 24 h via les bougies 1h, funding via l'historique des paiements (horaires).
SYMBOL = "BTC/USDC:USDC"


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


def _cached(key: str, ttl: float, fn):
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    try:
        with _ex_lock:
            value = fn()
        _cache[key] = (now, value)
        return value
    except Exception:
        return hit[1] if hit else None


# ── fetchers réseau (appelés via _cached ; préchauffés par _warm_loop) ──────
def _get_price() -> float | None:
    c = _hl().fetch_ohlcv(SYMBOL, "1m", limit=1)
    return float(c[-1][4]) if c else None


def _get_candles_1h() -> list[list]:
    raw = _hl().fetch_ohlcv(SYMBOL, "1h", limit=200)
    return [[r[0], r[1], r[2], r[3], r[4]] for r in raw]


def _get_funding() -> dict | None:
    """Dernier paiement de funding (HORAIRE sur Hyperliquid) + annualisation."""
    since = int((time.time() - 3 * 3600) * 1000)
    hist = _hl().fetch_funding_rate_history(SYMBOL, since=since)
    if not hist:
        return None
    rate = float(hist[-1]["fundingRate"])
    return {"rate": rate, "annualized": rate * 24 * 365}


def _get_fx_eur() -> float | None:
    t = _fx_ex().fetch_ticker("EUR/USDT")
    return float(t["last"]) if t and t.get("last") else None


#     (clé cache, TTL endpoint, TTL préchauffage, fetch)
_WARM_JOBS = [
    ("price",   30,   20, lambda: _timed("api", _get_price)),
    ("ohlcv1h", 300,  240, _get_candles_1h),
    ("funding", 300,  240, _get_funding),
    ("fx_eur",  3600, 3000, _get_fx_eur),
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


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _age_seconds(path: Path) -> float | None:
    return time.time() - path.stat().st_mtime if path.exists() else None


# ── cache de parsing des CSV d'état ──────────────────────────────────────────
# Un chargement de page = 9 endpoints, dont la plupart relisent les mêmes CSV
# (l'équity trend fait des centaines de Ko) : on ne reparse un fichier que
# lorsque (mtime, taille) a changé — le runner n'écrit qu'une fois par minute.
_parse_cache: dict = {}


def _file_key(path: Path) -> tuple | None:
    try:
        st = path.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _parsed(path: Path, parser):
    key = _file_key(path)
    hit = _parse_cache.get(str(path))
    if hit is not None and hit[0] == key:
        return hit[1]
    value = parser(path)
    _parse_cache[str(path)] = (key, value)
    return value


def _parse_equity(path: Path) -> pd.Series:
    """Lecture tolérante : le runner peut être en train d'appendre — une
    dernière ligne tronquée ne doit pas faire un 500 sur tout l'endpoint."""
    if not path.exists():
        return pd.Series(dtype=float)
    try:
        df = pd.read_csv(path, on_bad_lines="skip")
        ts = pd.to_datetime(df["ts"], utc=True, format="ISO8601", errors="coerce")
        s = pd.Series(df["equity"].values, index=ts)
        return s[s.index.notna()].sort_index()
    except Exception:
        return pd.Series(dtype=float)


def _read_equity(name: str) -> pd.Series:
    return _parsed(STATE / name, _parse_equity)


def _parse_trades(path: Path) -> pd.DataFrame:
    """Même tolérance pour trades.csv (append concurrent par le runner)."""
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path, on_bad_lines="skip")
    except Exception:
        return pd.DataFrame()


def _read_trades() -> pd.DataFrame:
    return _parsed(STATE / "trades.csv", _parse_trades)


def _parse_flows(path: Path) -> pd.DataFrame:
    """Journal des flux externes (apports, transferts entre poches) écrit par
    scripts/rebalance.py. Colonnes : ts, kind, trend_flow, carry_flow."""
    empty = pd.DataFrame(columns=["ts", "kind", "trend_flow", "carry_flow"])
    if not path.exists():
        return empty
    try:
        df = pd.read_csv(path, on_bad_lines="skip")
        df["ts"] = pd.to_datetime(df["ts"], utc=True, format="ISO8601", errors="coerce")
        # une ligne tronquée peut garder un timestamp lisible mais des montants
        # absents (NaN) : on n'accepte que les lignes complètes
        ok = df["ts"].notna() & df["trend_flow"].notna() & df["carry_flow"].notna()
        return df[ok].sort_values("ts")
    except Exception:
        return empty


def _read_flows() -> pd.DataFrame:
    return _parsed(STATE / "flows.csv", _parse_flows)


def _deposits_total(flows: pd.DataFrame) -> float:
    """Somme des apports (les transferts entre poches s'annulent)."""
    if not len(flows):
        return 0.0
    return float((flows["trend_flow"] + flows["carry_flow"]).sum())


def _net_of_flows(s: pd.Series, flows: pd.DataFrame, col: str) -> pd.Series:
    """Soustrait d'une série d'équity le cumul des flux externes de sa poche
    (col = "trend_flow" ou "carry_flow"). À appliquer sur les échantillons
    NATIFS, avant tout resampling : les moteurs sont arrêtés pendant un
    rebalance, donc le premier échantillon qui suit un flux l'inclut déjà —
    la série nette reste continue, sans faux creux ni faux gain."""
    if not len(s) or not len(flows):
        return s
    # agrégation par timestamp : deux flux au même instant (apport + transfert
    # dans le même run de rebalance) feraient échouer le reindex (labels dupliqués)
    cum = flows.groupby("ts")[col].sum().cumsum()
    return s - cum.reindex(s.index, method="ffill").fillna(0.0)


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
    key = (net_of_flows,
           _file_key(STATE / "equity_trend.csv"),
           _file_key(STATE / "equity_carry.csv"),
           _file_key(STATE / "flows.csv"))
    hit = _parse_cache.get(f"combined_{net_of_flows}")
    if hit is not None and hit[0] == key:
        return hit[1]
    trend = _read_equity("equity_trend.csv")
    carry = _read_equity("equity_carry.csv")
    if not len(trend) or not len(carry):
        combined = pd.Series(dtype=float)
    else:
        if net_of_flows:
            flows = _read_flows()
            trend = _net_of_flows(trend, flows, "trend_flow")
            carry = _net_of_flows(carry, flows, "carry_flow")
        t = trend.resample("1min").last().ffill()
        c = carry.resample("1min").last().ffill()
        idx = t.index.intersection(c.index)
        combined = (t[idx] + c[idx]).dropna()
    _parse_cache[f"combined_{net_of_flows}"] = (key, combined)
    return combined


def _live_metrics() -> dict:
    """Sharpe / Sortino / Calmar glissants + drawdown sur l'équity combinée
    (rendements journaliers), apports neutralisés. Renvoie None quand
    l'historique est trop court."""
    comb = _combined_equity(net_of_flows=True)
    out = {"sharpe": None, "sortino": None, "calmar": None, "cagr": None,
           "max_dd": None, "cur_dd": None, "vol_annual": None, "days": 0}
    if len(comb) < 3:
        return out
    daily = comb.resample("1D").last().dropna()
    out["days"] = int((comb.index[-1] - comb.index[0]).total_seconds() // 86400)
    max_dd = float((comb / comb.cummax() - 1.0).min())
    out["max_dd"] = max_dd
    out["cur_dd"] = float(comb.iloc[-1] / comb.cummax().iloc[-1] - 1.0)
    years = (comb.index[-1] - comb.index[0]).total_seconds() / (365.25 * 86400)
    if years > 0:
        cagr = (comb.iloc[-1] / comb.iloc[0]) ** (1.0 / years) - 1.0
        out["cagr"] = float(cagr)
        if max_dd < 0:
            out["calmar"] = float(cagr / abs(max_dd))
    rets = daily.pct_change().dropna()
    # moins de 14 rendements journaliers : un Sharpe annualisé n'a aucun sens
    # (le premier jour affichait 37) — on rend None, le front affiche « — »
    if len(rets) >= 14 and rets.std() > 0:
        sq = math.sqrt(365)
        out["sharpe"] = float(rets.mean() / rets.std() * sq)
        out["vol_annual"] = float(rets.std() * sq)
        downside = rets[rets < 0]
        if len(downside) > 1 and downside.std() > 0:
            out["sortino"] = float(rets.mean() / downside.std() * sq)
    return out


@app.route("/")
def index():
    return send_file(Path(__file__).parent / "index.html")


ICON_SVG = """<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'>
<defs><linearGradient id='g' x1='0' y1='0' x2='1' y2='1'>
<stop offset='0' stop-color='#3d8bec'/><stop offset='1' stop-color='#6b4de0'/></linearGradient></defs>
<rect width='100' height='100' rx='22' fill='url(#g)'/>
<text x='50' y='68' font-size='52' text-anchor='middle' fill='white'
 font-family='system-ui' font-weight='700'>&#8383;</text></svg>"""


@app.route("/icon.svg")
def icon():
    return Response(ICON_SVG, mimetype="image/svg+xml")


@app.route("/manifest.json")
def manifest():
    return jsonify(
        {
            "name": "Tandem",
            "short_name": "Tandem",
            "description": "Portefeuille systématique 60/40 — suivi paper trading",
            # le jeton reste dans l'URL de lancement : si le cookie de la PWA
            # expire ou est purgé, l'app se ré-authentifie seule au démarrage
            "start_url": f"/?k={AUTH_TOKEN}" if AUTH_TOKEN else "/",
            "display": "standalone",
            "background_color": "#0a0b0d",
            "theme_color": "#0a0b0d",
            "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}],
        }
    )


SERVICE_WORKER = """
// Service worker btcquant : cache offline léger + support notifications.
const CACHE = 'btcq-v1';
self.addEventListener('install', e => { self.skipWaiting(); });
self.addEventListener('activate', e => { e.waitUntil(clients.claim()); });
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  // les API passent toujours par le réseau ; le shell est mis en cache
  if (url.pathname.startsWith('/api/')) return;
  e.respondWith(
    fetch(e.request).then(r => {
      const copy = r.clone();
      caches.open(CACHE).then(c => c.put(e.request, copy)).catch(()=>{});
      return r;
    }).catch(() => caches.match(e.request))
  );
});
// affichage d'une notification demandée par la page (postMessage)
self.addEventListener('message', e => {
  if (e.data && e.data.type === 'notify') {
    self.registration.showNotification(e.data.title, {
      body: e.data.body, icon: '/icon.svg', badge: '/icon.svg', tag: e.data.tag,
    });
  }
});
self.addEventListener('notificationclick', e => {
  e.notification.close();
  e.waitUntil(clients.matchAll({type:'window'}).then(cs => {
    for (const c of cs) if ('focus' in c) return c.focus();
    return clients.openWindow('/');
  }));
});
"""


@app.route("/sw.js")
def service_worker():
    return Response(SERVICE_WORKER, mimetype="application/javascript",
                    headers={"Cache-Control": "no-cache"})


@app.route("/api/summary")
def summary():
    price = _cached("price", 30, lambda: _timed("api", _get_price))
    candles = _cached("ohlcv1h", 300, _get_candles_1h) or []
    funding = _cached("funding", 300, _get_funding)
    # taux EUR/USDT pour l'affichage optionnel en euros (1 EUR = X USDT)
    eur_usd = _cached("fx_eur", 3600, _get_fx_eur)

    # variation 24 h : clôture d'il y a 24 bougies 1h vs prix courant
    change_24h = None
    if price and len(candles) >= 25:
        ref = float(candles[-25][4])
        if ref > 0:
            change_24h = price / ref - 1.0

    trend_state = _read_json(STATE / "live_state_4x.json") or {}
    carry_state = _read_json(STATE / "carry_state.json") or {}
    trend_eq = _read_equity("equity_trend.csv")
    carry_eq = _read_equity("equity_carry.csv")

    trend_age = _age_seconds(STATE / "live_state_4x.json")
    carry_age = _age_seconds(STATE / "carry_state.json")

    trend_equity = float(trend_eq.iloc[-1]) if len(trend_eq) else sum(
        s.get("cash", 0.0) for s in trend_state.get("slots", {}).values()
    )
    carry_equity = float(carry_state.get("equity", 0.0))
    total = trend_equity + carry_equity
    initial_total = 10_000.0
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
        row = {"name": name, "cash": s.get("cash"), "state": "FLAT", "last_bar": s.get("last_bar_ts")}
        if pos:
            direction = pos.get("direction", 1)
            upnl = None
            if price:
                upnl = direction * pos["qty"] * (price - pos["entry_price"])
            row.update(
                state="LONG" if direction == 1 else "SHORT",
                qty=pos["qty"], entry=pos["entry_price"], stop=pos["stop_price"],
                bars=pos.get("bars_held"), upnl=upnl,
            )
        slots.append(row)

    # funding horaire sur Hyperliquid : prochain paiement à l'heure pile
    next_funding = (int(time.time() // 3600) + 1) * 3600 * 1000

    # exposition brute / levier effectif (somme des notionnels / équity totale)
    trend_notional = 0.0
    for sl in slots:
        if sl.get("qty") and price:
            trend_notional += abs(sl["qty"]) * price
    carry_notional = carry_equity * 3.0 if carry_state.get("in_position") else 0.0
    gross_notional = trend_notional + carry_notional
    leverage = gross_notional / total if total else 0.0

    # prochaine bougie 4h (le trend décide à la clôture des bougies 4h UTC)
    now_ts = pd.Timestamp.now(tz="UTC")
    next_bar = now_ts.floor("4h") + pd.Timedelta(hours=4)

    return jsonify(
        {
            "now": pd.Timestamp.now(tz="UTC").isoformat(),
            "mode": "PAPER",
            "btc": {
                "price": price,
                "change24h": change_24h,
            },
            "funding": {
                "rate": funding["rate"] if funding else None,
                "annualized": funding["annualized"] if funding else None,
                "next_ts": next_funding,
            },
            "trend": {
                "alive": trend_age is not None and trend_age < 240,
                "age_s": trend_age,
                "equity": trend_equity,
                "initial": 6000.0,
                "halted": trend_state.get("halted", False),
                "daily_lockout": trend_state.get("daily_lockout", False),
                "peak_equity": trend_state.get("peak_equity"),
                "slots": slots,
            },
            "carry": {
                "alive": carry_age is not None and carry_age < 900,
                "age_s": carry_age,
                "equity": carry_equity,
                "initial": 4000.0,
                "in_position": carry_state.get("in_position", False),
                "last_funding_ts": carry_state.get("last_funding_ts"),
            },
            "totals": {
                "equity": total,
                "initial": initial_total,
                "deposits": deposits,
                "pnl": total - invested,
                "pnl_pct": total / invested - 1.0,
                "day_pnl_pct": day_pnl_pct,
                "allocation_trend": trend_equity / total if total else None,
                "gross_notional": gross_notional,
                "leverage": leverage,
            },
            "health": {
                "server_uptime_s": time.time() - START_TIME,
                "api_latency_ms": _cache.get("api_ms"),
                "next_bar_ts": int(next_bar.timestamp() * 1000),
            },
            "fx": {"eur_usd": eur_usd},
        }
    )


@app.route("/api/equity")
def equity():
    trend = _read_equity("equity_trend.csv")
    carry = _read_equity("equity_carry.csv")

    def pack(s: pd.Series, max_points: int = 1500):
        if len(s) > max_points:
            s = s.resample("5min").last().dropna()
        return [[int(ts.timestamp() * 1000), round(float(v), 2)] for ts, v in s.items()]

    buyhold = _cached("buyhold", 600, _get_buyhold) or []

    return jsonify({
        "trend": pack(trend), "carry": pack(carry),
        "combined": pack(combined), "buyhold": buyhold,
    })


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


@app.route("/api/price")
def price_chart():
    """Dernières ~200 bougies 1h + positions ouvertes (entrée/stop) du trend."""
    candles = _cached("ohlcv1h", 300, _get_candles_1h) or []
    trend_state = _read_json(STATE / "live_state_4x.json") or {}
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
    return jsonify({"candles": candles, "positions": positions})


@app.route("/api/conformity")
def conformity():
    """Réalisé (paper) vs attendu (backtest) — la carte « Est-ce normal ? »."""
    ref_path = Path(__file__).parent / "backtest_reference.json"
    ref = json.loads(ref_path.read_text(encoding="utf-8")) if ref_path.exists() else None

    out = {"reference": ref, "realized": None, "drawdown": None}
    df = _read_trades()
    if len(df):
        n = len(df)
        wins = int((df["pnl"] > 0).sum())
        # intervalle de Wilson à 95 % sur le win rate
        p, z = wins / n, 1.96
        denom = 1 + z*z/n
        center = (p + z*z/(2*n)) / denom
        half = z * ((p*(1-p)/n + z*z/(4*n*n)) ** 0.5) / denom
        out["realized"] = {
            "n": n, "wins": wins, "win_rate": p,
            "win_rate_ci": [max(0.0, center-half), min(1.0, center+half)],
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


# ── critères go/no-go de la phase paper (fixés à froid, le 14/07/2026) ──────
# Le passage au testnet ne se décide PAS au feeling : chaque critère est
# mesurable et évalué automatiquement. Modifier ces seuils = décision de
# protocole, à documenter dans le journal du projet.
READINESS = {
    "min_days": 90,        # durée minimale de paper trading
    "min_trades": 30,      # échantillon minimal de trades clôturés
    "min_uptime": 0.95,    # part des jours avec données d'équity
    "max_dd_floor": -0.45, # DD paper toléré (backtest : -53 % ; au-delà de -45 %, discussion)
}


@app.route("/api/readiness")
def readiness():
    """Évaluation automatique des critères de passage paper → testnet."""
    ref_path = Path(__file__).parent / "backtest_reference.json"
    ref = _read_json(ref_path) or {}
    checks = []

    def add(key, label, status, value, target, note=""):
        checks.append({"key": key, "label": label, "status": status,
                       "value": value, "target": target, "note": note})

    trend_eq = _read_equity("equity_trend.csv")
    days = 0.0
    if len(trend_eq) > 1:
        days = (trend_eq.index[-1] - trend_eq.index[0]).total_seconds() / 86400
    add("days", "Durée du paper trading",
        "ok" if days >= READINESS["min_days"] else "pending",
        f"{days:.0f} j", f"≥ {READINESS['min_days']} j")

    if len(trend_eq) > 1:
        days_with_data = trend_eq.resample("1D").count()
        uptime = float((days_with_data > 0).mean())
        add("uptime", "Présence des moteurs",
            "ok" if uptime >= READINESS["min_uptime"] else "warn",
            f"{uptime:.0%}", f"≥ {READINESS['min_uptime']:.0%}",
            "" if uptime >= READINESS["min_uptime"] else "trous dans l'historique d'équity")
    else:
        add("uptime", "Présence des moteurs", "pending", "—", f"≥ {READINESS['min_uptime']:.0%}")

    df = _read_trades()
    n = int(len(df))
    add("trades", "Trades clôturés",
        "ok" if n >= READINESS["min_trades"] else "pending",
        str(n), f"≥ {READINESS['min_trades']}")

    # win rate compatible avec le backtest (IC de Wilson à 95 %)
    if n >= 20 and ref.get("win_rate") is not None:
        wins = int((df["pnl"] > 0).sum())
        p, z = wins / n, 1.96
        denom = 1 + z * z / n
        center = (p + z * z / (2 * n)) / denom
        half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / denom
        lo, hi = center - half, center + half
        compatible = lo <= ref["win_rate"] <= hi
        add("winrate", "Win rate vs backtest",
            "ok" if compatible else "warn",
            f"{p:.0%} [{max(0, lo):.0%}–{min(1, hi):.0%}]", f"∋ {ref['win_rate']:.0%}",
            "" if compatible else "écart significatif — comprendre avant d'avancer")
    else:
        add("winrate", "Win rate vs backtest", "pending", f"{n} trades",
            "≥ 20 trades requis")

    # drawdown paper dans l'enveloppe tolérée (apports neutralisés)
    comb = _combined_equity(net_of_flows=True)
    if len(comb) > 2:
        max_dd = float((comb / comb.cummax() - 1.0).min())
        add("drawdown", "Drawdown maximal paper",
            "ok" if max_dd >= READINESS["max_dd_floor"] else "warn",
            f"{max_dd:.1%}", f"≥ {READINESS['max_dd_floor']:.0%}",
            "" if max_dd >= READINESS["max_dd_floor"] else "plus profond que l'enveloppe tolérée")
    else:
        add("drawdown", "Drawdown maximal paper", "pending", "—",
            f"≥ {READINESS['max_dd_floor']:.0%}")

    trend_state = _read_json(STATE / "live_state_4x.json") or {}
    halted = bool(trend_state.get("halted", False))
    add("killswitch", "Kill-switch jamais déclenché",
        "warn" if halted else "ok", "déclenché" if halted else "non", "non")

    n_ok = sum(1 for c in checks if c["status"] == "ok")
    ready = all(c["status"] == "ok" for c in checks)
    return jsonify({"ready": ready, "n_ok": n_ok, "n_total": len(checks),
                    "checks": checks, "thresholds": READINESS})


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
    trend_state = _read_json(STATE / "live_state_4x.json") or {}
    slot = trend_state.get("slots", {}).get(name, {})
    out = {"name": name, "position": slot.get("position"), "cash": slot.get("cash"),
           "last_bar": slot.get("last_bar_ts"), "trades": [], "stats": {}}
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
    path = STATE / "trades.csv"
    if not path.exists():
        return Response("aucun trade\n", mimetype="text/csv")
    return Response(
        path.read_text(encoding="utf-8"),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=tandem_trades.csv"},
    )


@app.route("/api/analytics")
def analytics():
    """Répartition du PnL (sous-système, direction), records, funding cumulé."""
    out = {"by_strategy": [], "by_direction": [], "records": {}, "funding_cum": []}

    df = _read_trades()
    if len(df):
        for key, field in (("by_strategy", "strategy"), ("by_direction", "direction")):
            grp = df.groupby(field)
            out[key] = [
                {
                    "name": str(k).replace("trend_ls_", "D"),
                    "n": int(len(g)),
                    "wins": int((g["pnl"] > 0).sum()),
                    "pnl": float(g["pnl"].sum()),
                }
                for k, g in grp
            ]
        best = df.loc[df["pnl"].idxmax()]
        worst = df.loc[df["pnl"].idxmin()]
        # plus longue série gagnante / perdante (ordre chronologique)
        chrono = df.sort_values("exit_ts")["pnl"]
        def longest(win: bool) -> int:
            best_run = run = 0
            for v in chrono:
                hit = (v > 0) if win else (v <= 0)
                run = run + 1 if hit else 0
                best_run = max(best_run, run)
            return best_run
        out["records"] = {
            "biggest_win": float(best["pnl"]),
            "biggest_win_strat": str(best["strategy"]).replace("trend_ls_", "D"),
            "biggest_loss": float(worst["pnl"]),
            "biggest_loss_strat": str(worst["strategy"]).replace("trend_ls_", "D"),
            "longest_win_streak": longest(True),
            "longest_loss_streak": longest(False),
        }

    # PnL cumulé du carry = équity − capital initial (4000) − flux reçus par
    # la poche carry (apports 40 % et transferts de rééquilibrage : sans cette
    # soustraction, chaque apport apparaîtrait comme du funding gagné)
    carry = _read_equity("equity_carry.csv")
    flows = _read_flows()
    if len(carry) > 1:
        base = 4000.0
        cum = carry - base
        if len(flows):
            carry_flows = flows.groupby("ts")["carry_flow"].sum().cumsum()
            cum = cum - carry_flows.reindex(cum.index, method="ffill").fillna(0.0)
        out["records"]["funding_total"] = float(cum.iloc[-1])
        if len(cum) > 400:
            cum = cum.resample("1h").last().dropna()
        out["funding_cum"] = [[int(ts.timestamp() * 1000), round(float(v), 2)] for ts, v in cum.items()]

    # meilleur / pire jour sur l'équity combinée (apports neutralisés)
    comb = _combined_equity(net_of_flows=True)
    if len(comb) > 2:
        daily = comb.resample("1D").last().pct_change().dropna()
        if len(daily):
            out["records"]["best_day"] = float(daily.max())
            out["records"]["worst_day"] = float(daily.min())
    return jsonify(out)


LOG_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})[,.](\d+)\s+(\w+)\s+(.*)$")
KEEP = re.compile(r"Entrée|Sortie|ENTRÉE|SORTIE|Funding|stop|STOP|KILL|ERROR|WARNING|démarré|kill", re.I)


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
            out.append({
                "ts": m.group(1),
                "ts_ms": ts_ms,
                "level": m.group(3),
                "source": source,
                "msg": msg.strip(),
            })
    out.sort(key=lambda e: e["ts"], reverse=True)
    return jsonify(out[:60])


if __name__ == "__main__":
    # préchauffage réseau en arrière-plan : les requêtes ne paient jamais la
    # latence exchange (démarré ici et pas à l'import — les tests s'en passent)
    threading.Thread(target=_warm_loop, daemon=True).start()
    host = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
    app.run(host=host, port=int(os.environ.get("DASHBOARD_PORT", "8666")),
            debug=False, threaded=True)
