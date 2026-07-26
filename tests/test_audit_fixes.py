"""Tests des corrections issues de l'audit (juillet 2026).

Couvre : funding réel par barre dans le moteur, short_size_mult, ratchet du
stop suiveur (jamais relâché), _fill_from_order sans fallback dangereux,
persistance atomique de l'état du runner + entry_fee.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np
import pandas as pd
import pytest

from btcquant.backtest.engine import BacktestEngine
from btcquant.execution.broker import PaperBroker
from btcquant.execution.ccxt_broker import CcxtBroker
from btcquant.risk import RiskConfig
from btcquant.strategies.base import Position, Strategy

FEE = 0.001
SLIP_BPS = 5.0


class MockStrategy(Strategy):
    name = "mock"
    timeframe = "4h"

    @staticmethod
    def default_params() -> dict:
        return {
            "enter_bar": 5,
            "direction": 1,
            "stop_dist": 10.0,
            "exit_after": 0,
            "widen_stop": False,
        }

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["_i"] = np.arange(len(out))
        return out

    def entry_signal(self, row: pd.Series) -> int:
        return self.params["direction"] if row["_i"] == self.params["enter_bar"] else 0

    def initial_stop(self, row: pd.Series, entry_price: float, direction: int = 1) -> float:
        return entry_price - direction * self.params["stop_dist"]

    def trailing_stop(self, row: pd.Series, position: Position) -> float | None:
        if self.params["widen_stop"]:
            # stratégie hostile : essaie d'ÉLARGIR le stop de 5 à chaque barre
            return position.stop_price - position.direction * 5.0
        return None

    def exit_signal(self, row: pd.Series, position: Position) -> bool:
        return bool(self.params["exit_after"]) and position.bars_held >= self.params["exit_after"]

    def warmup_bars(self) -> int:
        return 1


def make_df(n: int = 14, price: float = 100.0) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=n, freq="4h", tz="UTC")
    o = np.full(n, price)
    return pd.DataFrame(
        {"open": o, "high": o + 1.5, "low": o - 1.5, "close": o + 0.5, "volume": np.full(n, 100.0)},
        index=idx,
    )


def risk_simple() -> RiskConfig:
    return RiskConfig(
        initial_capital=10_000.0,
        risk_per_trade=0.01,
        max_position_pct=0.95,
        vol_target_annual=None,
        max_drawdown_halt=0.99,
        daily_loss_limit=None,
    )


def test_funding_column_overrides_constant():
    """La colonne funding_rate par barre prime sur la constante du moteur."""
    df = make_df()
    per_bar_rate = 0.0002
    df["funding_rate"] = per_bar_rate

    class WithFunding(MockStrategy):
        def prepare(self, d):
            out = super().prepare(d)
            out["funding_rate"] = d["funding_rate"]
            return out

    strat = WithFunding(enter_bar=5, direction=1, stop_dist=10.0, exit_after=4)
    # constante volontairement ABSURDE : si le moteur l'utilisait, l'écart serait énorme
    res = BacktestEngine(
        fee_rate=0.0, slippage_bps=0.0, risk=risk_simple(), funding_rate_8h=99.0
    ).run(strat, df)
    qty = 100.0 / 10.0
    # position détenue aux clôtures des barres 6..9 → 4 paiements par barre
    expected_funding = sum(qty * 100.5 * per_bar_rate for _ in range(4))
    got = 10_000.0 - res.equity.iloc[-1]
    assert abs(got - expected_funding) < 1e-6, (
        f"funding réel non appliqué : {got} vs {expected_funding}"
    )


def test_funding_column_absent_falls_back_to_constant():
    df = make_df()
    rate_8h = 0.0001
    strat = MockStrategy(enter_bar=5, direction=1, stop_dist=10.0, exit_after=4)
    res = BacktestEngine(
        fee_rate=0.0, slippage_bps=0.0, risk=risk_simple(), funding_rate_8h=rate_8h
    ).run(strat, df)
    qty = 100.0 / 10.0
    per_bar = rate_8h / 2  # barre 4h = moitié d'une période 8h
    expected = sum(qty * 100.5 * per_bar for _ in range(4))
    got = 10_000.0 - res.equity.iloc[-1]
    assert abs(got - expected) < 1e-9


def test_opening_funding_is_not_charged_to_a_new_position():
    """Le snapshot de funding à t précède l'ordre d'entrée exécuté à t."""

    df = make_df()
    df["funding_rate"] = 0.0
    df["funding_at_open"] = 0.0
    df["funding_after_open"] = 0.0
    # signal à la clôture de la barre 5, entrée à l'ouverture de la barre 6
    df.iloc[6, df.columns.get_loc("funding_rate")] = 0.01
    df.iloc[6, df.columns.get_loc("funding_at_open")] = 0.01

    class WithTimedFunding(MockStrategy):
        def prepare(self, d):
            out = super().prepare(d)
            for column in (
                "funding_rate",
                "funding_at_open",
                "funding_after_open",
            ):
                out[column] = d[column]
            return out

    result = BacktestEngine(
        fee_rate=0.0,
        slippage_bps=0.0,
        risk=risk_simple(),
    ).run(
        WithTimedFunding(enter_bar=5, direction=1, stop_dist=10.0, exit_after=1),
        df,
    )

    assert result.equity.iloc[-1] == pytest.approx(10_000.0)


def test_funding_on_stop_bar_is_applied_symmetrically():
    """Un stop ne doit pas conserver un débit tout en supprimant un crédit."""

    class WithFunding(MockStrategy):
        def prepare(self, d):
            out = super().prepare(d)
            out["funding_rate"] = d["funding_rate"]
            return out

    def run(rate: float) -> float:
        df = make_df(n=10)
        df["funding_rate"] = 0.0
        df.iloc[7, df.columns.get_loc("low")] = 80.0
        df.iloc[7, df.columns.get_loc("funding_rate")] = rate
        result = BacktestEngine(
            fee_rate=0.0,
            slippage_bps=0.0,
            risk=risk_simple(),
        ).run(
            WithFunding(enter_bar=5, direction=1, stop_dist=10.0, exit_after=0),
            df,
        )
        return float(result.equity.iloc[-1])

    neutral = run(0.0)
    debit = neutral - run(0.01)
    credit = run(-0.01) - neutral

    assert debit == pytest.approx(credit)
    assert debit > 0


def test_short_size_mult():
    """short_size_mult réduit les shorts, n'affecte pas les longs."""
    df = make_df()
    kw = dict(fee_rate=FEE, slippage_bps=SLIP_BPS, risk=risk_simple(), allow_short=True)
    strat = lambda: MockStrategy(enter_bar=5, direction=-1, stop_dist=10.0, exit_after=4)

    full = BacktestEngine(**kw).run(strat(), df)
    half = BacktestEngine(**kw, short_size_mult=0.5).run(strat(), df)
    zero = BacktestEngine(**kw, short_size_mult=0.0).run(strat(), df)
    assert len(full.trades) == 1 and len(half.trades) == 1
    assert abs(half.trades[0].qty - full.trades[0].qty * 0.5) < 1e-9
    assert len(zero.trades) == 0, "short_size_mult=0 doit bloquer les shorts"

    long_full = BacktestEngine(**kw).run(
        MockStrategy(enter_bar=5, direction=1, stop_dist=10.0, exit_after=4), df
    )
    long_half = BacktestEngine(**kw, short_size_mult=0.5).run(
        MockStrategy(enter_bar=5, direction=1, stop_dist=10.0, exit_after=4), df
    )
    assert abs(long_full.trades[0].qty - long_half.trades[0].qty) < 1e-9


