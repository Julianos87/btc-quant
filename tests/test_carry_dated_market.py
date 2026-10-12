from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from urllib.error import HTTPError
from io import BytesIO

import pytest

from btcquant.execution.carry_dated_market import (
    DeribitPublicAPIError,
    DeribitPublicClient,
    SettlementCursorMissing,
)


NOW = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
CURSOR_MS = 1_791_619_201_684
DELIVERY_MS = 1_791_705_601_922
INSTRUMENT = "BTC_USDC-11OCT26"


def instrument(name: str, *, kind: str, state: str = "open") -> dict:
    return {
        "instrument_name": name,
        "base_currency": "BTC",
        "quote_currency": "USDC",
        "settlement_currency": "USDC" if kind == "future" else None,
        "kind": kind,
        "instrument_type": "linear",
        "settlement_period": "day" if kind == "future" else None,
        "contract_size": "0.0001" if kind == "future" else "0.00000001",
        "min_trade_amount": "0.0001" if kind == "future" else "0.00000001",
        "tick_size": "2.5" if kind == "future" else "0.01",
        "expiration_timestamp": 1_793_347_200_000 if kind == "future" else 32_503_708_800_000,
        "is_active": state == "open",
        "state": state,
        "maker_commission": "0.00015" if kind == "future" else "0.0002",
        "taker_commission": "0.00035" if kind == "future" else "0.0005",
    }


def settlement(kind: str, timestamp: int, *, price: str = "82952.11") -> dict:
    return {
        "instrument_name": INSTRUMENT,
        "type": kind,
        "timestamp": timestamp,
        "index_price": Decimal(price),
        "mark_price": Decimal(price),
        # These fields are public platform aggregates and the adapter must ignore them.
        "position": Decimal("999999"),
        "profit_loss": Decimal("777777"),
        "session_profit_loss": Decimal("888888"),
    }


def test_market_discovery_keeps_btc_spot_and_linear_dated_futures_only() -> None:
    client = DeribitPublicClient(clock=lambda: NOW, sleeper=lambda _: None)

    def fake_get(method: str, params: dict):
        if params["kind"] == "spot":
            return [instrument("BTC_USDC", kind="spot"), instrument("ETH_USDC", kind="spot")], NOW
        return [
            instrument("BTC_USDC-PERPETUAL", kind="future"),
            instrument(INSTRUMENT, kind="future"),
            {**instrument("BTC_USDC-OLD", kind="future"), "instrument_type": "reversed"},
        ], NOW

    client._get = fake_get  # type: ignore[method-assign]
    instruments = client.list_active_instruments()
    assert [item.instrument_id for item in instruments] == ["BTC_USDC", INSTRUMENT]
    spot, future = instruments
    assert spot.fee_currency is None
    assert spot.kind == "spot"
    assert future.kind == "dated_future"
    assert future.amount_step == Decimal("0.0001")
    assert future.contract_size == Decimal("0.0001")


def test_order_book_preserves_decimal_base_quantities_and_market_quality() -> None:
    client = DeribitPublicClient(clock=lambda: NOW, sleeper=lambda _: None)
    client._get = lambda method, params: (
        {
            "instrument_name": INSTRUMENT,
            "timestamp": int(NOW.timestamp() * 1000),
            "state": "open",
            "mark_price": Decimal("83000"),
            "index_price": Decimal("82900"),
            "change_id": 88,
            "bids": [[Decimal("82990"), Decimal("0.02")]],
            "asks": [[Decimal("83000"), Decimal("0.03")]],
        },
        NOW,
    )  # type: ignore[method-assign]
    book = client.get_book(INSTRUMENT)
    assert book.bids[0].quantity == Decimal("0.02")
    assert book.asks[0].quantity == Decimal("0.03")
    assert book.index_price == Decimal("82900")
    assert book.sequence == 88
    assert book.is_tradable(now=NOW)


def test_malformed_or_crossed_book_is_never_marked_tradable() -> None:
    client = DeribitPublicClient(clock=lambda: NOW, sleeper=lambda _: None)
    client._get = lambda method, params: (
        {
            "instrument_name": INSTRUMENT,
            "timestamp": int(NOW.timestamp() * 1000),
            "state": "open",
            "bids": [[Decimal("83000"), Decimal("0.02")]],
            "asks": [[Decimal("83000"), Decimal("0.03")]],
        },
        NOW,
    )  # type: ignore[method-assign]
    book = client.get_book(INSTRUMENT)
    assert not book.is_tradable(now=NOW)
    assert "BOOK_CROSSED_OR_LOCKED" in book.quality_reasons(now=NOW)


