"""Carry-only Hyperliquid market adapter with source-time and identity checks.

The shared ``Venue`` adapter remains frozen for existing historical and Trend
provenance. Carry's two-leg PAPER path gets the richer observation contract here.
"""

from __future__ import annotations

import ccxt
import pandas as pd

from .carry_paper import CarryMarketState
from .venue import NETWORK_ERRORS, Venue


class CarryVenue(Venue):
    """Public market-data adapter; it has no order-submission capability."""

    def current_carry_market_state(self, limit: int = 20) -> CarryMarketState:
        spot_book = self._retry.call(
            self.exchange.fetch_order_book,
            self.spot_symbol,
            limit=limit,
            retry_on=NETWORK_ERRORS,
        )
        perp_book = self._retry.call(
            self.exchange.fetch_order_book,
            self.symbol,
            limit=limit,
            retry_on=NETWORK_ERRORS,
        )

        def source_timestamp(book: dict, leg: str) -> pd.Timestamp:
            raw = book.get("timestamp")
            if raw is None:
                raise ccxt.ExchangeNotAvailable(f"timestamp source absent du carnet {leg}")
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                timestamp = pd.Timestamp(raw, unit="ms", tz="UTC")
            else:
                timestamp = pd.Timestamp(raw)
                if timestamp.tzinfo is None:
                    raise ccxt.ExchangeNotAvailable(f"timestamp source non UTC du carnet {leg}")
                timestamp = timestamp.tz_convert("UTC")
            if pd.isna(timestamp):
                raise ccxt.ExchangeNotAvailable(f"timestamp source invalide du carnet {leg}")
            return timestamp

        def top(book: dict, side: str) -> float:
            levels = book.get(side)
            if not isinstance(levels, list) or not levels or len(levels[0]) < 1:
                raise ccxt.ExchangeNotAvailable(f"carnet {side} vide")
            return float(levels[0][0])

        spot_bid, spot_ask = top(spot_book, "bids"), top(spot_book, "asks")
        perp_bid, perp_ask = top(perp_book, "bids"), top(perp_book, "asks")
        spot_market_timestamp = source_timestamp(spot_book, "spot")
        perp_market_timestamp = source_timestamp(perp_book, "perp")
        received_timestamp = pd.Timestamp.now(tz="UTC")
        market_timestamp = min(spot_market_timestamp, perp_market_timestamp)
        freshness_seconds = max(
            (received_timestamp - spot_market_timestamp).total_seconds(),
            (received_timestamp - perp_market_timestamp).total_seconds(),
        )
        if freshness_seconds < 0:
            raise ccxt.ExchangeNotAvailable("timestamp source futur par rapport à l'horloge locale")

        spot_market = self.exchange.market(self.spot_symbol)
        perp_market = self.exchange.market(self.symbol)
        spot_info = spot_market.get("info", {})
        spot_base_name = str(spot_market.get("baseName") or spot_market.get("base") or "")
        spot_currency = self.exchange.currency(spot_base_name) if spot_base_name else None
        spot_currency_info = (
            spot_currency.get("info", {}) if isinstance(spot_currency, dict) else {}
        )
        spot_tokens = spot_info.get("tokens") if isinstance(spot_info, dict) else None
        spot_token_index = (
            int(spot_tokens[0]) if isinstance(spot_tokens, (list, tuple)) and spot_tokens else None
        )
        spot_precision = spot_market.get("precision", {})
        perp_precision = perp_market.get("precision", {})
        spot_limits = spot_market.get("limits", {})
        perp_limits = perp_market.get("limits", {})
        spot_cost_limits = spot_limits.get("cost", {})
        perp_cost_limits = perp_limits.get("cost", {})
        return CarryMarketState(
            timestamp=received_timestamp,
            spot_bid=spot_bid,
            spot_ask=spot_ask,
            perp_bid=perp_bid,
            perp_ask=perp_ask,
            spot_mark=(spot_bid + spot_ask) / 2.0,
            perp_mark=(perp_bid + perp_ask) / 2.0,
            source="public_order_book_snapshot",
            freshness_seconds=freshness_seconds,
            spot_order_book=spot_book,
            perp_order_book=perp_book,
            market_timestamp=market_timestamp,
            received_timestamp=received_timestamp,
            spot_market_timestamp=spot_market_timestamp,
            perp_market_timestamp=perp_market_timestamp,
            spot_market_id=str(spot_market.get("id"))
            if spot_market.get("id") is not None
            else None,
            spot_base_name=spot_base_name or None,
            spot_quote_asset=str(spot_market.get("quote")) if spot_market.get("quote") else None,
            spot_token_index=spot_token_index,
            spot_token_id=(
                str(spot_currency_info.get("tokenId"))
                if isinstance(spot_currency_info, dict) and spot_currency_info.get("tokenId")
                else None
            ),
            spot_is_canonical=(
                spot_info.get("isCanonical") if isinstance(spot_info, dict) else None
            ),
            spot_price_step=(
                float(spot_precision["price"]) if spot_precision.get("price") is not None else None
            ),
            perp_market_id=str(perp_market.get("id"))
            if perp_market.get("id") is not None
            else None,
            perp_base_name=(
                str(perp_market.get("baseName") or perp_market.get("base"))
                if perp_market.get("baseName") or perp_market.get("base")
                else None
            ),
            perp_quote_asset=str(perp_market.get("quote")) if perp_market.get("quote") else None,
            perp_settle_asset=(
                str(perp_market.get("settle")) if perp_market.get("settle") else None
            ),
            perp_price_step=(
                float(perp_precision["price"]) if perp_precision.get("price") is not None else None
            ),
            spot_quantity_step=(
                float(spot_precision["amount"])
                if spot_precision.get("amount") is not None
                else None
            ),
            perp_quantity_step=(
                float(perp_precision["amount"])
                if perp_precision.get("amount") is not None
                else None
            ),
            spot_min_notional=(
                float(spot_cost_limits["min"]) if spot_cost_limits.get("min") is not None else None
            ),
            perp_min_notional=(
                float(perp_cost_limits["min"]) if perp_cost_limits.get("min") is not None else None
            ),
        )
