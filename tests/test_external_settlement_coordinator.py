from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

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
    ExternalSettlementAcquisitionResult,
    SettlementRetentionWitness,
)
from btcquant.execution.external_settlement_coordinator import (
    ExternalSettlementCoordinator,
    ExternalSettlementReconciliationStatus,
)
from btcquant.execution.external_settlement_finalization import (
    ExternalSettlementFinalizationStatus,
    ExternalSettlementFinalizer,
)

from btcquant.execution.external_submission_commitment import build_submission_response
from btcquant.execution.order_state import ExternalOrderState
from btcquant.execution.state_store import StateStore
from btcquant.execution.external_settlement_recovery import (
    ExternalSettlementStartupRecovery,
)


from test_financial_fill_application import _persisted, _plan


OBSERVED = "2026-09-05T12:00:00Z"
WINDOW_START = "2026-09-05T11:59:00Z"
WINDOW_END = "2026-09-05T12:03:00Z"
CLIENT_ORDER_ID = "0x" + "a" * 32
EXTERNAL_ORDER_ID = "oid-coordinator"


def _commitment(persisted):
    raw_payload = {
        "status": "closed",
        "info": {
            "response": {
                "data": {
                    "statuses": [
                        {
                            "filled": {
                                "oid": EXTERNAL_ORDER_ID,
                                "totalSz": "1",
                                "avgPx": "100",
                            }
                        }
                    ]
                }
            }
        },
    }
    return build_submission_response(
        local_order_id=persisted.local_order_id,
        intent_id=persisted.intent_id,
        venue="hyperliquid",
        environment="testnet",
        account_scope="main",
        instrument="BTC/USDC:USDC",
        side=persisted.plan.side,
        client_order_id=CLIENT_ORDER_ID,
        raw_payload=raw_payload,
        response_acquired_at=OBSERVED,
        ioc_expected=True,
    )


def _context(persisted, commitment):
    return ExternalSettlementAcquisitionContext(
        local_order_id=persisted.local_order_id,
        intent_id=persisted.intent_id,
        venue="hyperliquid",
        environment="testnet",
        account_scope="main",
        instrument="BTC/USDC:USDC",
        side=persisted.plan.side,
        engine="trend",
        client_order_id=CLIENT_ORDER_ID,
        requested_qty=persisted.plan.requested_qty,
        planned_effect_at=OBSERVED,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        submission_commitment=commitment,
        retention_witness=SettlementRetentionWitness(1, WINDOW_START),
        stability_lookups=1,
    )


def _order_lookup(context):
    evidence = ExternalOrderEvidence(
        local_order_id=context.local_order_id,
        intent_id=context.intent_id,
        venue=context.venue,
        account_scope=context.account_scope,
        instrument=context.instrument,
        side=context.side,
        engine=context.engine,
        expected_client_order_id=context.client_order_id,
        returned_client_order_id=context.client_order_id,
        external_order_id=EXTERNAL_ORDER_ID,
        ccxt_status="closed",
        venue_status="filled",
        normalized_state=ExternalOrderState.FILLED,
        requested_qty=context.requested_qty,
        filled_qty=context.requested_qty,
        remaining_qty=0.0,
        requested_qty_explicit=True,
        filled_qty_explicit=True,
        remaining_qty_explicit=True,
        source_kind=ExternalEvidenceSource.ORDER_LOOKUP,
        venue_event_at=OBSERVED,
        status_event_at=OBSERVED,
        observed_at=OBSERVED,
        raw_payload_hash="b" * 64,
        correlation_complete=True,
        quantities_complete=True,
        contradictory=False,
    )
    return OrderEvidenceLookup(
        OrderLookupContext(
            local_order_id=context.local_order_id,
            intent_id=context.intent_id,
            venue=context.venue,
            account_scope=context.account_scope,
            instrument=context.instrument,
            side=context.side,
            expected_client_order_id=context.client_order_id,
            requested_qty=context.requested_qty,
            engine=context.engine,
        ),
        EvidenceLookupOutcome.FOUND,
        evidence=evidence,
    )


def _fill_lookup(context):
    fill = ExternalFill(
        local_order_id=context.local_order_id,
        intent_id=context.intent_id,
        venue=context.venue,
        account_scope=context.account_scope,
        instrument=context.instrument,
        side=context.side,
        source_kind=ExternalEvidenceSource.FILL_LOOKUP,
        client_order_id=context.client_order_id,
        external_order_id=EXTERNAL_ORDER_ID,
        venue_fill_id=None,
        quantity=context.requested_qty,
        price=100.0,
        fee=-0.01,
        fee_asset="USDC",
        venue_event_at="2026-09-05T12:01:00Z",
        observed_at=OBSERVED,
        raw_payload_hash="c" * 64,
    )
    to_millis = lambda value: int(
        datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000
    )
    return FillEvidenceLookup(
        FillLookupContext(
            local_order_id=context.local_order_id,
            intent_id=context.intent_id,
            venue=context.venue,
            account_scope=context.account_scope,
            instrument=context.instrument,
            side=context.side,
            expected_external_order_id=EXTERNAL_ORDER_ID,
            expected_client_order_id=context.client_order_id,
            start_time_ms=to_millis(context.window_start),
            end_time_ms=to_millis(context.window_end),
            engine=context.engine,
        ),
        FillEvidenceLookupOutcome.FOUND,
        fills=(fill,),
        venue_fill_id_candidates=(None,),
        response_count=1,
        response_limit_reached=False,
    )


