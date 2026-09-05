from __future__ import annotations


from btcquant.execution.external_evidence import ExternalEvidenceSource, ExternalFill
from btcquant.execution.external_evidence_reader import (
    EvidenceLookupOutcome,
    ExternalOrderEvidence,
    OrderEvidenceLookup,
    OrderLookupContext,
)
from btcquant.execution.external_fill_evidence_reader import (
    FillEvidenceLookup,
    FillEvidenceLookupOutcome,
    FillLookupContext,
)
from btcquant.execution.external_settlement_acquisition import (
    CcxtExternalSettlementAcquirer,
    ExternalSettlementAcquisitionContext,
    SettlementRetentionWitness,
)
from btcquant.execution.financial_order_settlement import ExternalOrderSettlement
from btcquant.execution.external_submission_commitment import build_submission_response
from btcquant.execution.order_state import ExternalOrderState


CLOID = "0x" + "a" * 32
OBSERVED = "2026-09-05T12:00:00+00:00"
START = "2026-09-05T11:59:00+00:00"
END = "2026-09-05T12:03:00+00:00"


def commitment(total="1", average="100"):
    raw = {
        "status": "closed",
        "info": {
            "response": {
                "data": {
                    "statuses": [{"filled": {"oid": "oid-7", "totalSz": total, "avgPx": average}}]
                }
            }
        },
    }
    return build_submission_response(
        local_order_id=7,
        intent_id="intent-acquisition",
        venue="hyperliquid",
        environment="testnet",
        account_scope="acct-testnet",
        instrument="BTC/USDC:USDC",
        side="BUY",
        client_order_id=CLOID,
        raw_payload=raw,
        response_acquired_at=OBSERVED,
        ioc_expected=True,
    ).commitment


def context(*, with_commitment=True, with_witness=True, stability=2, commitment_value=None):
    return ExternalSettlementAcquisitionContext(
        local_order_id=7,
        intent_id="intent-acquisition",
        venue="hyperliquid",
        environment="testnet",
        account_scope="acct-testnet",
        instrument="BTC/USDC:USDC",
        side="BUY",
        engine="trend",
        client_order_id=CLOID,
        requested_qty=1.0,
        planned_effect_at=OBSERVED,
        window_start=START,
        window_end=END,
        submission_commitment=(commitment_value or commitment()) if with_commitment else None,
        retention_witness=(
            SettlementRetentionWitness(1, "2026-09-05T11:58:00Z") if with_witness else None
        ),
        stability_lookups=stability,
    )


def order_lookup(state=ExternalOrderState.FILLED, status_event_at=OBSERVED):
    evidence = ExternalOrderEvidence(
        local_order_id=7,
        intent_id="intent-acquisition",
        venue="hyperliquid",
        account_scope="acct-testnet",
        instrument="BTC/USDC:USDC",
        side="BUY",
        engine="trend",
        expected_client_order_id=CLOID,
        returned_client_order_id=CLOID,
        external_order_id="oid-7",
        ccxt_status="closed",
        venue_status="filled",
        normalized_state=state,
        requested_qty=1.0,
        filled_qty=1.0,
        remaining_qty=0.0,
        requested_qty_explicit=True,
        filled_qty_explicit=True,
        remaining_qty_explicit=True,
        source_kind=ExternalEvidenceSource.ORDER_LOOKUP,
        venue_event_at=OBSERVED,
        status_event_at=status_event_at,
        observed_at=OBSERVED,
        raw_payload_hash="b" * 64,
        correlation_complete=True,
        quantities_complete=True,
        contradictory=False,
    )
    return OrderEvidenceLookup(
        OrderLookupContext(
            local_order_id=7,
            intent_id="intent-acquisition",
            venue="hyperliquid",
            account_scope="acct-testnet",
            instrument="BTC/USDC:USDC",
            side="BUY",
            expected_client_order_id=CLOID,
            requested_qty=1.0,
            engine="trend",
        ),
        EvidenceLookupOutcome.FOUND,
        evidence=evidence,
    )


def fill(quantity=1.0, price=100.0, raw_hash="c" * 64, source=ExternalEvidenceSource.FILL_LOOKUP):
    return ExternalFill(
        local_order_id=7,
        intent_id="intent-acquisition",
        venue="hyperliquid",
        account_scope="acct-testnet",
        instrument="BTC/USDC:USDC",
        side="BUY",
        source_kind=source,
        quantity=quantity,
        price=price,
        observed_at=OBSERVED,
        raw_payload_hash=raw_hash,
        client_order_id=CLOID,
        external_order_id="oid-7",
        fee=0.01,
        fee_asset="USDC",
        venue_event_at="2026-09-05T12:01:00Z",
    )


