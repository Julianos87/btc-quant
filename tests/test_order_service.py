from __future__ import annotations

import pytest

from btcquant.execution.broker import Broker, BrokerOrderResult, Fill, PaperBroker
from btcquant.execution.errors import ReconciliationRequired
from btcquant.execution.order_service import OrderExecutionService
from btcquant.execution.order_state import (
    ExternalOrderState,
    FinancialTransitionType,
    LocalOrderState,
    LogicalOrderIdentity,
)
from btcquant.execution.state_store import StateStore
from btcquant.execution.financial_application_plan import FinancialApplicationPlan


def _test_application_plan(**kwargs):
    transition = FinancialTransitionType(kwargs["transition_type"])
    identity = LogicalOrderIdentity(
        kwargs["engine"],
        kwargs["slot"],
        kwargs["decision_checkpoint"],
        transition,
        kwargs.get("position_generation"),
        kwargs.get("transition_sequence", 0),
    )
    position = None
    if transition in {FinancialTransitionType.EXIT, FinancialTransitionType.ADD}:
        generation = kwargs["position_generation"]
        entry, initial = generation.removeprefix("entry=").split("|initial_qty=")
        direction = 1 if kwargs["side"] == "SELL" else -1
        position = {
            "entry_time": entry,
            "entry_price": kwargs["reference_price"],
            "qty": max(float(kwargs["qty"]), float(initial)),
            "stop_price": 90.0,
            "direction": direction,
            "bars_held": 0,
            "best_close": kwargs["reference_price"],
            "initial_qty": float(initial),
            "last_add_price": kwargs["reference_price"],
            "pyramid_adds": 0,
        }
    state = {
        "slots": {
            kwargs["slot"]: {
                "cash": 1000.0,
                "position": position,
                "stop_order_id": None,
                "stop_order_local_id": None,
                "stop_intent_id": None,
                "stop_transition": None,
                "entry_fee": 0.0,
                "last_bar_ts": None,
                "financial_transition_seq": kwargs.get("transition_sequence", 0),
            }
        },
        "peak_equity": 1000.0,
        "halted": False,
        "day": None,
        "day_start_equity": 1000.0,
        "daily_lockout": False,
        "reconciliation_required": False,
        "last_funding_ts": None,
        "stop_protection_mode": "SOFTWARE",
    }
    return FinancialApplicationPlan(
        identity=identity,
        side=kwargs["side"],
        requested_qty=kwargs["qty"],
        reference_price=kwargs["reference_price"],
        reason=kwargs["reason"],
        reduce_only=kwargs.get("reduce_only", False),
        planned_effect_at="2026-08-31T12:00:00Z",
        pre_state_payload=state,
        protection_mode="SOFTWARE",
        entry_direction=(
            1
            if transition == FinancialTransitionType.ENTER_LONG
            else -1
            if transition == FinancialTransitionType.ENTER_SHORT
            else None
        ),
        entry_stop_price=(
            90.0
            if transition
            in {FinancialTransitionType.ENTER_LONG, FinancialTransitionType.ENTER_SHORT}
            else None
        ),
    )


@pytest.fixture(autouse=True)
def _supply_durable_plan(monkeypatch):
    original = OrderExecutionService.submit_market

    def wrapped(self, **kwargs):
        kwargs.setdefault("application_plan", _test_application_plan(**kwargs))
        return original(self, **kwargs)

    monkeypatch.setattr(OrderExecutionService, "submit_market", wrapped)


class StubBroker(PaperBroker):
    def __init__(self, result):
        super().__init__()
        self.result = result
        self.intent_id = None

    def execute_market(self, side, qty, ref_price, *, client_order_id=None, **_kwargs):
        self.intent_id = client_order_id
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.mark.parametrize(
    "result, expected",
    [
        (
            BrokerOrderResult(Fill(100, 0, 0), ExternalOrderState.REJECTED, 1.0, 0.0),
            "REJECTED",
        ),
        (
            BrokerOrderResult(Fill(100, 0.4, 1), ExternalOrderState.PARTIAL_TERMINAL, 1.0, 0.0),
            "PARTIAL",
        ),
        (
            BrokerOrderResult(Fill(100, 0.4, 1), ExternalOrderState.CANCELED, 1.0, 0.0),
            "CANCELED",
        ),
        (
            BrokerOrderResult(Fill(100, 1.0, 1), ExternalOrderState.FILLED, 1.0, 0.0),
            "FILLED",
        ),
    ],
)
def test_result_uses_explicit_external_state_and_intent_is_stable(tmp_path, result, expected):
    store = StateStore(tmp_path / "state.db")
    broker = StubBroker(result)
    service = OrderExecutionService(store, broker)

    result = service.submit_market(
        engine="trend",
        slot="strategy",
        side="BUY",
        qty=1,
        reference_price=100,
        reason="signal",
        decision_checkpoint="2026-08-09T16:00:00Z",
        transition_type=FinancialTransitionType.ENTER_LONG,
    )

    identity = LogicalOrderIdentity(
        "trend",
        "strategy",
        "2026-08-09T16:00:00Z",
        FinancialTransitionType.ENTER_LONG,
    )
    assert result.status == expected
    assert result.intent_id == identity.intent_id
    assert result.logical_order_key == identity.logical_key
    assert broker.intent_id == result.intent_id


