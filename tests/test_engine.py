"""Tests du moteur de backtest sur données synthétiques.

Chaque scénario est construit pour que le résultat exact (prix d'exécution,
frais, PnL, cash final) se calcule à la main ; on vérifie que le moteur
reproduit ces valeurs au centime près. Lancement :

    python tests/test_engine.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np
import pandas as pd

from btcquant.backtest.engine import BacktestEngine
from btcquant.risk import RiskConfig, position_size
from btcquant.strategies.base import Position, Strategy

FEE = 0.001
SLIP_BPS = 5.0
SLIP = SLIP_BPS / 10_000.0

PASSED = []


def check(name: str, got: float, expected: float, tol: float = 1e-6) -> None:
    assert abs(got - expected) < tol, f"{name}: obtenu {got!r}, attendu {expected!r}"
    PASSED.append(name)


class MockStrategy(Strategy):
    """Entre à direction fixée au bar `enter_bar`, stop fixe, sortie après
    `exit_after` barres (0 = jamais). Aucun indicateur."""

    name = "mock"
    timeframe = "4h"

    @staticmethod
    def default_params() -> dict:
        return {"enter_bar": 5, "direction": 1, "stop_dist": 10.0, "exit_after": 0}

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["_i"] = np.arange(len(out))
        return out

    def entry_signal(self, row: pd.Series) -> int:
        return self.params["direction"] if row["_i"] == self.params["enter_bar"] else 0

    def initial_stop(self, row: pd.Series, entry_price: float, direction: int = 1) -> float:
        return entry_price - direction * self.params["stop_dist"]

    def exit_signal(self, row: pd.Series, position: Position) -> bool:
        return bool(self.params["exit_after"]) and position.bars_held >= self.params["exit_after"]

    def warmup_bars(self) -> int:
        return 1


def make_df(
    opens: list[float], lows: list[float] | None = None, highs: list[float] | None = None
) -> pd.DataFrame:
    n = len(opens)
    idx = pd.date_range("2024-01-01", periods=n, freq="4h", tz="UTC")
    o = np.array(opens, dtype=float)
    close = o + 0.5
    return pd.DataFrame(
        {
            "open": o,
            "high": np.array(highs, dtype=float) if highs else np.maximum(o, close) + 1.0,
            "low": np.array(lows, dtype=float) if lows else np.minimum(o, close) - 1.0,
            "close": close,
            "volume": np.full(n, 100.0),
        },
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


def test_long_win():
    """Long gagnant sorti sur signal : chaque nombre vérifié à la main."""
    opens = [100 + i for i in range(14)]  # lows par défaut restent > stop
    df = make_df(opens, lows=[o - 1.5 for o in opens])
    strat = MockStrategy(enter_bar=5, direction=1, stop_dist=10.0, exit_after=4)
    res = BacktestEngine(fee_rate=FEE, slippage_bps=SLIP_BPS, risk=risk_simple()).run(strat, df)

    entry = 106 * (1 + SLIP)  # signal clôture barre 5 -> open barre 6
    qty = 10_000 * 0.01 / 10.0  # risque 1 % / distance 10 -> 10 unités
    entry_fee = qty * entry * FEE
    exit_px = 110 * (1 - SLIP)  # exit_after=4 -> sortie open barre 10
    exit_fee = qty * exit_px * FEE
    expected_pnl = qty * (exit_px - entry) - entry_fee - exit_fee

    assert len(res.trades) == 1 and res.trades[0].exit_reason == "signal"
    check("long_win entry_price", res.trades[0].entry_price, entry)
    check("long_win exit_price", res.trades[0].exit_price, exit_px)
    check("long_win qty", res.trades[0].qty, qty)
    check("long_win pnl", res.trades[0].pnl, expected_pnl)
    check("long_win cash final", res.equity.iloc[-1], 10_000 + expected_pnl)


def test_long_stop():
    """Stop touché en intrabar : sortie exactement au stop − slippage."""
    opens = [100.0] * 8 + [95.0] + [95.0] * 3  # barre 8 : plonge sous le stop
    lows = [99.0] * 8 + [85.0] + [94.0] * 3
    df = make_df(opens, lows=lows)
    strat = MockStrategy(enter_bar=5, direction=1, stop_dist=10.0, exit_after=0)
    res = BacktestEngine(fee_rate=FEE, slippage_bps=SLIP_BPS, risk=risk_simple()).run(strat, df)

    entry = 100 * (1 + SLIP)
    stop = entry - 10.0
    qty = 100.0 / 10.0
    # open barre 8 (95) < stop -> gap : exécution à l'open, pas au stop
    exit_px = min(95.0, stop) * (1 - SLIP)
    expected_pnl = qty * (exit_px - entry) - qty * entry * FEE - qty * exit_px * FEE

    assert len(res.trades) == 1 and res.trades[0].exit_reason == "stop"
    check("long_stop exit gap au pire prix", res.trades[0].exit_price, exit_px)
    check("long_stop pnl", res.trades[0].pnl, expected_pnl)


def test_short_win():
    """Short gagnant : PnL symétrique, slippage dans l'autre sens."""
    opens = [100.0] * 6 + [100, 98, 96, 94, 92, 90, 88, 86]
    highs = [o + 1 for o in opens]  # jamais au-dessus du stop (entrée+10)
    df = make_df(opens, highs=highs, lows=[o - 1.5 for o in opens])
    strat = MockStrategy(enter_bar=5, direction=-1, stop_dist=10.0, exit_after=4)
    res = BacktestEngine(
        fee_rate=FEE, slippage_bps=SLIP_BPS, risk=risk_simple(), allow_short=True
    ).run(strat, df)

    entry = 100 * (1 - SLIP)  # vente : slippage défavorable = prix plus bas
    qty = 100.0 / 10.0
    exit_px = 92 * (1 + SLIP)  # rachat open barre 10
    expected_pnl = qty * (entry - exit_px) - qty * entry * FEE - qty * exit_px * FEE

    assert len(res.trades) == 1 and res.trades[0].direction == -1
    check("short_win entry", res.trades[0].entry_price, entry)
    check("short_win pnl", res.trades[0].pnl, expected_pnl)
    check("short_win cash final", res.equity.iloc[-1], 10_000 + expected_pnl)


