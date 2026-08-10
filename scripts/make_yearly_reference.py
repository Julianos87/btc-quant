"""Génère dashboard/yearly_reference.json : performances annuelles du backtest.

Rejoue le portefeuille 60/40 tel qu'il tourne en paper (trend 4x via
environments/paper/config.yaml + carry 3x, entrée 3 % / sortie 0 % / lissage 14 j) et
extrait, par année civile : rendement portefeuille, trend, carry, BTC
buy & hold et max drawdown intra-année. Le JSON est servi par le
dashboard (/api/yearly) — à régénérer après tout changement de config.

Usage : python scripts/make_yearly_reference.py [--no-refresh]
"""

import argparse
import hashlib
import json
import logging
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btcquant.console import enable_utf8_output

enable_utf8_output()

import pandas as pd

from btcquant.backtest import BacktestEngine
from btcquant.carry import add_funding_columns, backtest_carry, load_funding
from btcquant.config import (
    build_strategies,
    carry_policy_from_config,
    execution_config_from_config,
    load_config,
    risk_from_config,
)
from btcquant.provenance import quantitative_source_sha256
from btcquant.data import TIMEFRAME_TO_PANDAS, load_ohlcv, resample
from btcquant.domain import ExecutionSimulator
from btcquant.risk import RiskConfig

log = logging.getLogger(__name__)


def _portable_bytes(path: Path) -> bytes:
    """Normalise les fichiers texte suivis entre Windows et Linux."""

    return path.read_bytes().replace(b"\r\n", b"\n")


def _sha256(path: Path) -> str:
    return hashlib.sha256(_portable_bytes(path)).hexdigest()


def trend_equity(cfg: dict, base: pd.DataFrame, refresh: bool) -> pd.Series:
    """Équity du moteur trend : somme des slots du profil paper."""
    risk = risk_from_config(cfg)
    curves = []
    for strategy, fraction, market in build_strategies(cfg):
        df = (
            base
            if strategy.timeframe == cfg["data"]["base_timeframe"]
            else resample(base, TIMEFRAME_TO_PANDAS[strategy.timeframe])
        )
        slot_risk = RiskConfig(
            **{**risk.__dict__, "initial_capital": risk.initial_capital * fraction}
        )
        is_perp = market == "perp"
        if is_perp:
            try:
                fund = load_funding(
                    f"{cfg['symbol']}:{cfg['quote_currency']}",
                    data_dir=ROOT / "data",
                    refresh=refresh,
                )
                df = add_funding_columns(df, fund, TIMEFRAME_TO_PANDAS[strategy.timeframe])
            except Exception as e:
                log.warning("Funding réel indisponible (%s), constante plate utilisée", e)
        fee_rate = cfg["costs"]["perp_fee_rate"] if is_perp else cfg["costs"]["fee_rate"]
        engine = BacktestEngine(
            risk=slot_risk,
            funding_rate_8h=cfg["costs"].get("funding_rate_8h", 0.0) if is_perp else 0.0,
            allow_short=is_perp,
            execution_simulator=ExecutionSimulator(execution_config_from_config(cfg, fee_rate)),
        )
        curves.append(engine.run(strategy, df).equity)
    return pd.concat(curves, axis=1, sort=True).ffill().dropna().sum(axis=1)


