from __future__ import annotations

from datetime import UTC, datetime

from btcquant.execution.carry_venue import CarryVenue


class FakeRetry:
    def call(self, function, *args, **kwargs):
        kwargs.pop("retry_on", None)
        return function(*args, **kwargs)


class FakeExchange:
    def __init__(self) -> None:
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        self.books = {
            "BTC/USDC": {
                "timestamp": now_ms,
                "bids": [[83_000.0, 0.4]],
                "asks": [[83_001.0, 0.5]],
            },
            "BTC/USDC:USDC": {
                "timestamp": now_ms,
                "bids": [[83_039.0, 0.4]],
                "asks": [[83_040.0, 0.5]],
            },
        }
        self.markets = {
            "BTC/USDC": {
                "id": "@142",
                "base": "BTC",
                "baseName": "UBTC",
                "quote": "USDC",
                "info": {"tokens": [197, 0], "isCanonical": False},
                "precision": {"amount": 0.00001, "price": 1.0},
                "limits": {"cost": {"min": 10.0}},
            },
            "BTC/USDC:USDC": {
                "id": "0",
                "base": "BTC",
                "baseName": "BTC",
                "quote": "USDC",
                "settle": "USDC",
                "precision": {"amount": 0.00001, "price": 1.0},
                "limits": {"cost": {"min": 10.0}},
            },
        }

    def fetch_order_book(self, symbol: str, *, limit: int) -> dict:
        del limit
        return self.books[symbol]

    def market(self, symbol: str) -> dict:
        return self.markets[symbol]

    def currency(self, code: str) -> dict:
        assert code == "UBTC"
        return {
            "info": {
                "tokenId": "0x8f254b963e8468305d409b33aa137c67",
                "evmContract": {"address": "0x9fdbda0a5e284c32744d2f17ee5c74b284993463"},
            }
        }


def test_carry_venue_preserves_source_clocks_and_full_pair_identity() -> None:
    venue = CarryVenue.__new__(CarryVenue)
    venue._retry = FakeRetry()
    venue.exchange = FakeExchange()
    venue.spot_symbol = "BTC/USDC"
    venue.symbol = "BTC/USDC:USDC"

    market = venue.current_carry_market_state()
    assert market.spot_market_id == "@142"
    assert market.spot_base_name == "UBTC"
    assert market.spot_token_index == 197
    assert market.spot_token_id == "0x8f254b963e8468305d409b33aa137c67"
    assert market.spot_evm_contract_address == "0x9fdbda0a5e284c32744d2f17ee5c74b284993463"
    assert market.spot_is_canonical is False
    assert market.perp_market_id == "0"
    assert market.perp_settle_asset == "USDC"
    assert market.spot_quantity_step == market.perp_quantity_step == 0.00001
    assert market.spot_min_notional == market.perp_min_notional == 10.0
    assert market.spot_price_step == market.perp_price_step == 1.0
    assert market.freshness_seconds >= 0
    assert market.received_timestamp >= market.market_timestamp
