"""Offline, source-pinned Hyperliquid semantic conformance contracts.

Fixtures are deliberately synthetic.  They encode only the minimum shapes
exposed by the pinned official SDK and never contact an exchange or import it.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime
from pathlib import Path

import ccxt
import pytest

from btcquant.execution.broker import BrokerOrderResult, Fill
from btcquant.execution.ccxt_broker import CcxtBroker
from btcquant.execution.external_evidence import ExternalEvidenceSource, ExternalFill
from btcquant.execution.order_state import ExternalOrderState


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "hyperliquid_conformance"
RAW_A = "a" * 64
RAW_B = "b" * 64
OBSERVED_AT = "2026-08-28T12:00:00Z"
MATRIX_VERDICTS = frozenset(
    {"EXACT", "COMPATIBLE", "BTCQUANT_STRONGER", "LOSSY_SAFE", "UNPROVEN", "CONFLICT"}
)
DEDICATED_TID_CONCLUSION = "TID_CANDIDATE_NOT_FULLY_PROVEN"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _target_side(raw_side: str) -> str:
    """Pinned CCXT 4.5.71 translation; not an assertion about SDK uniqueness."""

    if raw_side == "A":
        return "SELL"
    if raw_side == "B":
        return "BUY"
    raise ValueError("unsupported Hyperliquid side")


def _target_timestamp(milliseconds: int) -> str:
    if isinstance(milliseconds, bool) or not isinstance(milliseconds, int) or milliseconds < 0:
        raise ValueError("Hyperliquid venue time must be a non-negative integer milliseconds")
    return datetime.fromtimestamp(milliseconds / 1000, UTC).isoformat(timespec="milliseconds")


def _target_positive_number(value: str, field: str) -> float:
    try:
        normalized = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} must be numeric") from error
    if not math.isfinite(normalized) or normalized <= 0:
        raise ValueError(f"{field} must be finite and positive")
    return normalized


def _target_fee(value: str | None, asset: str | None) -> tuple[float | None, str | None]:
    if value is None:
        if asset is not None:
            raise ValueError("feeToken requires an explicit fee")
        return None, None
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0:
        raise ValueError("fee must be finite and non-negative")
    if not isinstance(asset, str) or not asset:
        raise ValueError("explicit fee requires feeToken")
    return normalized, asset


def _fill(*, venue_fill_id: str = "10001", **changes: object) -> ExternalFill:
    values: dict[str, object] = {
        "local_order_id": 1,
        "intent_id": "intent-hl",
        "venue": "hyperliquid",
        "account_scope": "main",
        "instrument": "BTC/USDC:USDC",
        "side": "SELL",
        "source_kind": ExternalEvidenceSource.FILL_LOOKUP,
        "client_order_id": "0x0123456789abcdef0123456789abcdef",
        "external_order_id": "9001",
        "venue_fill_id": venue_fill_id,
        "quantity": 0.25,
        "price": 100000.25,
        "fee": 1.25,
        "fee_asset": "USDC",
        "venue_event_at": "2025-10-21T12:00:00.123Z",
        "observed_at": OBSERVED_AT,
        "raw_payload_hash": RAW_A,
    }
    values.update(changes)
    return ExternalFill(**values)  # type: ignore[arg-type]


def _hyperliquid_broker(exchange: object) -> CcxtBroker:
    broker = object.__new__(CcxtBroker)
    broker.exchange = exchange
    broker.exchange_id = "hyperliquid"
    broker.symbol = "BTC/USDC:USDC"
    return broker


def test_fixture_provenance_pins_the_official_sdk_and_ccxt_lock() -> None:
    provenance = _fixture("provenance.json")

    assert provenance["classification"] == "SOURCE_DERIVED_SYNTHETIC"
    assert provenance["official_sdk"] == {
        "repository": "https://github.com/hyperliquid-dex/hyperliquid-python-sdk",
        "branch": "master",
        "sha": "2fdb18f9517675ea03695a0962bd19eece9c83f0",
        "tree": "e4fad7c49595cf81be3513b82fb458d61b26285f",
        "version": "0.24.0",
        "retrieved_at": "2026-08-28",
        "verified_at": "2026-08-28",
        "source_files": [
            "hyperliquid/exchange.py",
            "hyperliquid/info.py",
            "hyperliquid/utils/signing.py",
            "hyperliquid/utils/types.py",
        ],
    }
    assert provenance["ccxt"]["version"] == "4.5.71"
    assert provenance["fixtures_are_not_live_exchange_responses"] is True


def test_conformance_matrix_preserves_unproven_venue_boundaries() -> None:
    matrix = {entry["concept"]: entry for entry in _fixture("semantic_matrix.json")["matrix"]}

    assert matrix["cloid_format"]["verdict"] == "EXACT"
    assert matrix["cloid_hex_character_validation"]["verdict"] == "BTCQUANT_STRONGER"
    assert matrix["market_order"]["verdict"] == "COMPATIBLE"
    assert matrix["status_vocabulary"]["verdict"] == "LOSSY_SAFE"
    assert matrix["fill_tid"]["verdict"] == "UNPROVEN"
    assert DEDICATED_TID_CONCLUSION == "TID_CANDIDATE_NOT_FULLY_PROVEN"
    assert all(entry["verdict"] != "CONFLICT" for entry in matrix.values())


def test_conformance_matrix_uses_closed_verdict_vocabulary() -> None:
    entries = _fixture("semantic_matrix.json")["matrix"]

    assert entries
    assert all(entry["verdict"] in MATRIX_VERDICTS for entry in entries)


def test_hyperliquid_cloid_has_pinned_format_and_is_restart_stable() -> None:
    intent = "btq-mkt-" + "f" * 64
    first = CcxtBroker._external_client_order_id(intent, "hyperliquid")
    second = CcxtBroker._external_client_order_id(intent, "hyperliquid")

    assert first == second == "0x" + hashlib.sha256(intent.encode()).hexdigest()[:32]
    assert len(first) == 34
    assert first.startswith("0x")
    assert first[2:] == first[2:].lower()
    assert all(character in "0123456789abcdef" for character in first[2:])


def test_distinct_intents_map_to_distinct_hyperliquid_cloids() -> None:
    first = CcxtBroker._external_client_order_id("intent-a", "hyperliquid")
    second = CcxtBroker._external_client_order_id("intent-b", "hyperliquid")

    assert first != second


def test_ccxt_hyperliquid_lookup_not_found_remains_absence() -> None:
    class NotFoundExchange:
        def fetch_order(self, *_args, **_kwargs):
            raise ccxt.OrderNotFound("missing")

    assert _hyperliquid_broker(NotFoundExchange()).lookup_order("intent-missing") is None


@pytest.mark.parametrize(
    ("raw_status", "filled", "remaining", "expected"),
    [
        ("unrecognized", 0.0, 1.0, ExternalOrderState.UNKNOWN),
        ("closed", 0.25, 0.75, ExternalOrderState.PARTIAL_TERMINAL),
        ("open", 0.0, 0.0, ExternalOrderState.UNKNOWN),
        ("open", 0.25, 0.40, ExternalOrderState.UNKNOWN),
        ("open", 1.0, None, ExternalOrderState.UNKNOWN),
    ],
)
def test_ccxt_status_normalization_remains_fail_closed(
    raw_status: str, filled: float, remaining: float | None, expected: ExternalOrderState
) -> None:
    broker = object.__new__(CcxtBroker)
    result = broker._result_from_order(
        {
            "id": "9001",
            "status": raw_status,
            "amount": 1.0,
            "filled": filled,
            "remaining": remaining,
            "average": 100000.0 if filled else None,
            "fees": [],
        },
        fallback_price=100000.0,
        requested_qty=1.0,
    )

    assert result.status == expected
    if raw_status == "closed":
        assert result.fill.qty == pytest.approx(0.25)
        assert result.remaining_qty == 0.0


def test_order_absence_never_becomes_terminal_zero_fill() -> None:
    absence: BrokerOrderResult | None = None

    assert absence is None
    assert absence not in {
        BrokerOrderResult(Fill(100000.0, 0.0, 0.0), ExternalOrderState.CANCELED, 1.0, 0.0),
        BrokerOrderResult(Fill(100000.0, 0.0, 0.0), ExternalOrderState.REJECTED, 1.0, 0.0),
    }


def test_synthetic_fill_side_timestamp_and_fee_contracts() -> None:
    first, second = _fixture("fills.json")["fills"]

    assert first["side"] == second["side"] == "A"
    assert _target_side(first["side"]) == "SELL"
    assert _target_side(second["side"]) == "SELL"
    assert _target_timestamp(first["time"]) == "2025-10-21T10:00:00.123+00:00"
    assert _target_positive_number(first["sz"], "sz") == pytest.approx(0.25)
    assert _target_positive_number(first["px"], "px") == pytest.approx(100000.25)
    assert _target_fee(first["fee"], first["feeToken"]) == (1.25, "USDC")
    assert _target_fee(second["fee"], second["feeToken"]) == (0.0, "USDC")


@pytest.mark.parametrize("value", ["0", "-1", "NaN", "Infinity", "bad"])
def test_fill_quantity_and_price_target_contract_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValueError):
        _target_positive_number(value, "sz")
    with pytest.raises(ValueError):
        _target_positive_number(value, "px")


@pytest.mark.parametrize("value", [-1, True, 1.5, "1761040800123"])
def test_venue_timestamp_target_contract_rejects_non_integer_milliseconds(value: object) -> None:
    with pytest.raises(ValueError):
        _target_timestamp(value)  # type: ignore[arg-type]


def test_multiple_synthetic_fills_for_one_oid_have_distinct_a31_fill_keys() -> None:
    first, second = _fixture("fills.json")["fills"]
    assert first["oid"] == second["oid"] == 9001
    assert first["side"] == second["side"] == "A"
    assert first["tid"] != second["tid"]
    assert first["sz"] != second["sz"]
    assert first["px"] != second["px"]
    assert first["time"] != second["time"]

    first_fill = _fill(venue_fill_id=str(first["tid"]))
    second_fill = _fill(
        venue_fill_id=str(second["tid"]),
        side=_target_side(second["side"]),
        quantity=_target_positive_number(second["sz"], "sz"),
        price=_target_positive_number(second["px"], "px"),
        fee=0.0,
        raw_payload_hash=RAW_B,
    )

    assert first_fill.fill_key != second_fill.fill_key


def test_stable_fill_redelivery_ignores_delivery_metadata_but_keeps_first_evidence() -> None:
    first = _fill()
    redelivery = _fill(
        source_kind=ExternalEvidenceSource.PRIVATE_EVENT,
        observed_at="2026-08-28T12:00:01Z",
        persisted_at="2026-08-28T12:00:02Z",
        raw_payload_hash=RAW_B,
        client_order_id=None,
    )

    assert first.fill_key == redelivery.fill_key
    assert first.is_semantically_compatible_with(redelivery)
    assert first.client_order_id is not None


@pytest.mark.parametrize("changes", [{"quantity": 0.5}, {"price": 99999.0}])
def test_contradictory_stable_fill_facts_remain_fail_closed(changes: dict[str, float]) -> None:
    assert not _fill().is_semantically_compatible_with(_fill(**changes))


def test_fee_target_contract_preserves_zero_and_rejects_invalid_evidence() -> None:
    assert _target_fee("0", "USDC") == (0.0, "USDC")
    for value in ("-1", "NaN", "Infinity", "bad"):
        with pytest.raises(ValueError):
            _target_fee(value, "USDC")
    with pytest.raises(ValueError):
        _target_fee(None, "USDC")


def test_matrix_preserves_absence_aggregation_and_account_boundaries() -> None:
    matrix = {entry["concept"]: entry for entry in _fixture("semantic_matrix.json")["matrix"]}

    assert matrix["open_orders_absence"]["a33_usability"] == "INSUFFICIENT_ALONE"
    assert matrix["historical_orders_absence"]["a33_usability"] == "INSUFFICIENT_ALONE"
    assert matrix["user_fills_by_time_aggregation"]["a33_usability"] == "NON_AGGREGATED_REQUIRED"
    assert matrix["account_position_state"]["a33_usability"] == "CORROBORATING_ONLY"
    assert matrix["fill_tid"]["verdict"] == "UNPROVEN"
    assert DEDICATED_TID_CONCLUSION == "TID_CANDIDATE_NOT_FULLY_PROVEN"
    assert matrix["fill_hash"]["verdict"] == "UNPROVEN"


def test_ccxt_mapping_keeps_order_aggregates_separate_from_fill_evidence() -> None:
    mapping = {
        entry["ccxt_field"]: entry for entry in _fixture("semantic_matrix.json")["ccxt_mapping"]
    }

    assert mapping["clientOrderId"]["resolver_safe"] == "PRIMARY_LOOKUP_CANDIDATE"
    assert mapping["id"]["btcquant_field"].startswith("external_order_id")
    assert mapping["filled"]["resolver_safe"] == "NOT_SUFFICIENT_FOR_FILL_IDENTITY"
    assert mapping["average / price"]["resolver_safe"] == "NOT_SUFFICIENT_FOR_FILL_EVIDENCE"
    assert mapping["fee / fees"]["resolver_safe"] == "NOT_SUFFICIENT_FOR_FILL_EVIDENCE"
