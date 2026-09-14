"""Tests for the isolated execution-evidence persistence boundary."""

from __future__ import annotations

import ast
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from btcquant.execution.errors import ExternalFillConflict, ExternalObservationConflict
from btcquant.execution.execution_evidence_repository import ExecutionEvidenceRepository
from btcquant.execution.external_evidence import (
    ExternalEvidenceSource,
    ExternalFill,
    ExternalOrderObservation,
)
from btcquant.execution.external_submission_commitment import (
    ExternalSubmissionResponse,
    SubmissionCommitmentError,
    build_submission_response,
)
from btcquant.execution.order_state import ExternalOrderState
from btcquant.execution.state_store import StateStore


OBSERVED = "2026-09-13T12:00:00+00:00"
VENUE_AT = "2026-09-14T11:59:00+00:00"
RAW_HASH = "a" * 64


def _store(tmp_path: Path) -> tuple[StateStore, int]:
    store = StateStore(tmp_path / "state.db")
    order_id = store.begin_order(
        "trend", "slot", "intent-evidence-repository", "MARKET", "BUY", 1.0, "entry"
    )
    return store, order_id


def _observation(order_id: int, **changes: object) -> ExternalOrderObservation:
    values: dict[str, object] = {
        "local_order_id": order_id,
        "intent_id": "intent-evidence-repository",
        "venue": "hyperliquid",
        "account_scope": "paper",
        "instrument": "BTC/USDC:USDC",
        "side": "BUY",
        "source_kind": ExternalEvidenceSource.ORDER_LOOKUP,
        "normalized_external_status": ExternalOrderState.OPEN,
        "requested_qty": 1.0,
        "cumulative_filled_qty": 0.0,
        "remaining_qty": 1.0,
        "client_order_id": "client-evidence-1",
        "external_order_id": "external-evidence-1",
        "venue_event_at": VENUE_AT,
        "observed_at": OBSERVED,
        "raw_payload_hash": RAW_HASH,
    }
    values.update(changes)
    return ExternalOrderObservation(**values)


def _fill(order_id: int, **changes: object) -> ExternalFill:
    values: dict[str, object] = {
        "local_order_id": order_id,
        "intent_id": "intent-evidence-repository",
        "venue": "hyperliquid",
        "account_scope": "paper",
        "instrument": "BTC/USDC:USDC",
        "side": "BUY",
        "source_kind": ExternalEvidenceSource.FILL_LOOKUP,
        "client_order_id": "client-evidence-1",
        "external_order_id": "external-evidence-1",
        "venue_fill_id": "venue-fill-1",
        "quantity": 0.25,
        "price": 100_000.0,
        "fee": -0.01,
        "fee_asset": "USDC",
        "venue_event_at": VENUE_AT,
        "observed_at": OBSERVED,
        "raw_payload_hash": RAW_HASH,
    }
    values.update(changes)
    return ExternalFill(**values)


def _response(order_id: int) -> ExternalSubmissionResponse:
    return build_submission_response(
        local_order_id=order_id,
        intent_id="intent-evidence-repository",
        venue="hyperliquid",
        environment="paper",
        account_scope="paper",
        instrument="BTC/USDC:USDC",
        side="BUY",
        client_order_id="client-evidence-1",
        raw_payload={"id": "external-evidence-1", "status": "closed", "filled": "1"},
        response_acquired_at=OBSERVED,
        ioc_expected=False,
    )


def _table_count(store: StateStore, table: str) -> int:
    with sqlite3.connect(store.path) as connection:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def test_repository_has_no_execution_or_financial_policy_imports() -> None:
    path = Path(__file__).parents[1] / "src/btcquant/execution/execution_evidence_repository.py"
    tree = ast.parse(path.read_text())
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    )
    forbidden = (
        "broker",
        "ccxt",
        "strategy",
        "risk",
        "runner",
        "settlement",
        "reconciliation",
        "notification",
        "state_store",
    )
    assert not any(any(token in module.lower() for token in forbidden) for module in imported)


