from __future__ import annotations

from btcquant.execution.broker import PaperBroker, ProtectiveOrderSnapshot
from btcquant.execution.protective_stops import ProtectiveStopService, StopDecisionKind


def _snapshot(status: str, filled: float = 0.0, remaining: float = 1.0):
    return ProtectiveOrderSnapshot(
        broker_order_id="stop-1",
        status=status,
        requested_qty=1.0,
        filled_qty=filled,
        remaining_qty=remaining,
        average_price=90.0 if filled else None,
    )


def test_missing_stop_is_recreated():
    broker = PaperBroker()
    broker.place_stop = lambda *_args: "replacement"

    decision = ProtectiveStopService(broker).inspect(
        stop_id=None, qty=1.0, stop_price=90.0, direction=1
    )

    assert decision.kind == StopDecisionKind.REPLACED
    assert decision.replacement_stop_id == "replacement"


def test_canceled_stop_is_recreated_with_previous_context():
    broker = PaperBroker()
    broker.protective_order_snapshot = lambda _stop_id: _snapshot("CANCELED")
    broker.place_stop = lambda *_args: "replacement"

    decision = ProtectiveStopService(broker).inspect(
        stop_id="stop-1", qty=1.0, stop_price=90.0, direction=1
    )

    assert decision.kind == StopDecisionKind.REPLACED
    assert decision.previous_status == "CANCELED"


def test_partial_fill_is_uncertain_but_complete_fill_is_terminal():
    broker = PaperBroker()
    service = ProtectiveStopService(broker)
    broker.protective_order_snapshot = lambda _stop_id: _snapshot(
        "PARTIAL", filled=0.4, remaining=0.6
    )
    partial = service.inspect(stop_id="stop-1", qty=1.0, stop_price=90.0, direction=1)

    broker.protective_order_snapshot = lambda _stop_id: _snapshot(
        "FILLED", filled=1.0, remaining=0.0
    )
    filled = service.inspect(stop_id="stop-1", qty=1.0, stop_price=90.0, direction=1)

    assert partial.kind == StopDecisionKind.UNCERTAIN
    assert "état ambigu" in (partial.message or "")
    assert filled.kind == StopDecisionKind.FILLED


def test_orphan_stop_and_failed_replacement_fail_closed():
    broker = PaperBroker()
    service = ProtectiveStopService(broker)
    orphan = service.inspect(stop_id="stop-1", qty=None, stop_price=None, direction=None)
    broker.place_stop = lambda *_args: None
    unconfirmed = service.inspect(stop_id=None, qty=1.0, stop_price=90.0, direction=1)

    assert orphan.kind == StopDecisionKind.UNCERTAIN
    assert unconfirmed.kind == StopDecisionKind.UNCERTAIN