class _Reader:
    def __init__(self, value):
        self.value = value

    def lookup_order(self, _context, *, observed_at=None):
        return self.value

    def lookup_fills(self, _context, *, observed_at=None):
        return self.value


class _Acquirer:
    def __init__(self, result: ExternalSettlementAcquisitionResult):
        self.result = result
        self.calls = 0

    def acquire(self, _context, *, observed_at=None):
        self.calls += 1
        return self.result


def _prepared(tmp_path: Path, *, persist_commitment: bool):
    store = StateStore(tmp_path / "state.db")
    persisted = _persisted(store, _plan())
    with store._transaction() as connection:
        connection.execute(
            "UPDATE orders SET local_state='PENDING_RECONCILIATION' WHERE id=?",
            (persisted.local_order_id,),
        )
    response = _commitment(persisted)
    if persist_commitment:
        store.append_external_submission_response(response, engine="trend")
    context = _context(persisted, response.commitment)
    order_lookup = _order_lookup(context)
    fill_lookup = _fill_lookup(context)
    acquisition = CcxtExternalSettlementAcquirer(
        _Reader(order_lookup), _Reader(fill_lookup)
    ).acquire(context, observed_at=OBSERVED)
    assert acquisition.settlement is not None
    return store, context, _Acquirer(acquisition), persisted


def test_external_coordinator_persists_assesses_applies_and_replays(tmp_path: Path) -> None:
    store, context, acquirer, persisted = _prepared(tmp_path, persist_commitment=True)
    coordinator = ExternalSettlementCoordinator(store)

    first = coordinator.reconcile(context, acquirer, observed_at=OBSERVED)
    assert first.status == ExternalSettlementReconciliationStatus.APPLIED
    assert first.evidence_persisted is True
    assert first.settlement_complete is True
    assert first.applied is True
    assert first.already_applied is False
    assert first.finalized is False
    assert first.manual_reconciliation_required is False
    assert first.financial_application_key is not None

    second = coordinator.reconcile(context, acquirer, observed_at=OBSERVED)
    assert second.status == ExternalSettlementReconciliationStatus.ALREADY_APPLIED
    assert second.applied is False
    assert second.already_applied is True
    assert second.financial_application_key == first.financial_application_key
    assert acquirer.calls == 2
    assert len(store.read_financial_settlement_application_chain(persisted.local_order_id)) == 1


def test_external_coordinator_requires_durable_submission_commitment(tmp_path: Path) -> None:
    store, context, acquirer, persisted = _prepared(tmp_path, persist_commitment=False)

    result = ExternalSettlementCoordinator(store).reconcile(context, acquirer, observed_at=OBSERVED)

    assert result.status == ExternalSettlementReconciliationStatus.APPLICATION_BLOCKED
    assert result.blocking_reason == "SUBMISSION_FILL_COMMITMENT_NOT_DURABLE"
    assert result.applied is False
    assert result.manual_reconciliation_required is True
    assert store.read_financial_settlement_application_chain(persisted.local_order_id) == ()


