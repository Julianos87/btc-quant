from __future__ import annotations

import hashlib
import json

import pytest

from btcquant.execution.ccxt_broker import CcxtBroker
from btcquant.execution.external_submission_commitment import (
    ExternalSubmissionOutcome,
    IOC_NO_MATCH_ERROR,
    SubmissionCommitmentError,
    build_submission_response,
)
from btcquant.execution.order_state import ExternalOrderState
from btcquant.execution.state_store import StateStore


RAW_FILLED = {
    "id": "9001",
    "status": "closed",
    "amount": "1.25",
    "filled": "1.25",
    "average": "100000",
    "info": {
        "status": "filled",
        "response": {
            "type": "order",
            "data": {"statuses": [{"filled": {"totalSz": "1.25", "avgPx": "100000", "oid": 9001}}]},
        },
    },
}


def _response(raw=None, *, acquired="2026-09-05T12:00:00Z"):
    return build_submission_response(
        local_order_id=1,
        intent_id="btq-mkt-intent",
        venue="hyperliquid",
        environment="testnet",
        account_scope="0x" + "1" * 40,
        instrument="BTC/USDC:USDC",
        side="BUY",
        client_order_id="0x" + "2" * 32,
        raw_payload=RAW_FILLED if raw is None else raw,
        response_acquired_at=acquired,
        ioc_expected=True,
    )


def test_filled_response_produces_decimal_commitment_without_network() -> None:
    response = _response()

    assert response.outcome == ExternalSubmissionOutcome.FILLED_COMMITMENT
    assert response.commitment is not None
    assert response.commitment.external_order_id == "9001"
    assert response.commitment.total_filled_qty == "1.25"
    assert response.commitment.average_price == "100000"
    assert (
        response.commitment.raw_response_hash
        == hashlib.sha256(
            json.dumps(RAW_FILLED, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    assert response.commitment.submission_key.startswith("submission-")


def test_same_submission_response_is_idempotent_even_if_acquisition_time_differs(tmp_path) -> None:
    store = StateStore(tmp_path / "state.db")
    order_id = store.begin_order("trend", "slot", "btq-mkt-intent", "MARKET", "BUY", 1.25, "entry")
    first = _response()
    second = _response(acquired="2026-09-05T12:01:00+00:00")
    assert order_id == 1

    _, first_created = store.append_external_submission_response(first, engine="trend")
    _, second_created = store.append_external_submission_response(second, engine="trend")

    assert first_created is True
    assert second_created is False
    assert len(store.read_external_submission_responses("btq-mkt-intent")) == 1


def test_same_submission_key_with_different_response_is_a_conflict(tmp_path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.begin_order("trend", "slot", "btq-mkt-intent", "MARKET", "BUY", 1.25, "entry")
    first = _response()
    changed = dict(RAW_FILLED)
    changed["average"] = "100001"
    second = _response(changed)
    store.append_external_submission_response(first, engine="trend")

    with pytest.raises(SubmissionCommitmentError, match="CONFLICT"):
        store.append_external_submission_response(second, engine="trend")


@pytest.mark.parametrize("status", ["open", "new", "accepted"])
def test_ioc_resting_response_is_not_a_fill_commitment(status: str) -> None:
    response = _response({"id": "9001", "status": status})

    assert response.commitment is None
    assert response.outcome == ExternalSubmissionOutcome.EXTERNAL_IOC_RESTING_CONFLICT


def test_missing_raw_response_is_ambiguous_and_durable() -> None:
    response = build_submission_response(
        local_order_id=1,
        intent_id="intent-lost",
        venue="hyperliquid",
        environment="testnet",
        account_scope="account",
        instrument="BTC/USDC:USDC",
        side="SELL",
        client_order_id="0x" + "3" * 32,
        raw_payload=None,
        response_acquired_at="2026-09-05T12:00:00Z",
        ioc_expected=True,
        structured_error="RequestTimeout: response lost",
    )

    assert response.outcome == ExternalSubmissionOutcome.AMBIGUOUS_TRANSPORT_FAILURE
    assert response.commitment is None
    assert response.structured_error == "RequestTimeout: response lost"


def test_exact_ioc_no_match_response_is_an_affirmative_zero_effect_fact() -> None:
    raw = {
        "status": "rejected",
        "info": {
            "response": {
                "type": "order",
                "data": {"statuses": [{"error": IOC_NO_MATCH_ERROR}]},
            }
        },
    }

    response = _response(raw)

    assert response.outcome == ExternalSubmissionOutcome.DETERMINISTIC_IOC_NO_MATCH
    assert response.commitment is None
    assert response.structured_error == IOC_NO_MATCH_ERROR


def test_ioc_no_match_requires_the_exact_ioc_request_profile() -> None:
    raw = {
        "status": "rejected",
        "info": {"response": {"data": {"statuses": [{"error": IOC_NO_MATCH_ERROR}]}}},
    }

    response = build_submission_response(
        local_order_id=1,
        intent_id="intent-non-ioc",
        venue="hyperliquid",
        environment="testnet",
        account_scope="account",
        instrument="BTC/USDC:USDC",
        side="BUY",
        client_order_id="0x" + "4" * 32,
        raw_payload=raw,
        response_acquired_at="2026-09-05T12:00:00Z",
        ioc_expected=False,
    )

    assert response.outcome == ExternalSubmissionOutcome.DETERMINISTIC_ORDER_ERROR


def test_unknown_or_non_exact_ioc_error_remains_deterministic_error() -> None:
    raw = {
        "status": "rejected",
        "info": {
            "response": {"data": {"statuses": [{"error": "Order could not immediately match."}]}}
        },
    }

    response = _response(raw)

    assert response.outcome == ExternalSubmissionOutcome.DETERMINISTIC_ORDER_ERROR


def test_ioc_no_match_with_a_filled_payload_is_conflicting() -> None:
    raw = {
        "status": "closed",
        "info": {
            "response": {
                "data": {
                    "statuses": [
                        {
                            "error": IOC_NO_MATCH_ERROR,
                            "filled": {"totalSz": "1.25", "avgPx": "100000", "oid": 9001},
                        }
                    ]
                }
            }
        },
    }

    response = _response(raw)

    assert response.outcome == ExternalSubmissionOutcome.CONFLICTING_RESPONSE
    assert response.commitment is None


def test_ccxt_result_retains_the_exact_order_response_for_commitment() -> None:
    broker = object.__new__(CcxtBroker)
    result = broker._result_from_order(
        {
            **RAW_FILLED,
            "filled": 1.25,
            "amount": 1.25,
            "average": 100000.0,
            "fees": [],
        },
        fallback_price=100000.0,
        requested_qty=1.25,
    )

    assert result.status == ExternalOrderState.FILLED
    assert result.raw_response is not None
    assert result.raw_response["info"] == RAW_FILLED["info"]
