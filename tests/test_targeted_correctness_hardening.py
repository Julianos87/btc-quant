"""Characterization and negative tests for targeted execution hardening."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from btcquant.execution.broker import Broker
from btcquant.execution.ccxt_broker import CcxtBroker
from btcquant.execution.external_settlement_recovery import ExternalSettlementStartupRecovery
from btcquant.execution.external_settlement_runtime import _commitment as runtime_commitment
from btcquant.execution.external_submission_commitment import build_submission_response


RAW_FILLED = {
    "id": "9001",
    "status": "closed",
    "amount": "1.25",
    "filled": "1.25",
    "remaining": "0",
    "average": "100000",
    "fees": [],
    "info": {
        "status": "filled",
        "response": {
            "type": "order",
            "data": {"statuses": [{"filled": {"totalSz": "1.25", "avgPx": "100000", "oid": 9001}}]},
        },
    },
}


def _response(raw: dict | None = None, *, local_order_id: int = 1):
    return build_submission_response(
        local_order_id=local_order_id,
        intent_id=f"intent-{local_order_id}",
        venue="hyperliquid",
        environment="testnet",
        account_scope="account",
        instrument="BTC/USDC:USDC",
        side="BUY",
        client_order_id="0x" + f"{local_order_id:032x}",
        raw_payload=deepcopy(RAW_FILLED if raw is None else raw),
        response_acquired_at="2026-09-05T12:00:00Z",
        ioc_expected=True,
    )


def _broker(exchange: object) -> CcxtBroker:
    broker = object.__new__(CcxtBroker)
    broker.exchange = exchange
    broker.symbol = "BTC/USDT"
    return broker


def test_valid_ccxt_values_retain_exact_previous_semantics() -> None:
    broker = _broker(
        SimpleNamespace(
            fetch_balance=lambda: {"free": {"USDT": "125.5"}},
            fetch_positions=lambda _symbols: [
                {"contracts": "2", "side": "long"},
                {"contracts": "0.75", "side": "short"},
            ],
        )
    )
    fill = broker._fill_from_order(RAW_FILLED, 99_999.0)
    assert (fill.qty, fill.price, fill.fee) == (1.25, 100_000.0, 0.0)
    assert broker.free_quote_balance() == 125.5
    assert broker.net_position("BTC/USDT") == 1.25


def test_empty_authoritative_position_list_still_proves_flat() -> None:
    broker = _broker(SimpleNamespace(fetch_positions=lambda _symbols: []))
    assert broker.net_position("BTC/USDT") == 0.0


@pytest.mark.parametrize(
    "missing", [object(), None, "", "not-a-number", float("nan"), float("inf")]
)
def test_missing_or_invalid_filled_never_becomes_zero(missing: object) -> None:
    order = dict(RAW_FILLED)
    if type(missing) is object:
        order.pop("filled")
    else:
        order["filled"] = missing
    with pytest.raises(ValueError, match="filled"):
        _broker(SimpleNamespace())._fill_from_order(order, 99_999.0)


@pytest.mark.parametrize("value", [None, "", "bad", float("nan"), float("inf"), -1])
def test_missing_or_invalid_authoritative_balance_never_becomes_zero(value: object) -> None:
    free = {} if value is None else {"USDT": value}
    broker = _broker(SimpleNamespace(fetch_balance=lambda: {"free": free}))
    with pytest.raises(ValueError, match="balance.*USDT"):
        broker.free_quote_balance()


@pytest.mark.parametrize(
    "position",
    [
        {},
        {"contracts": None, "side": "long"},
        {"contracts": "", "side": "long"},
        {"contracts": "bad", "side": "long"},
        {"contracts": float("nan"), "side": "long"},
        {"contracts": float("inf"), "side": "long"},
        {"contracts": -1, "side": "long"},
        {"contracts": 1, "side": None},
        {"contracts": 1, "side": "flat"},
    ],
)
def test_invalid_position_row_never_becomes_flat(position: dict) -> None:
    broker = _broker(SimpleNamespace(fetch_positions=lambda _symbols: [position]))
    with pytest.raises(ValueError, match="position"):
        broker.net_position("BTC/USDT")


@pytest.mark.parametrize("field", ["amount", "filled"])
def test_lookup_order_requires_authoritative_quantities(field: str) -> None:
    order = deepcopy(RAW_FILLED)
    order.pop(field)
    broker = _broker(SimpleNamespace(fetch_order=lambda *_args, **_kwargs: order))
    broker.exchange_id = "binance"
    with pytest.raises(ValueError, match=field):
        broker.lookup_order("intent-1")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("amount", 0),
        ("amount", -1),
        ("amount", float("nan")),
        ("remaining", -1),
        ("remaining", float("nan")),
        ("remaining", float("inf")),
    ],
)
def test_invalid_order_quantities_fail_closed(field: str, value: object) -> None:
    order = deepcopy(RAW_FILLED)
    order[field] = value
    with pytest.raises(ValueError, match=field):
        _broker(SimpleNamespace())._result_from_order(order, 100_000.0, 1.25)


@pytest.mark.parametrize("field", ["amount", "filled"])
def test_protective_snapshot_requires_authoritative_quantities(field: str) -> None:
    raw = {
        "id": "stop-1",
        "status": "open",
        "amount": 1.0,
        "filled": 0.0,
        "remaining": 1.0,
        "fees": [],
    }
    raw.pop(field)

    class _StopBroker(Broker):
        def market_buy(self, qty: float, ref_price: float):
            raise NotImplementedError

        def market_sell(self, qty: float, ref_price: float):
            raise NotImplementedError

        def stop_status(self, order_id: str) -> dict:
            return raw

    with pytest.raises(ValueError, match=field):
        _StopBroker().protective_order_snapshot("stop-1")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("amount", 0),
        ("amount", -1),
        ("filled", -1),
        ("filled", 2),
        ("remaining", -1),
    ],
)
def test_protective_snapshot_rejects_invalid_quantities(field: str, value: object) -> None:
    raw = {
        "id": "stop-1",
        "status": "open",
        "amount": 1.0,
        "filled": 0.0,
        "remaining": 1.0,
        "fees": [],
    }
    raw[field] = value

    class _StopBroker(Broker):
        def market_buy(self, qty: float, ref_price: float):
            raise NotImplementedError

        def market_sell(self, qty: float, ref_price: float):
            raise NotImplementedError

        def stop_status(self, order_id: str) -> dict:
            return raw

    with pytest.raises(ValueError, match=field):
        _StopBroker().protective_order_snapshot("stop-1")


@pytest.mark.parametrize("missing", ["average", "fees"])
def test_filled_protective_snapshot_requires_financial_evidence(missing: str) -> None:
    raw = {
        "id": "stop-1",
        "status": "closed",
        "amount": 1.0,
        "filled": 1.0,
        "remaining": 0.0,
        "average": 99.0,
        "fees": [],
    }
    raw.pop(missing)

    class _StopBroker(Broker):
        def market_buy(self, qty: float, ref_price: float):
            raise NotImplementedError

        def market_sell(self, qty: float, ref_price: float):
            raise NotImplementedError

        def stop_status(self, order_id: str) -> dict:
            return raw

    with pytest.raises(ValueError, match=missing if missing == "average" else "fee evidence"):
        _StopBroker().protective_order_snapshot("stop-1")


def test_runtime_and_recovery_commitment_selection_are_identical() -> None:
    first = _response()
    same = _response()
    other = _response(local_order_id=2)
    for responses in ([], [first], [first, same], [first, other]):
        assert runtime_commitment(responses) == ExternalSettlementStartupRecovery._commitment(
            responses
        )
