from __future__ import annotations

import pytest

from btcquant.execution.broker import Fill, PaperBroker
from btcquant.execution.order_service import OrderExecutionService
from btcquant.execution.state_store import StateStore


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
    "fill, expected",
    [
        (Fill(100, 0, 0), "REJECTED"),
        (Fill(100, 0.4, 1), "PARTIAL"),
        (Fill(100, 1.0, 1), "FILLED"),
    ],
)
def test_result_is_classified_and_intent_is_stable(tmp_path, fill, expected):
    store = StateStore(tmp_path / "state.db")
    broker = StubBroker(fill)
    service = OrderExecutionService(store, broker, intent_factory=lambda: "fixed")

    result = service.submit_market(
        engine="trend",
        slot="strategy",
        side="BUY",
        qty=1,
        reference_price=100,
        reason="signal",
    )

    assert result.status == expected
    assert result.intent_id == "trend-strategy-fixed"
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
        )

    external = StubBroker(TimeoutError("ambiguous"))
    external.supports_order_lookup = True
    with pytest.raises(TimeoutError):
        OrderExecutionService(store, external).submit_market(
            engine="trend",
            slot="external",
            side="BUY",
            qty=1,
            reference_price=100,
            reason="signal",
        )

    statuses = {order["slot"]: order["status"] for order in store.read_orders("trend")}
    assert statuses == {"paper": "FAILED", "external": "PENDING"}
