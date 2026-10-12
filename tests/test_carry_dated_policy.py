from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from btcquant.domain.carry_dated import BookLevel, DatedBook, DatedInstrument
from btcquant.domain.carry_dated_policy import (
    FeeSchedule,
    MarginPolicy,
    evaluate_entry,
)


NOW = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
SOURCE = "https://www.deribit.com/api/v2/public/get_order_book"


def instrument(
    instrument_id: str,
    *,
    kind: str,
    expiration: datetime | None = None,
    min_amount: str = "0.001",
) -> DatedInstrument:
    return DatedInstrument(
        venue="deribit",
        instrument_id=instrument_id,
        kind=kind,  # type: ignore[arg-type]
        base="BTC",
        quote="USDC",
        settlement_currency="USDC" if kind == "dated_future" else None,
        contract_size=Decimal("1"),
        amount_step=Decimal("0.0001"),
        min_amount=Decimal(min_amount),
        tick_size=Decimal("0.1"),
        expiration_utc=expiration,
        active=True,
        maker_commission=None,
        taker_commission=None,
        fee_currency=None,
        fee_provenance="public metadata; spot fee currency unconfirmed",
        metadata_timestamp=NOW - timedelta(minutes=1),
        metadata_hash=("a" if kind == "spot" else instrument_id[-1]) * 64,
        instrument_state="open",
        source=SOURCE,
    )


def book(
    instrument_id: str,
    *,
    bid: str,
    ask: str,
    timestamp: datetime = NOW - timedelta(seconds=1),
    quantity: str = "0.05",
    mark: str | None = None,
    index: str | None = None,
    content_hash: str | None = None,
) -> DatedBook:
    return DatedBook(
        instrument_id=instrument_id,
        bids=(BookLevel(Decimal(bid), Decimal(quantity)),),
        asks=(BookLevel(Decimal(ask), Decimal(quantity)),),
        market_timestamp=timestamp,
        received_timestamp=NOW,
        source=SOURCE,
        instrument_state="open",
        mark_price=Decimal(mark) if mark else None,
        index_price=Decimal(index) if index else None,
        content_hash=content_hash or ("b" if "SPOT" in instrument_id else "c") * 64,
    )


def candidate(
    *,
    expiry_days: int = 30,
    premium: str = "10000",
    future_id: str = "BTC_USDC-11NOV26",
    spot_quantity: str = "0.05",
    future_quantity: str = "0.05",
    min_amount: str = "0.001",
    spot_market_time: datetime = NOW - timedelta(seconds=1),
) -> tuple[DatedInstrument, DatedInstrument, DatedBook, DatedBook]:
    spot = instrument("BTC_USDC", kind="spot", min_amount=min_amount)
    future = instrument(
        future_id,
        kind="dated_future",
        expiration=NOW + timedelta(days=expiry_days),
        min_amount=min_amount,
    )
    spot_book = book(
        spot.instrument_id,
        bid="99900",
        ask="100000",
        timestamp=spot_market_time,
        quantity=spot_quantity,
    )
    future_book = book(
        future.instrument_id,
        bid=str(Decimal("100000") + Decimal(premium)),
        ask=str(Decimal("100100") + Decimal(premium)),
        quantity=future_quantity,
        mark=str(Decimal("100000") + Decimal(premium)),
        index="100000",
    )
    return spot, future, spot_book, future_book


def decide(*candidates, capital: str = "4000"):
    return evaluate_entry(
        candidates,
        capital=Decimal(capital),
        fee_schedule=FeeSchedule.standard_deribit(),
        margin_policy=MarginPolicy.deribit_ssm_linear_btc(),
        now=NOW,
    )


def test_margin_policy_applies_tier4_formula_and_plus_25_percent_cash_stress() -> None:
    assessment = MarginPolicy().assess(Decimal("0.01"), Decimal("110000"))

    assert assessment.initial_margin_rate == Decimal("0.04")
    assert assessment.maintenance_margin_rate == Decimal(2) / Decimal(3) * Decimal("0.04")
    assert assessment.stress_move_rate == Decimal("0.25")
    assert assessment.stress_price_usdc == Decimal("137500.00")
    assert assessment.stress_short_loss_usdc == Decimal("275.00")
    assert assessment.total_stress_collateral_usdc > assessment.initial_margin_usdc
    with pytest.raises(ValueError, match="max amount"):
        MarginPolicy().initial_margin(Decimal("886"), Decimal("100000"))


