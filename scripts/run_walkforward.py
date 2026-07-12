"""Validation walk-forward : optimisation glissante, mesure out-of-sample.

Un ratio d'efficacité (Sharpe OOS / Sharpe IS) > 0.5 indique des paramètres
robustes ; proche de 0 ou négatif = surapprentissage.
"""

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btcquant.backtest import BacktestEngine, walk_forward
from btcquant.backtest.metrics import format_metrics
from btcquant.config import load_config, risk_from_config
from btcquant.data import TIMEFRAME_TO_PANDAS, load_ohlcv, resample
from btcquant.indicators import bars_per_year
from btcquant.strategies import STRATEGY_REGISTRY

# Grilles volontairement restreintes : chaque paramètre supplémentaire
# augmente le risque de surapprentissage, pas la robustesse.
GRIDS = {
    "trend_swing": {
        "ema_fast": [30, 50],
        "ema_slow": [150, 200],
        "donchian": [40, 55],
        "atr_mult": [2.5, 3.5],
    },
    "intraday_breakout": {
        "lookback_high": [24, 48],
        "atr_mult": [2.0, 3.0],
        "volume_mult": [1.0, 1.3],
    },
}
# train/test en barres, par timeframe de stratégie
WINDOWS = {
    "trend_swing": (4380, 1095),        # 4h : 2 ans / 6 mois
    "intraday_breakout": (17520, 4380),  # 1h : 2 ans / 6 mois
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("strategy", choices=list(GRIDS))
    parser.add_argument("--config", default=ROOT / "config.yaml")
    parser.add_argument("--no-refresh", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)
    cfg = load_config(args.config)
    spec = cfg["strategies"][args.strategy]
    timeframe = spec["timeframe"]

    base = load_ohlcv(
        cfg["exchange"], cfg["symbol"], cfg["data"]["base_timeframe"],
        cfg["data"]["since"], data_dir=ROOT / cfg["data"]["dir"],
        refresh=not args.no_refresh,
    )
    df = base if timeframe == cfg["data"]["base_timeframe"] else resample(
        base, TIMEFRAME_TO_PANDAS[timeframe]
    )

    engine = BacktestEngine(
        fee_rate=cfg["costs"]["fee_rate"],
        slippage_bps=cfg["costs"]["slippage_bps"],
        risk=risk_from_config(cfg),
    )
    train_bars, test_bars = WINDOWS[args.strategy]
    print(f"Walk-forward {args.strategy} ({timeframe}) : train {train_bars} barres, "
          f"test {test_bars} barres, grille {GRIDS[args.strategy]}\n")

    result = walk_forward(
        STRATEGY_REGISTRY[args.strategy], df, GRIDS[args.strategy], engine,
        train_bars=train_bars, test_bars=test_bars,
        objective="sharpe", bars_per_year_value=bars_per_year(timeframe),
    )

    for fold in result.folds:
        print(f"Pli {fold['fold']}: test {fold['test'][0][:10]} → {fold['test'][1][:10]}  "
              f"params {fold['best_params']}  "
              f"OOS: ret {fold['oos_return']:+.1%}, sharpe {fold['oos_sharpe']:.2f}, "
              f"dd {fold['oos_max_dd']:.1%}, {fold['oos_trades']} trades")

    print("\n═══ Out-of-sample agrégé ═══")
    print(format_metrics(result.oos_metrics))
    print(f"\nEfficacité walk-forward (Sharpe OOS / IS) : {result.efficiency:.2f}"
          f"  {'✔ robuste' if result.efficiency and result.efficiency > 0.5 else '⚠ prudence'}")


if __name__ == "__main__":
    main()
