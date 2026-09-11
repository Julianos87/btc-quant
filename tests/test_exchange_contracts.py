"""Contrats locaux des adaptateurs exchange.

Ces tests ne remplacent pas le sandbox : ils figent les invariants que le
script d'intégration doit ensuite vérifier contre le vrai matching engine.
"""

from __future__ import annotations

import math

import pytest
import ccxt

from btcquant.execution.broker import Broker, BrokerOrderResult, Fill
from btcquant.execution.ccxt_broker import CcxtBroker
from btcquant.execution.order_state import ExternalOrderState


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


@pytest.mark.parametrize("fee", [0.01, 0.0, -0.01, -0.0])
def test_broker_order_result_accepts_finite_signed_fees(fee):
    result = BrokerOrderResult(
        fill=Fill(price=50_000.0, qty=0.01, fee=fee),
        status=ExternalOrderState.FILLED,
        requested_qty=0.01,
        remaining_qty=0.0,
    )

    assert result.fill.fee == fee


@pytest.mark.parametrize("fee", [math.nan, math.inf, -math.inf, True, "invalid"])
def test_broker_order_result_rejects_non_finite_signed_fees(fee):
    with pytest.raises(ValueError, match="fee"):
        BrokerOrderResult(
            fill=Fill(price=50_000.0, qty=0.01, fee=fee),
            status=ExternalOrderState.FILLED,
            requested_qty=0.01,
            remaining_qty=0.0,
        )


@pytest.mark.parametrize("field", ["requested_qty", "remaining_qty", "filled_qty"])
def test_signed_fee_contract_does_not_relax_quantity_signs(field):
    values = {"requested_qty": 0.01, "remaining_qty": 0.0, "filled_qty": 0.01}
    values[field] = -0.01

    with pytest.raises(ValueError, match=field):
        BrokerOrderResult(
            fill=Fill(price=50_000.0, qty=values["filled_qty"], fee=-0.01),
            status=ExternalOrderState.FILLED,
            requested_qty=values["requested_qty"],
            remaining_qty=values["remaining_qty"],
        )


def test_signed_fee_contract_does_not_relax_positive_fill_price():
    with pytest.raises(ValueError, match="prix fini strictement positif"):
        BrokerOrderResult(
            fill=Fill(price=-50_000.0, qty=0.01, fee=-0.01),
            status=ExternalOrderState.FILLED,
            requested_qty=0.01,
            remaining_qty=0.0,
        )


@pytest.mark.parametrize(
    ("fees", "single_fee", "expected"),
    [
        ([{"cost": 0.01}], {"cost": 9.0}, 0.01),
        ([{"cost": -0.01}], {"cost": 9.0}, -0.01),
        ([{"cost": 0.02}, {"cost": -0.01}], {"cost": 9.0}, 0.01),
        ([{"cost": -0.02}, {"cost": 0.01}], {"cost": 9.0}, -0.01),
        ([{"cost": 0.01}, {"cost": -0.01}], {"cost": 9.0}, 0.0),
        ([{"cost": -0.01}, {"cost": -0.02}], {"cost": 9.0}, -0.03),
        ([], {"cost": -0.01}, -0.01),
        (None, {"cost": 0.0}, 0.0),
        (None, {"cost": -0.01}, -0.01),
    ],
)
def test_ccxt_fee_authority_preserves_signed_and_observed_zero(fees, single_fee, expected):
    order = {
        "id": "order-1",
        "average": 50_000.0,
        "filled": 0.01,
        "fee": single_fee,
    }
    if fees is not None:
        order["fees"] = fees

    fill = object.__new__(CcxtBroker)._fill_from_order(order, 50_000.0)

    assert fill.fee == pytest.approx(expected)


@pytest.mark.parametrize("fee", [math.nan, math.inf, -math.inf, True, "invalid"])
def test_ccxt_single_fee_rejects_malformed_or_non_finite_values(fee):
    with pytest.raises(ValueError, match="order.fee.cost"):
        object.__new__(CcxtBroker)._fill_from_order(
            {
                "id": "order-1",
                "average": 50_000.0,
                "filled": 0.01,
                "fee": {"cost": fee},
            },
            50_000.0,
        )


@pytest.mark.parametrize("fee", [math.nan, math.inf, -math.inf, True, "invalid", None])
def test_ccxt_detailed_fee_rejects_malformed_or_non_finite_values(fee):
    with pytest.raises(ValueError, match=r"order\.fees\[0\]\.cost"):
        object.__new__(CcxtBroker)._fill_from_order(
            {
                "id": "order-1",
                "average": 50_000.0,
                "filled": 0.01,
                "fees": [{"cost": fee}],
            },
            50_000.0,
        )


class ProtectiveFeeBroker(Broker):
    def __init__(self, raw):
        self.raw = raw

    def market_buy(self, qty, ref_price):
        raise AssertionError("not used")

    def market_sell(self, qty, ref_price):
        raise AssertionError("not used")

    def stop_status(self, order_id):
        return self.raw


@pytest.mark.parametrize(
    ("fees", "single_fee", "expected"),
    [
        ([{"cost": -0.01}], {"cost": 9.0}, -0.01),
        ([{"cost": 0.01}, {"cost": -0.01}], {"cost": 9.0}, 0.0),
        ([], {"cost": -0.01}, -0.01),
        (None, {"cost": 0.0}, 0.0),
    ],
)
def test_protective_fee_authority_preserves_signed_and_observed_zero(fees, single_fee, expected):
    raw = {
        "id": "stop-1",
        "status": "closed",
        "amount": 0.01,
        "filled": 0.01,
        "remaining": 0.0,
        "average": 50_000.0,
        "fee": single_fee,
    }
    if fees is not None:
        raw["fees"] = fees

    snapshot = ProtectiveFeeBroker(raw).protective_order_snapshot("stop-1")

    assert snapshot.fee == pytest.approx(expected)


def test_protective_positive_fill_keeps_missing_fee_fail_closed():
    with pytest.raises(ValueError, match="fee evidence absente"):
        ProtectiveFeeBroker(
            {"status": "closed", "amount": 0.01, "filled": 0.01, "average": 50_000.0}
        ).protective_order_snapshot("stop-1")
