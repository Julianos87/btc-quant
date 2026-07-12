"""Backtest des stratégies activées dans config.yaml + portefeuille combiné.

Produit dans reports/ : courbes d'équity (PNG), liste des trades (CSV).
"""

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from btcquant.backtest import BacktestEngine
from btcquant.backtest.metrics import compute_metrics, format_metrics
from btcquant.carry import load_funding
from btcquant.config import build_strategies, load_config, risk_from_config
from btcquant.data import TIMEFRAME_TO_PANDAS, load_ohlcv, resample
from btcquant.indicators import bars_per_year
from btcquant.risk import RiskConfig

log = logging.getLogger(__name__)


def with_real_funding(df, symbol_perp: str, timeframe: str, refresh: bool):
    """Ajoute une colonne `funding_rate` par barre à partir du funding réel 8h
    (somme des paiements par barre ; positif = les longs paient). Sur échec de
    chargement, retourne df inchangé → le moteur retombe sur la constante plate."""
    try:
        fund = load_funding(symbol_perp, data_dir=ROOT / "data", refresh=refresh)
    except Exception as e:  # cache absent / réseau : on ne bloque pas le backtest
        log.warning("Funding réel indisponible (%s), constante plate utilisée", e)
        return df
    per_bar = fund.resample(TIMEFRAME_TO_PANDAS[timeframe], label="left", closed="left").sum()
    out = df.copy()
    out["funding_rate"] = per_bar.reindex(out.index).fillna(0.0)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=ROOT / "config.yaml")
    parser.add_argument("--no-refresh", action="store_true", help="utilise uniquement le cache local")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)
    cfg = load_config(args.config)
    risk = risk_from_config(cfg)
    reports = ROOT / "reports"
    reports.mkdir(exist_ok=True)

    base = load_ohlcv(
        cfg["exchange"], cfg["symbol"], cfg["data"]["base_timeframe"],
        cfg["data"]["since"], data_dir=ROOT / cfg["data"]["dir"],
        refresh=not args.no_refresh,
    )
    print(f"Données : {len(base)} bougies {cfg['data']['base_timeframe']}, "
          f"{base.index[0]} → {base.index[-1]}\n")

    curves: dict[str, pd.Series] = {}
    for strategy, fraction, market in build_strategies(cfg):
        df = base if strategy.timeframe == cfg["data"]["base_timeframe"] else resample(
            base, TIMEFRAME_TO_PANDAS[strategy.timeframe]
        )
        slot_risk = RiskConfig(**{**risk.__dict__, "initial_capital": risk.initial_capital * fraction})
        is_perp = market == "perp"
        if is_perp:
            symbol_perp = f"{cfg['symbol']}:{cfg['quote_currency']}"
            df = with_real_funding(df, symbol_perp, strategy.timeframe, refresh=not args.no_refresh)
        engine = BacktestEngine(
            fee_rate=cfg["costs"]["perp_fee_rate"] if is_perp else cfg["costs"]["fee_rate"],
            slippage_bps=cfg["costs"]["slippage_bps"],
            risk=slot_risk,
            funding_rate_8h=cfg["costs"].get("funding_rate_8h", 0.0) if is_perp else 0.0,
            allow_short=is_perp,
        )
        result = engine.run(strategy, df)
        curves[strategy.name] = result.equity

        print(f"═══ {strategy.name} ({strategy.timeframe}, {market}, {fraction:.0%} du capital) ═══")
        print(format_metrics(result.metrics))
        print()

        result.trades_frame().to_csv(reports / f"trades_{strategy.name}.csv", index=False)

    # portefeuille combiné : somme des courbes d'équity alignées sur l'index le plus fin
    combined = pd.concat(curves.values(), axis=1, sort=True).ffill().dropna().sum(axis=1)
    finest_bpy = max(bars_per_year(s.timeframe) for s, _, _ in build_strategies(cfg))
    combo_metrics = compute_metrics(combined, [], finest_bpy)
    print("═══ Portefeuille combiné ═══")
    print(format_metrics({**combo_metrics, "n_trades": sum(len(pd.read_csv(reports / f'trades_{n}.csv')) for n in curves),
                          "win_rate": float("nan"), "profit_factor": float("nan"),
                          "avg_win_pct": float("nan"), "avg_loss_pct": float("nan"),
                          "avg_bars_held": float("nan"), "exposure": float("nan")}))

    fig, ax = plt.subplots(figsize=(12, 6))
    for name, eq in curves.items():
        (eq / eq.iloc[0]).plot(ax=ax, label=name)
    (combined / combined.iloc[0]).plot(ax=ax, label="portefeuille", linewidth=2, color="black")
    ax.set_yscale("log")
    ax.set_title(f"Équity normalisée — {cfg['symbol']}")
    ax.legend()
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(reports / "equity_curves.png", dpi=130)
    print(f"\nRapports écrits dans {reports}")


if __name__ == "__main__":
    main()