def yearly_stats(daily: pd.Series) -> dict[int, dict]:
    """Rendement et max DD par année civile (bord = clôture de l'année précédente)."""
    out: dict[int, dict] = {}
    for year, seg in daily.groupby(daily.index.year):
        prev = daily[daily.index.year < year]
        start = float(prev.iloc[-1]) if len(prev) else float(seg.iloc[0])
        dd = float((seg / seg.cummax() - 1.0).min())
        out[int(year)] = {"ret": float(seg.iloc[-1]) / start - 1.0, "max_dd": dd}
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default=ROOT / "environments" / "paper" / "config.yaml",
    )
    parser.add_argument(
        "--no-refresh", action="store_true", help="utilise uniquement le cache local"
    )
    args = parser.parse_args()
    refresh = not args.no_refresh

    logging.basicConfig(level=logging.WARNING)
    cfg = load_config(args.config)
    carry_policy = carry_policy_from_config(cfg)

    base = load_ohlcv(
        cfg["exchange"],
        cfg["symbol"],
        cfg["data"]["base_timeframe"],
        cfg["data"]["since"],
        data_dir=ROOT / cfg["data"]["dir"],
        refresh=refresh,
    )
    print(f"Données : {len(base)} bougies, {base.index[0]} -> {base.index[-1]}")

    trend = trend_equity(cfg, base, refresh).resample("1D").last().dropna()
    funding = load_funding(data_dir=ROOT / "data", refresh=refresh)
    carry = (
        backtest_carry(
            funding,
            initial_capital=carry_policy.capital,
            leverage=carry_policy.leverage,
            enter_ann=carry_policy.enter_ann,
            exit_ann=carry_policy.exit_ann,
            smooth_days=carry_policy.smooth_days,
            fee_rate=carry_policy.fee_rate,
            slippage_bps=carry_policy.slippage_bps,
            borrow_rate_ann=carry_policy.borrow_rate_ann,
        )["equity"]
        .resample("1D")
        .last()
        .dropna()
    )

    idx = trend.index.intersection(carry.index)
    portfolio = trend[idx] + carry[idx]
    btc = base["close"].resample("1D").last().dropna()[idx[0] :]

    stats = {
        "portfolio": yearly_stats(portfolio),
        "trend": yearly_stats(trend[idx]),
        "carry": yearly_stats(carry[idx]),
        "btc": yearly_stats(btc),
    }
    current_year = date.today().year
    years = []
    for year in sorted(stats["portfolio"]):
        years.append(
            {
                "year": year,
                "portfolio": stats["portfolio"][year]["ret"],
                "trend": stats["trend"][year]["ret"],
                "carry": stats["carry"][year]["ret"],
                "btc": stats["btc"].get(year, {}).get("ret"),
                "max_dd": stats["portfolio"][year]["max_dd"],
                "partial": year == current_year
                or year == int(idx[0].year)
                and idx[0].dayofyear > 5,
            }
        )

    out = {
        "schema_version": 2,
        "generated": date.today().isoformat(),
        "profile": "portefeuille 60/40 — trend 4x (Donchian 20/55/100) + carry 3x, funding réel",
        "span": [str(idx[0].date()), str(idx[-1].date())],
        "provenance": {
            "base_git_commit": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
            ).strip(),
            "source_tree_sha256": quantitative_source_sha256(Path(__file__)),
            "config": {
                "path": Path(args.config).resolve().relative_to(ROOT).as_posix(),
                "sha256": _sha256(Path(args.config)),
            },
            "base_data_sha256": _sha256(
                ROOT
                / cfg["data"]["dir"]
                / (
                    f"{cfg['exchange']}_{cfg['symbol'].replace('/', '-')}_"
                    f"{cfg['data']['base_timeframe']}.csv"
                )
            ),
            "base_rows": len(base),
        },
        "years": years,
    }
    dest = ROOT / "dashboard" / "yearly_reference.json"
    dest.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\n{'année':>6} {'portefeuille':>13} {'trend':>8} {'carry':>8} {'BTC':>8} {'max DD':>8}")
    for y in years:
        print(
            f"{y['year']:>6} {y['portfolio']:>+12.1%} {y['trend']:>+7.1%} {y['carry']:>+7.1%} "
            f"{(y['btc'] if y['btc'] is not None else float('nan')):>+7.1%} {y['max_dd']:>7.1%}"
            + ("  (partielle)" if y["partial"] else "")
        )
    print(f"\nÉcrit : {dest}")


if __name__ == "__main__":
    main()
