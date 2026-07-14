"""Dashboard local du portefeuille 60/40 (paper trading).

Lit les états et journaux réels des runners — aucune donnée simulée.
Usage : python dashboard/app.py  (puis http://localhost:8666)
"""

from __future__ import annotations

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

# ── authentification HTTP Basic (obligatoire dès que le dashboard est exposé
# au-delà de localhost). Définir DASHBOARD_USER / DASHBOARD_PASSWORD dans
# l'environnement ; sans mot de passe défini, seul localhost est servi. ──────
AUTH_USER = os.environ.get("DASHBOARD_USER", "admin")
AUTH_PASS = os.environ.get("DASHBOARD_PASSWORD")


@app.before_request
def _guard():
    if AUTH_PASS:
        auth = request.authorization
        ok = (
            auth is not None
            and auth.username is not None and auth.password is not None
            and hmac.compare_digest(auth.username, AUTH_USER)
            and hmac.compare_digest(auth.password, AUTH_PASS)
        )
        if not ok:
            return Response(
                "Authentification requise", 401,
                {"WWW-Authenticate": 'Basic realm="btcquant"'},
            )
    elif request.remote_addr not in ("127.0.0.1", "::1"):
        return Response("Accès refusé : définir DASHBOARD_PASSWORD pour l'accès distant.", 403)

_cache: dict = {}
# les objets ccxt ne sont pas thread-safe ; le serveur Flask sert les requêtes
# en parallèle (7 fetchs simultanés au chargement de la page) → on sérialise
# tous les appels exchange derrière un verrou pour éviter les races.
_ex_lock = threading.Lock()


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


def _spot():
    if "spot_ex" not in _cache:
        _cache["spot_ex"] = ccxt.binance({"enableRateLimit": True, "timeout": 15_000})
    return _cache["spot_ex"]


def _perp():
    if "perp_ex" not in _cache:
        _cache["perp_ex"] = ccxt.binanceusdm({"enableRateLimit": True, "timeout": 15_000})
    return _cache["perp_ex"]


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _age_seconds(path: Path) -> float | None:
    return time.time() - path.stat().st_mtime if path.exists() else None


def _read_equity(name: str) -> pd.Series:
    """Lecture tolérante : le runner peut être en train d'appendre — une
    dernière ligne tronquée ne doit pas faire un 500 sur tout l'endpoint."""
    path = STATE / name
    if not path.exists():
        return pd.Series(dtype=float)
    try:
        df = pd.read_csv(path, on_bad_lines="skip")
        ts = pd.to_datetime(df["ts"], utc=True, format="ISO8601", errors="coerce")
        s = pd.Series(df["equity"].values, index=ts)
        return s[s.index.notna()].sort_index()
    except Exception:
        return pd.Series(dtype=float)


def _read_trades() -> pd.DataFrame:
    """Même tolérance pour trades.csv (append concurrent par le runner)."""
    path = STATE / "trades.csv"
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path, on_bad_lines="skip")
    except Exception:
        return pd.DataFrame()


def _timed(label: str, fn):
    """Exécute fn en mesurant sa latence (ms), stockée dans le cache."""
    t0 = time.time()
    try:
        return fn()
    finally:
        _cache[f"{label}_ms"] = round((time.time() - t0) * 1000, 1)


def _combined_equity() -> pd.Series:
    """Équity combinée trend + carry, alignée à la minute."""
    trend = _read_equity("equity_trend.csv")
    carry = _read_equity("equity_carry.csv")
    if not len(trend) or not len(carry):
        return pd.Series(dtype=float)
    t = trend.resample("1min").last().ffill()
    c = carry.resample("1min").last().ffill()
    idx = t.index.intersection(c.index)
    return (t[idx] + c[idx]).dropna()


