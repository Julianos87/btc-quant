"""Tests for the pure external-evidence resolution decision kernel."""

from __future__ import annotations

import inspect
from dataclasses import replace
from typing import Any, cast

import pytest

from btcquant.execution.external_evidence import (
    ExternalEvidenceSource,
    ExternalFill,
    ExternalOrderObservation,
)
from btcquant.execution.order_state import ExternalOrderState
from btcquant.execution.resolution import (
    ExpectedOrderBinding,
    FillLookupFact,
    FillLookupOutcome,
    OrderLookupEvidence,
    OrderLookupFact,
    OrderLookupOutcome,
    ResolutionEvidenceBundle,
    ResolutionOutcome,
    ResolutionReasonCode,
    assess_resolution,
)


OBSERVED = "2026-08-30T12:00:00Z"
OID = "oid-1"
CLOID = "0x" + "a" * 32
HASH = "0" * 64


def binding(**changes: object) -> ExpectedOrderBinding:
    values: dict[str, object] = {
        "local_order_id": 1,
        "intent_id": "intent-1",
        "venue": "hyperliquid",
        "account_scope": "main",
        "engine": "trend",
        "instrument": "BTC/USDC:USDC",
        "side": "SELL",
        "requested_qty": 1.0,
        "expected_client_order_id": CLOID,
        "expected_external_order_id": OID,
    }
    values.update(changes)
    return ExpectedOrderBinding(**cast(Any, values))


def fill(
    *,
    quantity: float = 0.25,
    price: float = 100_000.0,
    fee: float | None = -0.01,
    key: str = "1",
    **changes: object,
) -> ExternalFill:
    values: dict[str, object] = {
        "local_order_id": 1,
        "intent_id": "intent-1",
        "venue": "hyperliquid",
        "account_scope": "main",
        "instrument": "BTC/USDC:USDC",
        "side": "SELL",
        "source_kind": ExternalEvidenceSource.FILL_LOOKUP,
        "quantity": quantity,
        "price": price,
        "observed_at": OBSERVED,
        "raw_payload_hash": HASH,
        "client_order_id": CLOID,
        "external_order_id": OID,
        "fee": fee,
        "fee_asset": "USDC" if fee is not None else None,
        "fill_key": "fill-" + key.zfill(64),
    }
    values.update(changes)
    return ExternalFill(**cast(Any, values))


def observation(
    state: ExternalOrderState,
    *,
    filled: float | None = 0.0,
    remaining: float | None = 1.0,
    observed_at: str = OBSERVED,
    venue_event_at: str | None = "2026-08-30T11:00:00+00:00",
    **changes: object,
) -> ExternalOrderObservation:
    values: dict[str, object] = {
        "local_order_id": 1,
        "intent_id": "intent-1",
        "venue": "hyperliquid",
        "account_scope": "main",
        "instrument": "BTC/USDC:USDC",
        "side": "SELL",
        "source_kind": ExternalEvidenceSource.ORDER_LOOKUP,
        "normalized_external_status": state,
        "requested_qty": 1.0,
        "cumulative_filled_qty": filled,
        "remaining_qty": remaining,
        "client_order_id": CLOID,
        "external_order_id": OID,
        "venue_event_at": venue_event_at,
        "observed_at": observed_at,
        "raw_payload_hash": HASH,
    }
    values.update(changes)
    return ExternalOrderObservation(**cast(Any, values))


def order_evidence(
    state: ExternalOrderState,
    *,
    venue_status: str | None = None,
    ccxt_status: str | None = "canceled",
    filled: float | None = 0.0,
    remaining: float | None = 1.0,
    observed_at: str = OBSERVED,
    **changes: object,
) -> OrderLookupEvidence:
    values: dict[str, object] = {
        "local_order_id": 1,
        "intent_id": "intent-1",
        "venue": "hyperliquid",
        "account_scope": "main",
        "instrument": "BTC/USDC:USDC",
        "side": "SELL",
        "engine": "trend",
        "expected_client_order_id": CLOID,
        "returned_client_order_id": CLOID,
        "external_order_id": OID,
        "ccxt_status": ccxt_status,
        "venue_status": venue_status,
        "normalized_state": state,
        "requested_qty": 1.0,
        "filled_qty": filled,
        "remaining_qty": remaining,
        "requested_qty_explicit": True,
        "filled_qty_explicit": filled is not None,
        "remaining_qty_explicit": remaining is not None,
        "source_kind": ExternalEvidenceSource.ORDER_LOOKUP,
        "venue_event_at": "2026-08-30T11:00:00+00:00",
        "observed_at": observed_at,
        "raw_payload_hash": HASH,
        "correlation_complete": True,
        "quantities_complete": all(value is not None for value in (1.0, filled, remaining)),
        "contradictory": False,
    }
    values.update(changes)
    return OrderLookupEvidence(**cast(Any, values))