def test_short_blocked_when_not_allowed():
    df = make_df([100.0] * 12)
    strat = MockStrategy(enter_bar=5, direction=-1, stop_dist=10.0)
    res = BacktestEngine(
        fee_rate=FEE, slippage_bps=SLIP_BPS, risk=risk_simple(), allow_short=False
    ).run(strat, df)
    assert len(res.trades) == 0, "un short a été pris alors qu'allow_short=False"
    PASSED.append("short bloqué si allow_short=False")


def test_funding():
    """Funding débité chaque barre de détention pour un long (4h : taux 8h / 2)."""
    opens = [100.0] * 14
    df = make_df(opens, lows=[85.0] * 14)  # lows sous... non : stop 10 -> low doit rester > 90
    df["low"] = 99.0
    strat = MockStrategy(enter_bar=5, direction=1, stop_dist=10.0, exit_after=4)
    rate_8h = 0.0001
    res = BacktestEngine(
        fee_rate=0.0, slippage_bps=0.0, risk=risk_simple(), funding_rate_8h=rate_8h
    ).run(strat, df)

    qty = 100.0 / 10.0
    per_bar = rate_8h * (8760 / 2190) / 8  # = taux par barre 4h (0.5 × taux 8h... = 0.00005)
    closes_held = [100.5] * 4  # clôtures barres 6..9 (position ouverte)
    funding_paid = sum(qty * c * per_bar for c in closes_held)
    expected_pnl_price = qty * (100.0 - 100.0)  # entrée 100, sortie open barre 10 = 100
    check("funding total débité", 10_000 + expected_pnl_price - res.equity.iloc[-1], funding_paid)


def test_position_size_caps():
    cfg = RiskConfig(
        initial_capital=10_000,
        risk_per_trade=0.01,
        max_position_pct=0.95,
        vol_target_annual=0.40,
        max_drawdown_halt=0.99,
        daily_loss_limit=None,
    )
    # 1. risque fixe : 100 $ de risque / 10 $ de distance = 10 unités
    check("sizing risque fixe", position_size(10_000, 100, 90, None, cfg), 10.0)
    # 2. plafond vol target : vol 80 % > cible 40 % -> notionnel max 5 000 $ -> 50 unités
    check("sizing vol target", position_size(10_000, 100, 99.9, 0.80, cfg), 50.0)
    # 3. plafond notionnel : distance minuscule -> borné à 95 unités (9 500 $)
    check("sizing plafond spot", position_size(10_000, 100, 99.99, None, cfg), 95.0)
    # 4. levier : même cas à 3x -> 285 unités
    cfg3 = RiskConfig(**{**cfg.__dict__, "max_leverage": 3.0})
    check("sizing levier 3x", position_size(10_000, 100, 99.99, None, cfg3), 285.0)
    # 5. stop du mauvais côté -> 0
    check("sizing stop invalide", position_size(10_000, 100, 110, None, cfg), 0.0)
    # 6. short : quantité positive avec stop au-dessus
    check("sizing short", position_size(10_000, 100, 110, None, cfg, direction=-1), 10.0)


def test_kill_switch_exits_at_the_next_open():
    """Le drawdown constaté à la clôture t liquide à l'ouverture t+1."""

    class DeepStopStrategy(MockStrategy):
        def initial_stop(self, row: pd.Series, entry_price: float, direction: int = 1) -> float:
            return 1.0

    opens = [100.0] * 7 + [70.0, 60.0, 50.0, 50.0]
    df = make_df(
        opens,
        lows=[99.0] * 7 + [69.0, 59.0, 49.0, 49.0],
        highs=[101.0] * len(opens),
    )
    # La clôture de la barre 7 vaut 70.5 : avec presque 100 % de notionnel,
    # le drawdown dépasse 20 %, sans toucher le stop à 1 $.
    cfg = RiskConfig(
        initial_capital=10_000.0,
        risk_per_trade=1.0,
        max_position_pct=0.95,
        vol_target_annual=None,
        max_drawdown_halt=0.20,
        daily_loss_limit=None,
    )

    result = BacktestEngine(fee_rate=0.0, slippage_bps=0.0, risk=cfg).run(
        DeepStopStrategy(enter_bar=5, direction=1, stop_dist=10.0, exit_after=0),
        df,
    )

    assert len(result.trades) == 1
    assert result.trades[0].exit_reason == "kill_switch"
    assert result.trades[0].exit_time == df.index[8]
    assert result.trades[0].exit_price == 60.0


if __name__ == "__main__":
    test_long_win()
    test_long_stop()
    test_short_win()
    test_short_blocked_when_not_allowed()
    test_funding()
    test_position_size_caps()
    print(f"✔ {len(PASSED)} vérifications passées :")
    for name in PASSED:
        print(f"   · {name}")