def lookup(fills, *, count=None, outcome=FillEvidenceLookupOutcome.FOUND):
    fill_context = FillLookupContext(
        local_order_id=7,
        intent_id="intent-acquisition",
        venue="hyperliquid",
        account_scope="acct-testnet",
        instrument="BTC/USDC:USDC",
        side="BUY",
        expected_external_order_id="oid-7",
        expected_client_order_id=CLOID,
        start_time_ms=1788609540000,
        end_time_ms=1788609780000,
        engine="trend",
    )
    values = tuple(fills) if outcome == FillEvidenceLookupOutcome.FOUND else ()
    actual_count = len(values) if count is None else count
    return FillEvidenceLookup(
        fill_context,
        outcome,
        fills=values,
        venue_fill_id_candidates=tuple(None for _ in values),
        response_count=actual_count,
        response_limit_reached=actual_count >= 2000,
    )


class OrderReader:
    def __init__(self, value):
        self.value = value
        self.calls = 0

    def lookup_order(self, context, *, observed_at=None):
        self.calls += 1
        return self.value


class FillReader:
    def __init__(self, values):
        self.values = list(values)
        self.calls = []

    def lookup_fills(self, context, *, observed_at=None):
        self.calls.append(context)
        return self.values.pop(0)


def acquire(ctx, values, *, state=ExternalOrderState.FILLED):
    order_reader = OrderReader(order_lookup(state))
    fill_reader = FillReader(values)
    result = CcxtExternalSettlementAcquirer(order_reader, fill_reader).acquire(
        ctx, observed_at=OBSERVED
    )
    return result, order_reader, fill_reader


def test_positive_commitment_and_two_stable_fill_reads_build_settlement():
    result, order_reader, fill_reader = acquire(
        context(),
        [lookup([fill()]), lookup([fill()])],
    )
    assert order_reader.calls == 1
    assert len(fill_reader.calls) == 2
    assert result.settlement is not None
    assert result.settlement.completeness.is_complete
    assert result.settlement.total_qty == 1


def test_positive_settlement_persistence_round_trip_preserves_commitment():
    result, _, _ = acquire(context(stability=1), [lookup([fill()])])
    assert result.settlement is not None
    restored = ExternalOrderSettlement.from_persistence_payload(
        result.settlement.to_persistence_payload()
    )
    assert restored.to_persistence_payload() == result.settlement.to_persistence_payload()


def test_missing_submission_commitment_blocks_before_fill_lookup():
    result, _, fill_reader = acquire(context(with_commitment=False), [lookup([fill()])])
    assert result.settlement is None
    assert result.blocking_reason == "SUBMISSION_FILL_COMMITMENT_MISSING"
    assert fill_reader.calls == []


def test_response_limit_blocks_even_if_target_rows_match():
    result, _, _ = acquire(context(stability=1), [lookup([fill()], count=2000)])
    assert result.settlement is None
    assert result.blocking_reason == "SETTLEMENT_RESPONSE_LIMIT_REACHED"


def test_commitment_quantity_mismatch_is_fail_closed():
    result, _, _ = acquire(
        context(stability=1, commitment_value=commitment(total="2")),
        [lookup([fill()])],
    )
    assert result.settlement is None


def test_commitment_vwap_mismatch_is_fail_closed():
    result, _, _ = acquire(
        context(),
        [lookup([fill(price=101)]), lookup([fill(price=101)])],
    )
    assert result.settlement is None
    assert result.blocking_reason == "SETTLEMENT_COMMITMENT_VWAP_CONFLICT"


def test_second_snapshot_may_add_fill_but_cannot_drop_first():
    result, _, _ = acquire(
        context(),
        [
            lookup([fill(quantity=0.4)]),
            lookup([fill(quantity=0.4), fill(quantity=0.6, raw_hash="d" * 64)]),
        ],
    )
    assert result.settlement is not None
    assert result.settlement.raw_fill_count == 2


def test_second_snapshot_drop_is_conflict():
    result, _, _ = acquire(
        context(),
        [lookup([fill(raw_hash="c" * 64)]), lookup([fill(raw_hash="d" * 64)])],
    )
    assert result.settlement is None
    assert result.blocking_reason == "FILL_SNAPSHOT_CONFLICT"


def test_fill_source_must_be_fill_lookup():
    result, _, _ = acquire(
        context(stability=1),
        [lookup([fill(source=ExternalEvidenceSource.PRIVATE_EVENT)])],
    )
    assert result.settlement is None
    assert result.blocking_reason == "fill provenance is not FILL_LOOKUP"


def test_commitment_backed_completeness_does_not_require_retention_witness():
    result, _, _ = acquire(context(with_witness=False, stability=1), [lookup([fill()])])
    assert result.settlement is not None
    assert result.settlement.completeness.is_complete
    assert not result.settlement.completeness.retention_witness_present


def test_non_terminal_order_does_not_acquire_fills():
    result, _, fill_reader = acquire(context(), [lookup([fill()])], state=ExternalOrderState.OPEN)
    assert result.settlement is None
    assert result.blocking_reason == "ORDER_NOT_TERMINAL"
    assert fill_reader.calls == []