def assess(**changes: object):
    values: dict[str, object] = {"binding": binding()}
    values.update(changes)
    return assess_resolution(ResolutionEvidenceBundle(**cast(Any, values)))


def test_not_found_and_no_match_are_unresolved_not_zero_effect():
    result = assess(
        order_lookups=(OrderLookupFact(binding(), OrderLookupOutcome.NOT_FOUND),),
        fill_lookups=(FillLookupFact(binding(), FillLookupOutcome.NO_MATCH),),
    )

    assert result.outcome == ResolutionOutcome.UNRESOLVED
    assert result.zero_effect_proven is False
    assert result.proven_filled_lower_bound == 0.0


def test_canceled_zero_aggregate_is_not_zero_effect_proof():
    result = assess(order_observations=(observation(ExternalOrderState.CANCELED),))

    assert result.outcome == ResolutionOutcome.UNRESOLVED
    assert result.terminal_state_observed is True
    assert result.zero_effect_proven is False


def test_rejected_raw_status_is_not_zero_effect_in_v1():
    evidence = order_evidence(
        ExternalOrderState.REJECTED,
        ccxt_status="rejected",
        venue_status="tickRejected",
    )
    result = assess(order_lookups=(OrderLookupFact(binding(), OrderLookupOutcome.FOUND, evidence),))

    assert result.outcome == ResolutionOutcome.UNRESOLVED
    assert result.zero_effect_proven is False
    assert result.terminal_state_observed is True


def test_filled_aggregate_equal_to_individual_fill_is_still_incomplete():
    result = assess(
        order_observations=(observation(ExternalOrderState.FILLED, filled=1.0, remaining=0.0),),
        fills=(fill(quantity=1.0),),
    )

    assert result.outcome == ResolutionOutcome.EFFECT_PROVEN_INCOMPLETE
    assert result.proven_filled_lower_bound == pytest.approx(1.0)
    assert result.fill_completeness_proven is False
    assert result.zero_effect_proven is False


@pytest.mark.parametrize(
    ("state", "filled", "remaining"),
    [
        (ExternalOrderState.FILLED, 1.0, 0.0),
        (ExternalOrderState.CANCELED, 0.4, 0.6),
        (ExternalOrderState.PARTIAL_TERMINAL, 0.4, 0.6),
    ],
)
def test_positive_fill_is_lower_bound_even_for_terminal_order(state, filled, remaining):
    result = assess(
        order_observations=(observation(state, filled=filled, remaining=remaining),),
        fills=(fill(quantity=0.4),),
    )

    assert result.outcome == ResolutionOutcome.EFFECT_PROVEN_INCOMPLETE
    assert result.proven_filled_lower_bound == pytest.approx(0.4)
    assert result.fill_completeness_proven is False


def test_open_without_fill_is_external_active():
    result = assess(order_observations=(observation(ExternalOrderState.OPEN),))

    assert result.outcome == ResolutionOutcome.EXTERNAL_ACTIVE
    assert result.external_order_active is True
    assert result.proven_filled_lower_bound == 0.0


def test_partial_open_with_fill_reports_positive_effect():
    result = assess(
        order_observations=(
            observation(ExternalOrderState.PARTIAL_OPEN, filled=0.4, remaining=0.6),
        ),
        fills=(fill(quantity=0.4),),
    )

    assert result.outcome == ResolutionOutcome.EFFECT_PROVEN_INCOMPLETE
    assert result.proven_filled_lower_bound == pytest.approx(0.4)