def test_external_coordinator_persists_non_found_lookup_but_does_not_apply(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    persisted = _persisted(store, _plan())
    context = _context(persisted, None)
    lookup = OrderEvidenceLookup(
        OrderLookupContext(
            local_order_id=persisted.local_order_id,
            intent_id=persisted.intent_id,
            venue=context.venue,
            account_scope=context.account_scope,
            instrument=context.instrument,
            side=context.side,
            expected_client_order_id=context.client_order_id,
            requested_qty=context.requested_qty,
            engine=context.engine,
        ),
        EvidenceLookupOutcome.NOT_FOUND,
        reason="not found",
    )
    result = ExternalSettlementAcquisitionResult(
        context=context,
        order_lookup=lookup,
        acquisition_performed=True,
        blocking_reason="ORDER_LOOKUP_NOT_FOUND",
    )

    outcome = ExternalSettlementCoordinator(store).reconcile(
        context, _Acquirer(result), observed_at=OBSERVED
    )

    assert outcome.status == ExternalSettlementReconciliationStatus.NOT_READY
    assert outcome.evidence_persisted is True
    assert outcome.settlement_complete is False
    assert outcome.applied is False
    assert store.read_financial_settlement_application_chain(persisted.local_order_id) == ()


def test_external_finalization_is_atomic_and_idempotent(tmp_path: Path) -> None:
    store, context, acquirer, persisted = _prepared(tmp_path, persist_commitment=True)
    coordinator_result = ExternalSettlementCoordinator(store).reconcile(
        context, acquirer, observed_at=OBSERVED
    )
    assert coordinator_result.applied is True
    assert coordinator_result.settlement_key is not None

    finalizer = ExternalSettlementFinalizer(store)
    first = finalizer.finalize(
        persisted.local_order_id,
        settlement_key=coordinator_result.settlement_key,
    )
    assert first.status == ExternalSettlementFinalizationStatus.FINALIZED
    assert first.transition_sequence_before == 0
    assert first.transition_sequence_after == 1

    order = store.read_orders("trend")[0]
    assert order["local_state"] == "TERMINAL"
    assert order["status"] == "FILLED"
    state = store.load_engine_state("trend")
    assert state is not None
    assert state["slots"]["slot"]["financial_transition_seq"] == 1

    second = finalizer.finalize(
        persisted.local_order_id,
        settlement_key=coordinator_result.settlement_key,
    )
    assert second.status == ExternalSettlementFinalizationStatus.ALREADY_FINALIZED
    assert second.finalization_event_id == first.finalization_event_id
    assert (
        len(
            [
                event
                for event in store.read_events("trend")
                if event["event_type"] == "EXTERNAL_ORDER_FINALIZED"
            ]
        )
        == 1
    )


def test_external_startup_recovery_applies_and_replays_without_submission(tmp_path: Path) -> None:
    store, context, acquirer, persisted = _prepared(tmp_path, persist_commitment=True)
    recovery = ExternalSettlementStartupRecovery(store)
    report = recovery.recover(
        "trend",
        context_factory=lambda _order, commitment: _context(persisted, commitment),
        acquirer=acquirer,
        observed_at=OBSERVED,
    )
    assert report.inspected_order_ids == (persisted.local_order_id,)
    assert report.finalized_order_ids == (persisted.local_order_id,)
    assert report.manual_order_ids == ()
    assert store.read_orders("trend")[0]["local_state"] == "TERMINAL"

    second = recovery.recover(
        "trend",
        context_factory=lambda _order, commitment: _context(persisted, commitment),
        acquirer=acquirer,
    )
    assert second.inspected_order_ids == ()
    assert acquirer.calls == 1


def test_external_startup_recovery_blocks_missing_submission_commitment(tmp_path: Path) -> None:
    store, _context, acquirer, persisted = _prepared(tmp_path, persist_commitment=False)
    report = ExternalSettlementStartupRecovery(store).recover(
        "trend",
        context_factory=lambda _order, commitment: _context(persisted, commitment),
        acquirer=acquirer,
        observed_at=OBSERVED,
    )

    assert report.inspected_order_ids == (persisted.local_order_id,)
    assert report.finalized_order_ids == ()
    assert report.manual_order_ids == (persisted.local_order_id,)
    assert report.blocking_reasons == (
        (
            persisted.local_order_id,
            "MANUAL_RECONCILIATION_REQUIRED_MISSING_SUBMISSION_COMMITMENT",
        ),
    )
    assert acquirer.calls == 0
    assert store.read_orders("trend")[0]["local_state"] == "PENDING_RECONCILIATION"


def test_external_finalization_rolls_back_on_baseexception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, context, acquirer, persisted = _prepared(tmp_path, persist_commitment=True)
    coordinator_result = ExternalSettlementCoordinator(store).reconcile(
        context, acquirer, observed_at=OBSERVED
    )
    assert coordinator_result.settlement_key is not None

    class SimulatedPowerLoss(BaseException):
        pass

    original_insert_event = store._insert_event

    def fail_finalization_event(connection, engine, event_type, payload, **kwargs):
        if event_type == "EXTERNAL_ORDER_FINALIZED":
            raise SimulatedPowerLoss()
        return original_insert_event(connection, engine, event_type, payload, **kwargs)

    monkeypatch.setattr(store, "_insert_event", fail_finalization_event)
    with pytest.raises(SimulatedPowerLoss):
        ExternalSettlementFinalizer(store).finalize(
            persisted.local_order_id,
            settlement_key=coordinator_result.settlement_key,
        )

    order = store.read_orders("trend")[0]
    assert order["local_state"] == "PENDING_RECONCILIATION"
    state = store.load_engine_state("trend")
    assert state is not None
    assert state["slots"]["slot"]["financial_transition_seq"] == 0
    assert not [
        event
        for event in store.read_events("trend")
        if event["event_type"] == "EXTERNAL_ORDER_FINALIZED"
    ]
