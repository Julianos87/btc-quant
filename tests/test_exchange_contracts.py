"""Contrats locaux des adaptateurs exchange.

Ces tests ne remplacent pas le sandbox : ils figent les invariants que le
script d'intégration doit ensuite vérifier contre le vrai matching engine.
"""

from __future__ import annotations

import pytest
import ccxt

from btcquant.execution.broker import Broker
from btcquant.execution.ccxt_broker import CcxtBroker


class StopExchange:
    def __init__(self, order_types: list[str]):
        self.order_types = order_types
        self.created = None

    def amount_to_precision(self, _symbol, qty):
        return str(qty)

    def price_to_precision(self, _symbol, price):
        return str(price)

    def market(self, _symbol):
        return {"info": {"orderTypes": self.order_types}}

    def create_order(self, symbol, order_type, side, qty, price, params):
        self.created = (symbol, order_type, side, qty, price, params)
        return {"id": "stop-1"}


def test_hyperliquid_cloid_is_stable_128_bit_hex():
    first = CcxtBroker._external_client_order_id("local-intent", "hyperliquid")
    second = CcxtBroker._external_client_order_id("local-intent", "hyperliquid")

    assert first == second
    assert len(first) == 34
    assert first.startswith("0x")
    int(first[2:], 16)


def test_binance_client_order_id_is_stable_and_within_36_char_limit():
    intent = f"btq-mkt-{'a' * 64}"
    first = CcxtBroker._external_client_order_id(intent, "binance")
    second = CcxtBroker._external_client_order_id(intent, "binance")

    assert first == second
    assert len(first) == 36
    assert first.startswith("btq-")
    int(first[4:], 16)


def test_binance_legacy_client_order_id_mapping_is_preserved_for_recovery():
    legacy = CcxtBroker._external_client_order_id("trend-slot-old-uuid", "binance")

    assert len(legacy) == 32
    assert legacy.startswith("btq-")
    int(legacy[4:], 16)


def test_hyperliquid_stop_is_reduce_only_trigger_market():
    exchange = StopExchange([])
    broker = object.__new__(CcxtBroker)
    broker.exchange = exchange
    broker.exchange_id = "hyperliquid"
    broker.symbol = "BTC/USDC:USDC"
    broker.market_kind = "perp"
    broker._order_seq = 0

    stop_id = broker.place_stop(0.01, 50_000.0, -1, client_order_id="stop-intent")

    assert stop_id == "stop-1"
    assert exchange.created is not None
    _, order_type, side, qty, price, params = exchange.created
    assert order_type == "market"
    assert side == "buy"
    assert qty == pytest.approx(0.01)
    assert price == pytest.approx(50_000.0)
    assert params["stopLossPrice"] == pytest.approx(50_000.0)
    assert params["reduceOnly"] is True
    assert params["clientOrderId"] == CcxtBroker._external_client_order_id(
        "stop-intent", "hyperliquid"
    )


def test_hyperliquid_stop_without_response_oid_is_recovered_by_cloid(monkeypatch):
    exchange = StopExchange([])
    exchange.create_order = lambda *_args, **_kwargs: {"status": "waitingForTrigger"}
    broker = object.__new__(CcxtBroker)
    broker.exchange = exchange
    broker.exchange_id = "hyperliquid"
    broker.symbol = "BTC/USDC:USDC"
    broker.market_kind = "perp"
    broker._order_seq = 0
    attempts = iter([None, type("Snapshot", (), {"broker_order_id": "indexed-stop"})()])
    monkeypatch.setattr(broker, "lookup_order", lambda _intent: next(attempts))
    monkeypatch.setattr("btcquant.execution.ccxt_broker.time.sleep", lambda _seconds: None)

    stop_id = broker.place_stop(0.01, 50_000.0, client_order_id="delayed-index")

    assert stop_id == "indexed-stop"


def _spot_broker(exchange: StopExchange) -> CcxtBroker:
    broker = object.__new__(CcxtBroker)
    broker.exchange = exchange
    broker.symbol = "BTC/USDT"
    broker.market_kind = "spot"
    broker._order_seq = 0
    return broker


def test_spot_stop_is_market_after_trigger_not_limit():
    exchange = StopExchange(["LIMIT", "MARKET", "STOP_LOSS", "STOP_LOSS_LIMIT"])
    broker = _spot_broker(exchange)

    stop_id = broker.place_stop(0.01, 50_000.0, client_order_id="protective-intent")

    assert stop_id == "stop-1"
    assert exchange.created is not None
    _, order_type, side, qty, price, params = exchange.created
    assert order_type == "STOP_LOSS"
    assert side == "sell"
    assert qty == pytest.approx(0.01)
    assert price is None
    assert params["stopPrice"] == pytest.approx(50_000.0)
    assert params["newClientOrderId"] == CcxtBroker._external_client_order_id("protective-intent")