def test_unknown_order_with_fill_keeps_positive_lower_bound():
    result = assess(
        order_observations=(observation(ExternalOrderState.UNKNOWN, filled=0.4, remaining=0.6),),
        fills=(fill(quantity=0.4),),
    )

    assert result.outcome == ResolutionOutcome.EFFECT_PROVEN_INCOMPLETE
    assert result.proven_filled_lower_bound == pytest.approx(0.4)


def test_limit_reached_prevents_fill_completeness():
    result = assess(
        fill_lookups=(
            FillLookupFact(
                binding(),
                FillLookupOutcome.FOUND,
                fills=(fill(),),
                response_count=2000,
                response_limit_reached=True,
                venue_fill_id_candidates=("tid-1",),
            ),
        )
    )

    assert result.outcome == ResolutionOutcome.EFFECT_PROVEN_INCOMPLETE
    assert result.fill_completeness_proven is False
    assert ResolutionReasonCode.FILL_LOOKUP_INCOMPLETE in result.reasons
    assert ResolutionReasonCode.TID_CANDIDATE_NOT_IDENTITY in result.reasons


def test_binding_mismatch_blocks_all_interpretation():
    mismatched = fill(account_scope="subaccount-b")
    result = assess(fills=(mismatched,))

    assert result.outcome == ResolutionOutcome.BINDING_CONFLICT
    assert result.binding_complete is False
    assert result.proven_filled_lower_bound == 0.0


def test_incomplete_returned_cloid_is_not_a_bound_order():
    evidence = order_evidence(
        ExternalOrderState.CANCELED,
        returned_client_order_id=None,
        correlation_complete=False,
    )
    result = assess(
        order_lookups=(
            OrderLookupFact(binding(), OrderLookupOutcome.INCOMPLETE_RESPONSE, evidence),
        )
    )

    assert result.outcome == ResolutionOutcome.BINDING_INCOMPLETE
    assert result.binding_complete is False


def test_terminal_after_active_is_accepted_as_observed_sequence_but_not_finalized():
    result = assess(
        order_observations=(
            observation(ExternalOrderState.CANCELED, observed_at="2026-08-30T12:01:00Z"),
            observation(ExternalOrderState.OPEN, observed_at="2026-08-30T12:00:00Z"),
        )
    )

    assert result.outcome == ResolutionOutcome.UNRESOLVED
    assert result.terminal_state_observed is True
    assert result.external_order_active is False


def test_active_after_terminal_is_evidence_conflict():
    result = assess(
        order_observations=(
            observation(ExternalOrderState.CANCELED, observed_at="2026-08-30T12:00:00Z"),
            observation(ExternalOrderState.OPEN, observed_at="2026-08-30T12:01:00Z"),
        )
    )

    assert result.outcome == ResolutionOutcome.EVIDENCE_CONFLICT
    assert ResolutionReasonCode.STATUS_CONFLICT in result.conflicts


def test_order_venue_event_at_is_not_status_transition_time():
    result = assess(
        order_observations=(
            observation(
                ExternalOrderState.OPEN,
                observed_at="2026-08-30T12:00:00Z",
                venue_event_at="2026-08-30T11:00:00Z",
            ),
            observation(
                ExternalOrderState.CANCELED,
                observed_at="2026-08-30T12:01:00Z",
                venue_event_at="2026-08-30T11:00:00Z",
            ),
        )
    )

    assert result.outcome == ResolutionOutcome.UNRESOLVED
    assert ResolutionReasonCode.STATUS_CONFLICT not in result.conflicts


def test_later_fill_does_not_create_false_order_quantity_conflict():
    result = assess(
        order_observations=(
            observation(ExternalOrderState.PARTIAL_OPEN, filled=0.4, remaining=0.6),
        ),
        fills=(fill(quantity=0.7, key="7"),),
    )

    assert result.outcome == ResolutionOutcome.EFFECT_PROVEN_INCOMPLETE
    assert result.proven_filled_lower_bound == pytest.approx(0.7)
    assert ResolutionReasonCode.ORDER_QUANTITY_CONFLICT not in result.conflicts
    assert ResolutionReasonCode.ORDER_FILL_TIMING_UNPROVEN in result.reasons


