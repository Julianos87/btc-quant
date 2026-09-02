from __future__ import annotations

from dataclasses import replace
import json
import sqlite3

import pytest

from btcquant.execution.broker import BrokerOrderResult, Fill
from btcquant.execution.order_state import ExternalOrderState
from btcquant.execution.paper_execution_evidence import (
    PAPER_EVIDENCE_VERSION,
    PAPER_FEE_ASSET,
    PAPER_VENUE,
    PaperExecutionEvidence,
    PaperExecutionEvidenceContext,
    build_paper_execution_evidence,
)
from btcquant.execution.state_store import StateStore


OBSERVED_AT = "2026-09-02T12:00:00Z"


class InjectedPowerLoss(BaseException):
    pass


def _new_order(store: StateStore, *, intent_id: str = "paper-intent-1") -> int:
    return store.begin_order(
        "trend",
        "paper-slot",
        intent_id,
        "MARKET",
        "BUY",
        1.0,
        "enter",
        100_000.0,
    )


def _evidence(
    store: StateStore,
    *,
    intent_id: str = "paper-intent-1",
    status: ExternalOrderState = ExternalOrderState.FILLED,
    qty: float = 1.0,
    observed_at: str = OBSERVED_AT,
):
    order_id = _new_order(store, intent_id=intent_id)
    return build_paper_execution_evidence(
        PaperExecutionEvidenceContext(
            local_order_id=order_id,
            intent_id=intent_id,
            engine="trend",
            instrument="BTC/USDC:USDC",
            side="BUY",
        ),
        BrokerOrderResult(
            Fill(price=100_050.0 if qty else 0.0, qty=qty, fee=50.025 if qty else 0.0),
            status,
            1.0,
            0.0,
        ),
        observed_at=observed_at,
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


def test_paper_fill_identity_is_local_deterministic_and_independent_of_ingestion_time(tmp_path):
    first_store = StateStore(tmp_path / "first.db")
    first = _evidence(first_store, observed_at="2026-09-02T12:00:00Z")
    second_store = StateStore(tmp_path / "second.db")
    second = _evidence(second_store, observed_at="2026-09-02T12:05:00Z")

    assert first.fill is not None
    assert second.fill is not None
    assert first.fill.venue == PAPER_VENUE
    assert first.fill.venue_fill_id == second.fill.venue_fill_id
    assert first.fill.venue_fill_id.startswith("paper-local-fill-v1-")
    assert first.fill.fee_asset == PAPER_FEE_ASSET
    assert first.raw_payload_hash == second.raw_payload_hash


def test_paper_identity_cannot_alias_a_different_local_order(tmp_path):
    first_store = StateStore(tmp_path / "first.db")
    first = _evidence(first_store, intent_id="paper-intent-1")
    second_store = StateStore(tmp_path / "second.db")
    second = _evidence(second_store, intent_id="paper-intent-2")

    assert first.fill is not None
    assert second.fill is not None
    assert first.fill.venue_fill_id != second.fill.venue_fill_id


def test_persist_paper_evidence_is_atomic_and_idempotent(tmp_path):
    store = StateStore(tmp_path / "state.db")
    evidence = _evidence(store)

    first = store.persist_paper_execution_evidence(evidence)
    second = store.persist_paper_execution_evidence(evidence)

    assert first.observation_created is True
    assert first.fill_created is True
    assert first.fill is not None
    assert second.observation_created is False
    assert second.fill_created is False
    assert second.fill == first.fill
    assert _count(store, "external_order_observations") == 1
    assert _count(store, "external_fills") == 1
    assert _count(store, "events", event_type="PAPER_EXECUTION_EVIDENCE_PERSISTED") == 2
    with sqlite3.connect(store.path) as connection:
        row = connection.execute(
            "SELECT payload FROM events WHERE event_type = ? ORDER BY id LIMIT 1",
            ("PAPER_EXECUTION_EVIDENCE_PERSISTED",),
        ).fetchone()
    assert row is not None
    assert json.loads(str(row[0]))["contract"] == PAPER_EVIDENCE_VERSION


def test_paper_rejection_persists_an_observation_but_no_individual_fill(tmp_path):
    store = StateStore(tmp_path / "state.db")
    evidence = _evidence(store, status=ExternalOrderState.REJECTED, qty=0.0)

    result = store.persist_paper_execution_evidence(evidence)

    assert evidence.fill is None
    assert result.fill is None
    assert result.fill_created is False
    assert _count(store, "external_order_observations") == 1
    assert _count(store, "external_fills") == 0
    assert _count(store, "events", event_type="PAPER_EXECUTION_EVIDENCE_PERSISTED") == 1


@pytest.mark.parametrize(
    "exception", [RuntimeError("event failure"), InjectedPowerLoss("power loss")]
)
def test_paper_evidence_rollback_never_leaves_a_partial_observation_or_fill(
    tmp_path, monkeypatch, exception: BaseException
):
    store = StateStore(tmp_path / "state.db")
    evidence = _evidence(store)
    original_insert_event = store._insert_event

    def fail_after_evidence(connection, engine, event_type, *args, **kwargs):
        if event_type == "PAPER_EXECUTION_EVIDENCE_PERSISTED":
            raise exception
        return original_insert_event(connection, engine, event_type, *args, **kwargs)

    monkeypatch.setattr(store, "_insert_event", fail_after_evidence)
    with pytest.raises(type(exception)):
        store.persist_paper_execution_evidence(evidence)

    assert _count(store, "external_order_observations") == 0
    assert _count(store, "external_fills") == 0
    assert _count(store, "events", event_type="PAPER_EXECUTION_EVIDENCE_PERSISTED") == 0


def test_paper_evidence_rejects_non_local_or_misaligned_evidence(tmp_path):
    store = StateStore(tmp_path / "state.db")
    evidence = _evidence(store)
    invalid_observation = replace(evidence.observation, venue="hyperliquid")

    with pytest.raises(ValueError, match="local binding"):
        PaperExecutionEvidence(
            context=evidence.context,
            observation=invalid_observation,
            fill=evidence.fill,
            raw_payload_hash=evidence.raw_payload_hash,
        )
