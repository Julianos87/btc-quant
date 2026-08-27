"""Contrats passifs, immuables et dédupliqués des preuves externes."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3

import pytest

from btcquant.execution.errors import (
    ExternalFillConflict,
    ExternalObservationConflict,
    FillInvariantViolation,
    InvalidExternalObservation,
    MigrationRequiredError,
)
from btcquant.execution.external_evidence import (
    ExternalEvidenceSource,
    ExternalFill,
    ExternalOrderObservation,
)
from btcquant.execution.order_state import ExternalOrderState
from btcquant.execution.state_store import SCHEMA_VERSION, StateStore

RAW_A = "a" * 64
RAW_B = "b" * 64
OBSERVED_AT = "2026-08-26T12:00:00Z"
VENUE_AT = "2026-08-26T11:59:59Z"


def _order(store: StateStore, intent_id: str = "intent-evidence") -> int:
    return store.begin_order("trend", "slot", intent_id, "MARKET", "BUY", 1.0, "entry")


def _observation(
    order_id: int, *, intent_id: str = "intent-evidence", **changes
) -> ExternalOrderObservation:
    values = {
        "local_order_id": order_id,
        "intent_id": intent_id,
        "venue": "hyperliquid",
        "account_scope": "main",
        "instrument": "BTC/USDC:USDC",
        "side": "BUY",
        "source_kind": ExternalEvidenceSource.ORDER_LOOKUP,
        "normalized_external_status": ExternalOrderState.OPEN,
        "requested_qty": 1.0,
        "cumulative_filled_qty": 0.0,
        "remaining_qty": 1.0,
        "client_order_id": "client-1",
        "external_order_id": "external-1",
        "venue_event_at": VENUE_AT,
        "observed_at": OBSERVED_AT,
        "raw_payload_hash": RAW_A,
    }
    values.update(changes)
    return ExternalOrderObservation(**values)


def _fill(order_id: int, *, intent_id: str = "intent-evidence", **changes) -> ExternalFill:
    values = {
        "local_order_id": order_id,
        "intent_id": intent_id,
        "venue": "hyperliquid",
        "account_scope": "main",
        "instrument": "BTC/USDC:USDC",
        "side": "BUY",
        "source_kind": ExternalEvidenceSource.FILL_LOOKUP,
        "client_order_id": "client-1",
        "external_order_id": "external-1",
        "venue_fill_id": "fill-1",
        "quantity": 0.25,
        "price": 100_000.0,
        "fee": None,
        "fee_asset": None,
        "venue_event_at": VENUE_AT,
        "observed_at": OBSERVED_AT,
        "raw_payload_hash": RAW_A,
    }
    values.update(changes)
    return ExternalFill(**values)


def test_observation_insert_read_duplicate_and_append_only(tmp_path):
    store = StateStore(tmp_path / "state.db")
    order_id = _order(store)
    observation = _observation(order_id)

    persisted, created = store.append_external_order_observation(observation)
    duplicate, duplicate_created = store.append_external_order_observation(observation)
    second = _observation(
        order_id,
        normalized_external_status=ExternalOrderState.PARTIAL_OPEN,
        cumulative_filled_qty=0.25,
        remaining_qty=0.75,
        venue_event_at="2026-08-26T12:00:01Z",
        raw_payload_hash=RAW_B,
    )
    _, second_created = store.append_external_order_observation(second)

    rows = store.get_external_order_observations(order_id)
    assert created is True
    assert duplicate_created is False
    assert duplicate == persisted
    assert second_created is True
    assert rows == [persisted, store.get_external_order_observations(order_id)[1]]
    assert [row.normalized_external_status for row in rows] == [
        ExternalOrderState.OPEN,
        ExternalOrderState.PARTIAL_OPEN,
    ]
    assert not hasattr(store, "update_external_order_observation")


def test_observation_conflict_same_key_fails_closed(tmp_path):
    store = StateStore(tmp_path / "state.db")
    order_id = _order(store)
    first = _observation(order_id)
    store.append_external_order_observation(first)

    with pytest.raises(ExternalObservationConflict):
        store.append_external_order_observation(
            _observation(
                order_id,
                normalized_external_status=ExternalOrderState.PARTIAL_OPEN,
                cumulative_filled_qty=0.25,
                remaining_qty=0.75,
                observation_key=first.observation_key,
            )
        )

    assert len(store.get_external_order_observations(order_id)) == 1


def test_observation_redelivery_with_later_observed_at_is_a_noop(tmp_path):
    store = StateStore(tmp_path / "state.db")
    order_id = _order(store)
    first = _observation(order_id)
    redelivery = _observation(
        order_id,
        observed_at="2026-08-26T12:00:01Z",
        raw_payload_hash=RAW_B,
    )

    persisted, created = store.append_external_order_observation(first)
    duplicate, duplicate_created = store.append_external_order_observation(redelivery)

    assert created is True
    assert duplicate_created is False
    assert duplicate == persisted


def test_observation_redelivery_with_different_persisted_at_is_a_noop(tmp_path):
    store = StateStore(tmp_path / "state.db")
    order_id = _order(store)
    first = _observation(order_id, persisted_at="2026-08-26T12:00:01Z")
    redelivery = _observation(order_id, persisted_at="2026-08-26T12:00:02Z")

    persisted, created = store.append_external_order_observation(first)
    duplicate, duplicate_created = store.append_external_order_observation(redelivery)

    assert created is True
    assert duplicate_created is False
    assert duplicate == persisted
    assert duplicate.persisted_at == "2026-08-26T12:00:01+00:00"


def test_distinct_observations_same_venue_timestamp_persist_separately(tmp_path):
    store = StateStore(tmp_path / "state.db")
    order_id = _order(store)
    opened = _observation(order_id)
    partial = _observation(
        order_id,
        normalized_external_status=ExternalOrderState.PARTIAL_OPEN,
        cumulative_filled_qty=0.25,
        remaining_qty=0.75,
        raw_payload_hash=RAW_B,
    )

    _, opened_created = store.append_external_order_observation(opened)
    _, partial_created = store.append_external_order_observation(partial)

    assert opened.venue_event_at == partial.venue_event_at
    assert opened.observation_key != partial.observation_key
    assert opened_created is True
    assert partial_created is True
    assert len(store.get_external_order_observations(order_id)) == 2


def test_fill_insert_read_duplicate_conflict_and_same_client_multiple_fills(tmp_path):
    store = StateStore(tmp_path / "state.db")
    order_id = _order(store)
    first, created = store.append_external_fill(_fill(order_id))
    duplicate, duplicate_created = store.append_external_fill(_fill(order_id))
    second, second_created = store.append_external_fill(
        _fill(
            order_id,
            venue_fill_id="fill-2",
            quantity=0.75,
            price=100_100.0,
            fee=1.0,
            fee_asset="USDC",
            venue_event_at="2026-08-26T12:00:02Z",
            raw_payload_hash=RAW_B,
        )
    )

    with pytest.raises(ExternalFillConflict):
        store.append_external_fill(_fill(order_id, price=99_999.0))

    assert created is True
    assert duplicate_created is False
    assert duplicate == first
    assert second_created is True
    assert store.get_external_fills(order_id) == [first, second]
    assert first.fill_key != second.fill_key
    assert not hasattr(store, "update_external_fill")


def test_fill_redelivery_with_later_observed_at_is_a_noop(tmp_path):
    store = StateStore(tmp_path / "state.db")
    order_id = _order(store)
    first = _fill(order_id)
    redelivery = _fill(order_id, observed_at="2026-08-26T12:00:01Z")

    persisted, created = store.append_external_fill(first)
    duplicate, duplicate_created = store.append_external_fill(redelivery)

    assert created is True
    assert duplicate_created is False
    assert duplicate == persisted


def test_fill_cross_source_redelivery_is_a_noop(tmp_path):
    store = StateStore(tmp_path / "state.db")
    order_id = _order(store)
    lookup = _fill(order_id)
    private_event = _fill(
        order_id,
        source_kind=ExternalEvidenceSource.PRIVATE_EVENT,
        observed_at="2026-08-26T12:00:01Z",
        raw_payload_hash=RAW_B,
    )

    persisted, created = store.append_external_fill(lookup)
    duplicate, duplicate_created = store.append_external_fill(private_event)

    assert lookup.fill_key == private_event.fill_key
    assert created is True
    assert duplicate_created is False
    assert duplicate == persisted
    assert len(store.get_external_fills(order_id)) == 1


def test_fill_redelivery_with_only_raw_hash_change_is_a_noop(tmp_path):
    store = StateStore(tmp_path / "state.db")
    order_id = _order(store)
    persisted, created = store.append_external_fill(_fill(order_id))
    duplicate, duplicate_created = store.append_external_fill(
        _fill(order_id, raw_payload_hash=RAW_B)
    )

    assert created is True
    assert duplicate_created is False
    assert duplicate == persisted


def test_same_stable_fill_id_with_different_quantity_conflicts(tmp_path):
    store = StateStore(tmp_path / "state.db")
    order_id = _order(store)
    store.append_external_fill(_fill(order_id))

    with pytest.raises(ExternalFillConflict):
        store.append_external_fill(_fill(order_id, quantity=0.5))


def test_same_stable_fill_id_with_different_price_conflicts(tmp_path):
    store = StateStore(tmp_path / "state.db")
    order_id = _order(store)
    store.append_external_fill(_fill(order_id))

    with pytest.raises(ExternalFillConflict):
        store.append_external_fill(_fill(order_id, price=99_999.0))


def test_same_stable_fill_id_with_different_attribution_conflicts(tmp_path):
    store = StateStore(tmp_path / "state.db")
    first_order_id = _order(store, "intent-evidence-first")
    second_order_id = _order(store, "intent-evidence-second")
    store.append_external_fill(_fill(first_order_id, intent_id="intent-evidence-first"))

    with pytest.raises(ExternalFillConflict):
        store.append_external_fill(_fill(second_order_id, intent_id="intent-evidence-second"))


def test_distinct_fills_for_one_client_order_have_distinct_keys(tmp_path):
    store = StateStore(tmp_path / "state.db")
    order_id = _order(store)
    first, first_created = store.append_external_fill(_fill(order_id))
    second, second_created = store.append_external_fill(
        _fill(order_id, venue_fill_id="fill-2", quantity=0.75, raw_payload_hash=RAW_B)
    )

    assert first_created is True
    assert second_created is True
    assert first.fill_key != second.fill_key
    assert len(store.get_external_fills(order_id)) == 2


def test_same_stable_fill_id_fee_enrichment_conflicts_without_mutation(tmp_path):
    store = StateStore(tmp_path / "state.db")
    order_id = _order(store)
    store.append_external_fill(_fill(order_id, fee=None, fee_asset=None))

    with pytest.raises(ExternalFillConflict):
        store.append_external_fill(_fill(order_id, fee=1.0, fee_asset="USDC"))

    persisted = store.get_external_fills(order_id)
    assert len(persisted) == 1
    assert persisted[0].fee is None


def test_client_order_id_omission_is_compatible_and_does_not_mutate_first_row(tmp_path):
    store = StateStore(tmp_path / "state.db")
    order_id = _order(store)
    first = _fill(order_id, client_order_id="C1")
    second = _fill(order_id, client_order_id=None)

    persisted, created = store.append_external_fill(first)
    duplicate, duplicate_created = store.append_external_fill(second)

    assert created is True
    assert duplicate_created is False
    assert duplicate == persisted
    assert duplicate.client_order_id == "C1"
    assert store.get_external_fills(order_id)[0].client_order_id == "C1"


def test_client_order_id_enrichment_is_compatible_and_does_not_mutate_first_row(tmp_path):
    store = StateStore(tmp_path / "state.db")
    order_id = _order(store)
    first = _fill(order_id, client_order_id=None)
    second = _fill(order_id, client_order_id="C1")

    persisted, created = store.append_external_fill(first)
    duplicate, duplicate_created = store.append_external_fill(second)

    assert created is True
    assert duplicate_created is False
    assert duplicate == persisted
    assert duplicate.client_order_id is None
    assert store.get_external_fills(order_id)[0].client_order_id is None


def test_different_known_client_order_ids_conflict(tmp_path):
    store = StateStore(tmp_path / "state.db")
    order_id = _order(store)
    store.append_external_fill(_fill(order_id, client_order_id="C1"))

    with pytest.raises(ExternalFillConflict):
        store.append_external_fill(_fill(order_id, client_order_id="C2"))


def test_external_order_id_omission_is_compatible(tmp_path):
    store = StateStore(tmp_path / "state.db")
    order_id = _order(store)
    store.append_external_fill(_fill(order_id, external_order_id="O1"))

    duplicate, duplicate_created = store.append_external_fill(
        _fill(order_id, external_order_id=None)
    )

    assert duplicate_created is False
    assert duplicate.external_order_id == "O1"
    assert len(store.get_external_fills(order_id)) == 1


def test_different_known_external_order_ids_conflict(tmp_path):
    store = StateStore(tmp_path / "state.db")
    order_id = _order(store)
    store.append_external_fill(_fill(order_id, external_order_id="O1"))

    with pytest.raises(ExternalFillConflict):
        store.append_external_fill(_fill(order_id, external_order_id="O2"))


def test_venue_event_time_omission_is_compatible(tmp_path):
    store = StateStore(tmp_path / "state.db")
    order_id = _order(store)
    store.append_external_fill(_fill(order_id, venue_event_at=VENUE_AT))

    duplicate, duplicate_created = store.append_external_fill(_fill(order_id, venue_event_at=None))

    assert duplicate_created is False
    assert duplicate.venue_event_at == VENUE_AT.replace("Z", "+00:00")
    assert len(store.get_external_fills(order_id)) == 1


def test_different_known_venue_event_times_conflict(tmp_path):
    store = StateStore(tmp_path / "state.db")
    order_id = _order(store)
    store.append_external_fill(_fill(order_id, venue_event_at=VENUE_AT))

    with pytest.raises(ExternalFillConflict):
        store.append_external_fill(_fill(order_id, venue_event_at="2026-08-26T12:00:00Z"))


def test_cross_source_redelivery_omitting_both_order_ids_is_a_noop(tmp_path):
    store = StateStore(tmp_path / "state.db")
    order_id = _order(store)
    lookup = _fill(
        order_id,
        client_order_id=None,
        external_order_id=None,
        source_kind=ExternalEvidenceSource.FILL_LOOKUP,
    )
    private_event = _fill(
        order_id,
        client_order_id=None,
        external_order_id=None,
        source_kind=ExternalEvidenceSource.PRIVATE_EVENT,
        observed_at="2026-08-26T12:00:01Z",
        raw_payload_hash=RAW_B,
    )

    persisted, created = store.append_external_fill(lookup)
    duplicate, duplicate_created = store.append_external_fill(private_event)

    assert created is True
    assert duplicate_created is False
    assert duplicate == persisted
    assert len(store.get_external_fills(order_id)) == 1


def test_delivery_metadata_differences_are_ignored_for_fill_semantics(tmp_path):
    store = StateStore(tmp_path / "state.db")
    order_id = _order(store)
    first = _fill(
        order_id,
        persisted_at="2026-08-26T12:00:01Z",
        raw_payload_hash=RAW_A,
    )
    second = _fill(
        order_id,
        source_kind=ExternalEvidenceSource.PRIVATE_EVENT,
        observed_at="2026-08-26T12:00:01Z",
        persisted_at="2026-08-26T12:00:02Z",
        raw_payload_hash=RAW_B,
    )

    persisted, created = store.append_external_fill(first)
    duplicate, duplicate_created = store.append_external_fill(second)

    assert created is True
    assert duplicate_created is False
    assert duplicate == persisted
    assert duplicate.persisted_at == "2026-08-26T12:00:01+00:00"


def test_evidence_requires_existing_matching_local_order(tmp_path):
    store = StateStore(tmp_path / "state.db")
    with pytest.raises(InvalidExternalObservation, match="introuvable"):
        store.append_external_order_observation(_observation(999))

    order_id = _order(store)
    with pytest.raises(InvalidExternalObservation, match="intent_id incohérent"):
        store.append_external_fill(_fill(order_id, intent_id="wrong-intent"))


@pytest.mark.parametrize(
    "changes",
    [
        {"requested_qty": -1.0},
        {"requested_qty": math.nan},
        {"requested_qty": math.inf},
        {"cumulative_filled_qty": -0.1},
        {"cumulative_filled_qty": 1.1, "remaining_qty": None},
        {"cumulative_filled_qty": 0.4, "remaining_qty": 0.5},
        {"source_kind": "BAD"},
        {"normalized_external_status": "BAD"},
        {"observation_key": "obs-not-a-hash"},
    ],
)
def test_observation_validation_refuses_invalid_quantities_status_and_key(changes, tmp_path):
    order_id = _order(StateStore(tmp_path / "state.db"))
    with pytest.raises(InvalidExternalObservation):
        _observation(order_id, **changes)


def test_missing_optional_venue_timestamp_and_persisted_timestamp_exclusion(tmp_path):
    store = StateStore(tmp_path / "state.db")
    order_id = _order(store)
    first = _observation(order_id, venue_event_at=None, external_order_id=None)
    second = _observation(order_id, venue_event_at=None, external_order_id=None)

    assert first.venue_event_at is None
    assert first.observation_key == second.observation_key
    persisted, created = store.append_external_order_observation(first)
    duplicate, duplicate_created = store.append_external_order_observation(second)
    assert created is True
    assert duplicate_created is False
    assert persisted.persisted_at is not None
    assert duplicate.persisted_at == persisted.persisted_at


@pytest.mark.parametrize(
    "changes",
    [
        {"quantity": -1.0},
        {"quantity": 0.0},
        {"quantity": math.nan},
        {"quantity": math.inf},
        {"price": 0.0},
        {"price": -1.0},
        {"price": math.nan},
        {"price": math.inf},
        {"source_kind": "BAD"},
        {"fill_key": "fill-not-a-hash"},
    ],
)
def test_fill_validation_refuses_invalid_values_source_and_key(changes, tmp_path):
    order_id = _order(StateStore(tmp_path / "state.db"))
    with pytest.raises(FillInvariantViolation):
        _fill(order_id, **changes)


def test_fill_missing_fee_is_explicit_and_time_is_canonical(tmp_path):
    store = StateStore(tmp_path / "state.db")
    order_id = _order(store)
    fill = _fill(order_id, fee=None, fee_asset=None, venue_event_at=None)
    persisted, created = store.append_external_fill(fill)

    assert created is True
    assert persisted.fee is None
    assert persisted.fee_asset is None
    assert persisted.venue_event_at is None
    assert persisted.observed_at.endswith("+00:00")
    assert persisted.persisted_at is not None
    assert persisted.observed_at <= persisted.persisted_at


def test_fallback_keys_are_deterministic_and_json_order_independent(tmp_path):
    order_id = _order(StateStore(tmp_path / "state.db"))
    left = {"a": 1, "b": [2, 3]}
    right = {"b": [2, 3], "a": 1}
    assert json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)
    payload_hash = hashlib.sha256(json.dumps(left, sort_keys=True).encode()).hexdigest()
    first = _fill(order_id, venue_fill_id=None, raw_payload_hash=payload_hash)
    second = _fill(order_id, venue_fill_id=None, raw_payload_hash=payload_hash)

    assert first.fill_key == second.fill_key
    assert first.fill_key.startswith("fill-")


def test_schema_v6_to_v7_is_additive_preserves_orders_and_checks_integrity(tmp_path):
    database = tmp_path / "v6.db"
    current = StateStore(database)
    order_id = _order(current, "pre-v7-order")
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE external_order_observations")
        connection.execute("DROP TABLE external_fills")
        connection.execute("UPDATE metadata SET value='6' WHERE key='schema_version'")
        connection.commit()

    with pytest.raises(MigrationRequiredError):
        StateStore(database)

    migrated = StateStore(database, allow_migration=True)
    with sqlite3.connect(database) as connection:
        version = connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()

    assert version == str(SCHEMA_VERSION) == "7"
    assert {"external_order_observations", "external_fills"} <= tables
    assert migrated.read_order_by_intent("pre-v7-order")["id"] == order_id
    assert foreign_keys == []
    assert migrated.integrity_check()
    StateStore(database)


def test_schema_v7_failure_rolls_back_evidence_tables_and_metadata(tmp_path, monkeypatch):
    database = tmp_path / "v7-failure.db"
    StateStore(database)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE external_order_observations")
        connection.execute("DROP TABLE external_fills")
        connection.execute("UPDATE metadata SET value='6' WHERE key='schema_version'")
        connection.commit()

    original = StateStore._ensure_external_evidence_schema

    def partial_then_fail(connection):
        original(connection)
        raise RuntimeError("v7 injected")

    monkeypatch.setattr(
        StateStore,
        "_ensure_external_evidence_schema",
        staticmethod(partial_then_fail),
    )
    with pytest.raises(RuntimeError, match="v7 injected"):
        StateStore(database, allow_migration=True)

    with sqlite3.connect(database) as connection:
        version = connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert version == "6"
    assert "external_order_observations" not in tables
    assert "external_fills" not in tables