def test_duplicate_fill_key_is_counted_once():
    first = fill()
    redelivery = replace(first, observed_at="2026-08-30T12:01:00Z")
    result = assess(fills=(redelivery, first))

    assert result.proven_filled_lower_bound == pytest.approx(0.25)
    assert result.deduplicated_fill_keys == (first.fill_key,)


def test_same_fill_key_with_different_economic_fee_conflicts():
    first = fill(fee=-0.01)
    conflicting = replace(first, fee=-0.02)
    result = assess(fills=(first, conflicting))

    assert result.outcome == ResolutionOutcome.EVIDENCE_CONFLICT
    assert ResolutionReasonCode.FILL_QUANTITY_CONFLICT in result.conflicts


def test_same_tid_candidate_different_fill_keys_does_not_speculatively_double_count():
    result = assess(
        fill_lookups=(
            FillLookupFact(
                binding(),
                FillLookupOutcome.FOUND,
                fills=(fill(key="1"), fill(key="2")),
                venue_fill_id_candidates=("tid-1", "tid-1"),
                response_count=2,
            ),
        )
    )

    assert result.outcome == ResolutionOutcome.EVIDENCE_CONFLICT
    assert result.proven_filled_lower_bound == pytest.approx(0.25)
    assert ResolutionReasonCode.FILL_IDENTITY_AMBIGUITY in result.conflicts
    assert len(result.deduplicated_fill_keys) == 2
    assert result.tid_identity_proven is False


def test_signed_maker_rebate_is_preserved_and_does_not_change_quantity():
    maker_rebate = fill(quantity=0.25, fee=-1.25)
    result = assess(fills=(maker_rebate,))

    assert maker_rebate.fee == pytest.approx(-1.25)
    assert result.proven_filled_lower_bound == pytest.approx(0.25)
    assert result.outcome == ResolutionOutcome.EFFECT_PROVEN_INCOMPLETE


def test_fill_sum_above_requested_is_evidence_conflict():
    result = assess(fills=(fill(quantity=0.75, key="1"), fill(quantity=0.5, key="2")))

    assert result.outcome == ResolutionOutcome.EVIDENCE_CONFLICT
    assert ResolutionReasonCode.FILL_QUANTITY_CONFLICT in result.conflicts


def test_incompatible_terminal_states_are_fail_closed():
    result = assess(
        order_observations=(
            observation(ExternalOrderState.CANCELED),
            observation(ExternalOrderState.FILLED, filled=1.0, remaining=0.0),
        )
    )

    assert result.outcome == ResolutionOutcome.EVIDENCE_CONFLICT
    assert ResolutionReasonCode.STATUS_CONFLICT in result.conflicts


def test_transport_failures_remain_unresolved():
    result = assess(
        order_lookups=(OrderLookupFact(binding(), OrderLookupOutcome.TRANSPORT_FAILURE),),
        fill_lookups=(FillLookupFact(binding(), FillLookupOutcome.TRANSPORT_FAILURE),),
    )

    assert result.outcome == ResolutionOutcome.UNRESOLVED
    assert result.zero_effect_proven is False


def test_current_v1_never_emits_proven_terminal_or_zero_effect():
    cases = [
        ResolutionEvidenceBundle(
            binding(),
            order_observations=(observation(ExternalOrderState.FILLED, filled=1.0, remaining=0.0),),
            fills=(fill(quantity=1.0),),
        ),
        ResolutionEvidenceBundle(
            binding(),
            order_observations=(observation(ExternalOrderState.REJECTED),),
        ),
    ]

    for case in cases:
        result = assess_resolution(case)
        assert result.outcome not in {
            ResolutionOutcome.TERMINAL_EFFECT_PROVEN,
            ResolutionOutcome.ZERO_EFFECT_PROVEN,
        }
        assert result.zero_effect_proven is False
        assert result.fill_completeness_proven is False


