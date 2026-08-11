"""Référence dorée du moteur de backtest, sans données externes.

Pourquoi ce fichier existe. La seule protection anti-régression du moteur était
`audit/baseline_reference.json`, vérifié par un script hors pytest et lié par
hash à des CSV de plusieurs mégaoctets **non versionnés**. Sur un poste neuf ou
en CI, ce contrôle est désactivé : le moteur pouvait donc changer de résultat
sans qu'aucun test ne s'en aperçoive.

Ici, la série de prix est calculée par une formule fermée — aucun tirage
aléatoire, aucun fichier, aucun réseau — et les chiffres attendus sont figés.
Toute modification du séquencement, du sizing, des frais, du modèle de fill ou
du filtre funding fait échouer ce test avec un écart chiffré.

Mettre à jour les constantes ci-dessous est autorisé, mais c'est une décision
explicite : elle doit être justifiée dans le message de commit.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btcquant.backtest.engine import BacktestEngine
from btcquant.carry import add_funding_columns
from btcquant.domain.execution import ExecutionConfig, ExecutionSimulator
from btcquant.risk import RiskConfig
from btcquant.strategies.trend_ls import TrendLS

BARS = 3000
START = "2020-01-01"


def synthetic_ohlcv(bars: int = BARS) -> pd.DataFrame:
    """Marché déterministe : dérive lente + deux cycles + bruit périodique.

    Les périodes sont volontairement incommensurables pour produire des
    tendances franches, des retournements et des phases hachées — de quoi
    déclencher entrées longues, entrées courtes, stops et sorties de régime.
    """

    index = pd.date_range(START, periods=bars, freq="4h", tz="UTC")
    closes = []
    for i in range(bars):
        drift = 0.00012 * i
        cycle = 0.50 * math.sin(i / 110.0) + 0.25 * math.sin(i / 31.0 + 1.1)
        chop = 0.07 * math.sin(i / 4.1 + 0.5)
        closes.append(10_000.0 * math.exp(drift + cycle + chop))
    close = pd.Series(closes, index=index)
    open_ = close.shift(1).fillna(close.iloc[0])
    amplitude = 0.004 + 0.003 * pd.Series(
        [abs(math.sin(i / 11.0)) for i in range(bars)], index=index
    )
    high = pd.concat([open_, close], axis=1).max(axis=1) * (1.0 + amplitude)
    low = pd.concat([open_, close], axis=1).min(axis=1) * (1.0 - amplitude)
    volume = pd.Series([1_000.0 + 250.0 * math.sin(i / 17.0) for i in range(bars)], index=index)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume})


def synthetic_funding(bars: int = BARS) -> pd.Series:
    """Paiements 8 h alternant régimes positifs et négatifs, aux heures réelles."""

    index = pd.date_range(START, periods=max(1, bars // 2), freq="8h", tz="UTC")
    values = [0.0001 + 0.0009 * math.sin(i / 53.0) for i in range(len(index))]
    return pd.Series(values, index=index)


def _engine() -> BacktestEngine:
    return BacktestEngine(
        risk=RiskConfig(
            initial_capital=10_000.0,
            risk_per_trade=0.02,
            max_position_pct=0.95,
            vol_target_annual=0.80,
            max_drawdown_halt=0.60,
            daily_loss_limit=0.12,
            max_leverage=2.0,
        ),
        allow_short=True,
        execution_simulator=ExecutionSimulator(ExecutionConfig(fee_rate=0.0005, slippage_bps=5.0)),
    )


def _strategy() -> TrendLS:
    strategy = TrendLS(donchian=55, adx_min=20, funding_long_max=0.0008)
    strategy.name = "golden_trend_ls_55"
    return strategy


def run_golden():
    frame = add_funding_columns(synthetic_ohlcv(), synthetic_funding(), "4h")
    return _engine().run(_strategy(), frame)


# ── valeurs figées ──────────────────────────────────────────────────────────
# Régénération délibérée :
#   python -c "import sys;sys.path[:0]=['.','src'];import tests.test_backtest_golden as g;print(g.report())"
#
# ATTENTION : ce sont des chiffres de FIXTURE, pas une performance. Le marché
# synthétique est une somme de sinusoïdes, donc parfaitement prévisible : un
# suiveur de tendance y obtient un Sharpe absurde. Ces valeurs ne doivent jamais
# être citées comme un résultat du système — leur seul rôle est de changer si le
# moteur change.
GOLDEN = {
    "n_trades": 35,
    "final_equity": 35165.382985206736,
    "total_return": 2.5165382985206737,
    "max_drawdown": -0.05897862271816923,
    "sharpe": 4.268685227732425,
    "n_long": 17,
    "n_short": 18,
    "n_stops": 35,
    "gross_fees": 106.46008808849129,
}


def report() -> dict:
    """Recalcule les valeurs de référence (aide à la mise à jour délibérée)."""

    result = run_golden()
    trades = result.trades
    return {
        "n_trades": len(trades),
        "final_equity": float(result.equity.iloc[-1]),
        "total_return": float(result.metrics["total_return"]),
        "max_drawdown": float(result.metrics["max_drawdown"]),
        "sharpe": float(result.metrics["sharpe"]),
        "n_long": sum(t.direction == 1 for t in trades),
        "n_short": sum(t.direction == -1 for t in trades),
        "n_stops": sum(t.exit_reason == "stop" for t in trades),
        "gross_fees": sum(abs(t.qty) * t.exit_price * 0.0005 for t in trades),
    }


@pytest.mark.parametrize("key", sorted(GOLDEN))
def test_golden_backtest_is_unchanged(key: str):
    """Un écart ici signale une modification du comportement du moteur.

    Ce n'est pas nécessairement une régression, mais ce ne doit jamais être
    une surprise.
    """
    observed = report()[key]
    expected = GOLDEN[key]
    if isinstance(expected, int):
        assert observed == expected, f"{key} : {observed} != {expected} (référence)"
    else:
        assert observed == pytest.approx(expected, rel=1e-9), (
            f"{key} : {observed!r} != {expected!r} (référence)"
        )


def test_golden_run_is_reproducible_within_the_process():
    """Deux exécutions successives doivent produire exactement la même courbe :
    aucune session d'exécution ne doit fuir d'un run à l'autre."""
    first = run_golden()
    second = run_golden()
    pd.testing.assert_series_equal(first.equity, second.equity)
    assert [t.__dict__ for t in first.trades] == [t.__dict__ for t in second.trades]


