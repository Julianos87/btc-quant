"""Dashboard local du portefeuille 60/40 (paper trading).

Lit les états et journaux réels des runners — aucune donnée simulée.
Usage : python dashboard/app.py  (puis http://localhost:8666)
"""

from __future__ import annotations

import hmac
import json
import os
import re
import time
from pathlib import Path

import ccxt
import pandas as pd
from flask import Flask, Response, jsonify, request, send_file

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "state"

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


def _cached(key: str, ttl: float, fn):
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    try:
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
    path = STATE / name
    if not path.exists():
        return pd.Series(dtype=float)
    df = pd.read_csv(path)
    ts = pd.to_datetime(df["ts"], utc=True, format="ISO8601")
    return pd.Series(df["equity"].values, index=ts).sort_index()


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


@app.route("/api/summary")
def summary():
    ticker = _cached("ticker", 30, lambda: _spot().fetch_ticker("BTC/USDT"))
    funding = _cached("funding", 300, lambda: _perp().fetch_funding_rate("BTC/USDT:USDT"))

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
            },
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

    combined = pd.Series(dtype=float)
    if len(trend) and len(carry):
        t = trend.resample("1min").last().ffill()
        c = carry.resample("1min").last().ffill()
        idx = t.index.intersection(c.index)
        combined = (t[idx] + c[idx]).dropna()

    return jsonify({"trend": pack(trend), "carry": pack(carry), "combined": pack(combined)})


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
    tpath = STATE / "trades.csv"
    if tpath.exists():
        df = pd.read_csv(tpath)
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


@app.route("/api/trades")
def trades():
    path = STATE / "trades.csv"
    if not path.exists():
        return jsonify({"stats": {"n": 0, "wins": 0, "pnl": 0.0}, "rows": []})
    df = pd.read_csv(path)
    stats = {
        "n": int(len(df)),
        "wins": int((df["pnl"] > 0).sum()),
        "pnl": float(df["pnl"].sum()),
    }
    rows = df.tail(12).iloc[::-1].to_dict("records")
    return jsonify({"stats": stats, "rows": rows})


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
            out.append({"ts": m.group(1), "level": m.group(3), "source": source, "msg": msg.strip()})
    out.sort(key=lambda e: e["ts"], reverse=True)
    return jsonify(out[:60])


if __name__ == "__main__":
    host = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
    app.run(host=host, port=int(os.environ.get("DASHBOARD_PORT", "8666")), debug=False)