def test_tuple_permutations_and_repeated_execution_are_identical():
    opened = observation(ExternalOrderState.OPEN, observed_at="2026-08-30T12:00:00Z")
    canceled = observation(
        ExternalOrderState.CANCELED, filled=0.25, remaining=0.75, observed_at="2026-08-30T12:01:00Z"
    )
    first = fill(key="1")
    second = fill(quantity=0.5, price=100_100.0, key="2")
    facts = (
        OrderLookupFact(binding(), OrderLookupOutcome.NOT_FOUND),
        OrderLookupFact(binding(), OrderLookupOutcome.INCOMPLETE_RESPONSE),
    )
    fill_facts = (
        FillLookupFact(
            binding(),
            FillLookupOutcome.FOUND,
            fills=(first, second),
            venue_fill_id_candidates=(None, None),
            response_count=2,
        ),
        FillLookupFact(binding(), FillLookupOutcome.NO_MATCH),
    )
    one = assess_resolution(
        ResolutionEvidenceBundle(binding(), (opened, canceled), (first, second), facts, fill_facts)
    )
    two = assess_resolution(
        ResolutionEvidenceBundle(
            binding(), (canceled, opened), (second, first), facts[::-1], fill_facts[::-1]
        )
    )

    assert one == two
    assert (
        assess_resolution(
            ResolutionEvidenceBundle(
                binding(), (opened, canceled), (first, second), facts, fill_facts
            )
        )
        == one
    )


def test_kernel_has_no_reader_store_or_clock_dependency():
    import btcquant.execution.resolution as resolution

    source = inspect.getsource(resolution)
    assert "StateStore" not in source
    assert "fetch_" not in source
    assert "datetime.now" not in source
    assert not hasattr(resolution, "StateStore")


def test_same_tid_candidate_with_different_quantities_uses_conservative_component_minimum():
    result = assess(
        fill_lookups=(
            FillLookupFact(
                binding(),
                FillLookupOutcome.FOUND,
                fills=(fill(quantity=0.25, key="1"), fill(quantity=0.40, key="2")),
                venue_fill_id_candidates=("tid-1", "tid-1"),
                response_count=2,
            ),
        )
    )

    assert result.outcome == ResolutionOutcome.EVIDENCE_CONFLICT
    assert result.proven_filled_lower_bound == pytest.approx(0.25)
    assert ResolutionReasonCode.FILL_IDENTITY_AMBIGUITY in result.conflicts


def test_fill_lookup_none_candidate_is_accepted_and_aligned_with_its_fill():
    lookup = FillLookupFact(
        binding(),
        FillLookupOutcome.FOUND,
        fills=(fill(),),
        venue_fill_id_candidates=(None,),
        response_count=1,
    )

    assert lookup.venue_fill_id_candidates == (None,)
    assert len(lookup.fills) == len(lookup.venue_fill_id_candidates)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        (
            {
                "fills": (fill(),),
                "venue_fill_id_candidates": (None, None),
            },
            "align one-to-one",
        ),
        (
            {
                "fills": (fill(),),
                "venue_fill_id_candidates": (None,),
                "response_count": 2000,
                "response_limit_reached": False,
            },
            "reflect response_count",
        ),
        (
            {
                "fills": (fill(),),
                "venue_fill_id_candidates": (None,),
                "response_limit": 1999,
            },
            "fixed at 2000",
        ),
        (
            {
                "fills": (fill(),),
                "venue_fill_id_candidates": (None,),
                "retention_limit": 9999,
            },
            "fixed at 10000",
        ),
    ],
)
def test_fill_lookup_fact_rejects_contract_invariant_violations(kwargs, match):
    with pytest.raises(ValueError, match=match):
        FillLookupFact(binding(), FillLookupOutcome.FOUND, **kwargs)


def test_requested_qty_mismatch_in_order_lookup_binding_is_conflict():
    result = assess(
        order_lookups=(OrderLookupFact(binding(requested_qty=2.0), OrderLookupOutcome.NOT_FOUND),)
    )

    assert result.outcome == ResolutionOutcome.BINDING_CONFLICT


