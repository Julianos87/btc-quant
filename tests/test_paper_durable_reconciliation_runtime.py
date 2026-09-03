from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from btcquant.execution.broker import PaperBroker
from btcquant.execution.order_state import FinancialTransitionType
from btcquant.execution.paper_execution_evidence import (
    PaperExecutionEvidenceContext,
    build_paper_execution_evidence,
)
from btcquant.execution.recovery import recover_interrupted_orders
from btcquant.execution.runner import LiveRunner, StrategySlot
from btcquant.risk import RiskConfig
from btcquant.strategies.base import Direction, Position, Strategy


class PowerLoss(BaseException):
    """Simulated process death after E3 commits and before memory refresh."""


class PaperRuntimeStrategy(Strategy):
    name = "paper-runtime"
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


class NoopVenue:
    def fetch_ohlcv(self, timeframe: str, limit: int = 1000) -> list[list[float]]:
        del timeframe, limit
        return []

    def last_price(self) -> float:
        return 100.0

    def funding_rate_8h(self) -> float:
        return 0.0

    def funding_history(self, days: float) -> pd.Series:
        del days
        return pd.Series(dtype=float)

    def funding_history_since(self, since: pd.Timestamp) -> pd.Series:
        del since
        return pd.Series(dtype=float)


def _runner(tmp_path: Path) -> tuple[LiveRunner, StrategySlot]:
    slot = StrategySlot(PaperRuntimeStrategy(), 1.0, 1_000.0)
    runner = LiveRunner(
        [slot],
        PaperBroker(fee_rate=0.001, slippage_bps=0.0),
        RiskConfig(
            initial_capital=1_000.0,
            risk_per_trade=0.01,
            max_position_pct=0.95,
            vol_target_annual=None,
        ),
        "paper",
        "BTC/USDC:USDC",
        tmp_path / "state.json",
        venue=NoopVenue(),
    )
    return runner, slot


def _count(store, table: str) -> int:
    with sqlite3.connect(store.path) as connection:
        row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    assert row is not None
    return int(row[0])


def _entry_submission(runner: LiveRunner, slot: StrategySlot):
    return runner._execute_market_order(
        slot,
        "BUY",
        1.0,
        100.0,
        "entry",
        "2026-09-03T12:00:00Z",
        FinancialTransitionType.ENTER_LONG,
        None,
        entry_direction=1,
        entry_stop_price=90.0,
    )


def test_paper_entry_uses_evidence_coordinator_e3_and_durable_memory_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, slot = _runner(tmp_path)

    def forbidden_accounting(*_args, **_kwargs):
        raise AssertionError("runner must not directly account a PAPER fill")

    monkeypatch.setattr(runner.accounting_service, "open_position", forbidden_accounting)
    runner._enter_position(
        slot,
        pd.Series({"_rvol": float("nan"), "volume": 1_000.0}),
        100.0,
        1,
        decision_checkpoint="2026-09-03T12:00:00Z",
    )

    assert slot.position is not None
    assert slot.position.direction == Direction.LONG
    assert slot.entry_fee > 0.0
    assert _count(runner.store, "external_order_observations") == 1
    assert _count(runner.store, "external_fills") == 1
    assert _count(runner.store, "financial_fill_applications") == 1
    order = runner.store.read_orders("trend")[0]
    assert order["local_state"] == "TERMINAL"
    assert order["status"] == "FILLED"
    with sqlite3.connect(runner.store.path) as connection:
        finalized = connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_type = ?",
            ("PAPER_ORDER_FINALIZED",),
        ).fetchone()
    assert finalized == (1,)
    assert runner.store.load_engine_state("trend") == runner._state_payload()


