"""Vérifie qu'un capital donné passe les minimums Binance Futures / Spot.

Le backtest suppose des quantités parfaitement fractionnaires ; en réel,
Binance impose un notionnel minimum et un pas de quantité par marché. Ce
script calcule la taille d'ordre typique du trend (règle de risque réelle,
ATR courant des données en cache) et la jambe carry, puis les confronte aux
limites du marché. À lancer avant tout passage testnet/réel, et à chaque
changement de capital.

Usage : python scripts/check_min_sizes.py [--capital 10000 ...] [--no-refresh]
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import ccxt

from btcquant.config import load_config, risk_from_config
from btcquant.data import load_ohlcv, resample
from btcquant.indicators import atr
from btcquant.risk import RiskConfig, position_size

TARGET_TREND = 0.60  # répartition du portefeuille
N_SLOTS = 3
ATR_MULT = 3.0  # stop trend = 3×ATR
CARRY_LEVERAGE = 3.0


def market_limits(symbol: str, futures: bool) -> dict:
    ex = ccxt.binanceusdm() if futures else ccxt.binance()
    ex.load_markets()
    m = ex.market(symbol)
    lim = m.get("limits", {})
    return {
        "min_qty": (lim.get("amount") or {}).get("min"),
        "min_notional": (lim.get("cost") or {}).get("min"),
        "step": m.get("precision", {}).get("amount"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capital", type=float, nargs="+", default=[1000, 2200, 3400, 5000, 10000])
    parser.add_argument("--config", default=ROOT / "config_4x.yaml")
    parser.add_argument("--no-refresh", action="store_true")
    args = parser.parse_args()

    cfg = load_config(args.config)
    base = load_ohlcv(
        cfg["exchange"],
        cfg["symbol"],
        cfg["data"]["base_timeframe"],
        cfg["data"]["since"],
        data_dir=ROOT / "data",
        refresh=not args.no_refresh,
    )
    df4h = resample(base, "4h")
    cur_atr = float(atr(df4h, 14).iloc[-1])
    price = float(df4h["close"].iloc[-1])
    stop_dist = ATR_MULT * cur_atr
    print(
        f"BTC {price:,.0f} $ | ATR(14) 4h {cur_atr:,.0f} $ | stop 3×ATR = "
        f"{stop_dist:,.0f} $ ({stop_dist / price:.1%} du prix)\n"
    )

    try:
        perp = market_limits(f"{cfg['symbol']}:{cfg['quote_currency']}", futures=True)
        spot = market_limits(cfg["symbol"], futures=False)
    except Exception as e:
        print(f"⚠ Limites Binance indisponibles hors ligne ({e}) — valeurs connues utilisées.")
        perp = {"min_qty": 0.001, "min_notional": 100.0, "step": 3}
        spot = {"min_qty": 0.00001, "min_notional": 5.0, "step": 5}
    print(f"Perp BTCUSDT : min qty {perp['min_qty']} BTC, min notionnel {perp['min_notional']} $")
    print(
        f"Spot BTC/USDT : min qty {spot['min_qty']} BTC, min notionnel {spot['min_notional']} $\n"
    )

    risk = risk_from_config(cfg)
    for cap in args.capital:
        slot_equity = cap * TARGET_TREND / N_SLOTS
        slot_risk = RiskConfig(**{**risk.__dict__, "initial_capital": slot_equity})
        qty = position_size(slot_equity, price, price - stop_dist, None, slot_risk)
        notional = qty * price
        ok_qty = perp["min_qty"] is None or qty >= perp["min_qty"]
        ok_notional = perp["min_notional"] is None or notional >= perp["min_notional"]
        trend_ok = ok_qty and ok_notional

        carry_notional = cap * (1 - TARGET_TREND) * CARRY_LEVERAGE
        carry_qty = carry_notional / price
        c_ok = (
            carry_qty >= (perp["min_qty"] or 0)
            and carry_qty >= (spot["min_qty"] or 0)
            and carry_notional >= (perp["min_notional"] or 0)
        )

        print(f"── capital {cap:,.0f} $ ──")
        print(
            f"  trend/slot ({slot_equity:,.0f} $) : qty {qty:.5f} BTC, notionnel {notional:,.0f} $ "
            f"→ {'✓ passe' if trend_ok else '✗ SOUS LE MINIMUM'}"
        )
        print(
            f"  carry ({cap * (1 - TARGET_TREND):,.0f} $ à {CARRY_LEVERAGE:.0f}x) : "
            f"qty {carry_qty:.5f} BTC, notionnel {carry_notional:,.0f} $ "
            f"→ {'✓ passe' if c_ok else '✗ SOUS LE MINIMUM'}"
        )
        if not (trend_ok and c_ok):
            print("  ⚠ certains ordres seraient REFUSÉS par Binance à ce capital")
        print()


if __name__ == "__main__":
    main()