def test_requested_qty_within_tolerance_in_order_lookup_binding_is_compatible():
    result = assess(
        order_lookups=(
            OrderLookupFact(
                binding(requested_qty=1.0 + 0.5e-9),
                OrderLookupOutcome.NOT_FOUND,
            ),
        )
    )

    assert result.outcome == ResolutionOutcome.UNRESOLVED
    assert result.binding_complete is True


def test_requested_qty_mismatch_in_fill_lookup_binding_is_conflict():
    result = assess(
        fill_lookups=(
            FillLookupFact(
                binding(requested_qty=2.0),
                FillLookupOutcome.FOUND,
                fills=(fill(),),
                venue_fill_id_candidates=(None,),
                response_count=1,
            ),
        )
    )

    assert result.outcome == ResolutionOutcome.BINDING_CONFLICT


def test_distinct_known_oids_conflict_even_without_expected_oid():
    result = assess(
        binding=binding(expected_external_order_id=None),
        fills=(
            fill(key="a", external_order_id="oid-A"),
            fill(key="b", external_order_id="oid-B"),
        ),
    )

    assert result.outcome == ResolutionOutcome.BINDING_CONFLICT
    assert result.proven_filled_lower_bound == 0.0


def test_observation_and_fill_with_distinct_known_oids_conflict_without_expected_oid():
    result = assess(
        binding=binding(expected_external_order_id=None),
        order_observations=(observation(ExternalOrderState.OPEN, external_order_id="oid-A"),),
        fills=(fill(external_order_id="oid-B"),),
    )

    assert result.outcome == ResolutionOutcome.BINDING_CONFLICT


def test_missing_fill_cloid_requires_oid_bound_by_independent_context():
    result = assess(
        binding=binding(expected_external_order_id=None),
        fills=(fill(client_order_id=None, external_order_id="oid-A"),),
    )

    assert result.outcome == ResolutionOutcome.BINDING_INCOMPLETE


def test_missing_fill_cloid_is_compatible_with_exact_context_oid():
    result = assess(
        binding=binding(expected_external_order_id="oid-A"),
        fills=(fill(client_order_id=None, external_order_id="oid-A"),),
    )

    assert result.outcome == ResolutionOutcome.EFFECT_PROVEN_INCOMPLETE


def test_same_observation_key_with_conflicting_semantics_is_not_silently_deduplicated():
    key = "obs-" + "c" * 64
    result = assess(
        order_observations=(
            observation(ExternalOrderState.OPEN, observation_key=key),
            observation(ExternalOrderState.CANCELED, observation_key=key),
        )
    )

    assert result.outcome == ResolutionOutcome.EVIDENCE_CONFLICT
    assert ResolutionReasonCode.ORDER_OBSERVATION_IDENTITY_CONFLICT in result.conflicts


def test_same_observation_key_with_equivalent_semantics_is_idempotent():
    first = observation(ExternalOrderState.OPEN, observation_key="obs-" + "d" * 64)
    redelivery = replace(first, observed_at="2026-08-30T12:01:00Z")

    one = assess(order_observations=(first,))
    two = assess(order_observations=(redelivery, first))

    assert one == two
    assert two.outcome == ResolutionOutcome.EXTERNAL_ACTIVE


def test_order_lookup_evidence_rejects_naive_observed_at():
    with pytest.raises(ValueError, match="explicit timezone"):
        order_evidence(ExternalOrderState.OPEN, observed_at="2026-08-30T12:00:00")


def test_order_lookup_evidence_canonicalizes_z_and_utc_offset_equally():
    zulu = order_evidence(ExternalOrderState.OPEN, observed_at="2026-08-30T12:00:00Z")
    offset = order_evidence(ExternalOrderState.OPEN, observed_at="2026-08-30T12:00:00+00:00")

    assert zulu.observed_at == offset.observed_at
    assert assess(
        order_lookups=(OrderLookupFact(binding(), OrderLookupOutcome.FOUND, zulu),)
    ) == assess(order_lookups=(OrderLookupFact(binding(), OrderLookupOutcome.FOUND, offset),))


