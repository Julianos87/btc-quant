"""A protective fill must change funding exposure in its atomic checkpoint."""

import pandas as pd
import pytest
from types import SimpleNamespace
from btcquant.execution.funding_service import FundingPayment, FundingPoll

from btcquant.execution.broker import PaperBroker, ProtectiveOrderSnapshot
from btcquant.execution.errors import ReconciliationRequired
from btcquant.execution.protective_stops import StopDecision, StopDecisionKind
from test_protective_stop_saga import _runner, _position


@pytest.mark.parametrize("filled", [1.0, 0.4])
def test_stop_fill_checkpoints_remaining_exposure_and_survives_restart(tmp_path, filled):
    runner, slot = _runner(tmp_path / "trend.db", PaperBroker(simulate_exchange_stops=True))
    runner.notifier = lambda _: True
    slot.position = _position()
    slot.position_timeline = [
        {
            "effective_at": slot.position.entry_time.isoformat(),
            "intent_id": "entry",
            "qty": 1.0,
            "direction": 1,
            "generation": runner._position_generation(slot.position),
        }
    ]
    runner._save_state()
    decision = StopDecision(
        StopDecisionKind.FILLED if filled == 1 else StopDecisionKind.PARTIAL_FILLED,
        previous_stop_id="filled-stop",
        snapshot=ProtectiveOrderSnapshot(
            "filled-stop", "FILLED" if filled == 1 else "PARTIAL", 1.0, filled, 0.0, 90.0
        ),
    )
    runner._materialize_filled_stop(slot, decision)
    restored, restored_slot = _runner(
        tmp_path / "trend.db", PaperBroker(simulate_exchange_stops=True)
    )
    exposure, known = restored._exposure_at(
        restored_slot, pd.Timestamp.now(tz="UTC") + pd.Timedelta(seconds=1)
    )
    assert known and exposure["qty"] == pytest.approx(1 - filled)
    assert exposure["intent_id"] == "observed-stop-filled-stop"
    old, _ = restored._exposure_at(restored_slot, pd.Timestamp("2026-01-01T01:00:00Z"))
    assert old["qty"] == 1.0
    timestamp = pd.Timestamp.now(tz="UTC") + pd.Timedelta(hours=1)
    payment = FundingPayment(timestamp, 0.01, 100.0, timestamp, "fixture")
    restored.funding_service = SimpleNamespace(poll=lambda _: FundingPoll(timestamp, (payment,)))
    before = restored_slot.cash
    restored._apply_funding_payments(100.0)
    expected = before - (1 - filled) * 100.0 * 0.01
    assert restored_slot.cash == pytest.approx(expected)
    restored._apply_funding_payments(100.0)
    assert restored_slot.cash == pytest.approx(expected)


def test_stop_checkpoint_failure_requires_reconciliation_and_keeps_old_durable_state(tmp_path):
    runner, slot = _runner(tmp_path / "trend.db", PaperBroker(simulate_exchange_stops=True))
    runner.notifier = lambda _: True
    slot.position = _position()
    runner._save_state()

    def fail(**kwargs):
        raise OSError("disk unavailable")

    runner.store.record_observed_fill_and_checkpoint = fail
    decision = StopDecision(
        StopDecisionKind.FILLED,
        previous_stop_id="filled-stop",
        snapshot=ProtectiveOrderSnapshot("filled-stop", "FILLED", 1, 1, 0, 90),
    )
    with pytest.raises(ReconciliationRequired):
        runner._materialize_filled_stop(slot, decision)
    assert (
        runner.store.load_engine_state("trend")["slots"][slot.strategy.name]["position"]["qty"] == 1
    )
