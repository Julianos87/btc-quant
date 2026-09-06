from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from btcquant.execution.external_settlement_finalization import (
    ExternalSettlementFinalizer,
    ExternalZeroEffectFinalizationStatus,
)
from btcquant.execution.external_settlement_recovery import ExternalSettlementStartupRecovery
from btcquant.execution.external_settlement_runtime import (
    ExternalSettlementRuntime,
    ExternalZeroEffectRuntimeResult,
)
from btcquant.execution.external_submission_commitment import (
    IOC_NO_MATCH_ERROR,
    build_submission_response,
)
from btcquant.execution.financial_order_settlement import FinancialSettlementError
from btcquant.execution.state_store import StateStore

from test_financial_fill_application import _fill, _persisted, _plan


OBSERVED = "2026-09-05T12:00:00Z"
CLIENT_ORDER_ID = "0x" + "a" * 32


def _zero_response(persisted):
    raw_payload = {
        "status": "error",
        "info": {"response": {"data": {"statuses": [{"error": IOC_NO_MATCH_ERROR}]}}},
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


def _pending_order(tmp_path: Path):
    store = StateStore(tmp_path / "state.db")
    persisted = _persisted(store, _plan())
    with store._transaction() as connection:
        connection.execute(
            """
            UPDATE orders
            SET local_state='PENDING_RECONCILIATION', status='PENDING'
            WHERE id=?
            """,
            (persisted.local_order_id,),
        )
    response = _zero_response(persisted)
    store.append_external_submission_response(response, engine="trend")
    return store, persisted, response


def test_zero_effect_finalization_is_atomic_and_idempotent(tmp_path: Path) -> None:
    store, persisted, response = _pending_order(tmp_path)

    first = ExternalSettlementFinalizer(store).finalize_zero_effect(
        persisted.local_order_id,
        submission_key=response.submission_key,
    )
    second = ExternalSettlementFinalizer(store).finalize_zero_effect(
        persisted.local_order_id,
        submission_key=response.submission_key,
    )

    assert first.status == ExternalZeroEffectFinalizationStatus.FINALIZED
    assert second.status == ExternalZeroEffectFinalizationStatus.ALREADY_FINALIZED
    assert first.finalization_event_id == second.finalization_event_id
    assert first.transition_sequence_before == 0
    assert first.transition_sequence_after == 0
    order = store.read_order_by_intent(persisted.intent_id)
    assert order is not None
    assert order["local_state"] == "TERMINAL"
    assert order["status"] == "CANCELED"
    assert order["external_state"] == "CANCELED"
    assert order["filled_qty"] == 0
    assert order["remaining_qty"] == persisted.plan.requested_qty
    with sqlite3.connect(store.path) as connection:
        rows = connection.execute(
            """
            SELECT event_type FROM events
            WHERE event_type='EXTERNAL_ZERO_EFFECT_FINALIZED'
              AND aggregate_id=?
            """,
            (str(persisted.local_order_id),),
        ).fetchall()
    assert len(rows) == 1


def test_zero_effect_finalization_rolls_back_on_base_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, persisted, response = _pending_order(tmp_path)
    original = store._insert_event

    def fail_after_order_update(connection, engine, event_type, payload, *args, **kwargs):
        if event_type == "EXTERNAL_ZERO_EFFECT_FINALIZED":
            raise BaseException("simulated power loss")
        return original(connection, engine, event_type, payload, *args, **kwargs)

    monkeypatch.setattr(store, "_insert_event", fail_after_order_update)
    with pytest.raises(BaseException, match="simulated power loss"):
        ExternalSettlementFinalizer(store).finalize_zero_effect(
            persisted.local_order_id,
            submission_key=response.submission_key,
        )

    order = store.read_order_by_intent(persisted.intent_id)
    assert order is not None
    assert order["local_state"] == "PENDING_RECONCILIATION"
    assert order["status"] == "PENDING"
    with sqlite3.connect(store.path) as connection:
        assert (
            connection.execute(
                """
            SELECT COUNT(*) FROM events
            WHERE event_type='EXTERNAL_ZERO_EFFECT_FINALIZED'
            """
            ).fetchone()[0]
            == 0
        )


def test_zero_effect_finalization_rejects_positive_external_evidence(tmp_path: Path) -> None:
    store, persisted, response = _pending_order(tmp_path)
    store.append_external_fill(_fill(persisted))

    with pytest.raises(FinancialSettlementError, match="EXTERNAL_ZERO_EFFECT_POSITIVE_EVIDENCE"):
        ExternalSettlementFinalizer(store).finalize_zero_effect(
            persisted.local_order_id,
            submission_key=response.submission_key,
        )

    order = store.read_order_by_intent(persisted.intent_id)
    assert order is not None
    assert order["local_state"] == "PENDING_RECONCILIATION"


class _NeverCalledAcquirer:
    def __init__(self) -> None:
        self.calls = 0

    def acquire(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("zero-effect path must not acquire network evidence")


def test_runtime_finalizes_zero_effect_without_acquirer(tmp_path: Path) -> None:
    store, persisted, response = _pending_order(tmp_path)
    from btcquant.execution.broker import Broker

    class BrokerForTest(Broker):
        external_execution = True
        exchange_id = "hyperliquid"
        environment = "testnet"
        market_kind = "perp"
        account_scope = "main"
        symbol = "BTC/USDC:USDC"

        def market_buy(self, qty: float, ref_price: float):
            raise AssertionError("must not submit")

        def market_sell(self, qty: float, ref_price: float):
            raise AssertionError("must not submit")

    class Clock:
        def utc_now(self):
            from datetime import UTC, datetime

            return datetime(2026, 9, 5, 12, 5, tzinfo=UTC)

    acquirer = _NeverCalledAcquirer()
    result = ExternalSettlementRuntime(
        store,
        BrokerForTest(),
        Clock(),
        acquirer=acquirer,
    ).reconcile_order(persisted.local_order_id)

    assert isinstance(result, ExternalZeroEffectRuntimeResult)
    assert result.finalization.status == ExternalZeroEffectFinalizationStatus.FINALIZED
    assert acquirer.calls == 0
    assert response.outcome.value == "DETERMINISTIC_IOC_NO_MATCH"


def test_startup_recovery_finalizes_zero_effect_without_acquirer(tmp_path: Path) -> None:
    store, persisted, response = _pending_order(tmp_path)
    acquirer = _NeverCalledAcquirer()

    def never_context(*args, **kwargs):
        raise AssertionError("zero-effect path must not build an acquisition context")

    report = ExternalSettlementStartupRecovery(store).recover(
        "trend",
        context_factory=never_context,
        acquirer=acquirer,
    )

    assert report.inspected_order_ids == (persisted.local_order_id,)
    assert report.finalized_order_ids == (persisted.local_order_id,)
    assert report.manual_order_ids == ()
    assert report.blocking_reasons == ()
    assert acquirer.calls == 0
    assert response.submission_key