def test_candidate_alignment_survives_fill_tuple_permutation():
    first = fill(quantity=0.25, key="a")
    second = fill(quantity=0.40, key="b")
    forward = FillLookupFact(
        binding(),
        FillLookupOutcome.FOUND,
        fills=(first, second),
        venue_fill_id_candidates=("tid-a", "tid-b"),
        response_count=2,
    )
    reverse = FillLookupFact(
        binding(),
        FillLookupOutcome.FOUND,
        fills=(second, first),
        venue_fill_id_candidates=("tid-b", "tid-a"),
        response_count=2,
    )

    assert assess(fill_lookups=(forward,)) == assess(fill_lookups=(reverse,))


def test_binding_incomplete_does_not_attribute_a_fill_lower_bound():
    result = assess(
        binding=binding(expected_external_order_id=None),
        fills=(fill(client_order_id=None, external_order_id="oid-A"),),
    )

    assert result.outcome == ResolutionOutcome.BINDING_INCOMPLETE
    assert result.proven_filled_lower_bound == 0.0


@pytest.mark.parametrize(
    "outcome",
    (
        FillLookupOutcome.NO_MATCH,
        FillLookupOutcome.TRANSPORT_FAILURE,
        FillLookupOutcome.UNSUPPORTED,
        FillLookupOutcome.INVALID_RESPONSE,
        FillLookupOutcome.CONFLICTING_RESPONSE,
        FillLookupOutcome.INCOMPLETE_RESPONSE,
    ),
)
def test_non_found_fill_lookup_cannot_carry_new_fills(outcome):
    with pytest.raises(ValueError, match="only FOUND"):
        FillLookupFact(
            binding(),
            outcome,
            fills=(fill(),),
            venue_fill_id_candidates=(None,),
        )


def test_found_fill_lookup_requires_a_fill():
    with pytest.raises(ValueError, match="requires at least one fill"):
        FillLookupFact(binding(), FillLookupOutcome.FOUND)


def test_requested_qty_within_tolerance_in_fill_lookup_binding_is_compatible():
    result = assess(
        fill_lookups=(
            FillLookupFact(
                binding(requested_qty=1.0 + 0.5e-9),
                FillLookupOutcome.FOUND,
                fills=(fill(),),
                venue_fill_id_candidates=(None,),
                response_count=1,
            ),
        )
    )

    assert result.outcome == ResolutionOutcome.EFFECT_PROVEN_INCOMPLETE
    assert result.binding_complete is True


def test_distinct_known_oids_from_order_and_fill_lookup_bindings_conflict():
    result = assess(
        binding=binding(expected_external_order_id=None),
        order_lookups=(
            OrderLookupFact(
                binding(expected_external_order_id="oid-A"),
                OrderLookupOutcome.NOT_FOUND,
            ),
        ),
        fill_lookups=(
            FillLookupFact(
                binding(expected_external_order_id="oid-B"),
                FillLookupOutcome.NO_MATCH,
            ),
        ),
    )

    assert result.outcome == ResolutionOutcome.BINDING_CONFLICT


def test_missing_fill_cloid_is_compatible_with_oid_from_order_observation_context():
    result = assess(
        binding=binding(expected_external_order_id=None),
        order_observations=(observation(ExternalOrderState.OPEN, external_order_id="oid-A"),),
        fills=(fill(client_order_id=None, external_order_id="oid-A"),),
    )

    assert result.outcome == ResolutionOutcome.EFFECT_PROVEN_INCOMPLETE


def test_compatible_same_fill_key_enrichment_is_order_invariant_without_expected_oid():
    expected = binding(expected_external_order_id=None)
    missing_cloid = fill(
        key="e",
        client_order_id=None,
        external_order_id="oid-A",
    )
    known_cloid = replace(missing_cloid, client_order_id=CLOID)

    forward = assess(binding=expected, fills=(missing_cloid, known_cloid))
    reverse = assess(binding=expected, fills=(known_cloid, missing_cloid))

    assert forward == reverse
    assert forward.outcome == ResolutionOutcome.EFFECT_PROVEN_INCOMPLETE
    assert forward.proven_filled_lower_bound == pytest.approx(0.25)
    assert forward.binding_complete is True