def _live_metrics() -> dict:
    """Sharpe / Sortino / Calmar glissants + drawdown sur l'équity combinée
    (rendements journaliers). Renvoie None quand l'historique est trop court."""
    comb = _combined_equity()
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
    if len(rets) >= 2 and rets.std() > 0:
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
            "name": "BTC-QUANT",
            "short_name": "BTC-QUANT",
            "description": "Portefeuille systématique 60/40 — suivi paper trading",
            "start_url": "/",
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
    ticker = _cached("ticker", 30, lambda: _timed("api", lambda: _spot().fetch_ticker("BTC/USDT")))
    funding = _cached("funding", 300, lambda: _perp().fetch_funding_rate("BTC/USDT:USDT"))
    # taux EUR/USDT pour l'affichage optionnel en euros (1 EUR = X USDT)
    fx = _cached("fx_eur", 3600, lambda: _spot().fetch_ticker("EUR/USDT"))
    eur_usd = float(fx["last"]) if fx and fx.get("last") else None

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

    # PnL du jour (UTC) sur l'équity combinée
    day_pnl_pct = None
    if len(trend_eq) and len(carry_eq):
        today = pd.Timestamp.now(tz="UTC").normalize()
        t0 = trend_eq[trend_eq.index >= today]
        c0 = carry_eq[carry_eq.index >= today]
        if len(t0) and len(c0):
            start_day = float(t0.iloc[0]) + float(c0.iloc[0])
            if start_day > 0:
                day_pnl_pct = total / start_day - 1.0

    price = float(ticker["last"]) if ticker else None
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

    next_funding = funding.get("fundingTimestamp") if funding else None

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
                "change24h": float(ticker.get("percentage") or 0) / 100 if ticker else None,
            },
            "funding": {
                "rate": float(funding["fundingRate"]) if funding and funding.get("fundingRate") is not None else None,
                "annualized": float(funding["fundingRate"]) * 3 * 365 if funding and funding.get("fundingRate") is not None else None,
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
                "pnl": total - initial_total,
                "pnl_pct": total / initial_total - 1.0,
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

    combined = _combined_equity()

    # buy & hold : prix BTC sur la même fenêtre (le front normalise en base
    # 100). Timeframe adaptatif pour que TOUT l'historique tienne dans les
    # 1000 bougies d'un seul appel (1h ≈ 41 j, 4h ≈ 166 j, 1d au-delà).
    buyhold = []
    if len(combined):
        start_ms = int(combined.index[0].timestamp() * 1000)
        span_h = (time.time() * 1000 - start_ms) / 3_600_000
        bh_tf = "1h" if span_h <= 950 else "4h" if span_h <= 3_800 else "1d"
        def fetch_bh():
            raw = _spot().fetch_ohlcv("BTC/USDT", bh_tf, since=start_ms, limit=1000)
            return [[r[0], round(r[4], 2)] for r in raw]
        buyhold = _cached(f"buyhold_{bh_tf}", 600, fetch_bh) or []

    return jsonify({
        "trend": pack(trend), "carry": pack(carry),
        "combined": pack(combined), "buyhold": buyhold,
    })


@app.route("/api/price")
def price_chart():
    """Dernières ~200 bougies 1h + positions ouvertes (entrée/stop) du trend."""
    def fetch():
        raw = _spot().fetch_ohlcv("BTC/USDT", "1h", limit=200)
        return [[r[0], r[1], r[2], r[3], r[4]] for r in raw]  # ts, o, h, l, c

    candles = _cached("ohlcv1h", 300, fetch) or []
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
    trend_eq = _read_equity("equity_trend.csv")
    carry_eq = _read_equity("equity_carry.csv")
    if len(trend_eq) > 2 and len(carry_eq) > 2:
        t = trend_eq.resample("1min").last().ffill()
        c = carry_eq.resample("1min").last().ffill()
        idx = t.index.intersection(c.index)
        combined = (t[idx] + c[idx]).dropna()
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

    # drawdown paper dans l'enveloppe tolérée
    comb = _combined_equity()
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
        headers={"Content-Disposition": "attachment; filename=btcquant_trades.csv"},
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

    # funding cumulé du carry = équity − capital initial (4000)
    carry = _read_equity("equity_carry.csv")
    if len(carry) > 1:
        base = 4000.0
        cum = (carry - base)
        if len(cum) > 400:
            cum = cum.resample("1h").last().dropna()
        out["funding_cum"] = [[int(ts.timestamp() * 1000), round(float(v), 2)] for ts, v in cum.items()]
        out["records"]["funding_total"] = float(carry.iloc[-1] - base)

    # meilleur / pire jour sur l'équity combinée
    trend = _read_equity("equity_trend.csv")
    if len(trend) > 2 and len(carry) > 2:
        t = trend.resample("1min").last().ffill()
        c = carry.resample("1min").last().ffill()
        idx = t.index.intersection(c.index)
        comb = (t[idx] + c[idx]).dropna()
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
    host = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
    app.run(host=host, port=int(os.environ.get("DASHBOARD_PORT", "8666")), debug=False)