def test_paper_exit_refresh_clears_stale_in_memory_position(tmp_path: Path) -> None:
    runner, slot = _runner(tmp_path)
    slot.position = Position(
        entry_time=pd.Timestamp("2026-09-03T10:00:00Z"),
        entry_price=100.0,
        qty=1.0,
        stop_price=90.0,
        direction=Direction.LONG,
        bars_held=0,
        best_close=100.0,
        initial_qty=1.0,
        last_add_price=100.0,
        pyramid_adds=0,
    )
    slot.entry_fee = 0.1
    runner._save_state()

    runner._exit_position(
        slot,
        110.0,
        "exit",
        decision_checkpoint="2026-09-03T12:00:00Z",
    )

    assert slot.position is None
    assert slot.entry_fee == 0.0
    assert _count(runner.store, "financial_fill_applications") == 1
    assert _count(runner.store, "trades") == 1


def test_crash_after_e3_commit_before_memory_refresh_never_recovers_as_zero_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, slot = _runner(tmp_path)
    submitted = _entry_submission(runner, slot)

    def crash_during_refresh() -> None:
        raise PowerLoss("after durable E3 commit")

    monkeypatch.setattr(runner, "_load_state", crash_during_refresh)
    with pytest.raises(PowerLoss, match="after durable E3 commit"):
        runner._reconcile_paper_submission(submitted)

    assert _count(runner.store, "financial_fill_applications") == 1
    restarted, restarted_slot = _runner(tmp_path)
    assert restarted.store.read_orders("trend")[0]["status"] == "FILLED"
    assert _count(restarted.store, "financial_fill_applications") == 1
    assert restarted_slot.position is not None
    assert restarted_slot.financial_transition_seq == 1


def test_finalization_rolls_back_order_state_and_checkpoint_on_event_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, slot = _runner(tmp_path)
    submitted = _entry_submission(runner, slot)
    original_insert_event = runner.store._insert_event

    def fail_finalization_event(connection, engine, event_type, *args, **kwargs):
        if event_type == "PAPER_ORDER_FINALIZED":
            raise RuntimeError("finalization event failure")
        return original_insert_event(connection, engine, event_type, *args, **kwargs)

    monkeypatch.setattr(runner.store, "_insert_event", fail_finalization_event)
    with pytest.raises(RuntimeError, match="finalization event failure"):
        runner._reconcile_paper_submission(submitted)

    order = runner.store.read_orders("trend")[0]
    assert order["local_state"] == "PENDING_RECONCILIATION"
    assert order["status"] == "PENDING"
    assert _count(runner.store, "financial_fill_applications") == 1
    with sqlite3.connect(runner.store.path) as connection:
        finalized = connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_type = ?",
            ("PAPER_ORDER_FINALIZED",),
        ).fetchone()
    assert finalized == (0,)
    payload = runner.store.load_engine_state("trend")
    assert payload is not None
    assert payload["slots"]["paper-runtime"]["financial_transition_seq"] == 0


def test_finalization_commit_then_memory_crash_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, slot = _runner(tmp_path)
    submitted = _entry_submission(runner, slot)
    original_finalize = runner.store.finalize_paper_order_atomically

    def finalize_then_crash(order_id: int):
        original_finalize(order_id)
        raise PowerLoss("after durable PAPER finalization")

    monkeypatch.setattr(runner.store, "finalize_paper_order_atomically", finalize_then_crash)
    with pytest.raises(PowerLoss, match="after durable PAPER finalization"):
        runner._reconcile_paper_submission(submitted)

    order = runner.store.read_orders("trend")[0]
    assert order["local_state"] == "TERMINAL"
    assert order["status"] == "FILLED"
    assert _count(runner.store, "financial_fill_applications") == 1
    with sqlite3.connect(runner.store.path) as connection:
        finalized = connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_type = ?",
            ("PAPER_ORDER_FINALIZED",),
        ).fetchone()
    assert finalized == (1,)

    restarted, _ = _runner(tmp_path)
    assert restarted.store.read_orders("trend")[0]["local_state"] == "TERMINAL"
    decision = restarted.store.finalize_paper_order_atomically(submitted.order_id)
    assert decision.status.value == "ALREADY_FINALIZED"
    assert _count(restarted.store, "financial_fill_applications") == 1
    with sqlite3.connect(restarted.store.path) as connection:
        finalized = connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_type = ?",
            ("PAPER_ORDER_FINALIZED",),
        ).fetchone()
    assert finalized == (1,)


