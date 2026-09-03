from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from btcquant.execution.broker import BrokerOrderResult, Fill
from btcquant.execution.financial_application_plan import PersistedFinancialApplicationPlan
from btcquant.execution.financial_fill_application import FinancialFillApplicationError
from btcquant.execution.order_state import ExternalOrderState
from btcquant.execution.paper_execution_evidence import (
    PaperExecutionEvidenceContext,
    build_paper_execution_evidence,
)
from btcquant.execution.reconciliation_coordinator import (
    OrderReconciliationCoordinator,
    ReconciliationStatus,
)
from btcquant.execution.state_store import StateStore
from test_financial_fill_application import _persisted, _plan


OBSERVED_AT = "2026-09-02T12:00:00Z"


def _prepared_store(
    tmp_path: Path,
) -> tuple[StateStore, PersistedFinancialApplicationPlan]:
    store = StateStore(tmp_path / "state.db")
    persisted = _persisted(store, _plan())
    with store._transaction() as connection:
        connection.execute(
            "UPDATE orders SET local_state = 'PENDING_RECONCILIATION' WHERE id = ?",
            (persisted.local_order_id,),
        )
    return store, persisted


def _paper_evidence(
    persisted: PersistedFinancialApplicationPlan,
    *,
    status: ExternalOrderState = ExternalOrderState.FILLED,
    quantity: float = 1.0,
):
    return build_paper_execution_evidence(
        PaperExecutionEvidenceContext(
            local_order_id=persisted.local_order_id,
            intent_id=persisted.intent_id,
            engine="trend",
            instrument="BTC/USDC:USDC",
            side=persisted.plan.side,
        ),
        BrokerOrderResult(
            Fill(
                price=110.0 if quantity else 0.0,
                qty=quantity,
                fee=0.02 if quantity else 0.0,
            ),
            status,
            persisted.plan.requested_qty,
            (persisted.plan.requested_qty - quantity if not status.is_terminal else 0.0),
        ),
        observed_at=OBSERVED_AT,
    )


def _count(store: StateStore, table: str, *, event_type: str | None = None) -> int:
    with sqlite3.connect(store.path) as connection:
        if event_type is None:
            row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        else:
            row = connection.execute(
                "SELECT COUNT(*) FROM events WHERE event_type = ?", (event_type,)
            ).fetchone()
    assert row is not None
    return int(row[0])


def test_paper_coordinator_persists_projects_applies_and_reassesses(tmp_path: Path) -> None:
    store, persisted = _prepared_store(tmp_path)
    evidence = _paper_evidence(persisted)
    assert evidence.fill is not None and evidence.fill.fill_key is not None

    result = OrderReconciliationCoordinator(store).reconcile(
        persisted.local_order_id, paper_evidence=evidence
    )

    assert result.status == ReconciliationStatus.APPLIED
    assert result.evidence_persistence is not None
    assert result.evidence_persistence.fill_created is True
    assert result.before.assessment is not None
    assert result.after.assessment is not None
    assert result.financially_applicable_fill_keys == (evidence.fill.fill_key,)
    assert result.irreversibly_authorized_fill_keys == (evidence.fill.fill_key,)
    assert result.unapplied_fill_keys == (evidence.fill.fill_key,)
    assert len(result.commits) == 1
    assert result.commits[0].applied is True
    assert _count(store, "financial_fill_applications") == 1
    assert _count(store, "events", event_type="PAPER_EXECUTION_EVIDENCE_PERSISTED") == 1
    assert _count(store, "events", event_type="FINANCIAL_FILL_APPLIED") == 1
    assert store.read_orders("trend")[0]["local_state"] == "PENDING_RECONCILIATION"


def test_paper_coordinator_replay_is_idempotent_and_never_reapplies_the_fill(
    tmp_path: Path,
) -> None:
    store, persisted = _prepared_store(tmp_path)
    evidence = _paper_evidence(persisted)
    first = OrderReconciliationCoordinator(store).reconcile(
        persisted.local_order_id, paper_evidence=evidence
    )
    assert first.status == ReconciliationStatus.APPLIED

    second = OrderReconciliationCoordinator(store).reconcile(
        persisted.local_order_id, paper_evidence=evidence
    )

    assert second.status == ReconciliationStatus.ALREADY_APPLIED
    assert second.commits == ()
    assert _count(store, "financial_fill_applications") == 1
    assert _count(store, "events", event_type="PAPER_EXECUTION_EVIDENCE_PERSISTED") == 2
    assert _count(store, "events", event_type="FINANCIAL_FILL_APPLIED") == 1


def test_coordinator_without_persisted_evidence_is_not_ready_and_cannot_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, persisted = _prepared_store(tmp_path)

    def forbidden(**_kwargs):
        raise AssertionError("E3 writer must not run without a ready projection")

    monkeypatch.setattr(store, "apply_financial_fill_atomically", forbidden)
    result = OrderReconciliationCoordinator(store).reconcile(persisted.local_order_id)

    assert result.status == ReconciliationStatus.NOT_READY
    assert result.commits == ()
    assert _count(store, "financial_fill_applications") == 0


def test_paper_rejection_is_not_a_financially_applicable_fill(tmp_path: Path) -> None:
    store, persisted = _prepared_store(tmp_path)
    evidence = _paper_evidence(persisted, status=ExternalOrderState.REJECTED, quantity=0.0)

    result = OrderReconciliationCoordinator(store).reconcile(
        persisted.local_order_id, paper_evidence=evidence
    )

    assert result.status == ReconciliationStatus.NO_FINANCIALLY_APPLICABLE_FILL
    assert result.commits == ()
    assert result.after.assessment is not None
    assert result.after.assessment.zero_effect_proven is False
    assert _count(store, "financial_fill_applications") == 0


def test_coordinator_stops_fail_closed_after_one_writer_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, persisted = _prepared_store(tmp_path)
    evidence = _paper_evidence(persisted)
    calls = 0

    def refused(**kwargs):
        nonlocal calls
        calls += 1
        raise FinancialFillApplicationError("FINANCIAL_APPLICATION_STATE_CONFLICT")

    monkeypatch.setattr(store, "apply_financial_fill_atomically", refused)
    result = OrderReconciliationCoordinator(store).reconcile(
        persisted.local_order_id, paper_evidence=evidence
    )

    assert calls == 1
    assert result.status == ReconciliationStatus.APPLICATION_BLOCKED
    assert result.block_code == "FINANCIAL_APPLICATION_STATE_CONFLICT"
    assert result.commits == ()
    assert _count(store, "financial_fill_applications") == 0


def test_coordinator_rejects_evidence_for_a_different_order_before_writing(tmp_path: Path) -> None:
    store, first = _prepared_store(tmp_path)
    evidence = _paper_evidence(
        replace(first, local_order_id=first.local_order_id + 1, intent_id="other-intent")
    )

    with pytest.raises(ValueError, match="local_order_id differs"):
        OrderReconciliationCoordinator(store).reconcile(
            first.local_order_id, paper_evidence=evidence
        )

    assert _count(store, "external_order_observations") == 0
    assert _count(store, "external_fills") == 0
    assert _count(store, "events", event_type="PAPER_EXECUTION_EVIDENCE_PERSISTED") == 0