def test_paper_error_is_failed_but_external_ambiguity_stays_pending(tmp_path):
    store = StateStore(tmp_path / "state.db")
    paper = StubBroker(TimeoutError("offline"))
    with pytest.raises(TimeoutError):
        OrderExecutionService(store, paper).submit_market(
            engine="trend",
            slot="paper",
            side="BUY",
            qty=1,
            reference_price=100,
            reason="signal",
            decision_checkpoint="paper-checkpoint",
            transition_type=FinancialTransitionType.ENTER_LONG,
        )

    external = StubBroker(TimeoutError("ambiguous"))
    external.external_execution = True
    external.supports_order_lookup = True
    with pytest.raises(ReconciliationRequired, match="résultat externe ambigu"):
        OrderExecutionService(store, external).submit_market(
            engine="trend",
            slot="external",
            side="BUY",
            qty=1,
            reference_price=100,
            reason="signal",
            decision_checkpoint="external-checkpoint",
            transition_type=FinancialTransitionType.ENTER_LONG,
        )

    orders = {order["slot"]: order for order in store.read_orders("trend")}
    statuses = {slot: order["status"] for slot, order in orders.items()}
    assert statuses == {"paper": "FAILED", "external": "PENDING"}
    assert orders["paper"]["local_state"] == LocalOrderState.TERMINAL
    assert orders["external"]["local_state"] == LocalOrderState.PENDING_RECONCILIATION
    assert orders["external"]["external_state"] == ExternalOrderState.UNKNOWN

    paper.result = BrokerOrderResult(
        Fill(100.0, 1.0, 0.1),
        ExternalOrderState.FILLED,
        1.0,
        0.0,
    )
    retried = OrderExecutionService(store, paper).submit_market(
        engine="trend",
        slot="paper",
        side="BUY",
        qty=1,
        reference_price=100,
        reason="signal",
        decision_checkpoint="paper-checkpoint",
        transition_type=FinancialTransitionType.ENTER_LONG,
    )

    assert retried.order_id == orders["paper"]["id"]
    assert len(store.read_orders("trend")) == 2


@pytest.mark.parametrize(
    ("side", "transition"),
    [
        ("INVALID", FinancialTransitionType.EXIT),
        ("SELL", FinancialTransitionType.ENTER_LONG),
        ("BUY", FinancialTransitionType.ENTER_SHORT),
    ],
)
def test_invalid_or_incoherent_side_is_rejected_before_reservation(tmp_path, side, transition):
    store = StateStore(tmp_path / "state.db")
    broker = StubBroker(
        BrokerOrderResult(Fill(100.0, 1.0, 0.0), ExternalOrderState.FILLED, 1.0, 0.0)
    )

    with pytest.raises(ValueError):
        OrderExecutionService(store, broker).submit_market(
            engine="trend",
            slot="slot",
            side=side,
            qty=1.0,
            reference_price=100.0,
            reason="invalid",
            decision_checkpoint="checkpoint",
            transition_type=transition,
        )

    assert store.read_orders("trend") == []
    assert broker.intent_id is None


def test_external_broker_cannot_inherit_a_client_id_dropping_fallback(tmp_path):
    class LegacyExternalBroker(Broker):
        def __init__(self):
            self.market_calls = 0

        def market_buy(self, qty, ref_price):
            self.market_calls += 1
            return BrokerOrderResult(Fill(ref_price, qty, 0.0), ExternalOrderState.FILLED, qty, 0.0)

        def market_sell(self, qty, ref_price):
            return self.market_buy(qty, ref_price)

    store = StateStore(tmp_path / "state.db")
    broker = LegacyExternalBroker()

    with pytest.raises(ReconciliationRequired, match="résultat externe ambigu"):
        OrderExecutionService(store, broker).submit_market(
            engine="trend",
            slot="slot",
            side="BUY",
            qty=1.0,
            reference_price=100.0,
            reason="exit",
            decision_checkpoint="checkpoint",
            transition_type=FinancialTransitionType.EXIT,
            position_generation="entry=2026-08-01T00:00:00Z|initial_qty=1",
            reduce_only=True,
        )

    order = store.read_orders("trend")[0]
    assert broker.market_calls == 0
    assert order["status"] == "PENDING"
    assert order["external_state"] == ExternalOrderState.UNKNOWN


def test_broker_response_persistence_failure_stops_fail_closed(tmp_path, monkeypatch):
    store = StateStore(tmp_path / "state.db")
    broker = StubBroker(
        BrokerOrderResult(Fill(100.0, 1.0, 0.0), ExternalOrderState.FILLED, 1.0, 0.0)
    )
    broker.external_execution = True
    monkeypatch.setattr(
        store,
        "record_order_observation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk unavailable")),
    )

    with pytest.raises(ReconciliationRequired, match="réponse broker non persistée"):
        OrderExecutionService(store, broker).submit_market(
            engine="trend",
            slot="external",
            side="BUY",
            qty=1.0,
            reference_price=100.0,
            reason="entry",
            decision_checkpoint="checkpoint",
            transition_type=FinancialTransitionType.ENTER_LONG,
        )

    order = store.read_orders("trend")[0]
    assert broker.intent_id is not None
    assert order["local_state"] == LocalOrderState.SUBMITTING
    assert order["external_state"] is None


def test_ambiguous_error_persistence_failure_stops_fail_closed(tmp_path, monkeypatch):
    store = StateStore(tmp_path / "state.db")
    broker = StubBroker(TimeoutError("response lost"))
    broker.external_execution = True
    monkeypatch.setattr(
        store,
        "record_submission_error",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk unavailable")),
    )

    with pytest.raises(ReconciliationRequired, match="impossibilité de persister"):
        OrderExecutionService(store, broker).submit_market(
            engine="trend",
            slot="external",
            side="BUY",
            qty=1.0,
            reference_price=100.0,
            reason="entry",
            decision_checkpoint="checkpoint",
            transition_type=FinancialTransitionType.ENTER_LONG,
        )

    assert store.read_orders("trend")[0]["local_state"] == LocalOrderState.SUBMITTING