def test_paper_recovery_applies_durable_evidence_through_the_same_coordinator(
    tmp_path: Path,
) -> None:
    runner, slot = _runner(tmp_path)
    submitted = _entry_submission(runner, slot)
    assert submitted.broker_result is not None
    evidence = build_paper_execution_evidence(
        PaperExecutionEvidenceContext(
            local_order_id=submitted.order_id,
            intent_id=submitted.intent_id,
            engine="trend",
            instrument=runner.symbol,
            side=submitted.application_plan.side,
        ),
        submitted.broker_result,
        observed_at="2026-09-02T12:00:01Z",
    )
    runner.store.persist_paper_execution_evidence(evidence)

    report = recover_interrupted_orders(
        runner.store,
        PaperBroker(),
        "trend",
        external=False,
    )

    assert report.manual_order_ids == []
    assert report.recovered_order_ids == []
    assert report.finalized_order_ids == [submitted.order_id]
    assert _count(runner.store, "financial_fill_applications") == 1
    assert runner.store.read_orders("trend")[0]["status"] == "FILLED"


def test_paper_add_uses_the_durable_coordinator_instead_of_memory_accounting(
    tmp_path: Path,
) -> None:
    runner, slot = _runner(tmp_path)
    slot.position = Position(
        entry_time=pd.Timestamp("2026-09-03T10:00:00Z"),
        entry_price=100.0,
        qty=1.0,
        stop_price=90.0,
        direction=Direction.LONG,
        bars_held=0,
        best_close=100.0,
        initial_qty=1.0,
        last_add_price=100.0,
        pyramid_adds=0,
    )
    slot.entry_fee = 0.1
    runner._save_state()

    runner._pyramid_position(
        slot,
        pd.Series({"_rvol": float("nan"), "volume": 1_000.0}),
        110.0,
        0.1,
        decision_checkpoint="2026-09-03T12:00:00Z",
    )

    assert slot.position is not None
    assert slot.position.qty > 1.0
    assert slot.position.pyramid_adds == 1
    assert _count(runner.store, "financial_fill_applications") == 1


def test_paper_lifecycle_restarts_without_duplicate_financial_effects(tmp_path: Path) -> None:
    runner, slot = _runner(tmp_path)
    row = pd.Series({"_rvol": float("nan"), "volume": 1_000.0})

    runner._enter_position(
        slot,
        row,
        100.0,
        1,
        decision_checkpoint="2026-09-03T12:00:00Z",
    )
    runner, slot = _runner(tmp_path)
    assert slot.position is not None
    assert slot.financial_transition_seq == 1

    runner._pyramid_position(
        slot,
        row,
        110.0,
        0.1,
        decision_checkpoint="2026-09-03T13:00:00Z",
    )
    runner, slot = _runner(tmp_path)
    assert slot.position is not None
    assert slot.position.qty == pytest.approx(1.1)
    assert slot.position.pyramid_adds == 1
    assert slot.financial_transition_seq == 2

    runner._exit_position(
        slot,
        120.0,
        "exit",
        decision_checkpoint="2026-09-03T14:00:00Z",
    )
    runner, slot = _runner(tmp_path)
    assert slot.position is None
    assert slot.entry_fee == 0.0
    assert slot.financial_transition_seq == 3
    assert runner.store.unresolved_orders("trend") == []
    assert _count(runner.store, "external_order_observations") == 3
    assert _count(runner.store, "external_fills") == 3
    assert _count(runner.store, "financial_fill_applications") == 3
    assert _count(runner.store, "trades") == 1
    assert _count(runner.store, "events") >= 3
