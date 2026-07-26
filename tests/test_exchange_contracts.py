"""Contrats locaux des adaptateurs exchange.

Ces tests ne remplacent pas le sandbox : ils figent les invariants que le
script d'intégration doit ensuite vérifier contre le vrai matching engine.
"""

from __future__ import annotations

import pytest

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