def test_global_bundle_permutation_keeps_compatible_fill_enrichment_deterministic():
    expected = binding(expected_external_order_id=None)
    opened = observation(ExternalOrderState.OPEN, external_order_id="oid-A")
    missing_cloid = fill(key="f", client_order_id=None, external_order_id="oid-A")
    known_cloid = replace(missing_cloid, client_order_id=CLOID)
    lookup = OrderLookupFact(expected, OrderLookupOutcome.NOT_FOUND)

    forward = assess_resolution(
        ResolutionEvidenceBundle(expected, (opened,), (missing_cloid, known_cloid), (lookup,))
    )
    reverse = assess_resolution(
        ResolutionEvidenceBundle(expected, (opened,), (known_cloid, missing_cloid), (lookup,))
    )

    assert forward == reverse


def test_temporal_history_detects_cumulative_decrease_across_unknown_observation():
    result = assess(
        order_observations=(
            observation(
                ExternalOrderState.PARTIAL_OPEN,
                filled=0.8,
                remaining=0.2,
                observed_at="2026-08-30T12:00:00Z",
            ),
            observation(
                ExternalOrderState.UNKNOWN,
                filled=None,
                remaining=None,
                observed_at="2026-08-30T12:01:00Z",
            ),
            observation(
                ExternalOrderState.PARTIAL_OPEN,
                filled=0.5,
                remaining=0.5,
                observed_at="2026-08-30T12:02:00Z",
            ),
        )
    )

    assert result.outcome == ResolutionOutcome.EVIDENCE_CONFLICT
    assert ResolutionReasonCode.ORDER_QUANTITY_CONFLICT in result.conflicts


def test_temporal_history_keeps_terminal_memory_across_unknown_observation():
    result = assess(
        order_observations=(
            observation(ExternalOrderState.CANCELED, observed_at="2026-08-30T12:00:00Z"),
            observation(
                ExternalOrderState.UNKNOWN,
                filled=None,
                remaining=None,
                observed_at="2026-08-30T12:01:00Z",
            ),
            observation(ExternalOrderState.OPEN, observed_at="2026-08-30T12:02:00Z"),
        )
    )

    assert result.outcome == ResolutionOutcome.EVIDENCE_CONFLICT
    assert ResolutionReasonCode.STATUS_CONFLICT in result.conflicts


def test_same_observed_at_does_not_invent_cumulative_ordering():
    result = assess(
        order_observations=(
            observation(
                ExternalOrderState.PARTIAL_OPEN,
                filled=0.8,
                remaining=0.2,
                observed_at="2026-08-30T12:00:00Z",
            ),
            observation(
                ExternalOrderState.PARTIAL_OPEN,
                filled=0.5,
                remaining=0.5,
                observed_at="2026-08-30T12:00:00Z",
            ),
        )
    )

    assert ResolutionReasonCode.ORDER_QUANTITY_CONFLICT not in result.conflicts


@pytest.mark.parametrize(
    ("fills", "response_count"),
    [((fill(),), 0), ((fill(key="1"), fill(key="2")), 1)],
)
def test_found_fill_lookup_requires_response_count_to_cover_returned_fills(fills, response_count):
    with pytest.raises(ValueError, match="cover every returned fill"):
        FillLookupFact(
            binding(),
            FillLookupOutcome.FOUND,
            fills=fills,
            venue_fill_id_candidates=(None,) * len(fills),
            response_count=response_count,
        )


def test_active_flag_is_false_when_another_bundle_fact_conflicts_binding():
    result = assess(
        order_observations=(observation(ExternalOrderState.OPEN),),
        fills=(fill(account_scope="other-account"),),
    )

    assert result.outcome == ResolutionOutcome.BINDING_CONFLICT
    assert result.binding_complete is False
    assert result.external_order_active is False


def test_order_lookup_fact_found_requires_evidence():
    with pytest.raises(ValueError, match="FOUND order lookup requires evidence"):
        OrderLookupFact(binding(), OrderLookupOutcome.FOUND)


def test_order_lookup_fact_not_found_cannot_carry_evidence():
    with pytest.raises(ValueError, match="NOT_FOUND order lookup cannot carry evidence"):
        OrderLookupFact(
            binding(),
            OrderLookupOutcome.NOT_FOUND,
            order_evidence(ExternalOrderState.OPEN),
        )
