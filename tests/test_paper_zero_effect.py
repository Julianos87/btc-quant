from __future__ import annotations

import json
import sqlite3

import pytest

from btcquant.execution.broker import BrokerOrderResult, Fill
from btcquant.execution.order_state import ExternalOrderState
from btcquant.execution.paper_execution_evidence import (
    PaperExecutionEvidenceContext,
    build_paper_execution_evidence,
)
from btcquant.execution.paper_zero_effect import (
    PAPER_ZERO_EFFECT_EVENT_TYPE,
    PAPER_ZERO_EFFECT_EVIDENCE_VERSION,
    PaperZeroEffectStatus,
    decide_paper_zero_effect,
)
from btcquant.execution.state_store import StateStore
from btcquant.execution.safe_retry import (
    SafeRetryProofSource,
    SafeRetryStatus,
    decide_safe_retry,
)


def _evidence(
    store: StateStore,
    status: ExternalOrderState,
    qty: float = 0.0,
    intent_id: str = "paper-zero-intent",
):
    order_id = store.begin_order(
        "trend", "paper-slot", intent_id, "MARKET", "BUY", 1.0, "entry", 100.0
    )
    return build_paper_execution_evidence(
        PaperExecutionEvidenceContext(
            local_order_id=order_id,
            intent_id=intent_id,
            engine="trend",
            instrument="BTC/USDC:USDC",
            side="BUY",
        ),
        BrokerOrderResult(
            Fill(price=100.0, qty=qty, fee=0.0),
            status,
            1.0,
            0.0,
        ),
        observed_at="2026-09-03T12:00:00Z",
    )


def _event_count(store: StateStore) -> int:
    with sqlite3.connect(store.path) as connection:
        row = connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_type = ?",
            (PAPER_ZERO_EFFECT_EVENT_TYPE,),
        ).fetchone()
    assert row is not None
    return int(row[0])


def test_rejected_paper_response_is_durable_affirmative_zero_effect(tmp_path):
    store = StateStore(tmp_path / "state.db")
    evidence = _evidence(store, ExternalOrderState.REJECTED)

    store.persist_paper_execution_evidence(evidence)

    assert _event_count(store) == 1
    with sqlite3.connect(store.path) as connection:
        row = connection.execute(
            "SELECT payload FROM events WHERE event_type = ?",
            (PAPER_ZERO_EFFECT_EVENT_TYPE,),
        ).fetchone()
    assert row is not None
    payload = json.loads(str(row[0]))
    assert payload["contract"] == PAPER_ZERO_EFFECT_EVIDENCE_VERSION
    assert payload["filled_qty"] == 0.0
    assert payload["remaining_qty"] == 0.0


def test_rejected_zero_effect_journal_is_per_invocation_and_positive_is_not_zero_effect(
    tmp_path,
):
    store = StateStore(tmp_path / "state.db")
    rejected = _evidence(store, ExternalOrderState.REJECTED)
    store.persist_paper_execution_evidence(rejected)
    store.persist_paper_execution_evidence(rejected)
    assert _event_count(store) == 2

    positive = _evidence(
        store, ExternalOrderState.FILLED, qty=1.0, intent_id="paper-positive-intent"
    )
    store.persist_paper_execution_evidence(positive)
    assert _event_count(store) == 2


@pytest.mark.parametrize(
    ("external_execution", "evidence_persisted", "status", "filled", "remaining", "has_fill"),
    [
        (True, True, ExternalOrderState.REJECTED, 0.0, 0.0, False),
        (False, False, ExternalOrderState.REJECTED, 0.0, 0.0, False),
        (False, True, ExternalOrderState.FILLED, 0.0, 0.0, False),
        (False, True, ExternalOrderState.REJECTED, 0.1, 0.0, True),
        (False, True, ExternalOrderState.REJECTED, 0.0, None, False),
    ],
)
def test_zero_effect_policy_is_fail_closed_outside_local_rejection(
    external_execution, evidence_persisted, status, filled, remaining, has_fill
):
    decision = decide_paper_zero_effect(
        external_execution=external_execution,
        evidence_persisted=evidence_persisted,
        external_state=status,
        filled_qty=filled,
        remaining_qty=remaining,
        individual_fill_present=has_fill,
    )
    assert decision.status == PaperZeroEffectStatus.NOT_PROVEN


def test_zero_effect_policy_accepts_only_local_rejected_empty_response():
    decision = decide_paper_zero_effect(
        external_execution=False,
        evidence_persisted=True,
        external_state=ExternalOrderState.REJECTED,
        filled_qty=0.0,
        remaining_qty=0.0,
        individual_fill_present=False,
    )
    assert decision.status == PaperZeroEffectStatus.PROVEN
    assert decision.reason == "PAPER_LOCAL_REJECTION_BEFORE_EFFECT"


def test_external_terminal_zero_aggregates_never_authorize_retry():
    decision = decide_safe_retry(
        external_execution=True,
        local_state="TERMINAL",
        external_state="CANCELED",
        status="CANCELED",
        filled_qty=0.0,
        remaining_qty=0.0,
        zero_effect_proven=False,
        proof_source=None,
    )
    assert decision.status == SafeRetryStatus.BLOCKED
    assert decision.reason == "ZERO_EFFECT_PROOF_REQUIRED"


def test_paper_retry_requires_explicit_local_zero_effect_source():
    blocked = decide_safe_retry(
        external_execution=False,
        local_state="TERMINAL",
        external_state=None,
        status="FAILED",
        filled_qty=0.0,
        remaining_qty=0.0,
        zero_effect_proven=True,
        proof_source=None,
    )
    assert blocked.status == SafeRetryStatus.BLOCKED

    allowed = decide_safe_retry(
        external_execution=False,
        local_state="TERMINAL",
        external_state=None,
        status="FAILED",
        filled_qty=0.0,
        remaining_qty=0.0,
        zero_effect_proven=True,
        proof_source=SafeRetryProofSource.PAPER_LOCAL_SUBMISSION_RESPONSE,
    )
    assert allowed.status == SafeRetryStatus.ALLOWED


def test_zero_effect_journal_failure_rolls_back_its_observation(tmp_path, monkeypatch):
    store = StateStore(tmp_path / "state.db")
    evidence = _evidence(store, ExternalOrderState.REJECTED)
    original_insert_event = store._insert_event

    def fail_zero_effect_event(connection, engine, event_type, *args, **kwargs):
        if event_type == PAPER_ZERO_EFFECT_EVENT_TYPE:
            raise RuntimeError("zero-effect event failure")
        return original_insert_event(connection, engine, event_type, *args, **kwargs)

    monkeypatch.setattr(store, "_insert_event", fail_zero_effect_event)
    with pytest.raises(RuntimeError, match="zero-effect event failure"):
        store.persist_paper_execution_evidence(evidence)

    assert _event_count(store) == 0
    with sqlite3.connect(store.path) as connection:
        observations = connection.execute(
            "SELECT COUNT(*) FROM external_order_observations"
        ).fetchone()
    assert observations == (0,)
