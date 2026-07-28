"""Parité de RÉSULTAT entre le moteur de backtest et le runner paper.

Le dépôt testait déjà la parité du NOYAU de décision (`test_decision_kernel`) :
à barre et position identiques, les deux chemins produisent les mêmes
événements. Ce n'était pas suffisant. Entre la décision et le trade enregistré
s'intercalent deux implémentations distinctes du dimensionnement, de la
comptabilité des frais et de la construction du trade — `BacktestEngine` d'un
côté, `LiveRunner` de l'autre. Rien ne vérifiait qu'elles arrivaient au même
résultat, alors que c'est précisément l'hypothèse sur laquelle repose la
comparaison paper-vs-backtest du protocole de qualification.

Les deux moteurs appliquent la même chronologie : décision sur la clôture de t,
exécution à l'ouverture de t+1 pour le backtest et au prix de marché observé
juste après cette ouverture pour le runner. Le rejeu injecte exactement
`open[t+1]` comme observation paper et teste aussi des gaps non nuls.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btcquant.backtest.engine import BacktestEngine
from btcquant.domain.execution import ExecutionConfig, ExecutionSimulator
from btcquant.execution.broker import PaperBroker
from btcquant.execution.runner import LiveRunner, StrategySlot
from btcquant.risk import RiskConfig
from btcquant.strategies.base import Position, Strategy

FEE = 0.0005
SLIPPAGE_BPS = 5.0
CAPITAL = 10_000.0
STOP_DISTANCE = 5_000.0  # très large : aucun stop ne se déclenche dans ce scénario


class ScriptedStrategy(Strategy):
    """Entrées et sorties dictées par un script, sans indicateur.

    Le but n'est pas de tester une stratégie mais les deux moteurs : le signal
    doit donc être parfaitement reproductible et indépendant du prix.
    """

    name = "parity"
    timeframe = "4h"

    @staticmethod
    def default_params() -> dict:
        # {index de barre de décision: direction} et durée de détention
        return {"entries": {}, "hold_bars": 3}

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["_bar"] = np.arange(len(out))
        return out

    def entry_signal(self, row: pd.Series) -> int:
        return int(self.params["entries"].get(int(row["_bar"]), 0))

    def initial_stop(self, row: pd.Series, entry_price: float, direction: int = 1) -> float:
        return entry_price - direction * STOP_DISTANCE

    def exit_signal(self, row: pd.Series, position: Position) -> bool:
        return position.bars_held >= int(self.params["hold_bars"])

    def warmup_bars(self) -> int:
        return 2


def continuous_frame(bars: int = 60) -> pd.DataFrame:
    """Série sans gap : l'ouverture d'une barre est la clôture de la précédente.

    C'est ce qui rend les deux prix d'exécution comparables. Les mèches restent
    étroites pour qu'aucun stop intrabar ne se déclenche d'un côté seulement.
    """

    index = pd.date_range("2026-01-01", periods=bars, freq="4h", tz="UTC")
    closes = 30_000.0 + 400.0 * np.sin(np.arange(bars) / 6.0) + 12.0 * np.arange(bars)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    return pd.DataFrame(
        {
            "open": opens,
            "high": np.maximum(opens, closes) + 20.0,
            "low": np.minimum(opens, closes) - 20.0,
            "close": closes,
            "volume": np.full(bars, 5_000.0),
        },
        index=index,
    )


def gapped_frame(bars: int = 60) -> pd.DataFrame:
    """Série dont chaque ouverture t+1 diffère volontairement de close[t]."""

    frame = continuous_frame(bars)
    gaps = np.resize(np.array([0.012, -0.009, 0.006, -0.004]), bars)
    frame["open"] = frame["close"].shift(1).fillna(frame["open"].iloc[0]) * (1.0 + gaps)
    frame["high"] = frame[["open", "close"]].max(axis=1) + 20.0
    frame["low"] = frame[["open", "close"]].min(axis=1) - 20.0
    return frame


def _risk() -> RiskConfig:
    # `vol_target_annual=None` et un plafond de notionnel généreux rendent la
    # contrainte de risque par trade seule active. Le dimensionnement ne dépend
    # alors plus du prix d'entrée, et les deux moteurs doivent trouver la même
    # quantité au dernier chiffre.
    return RiskConfig(
        initial_capital=CAPITAL,
        risk_per_trade=0.01,
        max_position_pct=0.95,
        vol_target_annual=None,
        max_drawdown_halt=0.99,
        daily_loss_limit=None,
        max_leverage=3.0,
    )


def _simulator() -> ExecutionSimulator:
    return ExecutionSimulator(ExecutionConfig(fee_rate=FEE, slippage_bps=SLIPPAGE_BPS))


class ReplayVenue:
    """Venue déterministe : ni réseau, ni funding."""

    payments_per_day = 3

    def __init__(self, frame: pd.DataFrame) -> None:
        self.frame = frame

    @property
    def payments_per_year(self) -> int:
        return self.payments_per_day * 365

    def last_price(self) -> float:
        return float(self.frame["close"].iloc[-1])

    def fetch_ohlcv(self, timeframe: str, limit: int = 1000) -> list[list]:
        raise AssertionError("_fetch_frame est remplacé dans ce test")

    def funding_rate_8h(self) -> float:
        return 0.0

    def funding_history(self, days: float) -> pd.Series:
        return pd.Series(dtype=float)

    def funding_history_since(self, since: pd.Timestamp) -> pd.Series:
        return pd.Series(dtype=float)


class ReplayClock:
    """Horloge pilotée par le test, calée sur la barre en cours de traitement."""

    def __init__(self, start: pd.Timestamp) -> None:
        self.now = start

    def utc_now(self) -> pd.Timestamp:
        return self.now

    def time(self) -> float:
        return self.now.timestamp()

    def monotonic(self) -> float:
        return self.now.timestamp()


def run_backtest(frame: pd.DataFrame, strategy: ScriptedStrategy):
    engine = BacktestEngine(
        risk=_risk(),
        allow_short=True,
        execution_simulator=_simulator(),
    )
    return engine.run(strategy, frame)


def run_runner(frame: pd.DataFrame, strategy: ScriptedStrategy, tmp_path, monkeypatch):
    """Rejoue le runner barre par barre, comme le ferait `run_forever`."""

    slot = StrategySlot(strategy, 1.0, CAPITAL)
    clock = ReplayClock(frame.index[0])
    runner = LiveRunner(
        [slot],
        PaperBroker(simulator=_simulator()),
        _risk(),
        "binance",
        "BTC/USDT",
        tmp_path / "btcquant.db",
        venue=ReplayVenue(frame),
        clock=clock,
        notifier=lambda _message: True,
    )
    warmup = strategy.warmup_bars()
    for index in range(warmup, len(frame)):
        visible = frame.iloc[: index + 1]
        clock.now = frame.index[index] + pd.Timedelta(hours=4, seconds=30)
        price = float(frame["close"].iloc[index])
        monkeypatch.setattr(runner, "_fetch_frame", lambda _strategy, v=visible: v)
        runner._update_kill_switches(price)
        runner._liquidate_if_halted(price)
        runner._check_soft_stops(price)
        execution_price = (
            float(frame["open"].iloc[index + 1])
            if index + 1 < len(frame)
            else float(frame["close"].iloc[index])
        )
        runner._process_bar(slot, execution_price)
    return runner


def _comparable(trade: dict[str, object]) -> tuple:
    return (
        int(trade["direction"]),
        round(float(trade["qty"]), 12),
        round(float(trade["entry_price"]), 8),
        round(float(trade["exit_price"]), 8),
        round(float(trade["pnl"]), 8),
        trade["reason"],
    )


def _backtest_trades(result) -> list[tuple]:
    return [
        _comparable(
            {
                "direction": trade.direction,
                "qty": trade.qty,
                "entry_price": trade.entry_price,
                "exit_price": trade.exit_price,
                "pnl": trade.pnl,
                "reason": trade.exit_reason,
            }
        )
        for trade in result.trades
    ]


def _runner_trades(runner) -> list[tuple]:
    return [
        _comparable(
            {
                "direction": 1 if row["direction"] == "LONG" else -1,
                "qty": row["qty"],
                "entry_price": row["entry_price"],
                "exit_price": row["exit_price"],
                "pnl": row["pnl"],
                "reason": row["reason"],
            }
        )
        for row in runner.store.read_trades()
    ]


SCENARIO = {"entries": {10: 1, 20: -1, 30: 1, 42: -1}, "hold_bars": 3}


def test_both_engines_produce_the_same_trades(tmp_path, monkeypatch):
    frame = continuous_frame()

    backtest = run_backtest(frame, ScriptedStrategy(**SCENARIO))
    runner = run_runner(frame, ScriptedStrategy(**SCENARIO), tmp_path, monkeypatch)

    expected = _backtest_trades(backtest)
    observed = _runner_trades(runner)
    assert expected, "scénario vide : le test ne prouverait rien"
    # La dernière position du backtest est liquidée en `end_of_data`, ce que le
    # runner ne fait pas : il reste en position. On compare le préfixe commun.
    common = min(len(expected), len(observed))
    assert observed[:common] == expected[:common]


def test_gap_between_close_and_next_open_uses_the_same_fill_reference(tmp_path, monkeypatch):
    frame = gapped_frame()

    backtest = run_backtest(frame, ScriptedStrategy(**SCENARIO))
    runner = run_runner(frame, ScriptedStrategy(**SCENARIO), tmp_path, monkeypatch)

    expected = _backtest_trades(backtest)
    observed = _runner_trades(runner)
    common = min(len(expected), len(observed))
    assert common > 0
    assert observed[:common] == expected[:common]
    first_decision_bar = min(SCENARIO["entries"])
    assert observed[0][2] != pytest.approx(float(frame["close"].iloc[first_decision_bar]))


def test_both_engines_reach_the_same_equity(tmp_path, monkeypatch):
    frame = continuous_frame()

    backtest = run_backtest(frame, ScriptedStrategy(**SCENARIO))
    runner = run_runner(frame, ScriptedStrategy(**SCENARIO), tmp_path, monkeypatch)

    closed_pnl = sum(trade.pnl for trade in backtest.trades if trade.exit_reason != "end_of_data")
    runner_pnl = sum(float(row["pnl"]) for row in runner.store.read_trades())
    assert runner_pnl == pytest.approx(closed_pnl, abs=1e-8)


def test_position_sizing_is_identical_on_both_paths(tmp_path, monkeypatch):
    """Le dimensionnement est implémenté une fois (`risk.position_size`) mais
    appelé avec des arguments construits séparément : c'est là que les deux
    chemins peuvent diverger sans que personne ne le voie."""
    frame = continuous_frame()

    backtest = run_backtest(frame, ScriptedStrategy(**SCENARIO))
    runner = run_runner(frame, ScriptedStrategy(**SCENARIO), tmp_path, monkeypatch)

    backtest_qty = [round(trade.qty, 12) for trade in backtest.trades]
    runner_qty = [round(float(row["qty"]), 12) for row in runner.store.read_trades()]
    common = min(len(backtest_qty), len(runner_qty))
    assert common > 0
    assert runner_qty[:common] == backtest_qty[:common]


def test_scenario_actually_trades_both_directions(tmp_path, monkeypatch):
    """Garde-fou : un scénario qui ne trade pas ferait passer les tests ci-dessus."""
    backtest = run_backtest(continuous_frame(), ScriptedStrategy(**SCENARIO))

    directions = {trade.direction for trade in backtest.trades}
    assert directions == {1, -1}
    assert len(backtest.trades) >= 4
    assert not any(trade.exit_reason == "stop" for trade in backtest.trades), (
        "le scénario doit rester hors stop pour isoler la parité de décision"
    )
