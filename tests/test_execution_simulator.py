"""Contrat du simulateur d'exécution commun."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from btcquant.backtest.engine import BacktestEngine
from btcquant.config import execution_config_from_config
from btcquant.domain import (
    ExecutionConfig,
    ExecutionSimulator,
    FillStatus,
    MarketOrder,
    OrderSide,
)
from btcquant.execution.broker import PaperBroker
from btcquant.execution.runner import LiveRunner, StrategySlot
from btcquant.risk import RiskConfig
from btcquant.strategies.base import Position, Strategy


def test_full_fills_share_fees_and_adverse_slippage():
    simulator = ExecutionSimulator(ExecutionConfig(fee_rate=0.001, slippage_bps=5.0))

    buy = simulator.execute_market(MarketOrder("buy-1", OrderSide.BUY, 2.0, 100.0))
    sell = simulator.execute_market(MarketOrder("sell-1", OrderSide.SELL, 2.0, 100.0))

    assert buy.status == sell.status == FillStatus.FILLED
    assert buy.price == pytest.approx(100.05)
    assert sell.price == pytest.approx(99.95)
    assert buy.fee == pytest.approx(2.0 * 100.05 * 0.001)
    assert sell.fee == pytest.approx(2.0 * 99.95 * 0.001)


def test_volume_limit_produces_a_partial_fill():
    simulator = ExecutionSimulator(
        ExecutionConfig(
            fee_rate=0.001,
            max_volume_participation=0.25,
            market_impact_bps=20.0,
        )
    )
    fill = simulator.execute_market(
        MarketOrder(
            "partial-1",
            OrderSide.BUY,
            qty=10.0,
            reference_price=100.0,
            available_volume=4.0,
        )
    )

    assert fill.status == FillStatus.PARTIAL
    assert fill.qty == 1.0
    # 5 bps de slippage + 20 bps × 25 % de participation
    assert fill.price == pytest.approx(100.10)
    assert fill.requested_qty == 10.0


def test_volatility_and_participation_increase_slippage_with_a_cap():
    simulator = ExecutionSimulator(
        ExecutionConfig(
            slippage_bps=5.0,
            market_impact_bps=20.0,
            volatility_impact_bps=4.0,
            volatility_reference_annual=0.40,
            volatility_multiplier_cap=2.0,
        )
    )

    normal = simulator.quote_price(
        OrderSide.BUY,
        100.0,
        participation=0.25,
        volatility_annual=0.40,
    )
    stressed = simulator.quote_price(
        OrderSide.BUY,
        100.0,
        participation=0.25,
        volatility_annual=4.0,
    )

    # normal : 5 bps fixes + 4 bps de vol + 5 bps de participation
    assert normal == pytest.approx(100.14)
    # stress : multiplicateur de vol borné à 2, donc 5 + 8 + 5 = 18 bps
    assert stressed == pytest.approx(100.18)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"volatility_impact_bps": -1.0}, "impacts"),
        ({"volatility_reference_annual": 0.0}, "reference"),
        ({"volatility_multiplier_cap": 0.0}, "cap"),
    ],
)
def test_volatility_model_rejects_invalid_parameters(kwargs, message):
    with pytest.raises(ValueError, match=message):
        ExecutionConfig(**kwargs)


def test_rejection_and_replay_are_deterministic_across_restarts():
    config = ExecutionConfig(rejection_rate=0.5, seed=42)
    first_process = ExecutionSimulator(config)
    second_process = ExecutionSimulator(config)
    orders = [MarketOrder(f"order-{i}", OrderSide.BUY, 1.0, 100.0) for i in range(20)]

    first_results = [first_process.execute_market(order) for order in orders]
    second_results = [second_process.execute_market(order) for order in orders]

    assert first_results == second_results
    assert {result.status for result in first_results} == {
        FillStatus.FILLED,
        FillStatus.REJECTED,
    }
    assert first_process.execute_market(orders[0]) is first_results[0]


def test_same_id_with_different_order_is_rejected():
    simulator = ExecutionSimulator()
    simulator.execute_market(MarketOrder("stable-id", OrderSide.BUY, 1.0, 100.0))

    with pytest.raises(ValueError, match="Conflit d'idempotence"):
        simulator.execute_market(MarketOrder("stable-id", OrderSide.BUY, 2.0, 100.0))


def test_latency_uses_delayed_market_price_and_reports_latency():
    simulator = ExecutionSimulator(
        ExecutionConfig(slippage_bps=10.0, latency_ms=750, market_impact_bps=20.0)
    )
    fill = simulator.execute_market(
        MarketOrder(
            "delayed",
            OrderSide.SELL,
            qty=1.0,
            reference_price=100.0,
            delayed_price=98.0,
        )
    )

    assert fill.latency_ms == 750
    assert fill.price == pytest.approx(98.0 * (1.0 - 10.0 / 10_000.0))


def test_latency_never_silently_uses_a_non_delayed_price():
    simulator = ExecutionSimulator(ExecutionConfig(latency_ms=750))

    with pytest.raises(ValueError, match="delayed_price est requis"):
        simulator.execute_market(MarketOrder("missing-delay", OrderSide.BUY, 1.0, 100.0))


@pytest.mark.parametrize(
    ("direction", "open_price", "high_price", "low_price", "stop_price", "expected"),
    [
        (1, 95.0, 102.0, 89.0, 90.0, 90.0),
        (1, 85.0, 90.0, 80.0, 90.0, 85.0),
        (-1, 105.0, 111.0, 98.0, 110.0, 110.0),
        (-1, 115.0, 120.0, 112.0, 110.0, 115.0),
        (1, 100.0, 105.0, 95.0, 90.0, None),
    ],
)
def test_stop_trigger_price_is_conservative(
    direction, open_price, high_price, low_price, stop_price, expected
):
    assert (
        ExecutionSimulator.stop_trigger_price(
            direction=direction,
            open_price=open_price,
            high_price=high_price,
            low_price=low_price,
            stop_price=stop_price,
        )
        == expected
    )


def test_paper_broker_is_an_adapter_over_the_shared_simulator():
    simulator = ExecutionSimulator(ExecutionConfig(fee_rate=0.002, slippage_bps=7.0))
    broker = PaperBroker(simulator=simulator)

    result = broker.execute_market("BUY", 3.0, 100.0, client_order_id="paper-order")
    replay = broker.execute_market("BUY", 3.0, 100.0, client_order_id="paper-order")

    assert result == replay
    assert result.fill.price == pytest.approx(100.07)
    assert result.fill.fee == pytest.approx(3.0 * 100.07 * 0.002)


def test_runner_passes_bar_volume_to_paper_execution(tmp_path, monkeypatch):
    simulator = ExecutionSimulator(
        ExecutionConfig(
            fee_rate=0.0,
            slippage_bps=0.0,
            max_volume_participation=0.10,
        )
    )
    strategy = OneTradeStrategy()
    slot = StrategySlot(strategy, 1.0, 10_000.0)
    runner = LiveRunner(
        [slot],
        PaperBroker(simulator=simulator),
        RiskConfig(
            initial_capital=10_000.0,
            risk_per_trade=0.01,
            max_position_pct=0.95,
            vol_target_annual=None,
        ),
        "binance",
        "BTC/USDT",
        tmp_path / "state.json",
    )
    index = pd.date_range("2026-01-01", periods=6, freq="4h", tz="UTC")
    price = np.full(len(index), 100.0)
    frame = pd.DataFrame(
        {
            "open": price,
            "high": price + 1.0,
            "low": price - 1.0,
            "close": price,
            "volume": np.full(len(index), 20.0),
        },
        index=index,
    )
    monkeypatch.setattr(runner, "_fetch_frame", lambda _strategy: frame)
    monkeypatch.setattr(runner.venue, "funding_rate_8h", lambda: 0.0)

    runner._process_bar(slot, 105.0)

    assert slot.position is not None
    assert slot.position.qty == pytest.approx(2.0)
    orders = runner.store.read_orders("trend")
    assert orders[0]["status"] == "PENDING"
    assert orders[0]["local_state"] == "PENDING_RECONCILIATION"
    assert orders[0]["reference_price"] == pytest.approx(105.0)


def test_yaml_simulation_config_keeps_costs_as_single_source_of_truth():
    cfg = {
        "costs": {"slippage_bps": 8.0},
        "execution": {
            "simulation": {
                "rejection_rate": 0.1,
                "max_volume_participation": 0.05,
                "seed": 7,
            }
        },
    }

    config = execution_config_from_config(cfg, fee_rate=0.002)

    assert config.fee_rate == 0.002
    assert config.slippage_bps == 8.0
    assert config.rejection_rate == 0.1
    assert config.max_volume_participation == 0.05
    with pytest.raises(ValueError, match="dans costs"):
        execution_config_from_config(
            {
                "costs": {"slippage_bps": 8.0},
                "execution": {"simulation": {"fee_rate": 99.0}},
            },
            fee_rate=0.002,
        )


def test_yaml_selects_explicit_normal_and_stress_profiles():
    cfg = {
        "costs": {"slippage_bps": 5.0},
        "execution": {
            "simulation": {
                "profile": "stress",
                "profiles": {
                    "normal": {
                        "market_impact_bps": 15.0,
                        "volatility_impact_bps": 1.5,
                    },
                    "stress": {
                        "market_impact_bps": 75.0,
                        "volatility_impact_bps": 6.0,
                        "max_volume_participation": 0.01,
                    },
                },
            }
        },
    }

    config = execution_config_from_config(cfg, fee_rate=0.002)

    assert config.slippage_bps == 5.0
    assert config.market_impact_bps == 75.0
    assert config.volatility_impact_bps == 6.0
    assert config.max_volume_participation == 0.01

    cfg["execution"]["simulation"]["profile"] = "missing"
    with pytest.raises(ValueError, match="inconnu"):
        execution_config_from_config(cfg, fee_rate=0.002)


class OneTradeStrategy(Strategy):
    name = "execution-test"
    timeframe = "4h"

    @staticmethod
    def default_params() -> dict:
        return {}

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["_i"] = np.arange(len(out))
        return out

    def entry_signal(self, row: pd.Series) -> int:
        return 1 if row["_i"] == 5 else 0

    def initial_stop(self, row: pd.Series, entry_price: float, direction: int = 1) -> float:
        return entry_price - 10.0

    def exit_signal(self, row: pd.Series, position: Position) -> bool:
        return position.bars_held >= 4

    def warmup_bars(self) -> int:
        return 1


class FundingFilteredStrategy(OneTradeStrategy):
    @staticmethod
    def default_params() -> dict:
        return {"funding_long_max": 0.0008, "funding_short_min": -0.0008}


def test_missing_funding_blocks_new_entries_when_filter_is_active(tmp_path, monkeypatch):
    strategy = FundingFilteredStrategy()
    slot = StrategySlot(strategy, 1.0, 10_000.0)
    runner = LiveRunner(
        [slot],
        PaperBroker(),
        RiskConfig(initial_capital=10_000.0, vol_target_annual=None),
        "binance",
        "BTC/USDT",
        tmp_path / "state.json",
    )
    index = pd.date_range("2026-01-01", periods=6, freq="4h", tz="UTC")
    price = np.full(len(index), 100.0)
    frame = pd.DataFrame(
        {
            "open": price,
            "high": price + 1.0,
            "low": price - 1.0,
            "close": price,
            "volume": np.full(len(index), 20.0),
        },
        index=index,
    )
    monkeypatch.setattr(runner, "_fetch_frame", lambda _strategy: frame)
    monkeypatch.setattr(
        runner.venue,
        "funding_rate_8h",
        lambda: (_ for _ in ()).throw(RuntimeError("venue down")),
    )

    runner._process_bar(slot, 105.0)

    assert slot.position is None
    assert runner.store.read_orders("trend") == []


def test_missing_funding_still_exits_an_open_position(tmp_path, monkeypatch):
    strategy = FundingFilteredStrategy()
    slot = StrategySlot(strategy, 1.0, 10_000.0)
    slot.position = Position(
        entry_time=pd.Timestamp("2026-01-01T00:00:00Z"),
        entry_price=100.0,
        qty=1.0,
        stop_price=1.0,
        bars_held=4,
    )
    runner = LiveRunner(
        [slot],
        PaperBroker(),
        RiskConfig(initial_capital=10_000.0, vol_target_annual=None),
        "binance",
        "BTC/USDT",
        tmp_path / "state.json",
    )
    index = pd.date_range("2026-01-01", periods=6, freq="4h", tz="UTC")
    price = np.full(len(index), 100.0)
    frame = pd.DataFrame(
        {
            "open": price,
            "high": price + 1.0,
            "low": price - 1.0,
            "close": price,
            "volume": np.full(len(index), 20.0),
        },
        index=index,
    )
    monkeypatch.setattr(runner, "_fetch_frame", lambda _strategy: frame)
    monkeypatch.setattr(
        runner.venue,
        "funding_rate_8h",
        lambda: (_ for _ in ()).throw(RuntimeError("venue down")),
    )

    runner._process_bar(slot, 105.0)

    assert slot.position is None
    orders = runner.store.read_orders("trend")
    assert orders
    assert orders[-1]["reason"] != "entry"


def test_backtest_handles_partial_entries_and_exits():
    index = pd.date_range("2026-01-01", periods=16, freq="4h", tz="UTC")
    price = np.full(len(index), 100.0)
    volume = np.full(len(index), 20.0)
    volume[6] = 100.0  # entrée : capacité 10, donc fill complet
    frame = pd.DataFrame(
        {
            "open": price,
            "high": price + 1.0,
            "low": price - 1.0,
            "close": price + 0.5,
            "volume": volume,
        },
        index=index,
    )
    simulator = ExecutionSimulator(
        ExecutionConfig(
            fee_rate=0.0,
            slippage_bps=0.0,
            max_volume_participation=0.10,
        )
    )
    risk = RiskConfig(
        initial_capital=10_000.0,
        risk_per_trade=0.01,
        max_position_pct=0.95,
        vol_target_annual=None,
        max_drawdown_halt=0.99,
        daily_loss_limit=None,
    )

    result = BacktestEngine(risk=risk, execution_simulator=simulator).run(OneTradeStrategy(), frame)

    assert len(result.trades) > 1
    assert sum(trade.qty for trade in result.trades) == pytest.approx(10.0)
    assert all(trade.qty <= 2.0 for trade in result.trades)
    assert result.metrics["exposure"] == pytest.approx(9 / 15)
    assert 0.0 <= result.metrics["exposure"] <= 1.0


def test_backtest_rejected_entry_does_not_create_a_position():
    index = pd.date_range("2026-01-01", periods=12, freq="4h", tz="UTC")
    price = np.full(len(index), 100.0)
    frame = pd.DataFrame(
        {
            "open": price,
            "high": price + 1.0,
            "low": price - 1.0,
            "close": price + 0.5,
            "volume": np.full(len(index), 100.0),
        },
        index=index,
    )
    simulator = ExecutionSimulator(ExecutionConfig(rejection_rate=1.0))

    result = BacktestEngine(execution_simulator=simulator).run(OneTradeStrategy(), frame)

    assert result.trades == []


def test_reusing_a_backtest_engine_starts_a_fresh_execution_session():
    index = pd.date_range("2026-01-01", periods=12, freq="4h", tz="UTC")
    price = np.full(len(index), 100.0)
    frame = pd.DataFrame(
        {
            "open": price,
            "high": price + 1.0,
            "low": price - 1.0,
            "close": price + 0.5,
            "volume": np.full(len(index), 100.0),
        },
        index=index,
    )
    engine = BacktestEngine(execution_simulator=ExecutionSimulator(ExecutionConfig(seed=123)))

    first = engine.run(OneTradeStrategy(), frame)
    second = engine.run(OneTradeStrategy(), frame)

    assert first.trades == second.trades
    assert first.equity.equals(second.equity)
