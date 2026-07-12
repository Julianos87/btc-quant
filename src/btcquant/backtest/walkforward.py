"""Validation walk-forward : optimisation glissante + test out-of-sample.

Pour chaque pli : grid-search des paramètres sur la fenêtre d'entraînement
(in-sample), puis application du meilleur jeu sur la fenêtre suivante
(out-of-sample), indicateurs chauffés mais entrées interdites avant le début
de la fenêtre. La courbe OOS agrégée et le ratio d'efficacité
(Sharpe OOS / Sharpe IS, robuste si > 0.5) mesurent le surapprentissage.
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..strategies.base import Strategy
from .engine import BacktestEngine
from .metrics import compute_metrics

log = logging.getLogger(__name__)


@dataclass
class WalkForwardResult:
    folds: list[dict]
    oos_equity: pd.Series
    oos_metrics: dict
    efficiency: float  # Sharpe OOS moyen / Sharpe IS moyen


def _param_combos(grid: dict[str, list]) -> list[dict]:
    keys = list(grid)
    return [dict(zip(keys, values)) for values in itertools.product(*(grid[k] for k in keys))]


def walk_forward(
    strategy_cls: type[Strategy],
    df: pd.DataFrame,
    param_grid: dict[str, list],
    engine: BacktestEngine,
    train_bars: int,
    test_bars: int,
    objective: str = "sharpe",
    bars_per_year_value: float | None = None,
) -> WalkForwardResult:
    combos = _param_combos(param_grid)
    warmup = strategy_cls().warmup_bars()
    folds: list[dict] = []
    oos_curves: list[pd.Series] = []
    is_sharpes: list[float] = []
    oos_sharpes: list[float] = []

    fold_start = 0
    fold_num = 0
    while fold_start + train_bars + test_bars <= len(df):
        train = df.iloc[fold_start : fold_start + train_bars]
        test_begin = fold_start + train_bars
        # le test embarque `warmup` barres d'historique pour chauffer les
        # indicateurs, mais no_trade_before interdit toute entrée avant l'OOS
        test_ctx = df.iloc[max(0, test_begin - warmup) : test_begin + test_bars]
        oos_start_ts = df.index[test_begin]

        best_score, best_params, best_is_sharpe = -np.inf, None, np.nan
        for params in combos:
            try:
                res = engine.run(strategy_cls(**params), train)
            except ValueError:
                continue
            score = res.metrics.get(objective)
            if score is not None and np.isfinite(score) and score > best_score:
                best_score, best_params = score, params
                best_is_sharpe = res.metrics.get("sharpe", np.nan)

        if best_params is None:
            log.warning("Pli %d : aucun paramétrage valide, pli ignoré", fold_num)
            fold_start += test_bars
            continue

        oos_res = engine.run(strategy_cls(**best_params), test_ctx, no_trade_before=oos_start_ts)
        oos_equity = oos_res.equity[oos_res.equity.index >= oos_start_ts]
        folds.append(
            {
                "fold": fold_num,
                "train": (str(train.index[0]), str(train.index[-1])),
                "test": (str(oos_start_ts), str(test_ctx.index[-1])),
                "best_params": best_params,
                f"is_{objective}": best_score,
                "oos_sharpe": oos_res.metrics.get("sharpe"),
                "oos_return": oos_equity.iloc[-1] / oos_equity.iloc[0] - 1.0 if len(oos_equity) > 1 else 0.0,
                "oos_max_dd": oos_res.metrics.get("max_drawdown"),
                "oos_trades": oos_res.metrics.get("n_trades"),
            }
        )
        is_sharpes.append(best_is_sharpe)
        oos_sharpes.append(oos_res.metrics.get("sharpe", np.nan))
        oos_curves.append(oos_equity / oos_equity.iloc[0])
        fold_start += test_bars
        fold_num += 1

    if not oos_curves:
        raise ValueError("Aucun pli walk-forward exploitable (historique trop court ?)")

    # enchaînement multiplicatif des courbes OOS
    stitched = [oos_curves[0]]
    for curve in oos_curves[1:]:
        stitched.append(curve * stitched[-1].iloc[-1])
    oos_equity_full = pd.concat(stitched)
    oos_equity_full = oos_equity_full[~oos_equity_full.index.duplicated(keep="last")]

    bpy = bars_per_year_value or 8760.0
    oos_metrics = compute_metrics(oos_equity_full, [], bpy)
    mean_is = np.nanmean(is_sharpes)
    mean_oos = np.nanmean(oos_sharpes)
    efficiency = mean_oos / mean_is if mean_is and np.isfinite(mean_is) and mean_is != 0 else np.nan

    return WalkForwardResult(
        folds=folds,
        oos_equity=oos_equity_full,
        oos_metrics=oos_metrics,
        efficiency=efficiency,
    )