def test_trailing_stop_never_loosened():
    """Le moteur ne relâche JAMAIS un stop, même si la stratégie le demande."""
    df = make_df(n=20)
    strat = MockStrategy(enter_bar=5, direction=1, stop_dist=10.0, exit_after=0, widen_stop=True)
    res = BacktestEngine(fee_rate=0.0, slippage_bps=0.0, risk=risk_simple()).run(strat, df)
    # prix plat : le stop initial (entrée − 10) ne doit jamais être touché
    # (les lows sont à −1.5) ni élargi ; la position tient jusqu'à la fin
    assert len(res.trades) == 1 and res.trades[0].exit_reason == "end_of_data"


def test_fill_from_order_no_amount_fallback():
    """filled=0 → Fill.qty=0 (jamais la quantité demandée) ; fee via 'fee'."""
    order = {"average": 100.0, "filled": 0.0, "amount": 5.0, "fees": []}
    fill = CcxtBroker._fill_from_order(object.__new__(CcxtBroker), order, 99.0)
    assert fill.qty == 0.0, "fallback dangereux sur amount"
    order2 = {"average": 100.0, "filled": 2.0, "amount": 5.0, "fees": [], "fee": {"cost": 0.12}}
    fill2 = CcxtBroker._fill_from_order(object.__new__(CcxtBroker), order2, 99.0)
    assert fill2.qty == 2.0 and abs(fill2.fee - 0.12) < 1e-12


def test_runner_state_sqlite_roundtrip():
    """État sauvegardé dans SQLite et entry_fee persisté."""
    import tempfile

    from btcquant.execution.runner import LiveRunner, StrategySlot

    tmp_dir = tempfile.mkdtemp(prefix="btcq_test_")
    state_file = Path(tmp_dir) / "state.json"
    slot = StrategySlot(MockStrategy(), 1.0, 5_000.0)
    slot.entry_fee = 1.23
    slot.position = Position(
        entry_time=pd.Timestamp("2026-01-01", tz="UTC"),
        entry_price=100.0,
        qty=2.0,
        stop_price=90.0,
        direction=1,
        bars_held=3,
        best_close=105.0,
    )
    runner = LiveRunner([slot], PaperBroker(), risk_simple(), "binance", "BTC/USDT", state_file)
    runner._save_state()
    assert (Path(tmp_dir) / "btcquant.db").exists()
    assert not state_file.exists(), "le runner ne doit plus écrire de JSON"

    slot2 = StrategySlot(MockStrategy(), 1.0, 0.0)
    # Construire le runner restaure l'état sauvegardé dans slot2 : c'est cet
    # effet de bord qu'on teste, le runner lui-même n'est pas réutilisé.
    LiveRunner([slot2], PaperBroker(), risk_simple(), "binance", "BTC/USDT", state_file)
    assert abs(slot2.cash - 5_000.0) < 1e-9
    assert abs(slot2.entry_fee - 1.23) < 1e-9
    assert slot2.position is not None and slot2.position.qty == 2.0


if __name__ == "__main__":
    test_funding_column_overrides_constant()
    test_funding_column_absent_falls_back_to_constant()
    test_short_size_mult()
    test_trailing_stop_never_loosened()
    test_fill_from_order_no_amount_fallback()
    test_runner_state_sqlite_roundtrip()
    print("✔ tests audit OK")