def test_spot_live_fails_closed_without_market_stop_contract():
    broker = _spot_broker(StopExchange(["LIMIT", "MARKET", "STOP_LOSS_LIMIT"]))

    with pytest.raises(RuntimeError, match="STOP_LOSS market"):
        broker.place_stop(0.01, 50_000.0)


def test_spot_short_stop_is_rejected_before_exchange_call():
    exchange = StopExchange(["STOP_LOSS"])
    broker = _spot_broker(exchange)

    with pytest.raises(ValueError, match="short impossible"):
        broker.place_stop(0.01, 50_000.0, direction=-1)
    assert exchange.created is None


def test_cancel_order_not_found_is_not_swallowed():
    class CancelExchange:
        def cancel_order(self, _order_id, _symbol):
            raise ccxt.OrderNotFound("gone")

    broker = object.__new__(CcxtBroker)
    broker.exchange = CancelExchange()
    broker.symbol = "BTC/USDT"

    with pytest.raises(ccxt.OrderNotFound):
        broker.cancel_stop("gone")


def test_broker_order_result_accepts_signed_fees():
    from btcquant.execution.broker import BrokerOrderResult, Fill
    from btcquant.execution.order_state import ExternalOrderState

    for fee in (1.25, 0.0, -1.25):
        result = BrokerOrderResult(
            Fill(price=100.0, qty=1.0, fee=fee),
            ExternalOrderState.FILLED,
            requested_qty=1.0,
            remaining_qty=0.0,
        )
        assert result.fill.fee == fee


@pytest.mark.parametrize("fee", [float("nan"), float("inf"), float("-inf")])
def test_broker_order_result_rejects_nonfinite_fees(fee):
    from btcquant.execution.broker import BrokerOrderResult, Fill
    from btcquant.execution.order_state import ExternalOrderState

    with pytest.raises(ValueError, match="fee"):
        BrokerOrderResult(
            Fill(price=100.0, qty=1.0, fee=fee),
            ExternalOrderState.FILLED,
            requested_qty=1.0,
            remaining_qty=0.0,
        )


def test_ccxt_detailed_fee_records_take_precedence_even_when_net_zero():
    broker = object.__new__(CcxtBroker)
    fill = broker._fill_from_order(
        {
            "id": "order-1",
            "average": 100.0,
            "filled": 1.0,
            "fees": [{"cost": 1.0}, {"cost": -1.0}],
            "fee": {"cost": 2.0},
        },
        fallback_price=100.0,
    )
    assert fill.fee == pytest.approx(0.0)


@pytest.mark.parametrize(
    "fees, singular, expected",
    [
        ([{"cost": -1.0}], {"cost": 2.0}, -1.0),
        ([{"cost": 1.0}, {"cost": 2.0}], {"cost": 9.0}, 3.0),
        ([{"cost": None}], {"cost": 2.0}, 0.0),
        (None, {"cost": -1.0}, -1.0),
        ([], {"cost": -1.0}, -1.0),
        ([{"cost": 0.0}], {"cost": 2.0}, 0.0),
    ],
)
def test_ccxt_fee_aggregation_preserves_signed_detailed_evidence(fees, singular, expected):
    broker = object.__new__(CcxtBroker)
    order = {"id": "order-1", "average": 100.0, "filled": 1.0, "fee": singular}
    if fees is not None:
        order["fees"] = fees
    fill = broker._fill_from_order(order, fallback_price=100.0)
    assert fill.fee == pytest.approx(expected)


class _ProtectiveFeeBroker(Broker):
    def __init__(self, raw):
        self.raw = raw

    def market_buy(self, qty, ref_price):
        raise AssertionError("not used")

    def market_sell(self, qty, ref_price):
        raise AssertionError("not used")

    def stop_status(self, order_id):
        return self.raw


def _protective_fee_snapshot(raw):
    return _ProtectiveFeeBroker(raw).protective_order_snapshot("stop-1")


@pytest.mark.parametrize(
    "fees, singular, expected",
    [
        ([{"cost": 1.0}, {"cost": -1.0}], {"cost": 2.0}, 0.0),
        ([{"cost": -1.25}], {"cost": 2.0}, -1.25),
        (None, {"cost": -1.25}, -1.25),
        ([], {"cost": -1.25}, -1.25),
    ],
)
def test_protective_fee_aggregation_preserves_signed_detailed_evidence(fees, singular, expected):
    raw = {
        "id": "stop-1",
        "status": "closed",
        "amount": 1.0,
        "filled": 1.0,
        "remaining": 0.0,
        "average": 100.0,
        "fee": singular,
    }
    if fees is not None:
        raw["fees"] = fees
    snapshot = _protective_fee_snapshot(raw)
    assert snapshot.fee == pytest.approx(expected)
