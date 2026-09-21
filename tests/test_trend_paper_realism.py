"""Regression evidence for the causal, liquidity-aware Trend PAPER model."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from btcquant.domain.execution import (
    ExecutionConfig,
    ExecutionSimulator,
    FillStatus,
    MarketOrder,
    OrderSide,
)
from btcquant.execution.broker import PaperBroker
from btcquant.execution.errors import ReconciliationRequired
from btcquant.execution.margin import SharedCrossMarginModel
from btcquant.execution.runner import LiveRunner, StrategySlot
from btcquant.risk import RiskConfig
from btcquant.strategies.base import Strategy


class FlatStrategy(Strategy):
    name = "realism"
    timeframe = "4h"

    @staticmethod
    def default_params() -> dict:
        return {}

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        return df

    def entry_signal(self, row: pd.Series) -> int:
        return 0

    def initial_stop(self, row: pd.Series, entry_price: float, direction: int = 1) -> float:
        return entry_price - direction * 10.0


def _runner(tmp_path: Path, *, broker: PaperBroker | None = None) -> LiveRunner:
    strategy = FlatStrategy()
    strategy.name = "realism"
    broker = broker or PaperBroker(
        simulator=ExecutionSimulator(ExecutionConfig(fee_rate=0, slippage_bps=0))
    )
    return LiveRunner(
        [StrategySlot(strategy, 1.0, 20_000.0)],
        broker,
        RiskConfig(initial_capital=20_000.0, vol_target_annual=None),
        "hyperliquid",
        "BTC/USDC:USDC",
        tmp_path / "state.db",
    )


def _timeline(slot: StrategySlot, items: list[tuple[str, str, float, int]]) -> None:
    slot.position_timeline = [
        {
            "effective_at": timestamp,
            "intent_id": intent,
            "qty": qty,
            "direction": direction,
            "generation": f"{intent}-generation",
        }
        for timestamp, intent, qty, direction in items
    ]


def test_funding_replay_uses_event_prices_and_exposure_at_each_timestamp(tmp_path, monkeypatch):
    runner = _runner(tmp_path)
    slot = runner.slots[0]
    slot.position = None
    first = pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=2)
    second = first + pd.Timedelta(hours=1)
    slot.position_timeline = [
        {
            "effective_at": (first - pd.Timedelta(minutes=10)).isoformat(),
            "intent_id": "entry",
            "qty": 2.0,
            "direction": 1,
            "generation": "long-generation",
        },
        {
            "effective_at": (second - pd.Timedelta(minutes=1)).isoformat(),
            "intent_id": "reduce",
            "qty": 1.0,
            "direction": 1,
            "generation": "long-generation",
        },
    ]
    payments = pd.Series(
        [0.001, 0.001],
        index=pd.DatetimeIndex([first, second]),
    )
    monkeypatch.setattr(runner.venue, "funding_history_since", lambda _since: payments)
    prices = {first: 100.0, second: 200.0}
    monkeypatch.setattr(
        runner.venue,
        "funding_reference_price",
        lambda timestamp: {
            "price": prices[pd.Timestamp(timestamp)],
            "timestamp": timestamp,
            "source": "recorded-oracle-fixture",
        },
    )
    runner.last_funding_ts = first - pd.Timedelta(hours=1)

    runner._apply_funding_payments(999_999.0)

    # 2 BTC at 100 for the first event, 1 BTC at 200 for the second.
    assert slot.cash == pytest.approx(20_000.0 - 0.2 - 0.2)
    ledger = runner.store.read_funding_ledger()
    assert [row["funding_notional_price"] for row in ledger] == [100.0, 200.0]
    assert all(row["funding_notional_price_source"] == "recorded-oracle-fixture" for row in ledger)
    assert (
        len([e for e in runner.store.read_events("trend") if e["event_type"] == "funding_payment"])
        == 2
    )

    runner.funding_service.last_poll_monotonic = float("-inf")
    runner._apply_funding_payments(1.0)
    assert slot.cash == pytest.approx(20_000.0 - 0.4)
    assert len(runner.store.read_funding_ledger()) == 2


def test_funding_without_historical_reference_blocks_and_does_not_advance(tmp_path, monkeypatch):
    runner = _runner(tmp_path)
    slot = runner.slots[0]
    now = pd.Timestamp.now(tz="UTC")
    slot.position = None
    _timeline(slot, [("2026-01-01T00:00:00Z", "entry", 1.0, 1)])
    payment = now - pd.Timedelta(minutes=1)
    runner.last_funding_ts = payment - pd.Timedelta(hours=1)
    monkeypatch.setattr(
        runner.venue,
        "funding_history_since",
        lambda _since: pd.Series([0.001], index=pd.DatetimeIndex([payment])),
    )
    # Venue's default resolver returns unresolved rather than a current mark.
    with pytest.raises(ReconciliationRequired, match="Prix de référence funding"):
        runner._apply_funding_payments(123_456.0)
    assert runner.last_funding_ts == payment - pd.Timedelta(hours=1)
    assert slot.cash == pytest.approx(20_000.0)
    assert runner.store.read_funding_ledger() == []


def test_latency_uses_a_post_decision_price_and_never_current_fallback(tmp_path, monkeypatch):
    simulator = ExecutionSimulator(ExecutionConfig(fee_rate=0, slippage_bps=0, latency_ms=250))
    runner = _runner(tmp_path, broker=PaperBroker(simulator=simulator))
    observed = pd.Timestamp("2026-09-21T10:00:00Z") + pd.Timedelta(milliseconds=250)
    monkeypatch.setattr(
        runner.venue,
        "execution_price_after",
        lambda timestamp, latency: {
            "price": 110.0,
            "timestamp": observed,
            "source": "recorded-ticks",
        },
    )
    submitted = runner._execute_market_order(
        runner.slots[0],
        "BUY",
        1.0,
        100.0,
        "entry",
        "2026-09-21T10:00:00Z",
        "ENTER_LONG",
        None,
        entry_direction=1,
        entry_stop_price=90.0,
    )
    assert submitted.fill.price == pytest.approx(110.0)
    plan_context = submitted.application_plan.pre_state_payload["execution_context"]
    assert plan_context["delayed_price"] == pytest.approx(110.0)
    assert {
        "market_timestamp",
        "reception_timestamp",
        "decision_timestamp",
        "submission_timestamp",
    } <= plan_context.keys()
    assert plan_context["observed_timestamp"] == observed.isoformat()
    assert plan_context["price_source"] == "recorded-ticks"

    missing_runner = _runner(tmp_path / "missing", broker=PaperBroker(simulator=simulator))
    with pytest.raises(ReconciliationRequired, match="Prix d'exécution retardé"):
        missing_runner._execute_market_order(
            missing_runner.slots[0],
            "BUY",
            1.0,
            100.0,
            "entry",
            "2026-09-21T10:00:00Z",
            "ENTER_LONG",
            None,
            entry_direction=1,
            entry_stop_price=90.0,
        )
    assert missing_runner.store.pending_orders("trend") == []


def test_order_book_consumes_levels_and_does_not_add_slippage_twice():
    simulator = ExecutionSimulator(
        ExecutionConfig(fee_rate=0.001, slippage_bps=100, liquidity_model="order_book")
    )
    book = {
        "snapshot_id": "book-1",
        "asks": [[100.0, 1.0], [101.0, 2.0]],
        "bids": [[99.0, 1.0], [98.0, 2.0]],
    }
    first = simulator.execute_market(
        MarketOrder("buy-1", OrderSide.BUY, 2.0, 100.0, order_book=book)
    )
    assert first.status is FillStatus.FILLED
    assert first.qty == pytest.approx(2.0)
    assert first.price == pytest.approx(100.5)
    assert first.fee == pytest.approx(0.201)
    assert first.liquidity_model == "order_book"

    second = simulator.execute_market(
        MarketOrder("buy-2", OrderSide.BUY, 2.0, 100.0, order_book=book)
    )
    assert second.status is FillStatus.PARTIAL
    assert second.qty == pytest.approx(1.0)
    exhausted = simulator.execute_market(
        MarketOrder("buy-3", OrderSide.BUY, 1.0, 100.0, order_book=book)
    )
    assert exhausted.status is FillStatus.EXPIRED

    sell = simulator.execute_market(
        MarketOrder("sell-1", OrderSide.SELL, 2.0, 100.0, order_book=book)
    )
    assert sell.price == pytest.approx(98.5)


def test_exchange_style_paper_stop_is_persistent_and_partial_fill_is_replaced(tmp_path):
    simulator = ExecutionSimulator(
        ExecutionConfig(fee_rate=0, slippage_bps=0, max_volume_participation=0.5)
    )
    broker = PaperBroker(simulator=simulator, simulate_exchange_stops=True)
    runner = _runner(tmp_path, broker=broker)
    slot = runner.slots[0]
    runner._enter_position(
        slot,
        pd.Series({"_rvol": float("nan"), "volume": 1000.0}),
        100.0,
        1,
        decision_checkpoint="2026-09-21T10:00:00Z",
    )
    runner._monitor_exchange_stops()
    first_stop = slot.stop_order_id
    assert first_stop is not None
    assert broker.protective_order_snapshot(first_stop).status == "OPEN"

    broker.observe_market_price(89.0, available_volume=3.0)
    runner._monitor_exchange_stops()
    assert slot.position is not None
    assert slot.position.qty == pytest.approx(13.5)
    assert slot.stop_order_id != first_stop
    assert broker.protective_order_snapshot(slot.stop_order_id).status == "OPEN"

    # A fresh runner reconstructs the confirmed stop from the durable state.
    restarted = _runner(runner.state_path.parent, broker=PaperBroker(simulate_exchange_stops=True))
    assert restarted.slots[0].position is not None


def test_shared_cross_margin_uses_one_collateral_pool_and_exposes_liquidation():
    model = SharedCrossMarginModel(max_leverage=4.0, venue="hyperliquid")
    snapshot = model.evaluate(
        collateral=1_000.0,
        mark_price=100.0,
        positions=[(1, 10.0, 100.0, 100.0), (-1, 10.0, 100.0, 100.0)],
    )
    assert snapshot.gross_notional == pytest.approx(2_000.0)
    assert snapshot.initial_margin == pytest.approx(500.0)
    assert model.max_additional_qty(snapshot, price=100.0) == pytest.approx(20.0)
    liquidated = model.evaluate(
        collateral=10.0,
        mark_price=100.0,
        positions=[(1, 10.0, 100.0, 100.0)],
    )
    assert liquidated.liquidatable
    assert not liquidated.qualified