def test_entry_uses_earliest_expiry_and_reports_unknown_fee_currency_residual() -> None:
    early = candidate(expiry_days=20, premium="10000", future_id="BTC_USDC-01NOV26")
    later = candidate(expiry_days=30, premium="20000", future_id="BTC_USDC-11NOV26")

    decision = decide(later, early)

    assert decision.action == "OPEN"
    assert decision.selected_instrument_id == "BTC_USDC-01NOV26"
    assert decision.spot_fee_currency_assumption == "UNKNOWN_WORST_CASE"
    assert decision.spot_gross_quantity > decision.quantity
    assert decision.spot_expected_net_quantity >= decision.quantity
    assert decision.expected_hedge_residual > 0
    assert decision.net_after_reserve is not None and decision.net_after_reserve > 0
    assert decision.spot_market_timestamp is not None
    assert decision.future_market_timestamp is not None


def test_decision_id_changes_with_book_hash_but_repeats_for_identical_inputs() -> None:
    original = candidate()
    same = decide(original)
    repeated = decide(original)
    changed_spot_book = replace(original[2], content_hash="d" * 64)
    changed = decide((original[0], original[1], changed_spot_book, original[3]))

    assert same.decision_id == repeated.decision_id
    assert same.decision_id != changed.decision_id
    assert same.source_hashes != changed.source_hashes


def test_selected_expiry_with_bad_data_does_not_fall_through_to_later_contract() -> None:
    early = candidate(expiry_days=20, future_id="BTC_USDC-01NOV26")
    later = candidate(expiry_days=30, future_id="BTC_USDC-11NOV26", premium="20000")
    stale_book = replace(early[2], market_timestamp=NOW - timedelta(seconds=20))

    decision = decide((early[0], early[1], stale_book, early[3]), later)

    assert decision.action == "WAIT"
    assert decision.reason_code == "BOOK_INVALID_OR_STALE"
    assert decision.selected_instrument_id == "BTC_USDC-01NOV26"


def test_book_identity_must_match_instrument_and_content_hash_is_required() -> None:
    item = candidate()
    mismatched = replace(item[2], instrument_id="BTC_USDC_OTHER")
    decision = decide((item[0], item[1], mismatched, item[3]))
    no_hash = replace(item[2], content_hash=None)
    missing_hash = decide((item[0], item[1], no_hash, item[3]))

    assert decision.reason_code == "BOOK_INSTRUMENT_MISMATCH"
    assert missing_hash.action == "WAIT"
    assert missing_hash.reason_code == "BOOK_INVALID_OR_STALE"


def test_unknown_spot_fee_currency_blocks_only_if_its_quote_cash_case_blocks() -> None:
    item = candidate(
        spot_quantity="0.02", future_quantity="0.02", min_amount="0.0199"
    )
    fees = FeeSchedule.standard_deribit()
    margin = MarginPolicy()
    baseline = evaluate_entry(
        (item,), capital=Decimal("4000"), fee_schedule=fees, margin_policy=margin, now=NOW
    )
    assert baseline.action == "OPEN"
    base_only_cash = baseline.total_cash_required - (baseline.spot_fee or Decimal("0"))
    capital_between_cases = (base_only_cash + baseline.total_cash_required) / 2

    decision = evaluate_entry(
        (item,),
        capital=capital_between_cases,
        fee_schedule=fees,
        margin_policy=margin,
        now=NOW,
    )

    assert decision.action == "WAIT"
    assert decision.reason_code == "WAIT_FEE_CURRENCY_UNCERTAIN"


def test_deribit_model_assumptions_are_versioned_and_fees_are_not_funding() -> None:
    fees = FeeSchedule.standard_deribit()
    assert fees.spot_taker_rate == Decimal("0.0005")
    assert fees.future_taker_rate == Decimal("0.00035")
    assert fees.delivery_rate == Decimal("0.00025")
    assert fees.spot_exit_taker_rate == Decimal("0.0005")
    assert fees.version
    assert fees.fee_sources
    assert "funding" not in fees.version.lower()
