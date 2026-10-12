from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from btcquant.domain.carry_dated import (
    BookLevel,
    DatedBook,
    DatedCarryDecision,
    DatedInstrument,
    DatedSettlement,
)


NOW = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)


def test_book_uses_base_units_and_refuses_stale_future_or_crossed_data() -> None:
    book = DatedBook(
        instrument_id="BTC_USDC-30OCT26",
        bids=(BookLevel(Decimal("84380"), Decimal("0.1")),),
        asks=(BookLevel(Decimal("84382.5"), Decimal("0.2")),),
        market_timestamp=NOW - timedelta(seconds=2),
        received_timestamp=NOW,
        source="https://www.deribit.com/api/v2/public/get_order_book",
        instrument_state="open",
        sequence=42,
    )
    assert book.is_tradable(now=NOW)
    assert book.bids[0].quantity == Decimal("0.1")
    assert book.quality_reasons(now=NOW + timedelta(seconds=20)) == ("BOOK_STALE",)

    crossed = DatedBook(
        instrument_id="BTC_USDC",
        bids=(BookLevel(Decimal("100"), Decimal("1")),),
        asks=(BookLevel(Decimal("100"), Decimal("1")),),
        market_timestamp=NOW,
        received_timestamp=NOW,
        source="fixture",
        instrument_state="open",
    )
    assert not crossed.is_tradable(now=NOW)
    assert "BOOK_CROSSED_OR_LOCKED" in crossed.quality_reasons(now=NOW)


def test_book_rejects_timezone_naive_timestamps() -> None:
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        BookLevel(Decimal("1"), Decimal("1"))
        DatedBook(
            instrument_id="BTC_USDC",
            bids=(),
            asks=(),
            market_timestamp=datetime(2026, 10, 12),
            received_timestamp=NOW,
            source="fixture",
            instrument_state="open",
        )


def test_instrument_rejects_perpetual_masquerading_as_dated_future() -> None:
    with pytest.raises(ValueError, match="require an expiry"):
        DatedInstrument(
            venue="deribit",
            instrument_id="BTC_USDC-PERPETUAL",
            kind="dated_future",
            base="BTC",
            quote="USDC",
            settlement_currency="USDC",
            contract_size=Decimal("0.0001"),
            amount_step=Decimal("0.0001"),
            min_amount=Decimal("0.0001"),
            tick_size=Decimal("0.1"),
            expiration_utc=None,
            active=True,
            maker_commission=Decimal("0.00015"),
            taker_commission=Decimal("0.00035"),
            fee_provenance="public instrument metadata",
            metadata_timestamp=NOW,
            metadata_hash="a" * 64,
            instrument_state="open",
            source="fixture",
        )


def test_settlement_identity_and_official_price_are_exact_decimals() -> None:
    settlement = DatedSettlement(
        venue="deribit",
        instrument_id="BTC_USDC-11OCT26",
        settlement_key="deribit:BTC_USDC-11OCT26:expiry:1791705601922",
        kind="expiry",
        period_start=NOW - timedelta(minutes=30),
        period_end=NOW,
        official_index_price=Decimal("82952.11"),
        source="public/get_delivery_prices",
        received_at=NOW,
        content_hash="b" * 64,
        correction_version="sha256:b" * 64,
        event_timestamp=NOW,
    )
    assert settlement.official_index_price == Decimal("82952.11")


def test_carry_decision_serializes_decimal_strings_and_nullable_costs() -> None:
    decision = DatedCarryDecision(
        decision_id="dec-1",
        decision_at=NOW,
        action="WAIT",
        reason_code="COST_MODEL_INCOMPLETE",
        reason_message="delivery cost is unknown",
        as_of=NOW,
        selected_instrument_id=None,
        expiry_utc=None,
        quantity=Decimal("0"),
        spot_estimated_price=None,
        future_estimated_price=None,
        spot_depth_quantity=Decimal("0"),
        future_depth_quantity=Decimal("0"),
        capital=Decimal("4000.00"),
        free_cash=Decimal("4000.00"),
        spot_cash_required=Decimal("0"),
        margin_cash=Decimal("0"),
        stress_cash=Decimal("0"),
        fees=None,
        gross_premium=Decimal("0"),
        net_before_reserve=None,
        risk_reserve=Decimal("0"),
        net_after_reserve=None,
        policy_version="dated-v1",
        spot_market_timestamp=None,
        future_market_timestamp=None,
        spot_fee=None,
        future_entry_fee=None,
        delivery_fee_estimate=None,
        exit_fee_estimate=None,
        exit_execution_reserve=Decimal("0"),
        initial_margin_rate=None,
        maintenance_margin_rate=None,
        total_cash_required=Decimal("0"),
        cash_after_entry=Decimal("4000"),
        fee_model_version="fees-unknown-v1",
        margin_model_version="margin-unknown-v1",
        source_hashes=("c" * 64,),
    )
    payload = decision.to_dict()
    json.dumps(payload)
    assert payload["capital"] == "4000.00"
    assert payload["delivery_fee_estimate"] is None
    assert payload["action"] == "WAIT"

