"""Backtest des stratégies activées dans un profil YAML + portefeuille combiné.

Produit dans reports/ : courbes d'équity (PNG), liste des trades (CSV).
"""

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btcquant.console import enable_utf8_output

enable_utf8_output()

import pandas as pd

from btcquant.backtest import BacktestEngine
from btcquant.backtest.metrics import compute_metrics, format_metrics
from btcquant.carry import add_funding_columns, load_funding
from btcquant.config import (
    build_strategies,
    execution_config_from_config,
    load_config,
    risk_from_config,
)
from btcquant.data import TIMEFRAME_TO_PANDAS, load_ohlcv, resample
from btcquant.domain import ExecutionSimulator
from btcquant.indicators import bars_per_year
from btcquant.risk import RiskConfig

log = logging.getLogger(__name__)


def with_real_funding(df, symbol_perp: str, timeframe: str, refresh: bool):
    """Ajoute les colonnes `funding_rate` (P&L) et `funding` (filtre d'entrée)
    à partir du funding réel 8 h. Sur échec de chargement, retourne df inchangé
    → le moteur retombe sur la constante plate et le filtre reste neutre."""
    try:
        fund = load_funding(symbol_perp, data_dir=ROOT / "data", refresh=refresh)
    except Exception as e:  # cache absent / réseau : on ne bloque pas le backtest
        log.warning("Funding réel indisponible (%s), constante plate utilisée", e)
        return df
    return add_funding_columns(df, fund, TIMEFRAME_TO_PANDAS[timeframe])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default=ROOT / "environments" / "dev" / "config.yaml",
    )
    parser.add_argument(
        "--no-refresh", action="store_true", help="utilise uniquement le cache local"
    )
    parser.add_argument(
        "--execution-profile",
        choices=("normal", "stress"),
        help="remplace execution.simulation.profile pour comparer les scénarios",
    )
    parser.add_argument(
        "--no-reports",
        action="store_true",
        help="affiche les métriques sans écrire de CSV ou de graphique",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)
    cfg = load_config(args.config)
    if args.execution_profile is not None:
        cfg["execution"]["simulation"]["profile"] = args.execution_profile
    risk = risk_from_config(cfg)
    reports = ROOT / "reports"
    if not args.no_reports:
        reports.mkdir(exist_ok=True)

    base = load_ohlcv(
        cfg["exchange"],
        cfg["symbol"],
        cfg["data"]["base_timeframe"],
        cfg["data"]["since"],
        data_dir=ROOT / cfg["data"]["dir"],
        refresh=not args.no_refresh,
    )
    print(
        f"Données : {len(base)} bougies {cfg['data']['base_timeframe']}, "
        f"{base.index[0]} → {base.index[-1]}\n"
    )
    profile = cfg.get("execution", {}).get("simulation", {}).get("profile", "legacy")
    print(f"Profil d'exécution : {profile}\n")

    curves: dict[str, pd.Series] = {}
    trade_count = 0
    timeframes: list[str] = []
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
            symbol_perp = f"{cfg['symbol']}:{cfg['quote_currency']}"
            df = with_real_funding(df, symbol_perp, strategy.timeframe, refresh=not args.no_refresh)
        fee_rate = cfg["costs"]["perp_fee_rate"] if is_perp else cfg["costs"]["fee_rate"]
        engine = BacktestEngine(
            risk=slot_risk,
            funding_rate_8h=cfg["costs"].get("funding_rate_8h", 0.0) if is_perp else 0.0,
            allow_short=is_perp,
            execution_simulator=ExecutionSimulator(execution_config_from_config(cfg, fee_rate)),
        )
        result = engine.run(strategy, df)
        curves[strategy.name] = result.equity
        trade_count += len(result.trades)
        timeframes.append(strategy.timeframe)

        print(
            f"═══ {strategy.name} ({strategy.timeframe}, {market}, {fraction:.0%} du capital) ═══"
        )
        print(format_metrics(result.metrics))
        print()

        if not args.no_reports:
            result.trades_frame().to_csv(reports / f"trades_{strategy.name}.csv", index=False)

    # portefeuille combiné : somme des courbes d'équity alignées sur l'index le plus fin
    combined = pd.concat(curves.values(), axis=1, sort=True).ffill().dropna().sum(axis=1)
    finest_bpy = max(bars_per_year(timeframe) for timeframe in timeframes)
    combo_metrics = compute_metrics(combined, [], finest_bpy)
    print("═══ Portefeuille combiné ═══")
    print(
        format_metrics(
            {
                **combo_metrics,
                "n_trades": trade_count,
                "win_rate": float("nan"),
                "profit_factor": float("nan"),
                "avg_win_pct": float("nan"),
                "avg_loss_pct": float("nan"),
                "avg_bars_held": float("nan"),
                "exposure": float("nan"),
            }
        )
    )

    if not args.no_reports:
        _plot_equity(curves, combined, cfg["symbol"], reports)
        print(f"\nRapports écrits dans {reports}")


def _plot_equity(curves, combined, symbol: str, reports: Path) -> None:
    """Graphe optionnel : matplotlib appartient au groupe `research`, opt-in.

    L'import était en tête de module, si bien que la commande de backtest
    documentée au README échouait sur un `uv sync` standard. Le backtest et ses
    métriques ne dépendent pas du graphe : son absence ne doit pas empêcher de
    rejouer une stratégie.
    """

    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("\n(graphe ignoré : matplotlib absent — `uv sync --group research` pour l'obtenir)")
        return

    fig, ax = plt.subplots(figsize=(12, 6))
    for name, eq in curves.items():
        (eq / eq.iloc[0]).plot(ax=ax, label=name)
    (combined / combined.iloc[0]).plot(ax=ax, label="portefeuille", linewidth=2, color="black")
    ax.set_yscale("log")
    ax.set_title(f"Équity normalisée — {symbol}")
    ax.legend()
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(reports / "equity_curves.png", dpi=130)


if __name__ == "__main__":
    main()