def test_active_settlement_cursor_is_inclusive_and_expiry_is_verified() -> None:
    client = DeribitPublicClient(clock=lambda: NOW, sleeper=lambda _: None)
    calls: list[tuple[str, dict]] = []

    def fake_get(method: str, params: dict):
        calls.append((method, params))
        if method == "get_last_settlements_by_instrument" and params["type"] == "settlement":
            return {"settlements": [settlement("settlement", CURSOR_MS)], "continuation": "none"}, NOW
        if method == "get_last_settlements_by_instrument" and params["type"] == "delivery":
            return {"settlements": [settlement("delivery", DELIVERY_MS)], "continuation": "none"}, NOW
        if method == "get_delivery_prices":
            return {
                "data": [{"date": "2026-10-11", "delivery_price": Decimal("82952.11")}],
                "records_total": 1,
            }, NOW
        raise AssertionError(f"unexpected API method {method}")

    client._get = fake_get  # type: ignore[method-assign]
    cursor = f"deribit:{INSTRUMENT}:daily:{CURSOR_MS}"
    events = client.get_settlements(INSTRUMENT, after_key=cursor)
    assert [item.kind for item in events] == ["daily", "expiry"]
    assert events[0].settlement_key == cursor
    expiry = events[1]
    assert expiry.official_index_price == Decimal("82952.11")
    assert expiry.source.endswith("verified delivery-price history")
    assert "profit_loss" not in expiry.content_hash
    assert [name for name, _ in calls].count("get_delivery_prices") == 1


def test_inactive_contract_uses_exact_currency_history_and_verifies_delivery() -> None:
    client = DeribitPublicClient(clock=lambda: NOW, sleeper=lambda _: None)

    def fake_get(method: str, params: dict):
        if method == "get_last_settlements_by_instrument":
            raise DeribitPublicAPIError(
                method,
                "Invalid params",
                reason="instrument is not active",
                http_status=400,
            )
        if method == "get_last_settlements_by_currency" and params["type"] == "settlement":
            return {
                "settlements": [settlement("settlement", CURSOR_MS)],
                "continuation": "none",
            }, NOW
        if method == "get_last_settlements_by_currency" and params["type"] == "delivery":
            return {
                "settlements": [settlement("delivery", DELIVERY_MS)],
                "continuation": "none",
            }, NOW
        if method == "get_delivery_prices":
            return {
                "data": [{"date": "2026-10-11", "delivery_price": Decimal("82952.11")}],
                "records_total": 1,
            }, NOW
        raise AssertionError(f"unexpected API method {method}")

    client._get = fake_get  # type: ignore[method-assign]
    cursor = f"deribit:{INSTRUMENT}:daily:{CURSOR_MS}"
    events = client.get_settlements(INSTRUMENT, after_key=cursor)
    assert len(events) == 2
    assert events[-1].kind == "expiry"
    assert events[-1].official_index_price == Decimal("82952.11")


def test_expired_contract_without_cursor_fails_closed() -> None:
    client = DeribitPublicClient(clock=lambda: NOW, sleeper=lambda _: None)
    client._get = lambda method, params: (_ for _ in ()).throw(
        DeribitPublicAPIError(
            method,
            "Invalid params",
            reason="instrument is not active",
            http_status=400,
        )
    )  # type: ignore[method-assign]
    with pytest.raises(SettlementCursorMissing, match="requires the last persisted"):
        client.get_settlements(INSTRUMENT)


def test_bad_cursor_never_skips_history() -> None:
    client = DeribitPublicClient(clock=lambda: NOW, sleeper=lambda _: None)
    client._get = lambda method, params: (
        {"settlements": [settlement("settlement", CURSOR_MS + 1000)], "continuation": "none"}, NOW
    )  # type: ignore[method-assign]
    cursor = f"deribit:{INSTRUMENT}:daily:{CURSOR_MS}"
    with pytest.raises(SettlementCursorMissing, match="cursor is absent"):
        client.get_settlements(INSTRUMENT, after_key=cursor)


def test_cursor_shape_is_bound_to_instrument() -> None:
    client = DeribitPublicClient(clock=lambda: NOW, sleeper=lambda _: None)
    with pytest.raises(SettlementCursorMissing, match="does not match"):
        client.get_settlements(INSTRUMENT, after_key="deribit:OTHER:daily:123")


def test_delivery_price_disagreement_is_not_replaced_by_a_proxy() -> None:
    client = DeribitPublicClient(clock=lambda: NOW, sleeper=lambda _: None)
    event = client._normalize_settlement(
        INSTRUMENT,
        settlement("delivery", DELIVERY_MS),
        NOW,
    )
    client._get = lambda method, params: (
        {"data": [{"date": "2026-10-11", "delivery_price": Decimal("82952.12")}], "records_total": 1},
        NOW,
    )  # type: ignore[method-assign]
    from btcquant.execution.carry_dated_market import SettlementDataMismatch

    with pytest.raises(SettlementDataMismatch, match="disagree"):
        client._verify_expiry_delivery(event)


def test_retry_count_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    client = DeribitPublicClient(max_attempts=2, sleeper=lambda _: None)
    attempts = 0

    def unavailable(request, timeout):
        nonlocal attempts
        attempts += 1
        raise HTTPError(request.full_url, 503, "unavailable", {}, BytesIO(b""))

    monkeypatch.setattr("btcquant.execution.carry_dated_market.urlopen", unavailable)
    with pytest.raises(DeribitPublicAPIError, match="HTTP 503"):
        client._get("get_instruments", {"kind": "spot"})
    assert attempts == 2