def test_submission_replay_and_conflict_use_state_store_facade(tmp_path: Path) -> None:
    store, order_id = _store(tmp_path)
    response = _response(order_id)

    _, first_created = store.append_external_submission_response(response, engine="trend")
    _, replay_created = store.append_external_submission_response(response, engine="trend")

    changed = build_submission_response(
        local_order_id=order_id,
        intent_id="intent-evidence-repository",
        venue="hyperliquid",
        environment="paper",
        account_scope="paper",
        instrument="BTC/USDC:USDC",
        side="BUY",
        client_order_id="client-evidence-1",
        raw_payload={"id": "external-evidence-1", "status": "closed", "filled": "0.9"},
        response_acquired_at=OBSERVED,
        ioc_expected=False,
    )
    assert first_created is True
    assert replay_created is False
    with pytest.raises(SubmissionCommitmentError):
        store.append_external_submission_response(changed, engine="trend")


def test_observation_and_fill_identity_conflicts_fail_closed(tmp_path: Path) -> None:
    store, order_id = _store(tmp_path)
    observation = _observation(order_id)
    fill = _fill(order_id)

    assert store.append_external_order_observation(observation)[1] is True
    assert store.append_external_order_observation(observation)[1] is False
    with pytest.raises(ExternalObservationConflict):
        store.append_external_order_observation(
            _observation(
                order_id,
                normalized_external_status=ExternalOrderState.PARTIAL_OPEN,
                observation_key=observation.observation_key,
            )
        )

    assert store.append_external_fill(fill)[1] is True
    assert store.append_external_fill(fill)[1] is False
    with pytest.raises(ExternalFillConflict):
        store.append_external_fill(_fill(order_id, price=99_999.0, fill_key=fill.fill_key))


def test_caller_owned_transaction_is_atomic_on_failure(tmp_path: Path) -> None:
    store, order_id = _store(tmp_path)
    repository = store._execution_evidence_repository
    observation = _observation(order_id)

    with pytest.raises(RuntimeError, match="simulated crash"):
        with store._transaction() as connection:
            repository.append_external_order_observation_in_transaction(
                connection, observation.with_persisted_at(OBSERVED)
            )
            raise RuntimeError("simulated crash")

    assert store.get_external_order_observations(order_id) == []


def test_snapshot_is_read_through_repository_and_contains_evidence(tmp_path: Path) -> None:
    store, order_id = _store(tmp_path)
    observation = _observation(order_id)
    fill = _fill(order_id)
    store.append_external_order_observation(observation)
    store.append_external_fill(fill)

    snapshot = store.read_resolution_snapshot(order_id)

    assert snapshot.order is not None
    assert snapshot.order_observations[0].observation_key == observation.observation_key
    assert snapshot.fills[0].fill_key == fill.fill_key
    assert isinstance(snapshot.lookup_events, tuple)


def test_evidence_writes_do_not_mutate_financial_domains(tmp_path: Path) -> None:
    store, order_id = _store(tmp_path)
    tables = (
        "orders",
        "financial_fill_applications",
        "financial_settlement_applications",
        "trades",
    )
    before = {table: _table_count(store, table) for table in tables}

    store.append_external_submission_response(_response(order_id), engine="trend")
    store.append_external_order_observation(_observation(order_id))
    store.append_external_fill(_fill(order_id))

    after = {table: _table_count(store, table) for table in tables}
    assert after == before


def test_concurrent_duplicate_observation_is_idempotent(tmp_path: Path) -> None:
    store, order_id = _store(tmp_path)
    first = _observation(order_id)
    peers = [StateStore(store.path) for _ in range(2)]

    def append(peer: StateStore) -> tuple[ExternalOrderObservation, bool]:
        return peer.append_external_order_observation(first)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(append, peers))

    assert sorted(created for _, created in results) == [False, True]
    assert len(store.get_external_order_observations(order_id)) == 1


def test_state_store_facade_owns_repository_instance(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    assert isinstance(store._execution_evidence_repository, ExecutionEvidenceRepository)