def test_golden_scenario_exercises_both_directions_and_stops():
    """Une référence dorée ne vaut que si elle traverse les chemins qu'elle
    prétend protéger."""
    values = report()
    assert values["n_long"] > 0 and values["n_short"] > 0
    assert values["n_stops"] > 0
    assert values["n_trades"] >= 15, "scénario trop pauvre pour détecter une régression"


def test_every_exit_is_a_stop_as_in_the_real_backtest():
    """Constat mesuré sur l'historique réel : 100 % des sorties de `trend_ls`
    sont des stops — le retournement de régime EMA n'a jamais le temps de se
    déclencher. Le scénario doré doit reproduire cette réalité, sans quoi il
    protégerait un chemin que la production n'emprunte pas."""
    reasons = {trade.exit_reason for trade in run_golden().trades}
    assert reasons == {"stop"}


def test_funding_filter_actually_bites_in_the_golden_scenario():
    """Sans filtre funding, le scénario doit produire des trades différents :
    sinon le test doré ne protégerait pas ce chemin."""
    frame = add_funding_columns(synthetic_ohlcv(), synthetic_funding(), "4h")
    unfiltered = TrendLS(donchian=55, adx_min=20, funding_long_max=None)
    unfiltered.name = "golden_no_funding_filter"
    without = _engine().run(unfiltered, frame)
    assert len(without.trades) != GOLDEN["n_trades"]
