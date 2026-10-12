from __future__ import annotations

import hashlib
import json
import copy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from btcquant.domain.carry_dated import BookLevel, DatedBook, DatedInstrument
from btcquant.domain.carry_dated_policy import FeeSchedule, MarginPolicy, evaluate_entry
from btcquant.research.carry_dated_governance import (
    build_experiment_spec as build_v1_spec,
    register_experiment as register_v1,
)
from btcquant.research.carry_dated_governance_v2 import (
    BOOK_DEPTH_LEVELS_PER_SIDE,
    EXPERIMENT_ID,
    MINIMUM_COMPLETED_CYCLE_DURATION_DAYS,
    MINIMUM_RETENTION_AFTER_FIRST_ENTRY_DAYS,
    POLL_INTERVAL_SECONDS,
    PROTOCOL_VERSION,
    RETENTION_TARGET_DAYS,
    build_experiment_spec,
    register_experiment,
)
from btcquant.research.governance import DatasetProvenance, DatasetRole
from btcquant.research.governance_store import GovernanceStore

ROOT = Path(__file__).resolve().parents[1]
V1_PATH = ROOT / "audit/carry_dated_protocol_v1.json"
V2_PATH = ROOT / "audit/carry_dated_protocol_v2.json"
V1_REGISTRATION_PATH = ROOT / "audit/carry_dated_governance_registration_v1.json"
NOW = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
SOURCE = "https://www.deribit.com/api/v2/public/get_order_book"


def _dataset() -> DatasetProvenance:
    return DatasetProvenance(
        dataset_id="deribit-diagnostic-capture-20261012T071632Z",
        venue="deribit",
        network="mainnet_public_data_only",
        symbol="BTC_USDC dated futures + BTC_USDC spot",
        role=DatasetRole.SEEN_RESEARCH_DATA,
        start="2026-10-12T07:16:32Z",
        end="2026-10-12T07:16:32Z",
        rows_or_events=6,
        sha256="abaf0506cb370d5132431c7f3ca3f096328df8f27c9d33842b9798a75d766e22",
        cutoff="2026-10-12T07:16:32Z",
        manifest="DIAGNOSTIC_DATED_CARRY_20261012T071632Z.json",
        already_seen=True,
    )


def _protocol_pair() -> tuple[dict, dict]:
    return (
        json.loads(V1_PATH.read_text(encoding="utf-8")),
        json.loads(V2_PATH.read_text(encoding="utf-8")),
    )


def test_v2_has_new_identity_and_preserves_v1_economic_protocol_exactly() -> None:
    v1, v2 = _protocol_pair()
    immutable_economic_sections = (
        "strategy",
        "selection_policy",
        "exit_policy",
        "settlement_accounting",
        "cost_model",
        "margin_and_cash_model",
        "decision_threshold",
        "sample_and_evaluation",
        "stress_tests",
    )

    assert v2["protocol_version"] == PROTOCOL_VERSION
    assert v2["experiment_id"] == EXPERIMENT_ID
    assert v2["protocol_version"] != v1["protocol_version"]
    assert v2["experiment_id"] != v1["experiment_id"]
    v2_sample = copy.deepcopy(v2["sample_and_evaluation"])
    v2_sample.pop("minimum_completed_cycle_duration_days")
    v2_sample.pop("early_protective_exit_rule")
    assert {
        key: (v2_sample if key == "sample_and_evaluation" else v2[key])
        for key in immutable_economic_sections
    } == {key: v1[key] for key in immutable_economic_sections}
    assert v2["sample_and_evaluation"]["development_completed_non_overlapping_cycles_minimum"] == 30
    assert v2["sample_and_evaluation"]["untouched_forward_holdout_completed_cycles_minimum"] == 10
    assert v2["strategy"]["one_open_cycle_at_a_time"] is True
    assert v2["sample_and_evaluation"]["minimum_completed_cycle_duration_days"] == 14


def test_v1_protocol_file_hash_remains_the_historical_registered_hash() -> None:
    registration = json.loads(V1_REGISTRATION_PATH.read_text(encoding="utf-8"))
    actual = hashlib.sha256(V1_PATH.read_bytes()).hexdigest()

    assert actual == registration["protocol_file_sha256"]


def test_v2_freezes_collection_depth_cadence_retention_and_fail_closed_rules() -> None:
    _v1, v2 = _protocol_pair()
    policy = v2["collection_policy"]

    assert policy["poll_interval_seconds"] == POLL_INTERVAL_SECONDS == 30
    assert policy["book_depth_levels_per_side"] == BOOK_DEPTH_LEVELS_PER_SIDE == 20
    assert policy["retention_target_days_from_collector_start"] == RETENTION_TARGET_DAYS == 700
    assert policy["minimum_retention_days_after_first_entry"] == (
        MINIMUM_RETENTION_AFTER_FIRST_ENTRY_DAYS
    ) == 560
    assert policy["persist_exact_runner_books"] is True
    assert policy["one_authoritative_book_writer"].startswith("The runner-supplied recorder")
    assert policy["compact_evidence_layout"].startswith("Store each exact runner DatedBook once")
    assert "never extrapolate" in policy["finite_depth_rule"]
    assert "No fallback expiry" in policy["book_universe"]
    assert "Do not start forward evidence" in policy["forward_collection_gate"]
    assert v2["sample_and_evaluation"]["minimum_completed_cycle_duration_days"] == (
        MINIMUM_COMPLETED_CYCLE_DURATION_DAYS
    )
    assert "cannot substitute" in v2["sample_and_evaluation"]["early_protective_exit_rule"]
    estimate = policy["estimated_storage"]
    assert Decimal(str(estimate["measured_fixture_projection_560d_books_plus_use_proxy_gib"])) < 2
    assert Decimal(str(estimate["measured_fixture_projection_700d_books_plus_use_proxy_gib"])) > 2
    assert Decimal(str(estimate["current_duplicate_collector_lower_bound_700d_gib"])) > 2
    assert Decimal(str(estimate["measured_fixture_projection_700d_books_plus_use_proxy_gib"])) < 4
    assert estimate["retained_dataset_projection_limit_gib"] == 4.0
    assert estimate["peak_compaction_restore_limit_gib"] == 5.0
    assert "<=4 GiB" in policy["storage_acceptance_gate"]
    assert "<5 GiB" in policy["storage_acceptance_gate"]
    assert "<5 GiB" in policy["forward_collection_gate"]


def test_v2_spec_is_separate_search_ready_and_registers_alongside_v1(tmp_path) -> None:
    dataset = _dataset()
    base_sha = "b" * 40
    created = datetime(2026, 10, 12, 10, 0, tzinfo=UTC)
    v1 = build_v1_spec(dataset=dataset, base_git_sha=base_sha, created_at=created)
    v2 = build_experiment_spec(dataset=dataset, base_git_sha=base_sha, created_at=created)
    db_path = tmp_path / "isolated-carry-governance.sqlite3"

    assert v2.protocol_version == PROTOCOL_VERSION
    assert v2.experiment_id == EXPERIMENT_ID
    assert v2.acceptance_rules == v1.acceptance_rules
    assert v2.stress_tests == v1.stress_tests
    assert v2.cost_assumptions == v1.cost_assumptions
    assert v2.fee_assumptions == v1.fee_assumptions
    assert v2.parameter_space["poll_interval_seconds"] == [30]
    assert v2.parameter_space["book_depth_levels_per_side"] == [20]
    assert v2.parameter_space["minimum_completed_cycle_duration_days"] == [14]
    assert v2.feature_policy["collection_policy"]["max_projected_retained_dataset_gib"] == 4.0
    assert v2.feature_policy["collection_policy"]["max_peak_storage_including_compaction_gib"] == 5.0

    with GovernanceStore(db_path) as store:
        register_v1(store, v1)
        fingerprint = register_experiment(store, v2)
        assert store.get_experiment_spec(v1.experiment_id).fingerprint == v1.fingerprint
        assert store.get_experiment_spec(EXPERIMENT_ID).fingerprint == v2.fingerprint
        assert fingerprint == v2.fingerprint


def test_policy_cannot_size_beyond_captured_levels() -> None:
    spot = DatedInstrument(
        venue="deribit",
        instrument_id="BTC_USDC",
        kind="spot",
        base="BTC",
        quote="USDC",
        settlement_currency=None,
        contract_size=Decimal("1"),
        amount_step=Decimal("0.0001"),
        min_amount=Decimal("0.001"),
        tick_size=Decimal("0.1"),
        expiration_utc=None,
        active=True,
        maker_commission=None,
        taker_commission=None,
        fee_currency=None,
        fee_provenance="unknown routed spot fee currency",
        metadata_timestamp=NOW,
        metadata_hash="a" * 64,
        instrument_state="open",
        source=SOURCE,
    )
    future = DatedInstrument(
        venue="deribit",
        instrument_id="BTC_USDC-11NOV26",
        kind="dated_future",
        base="BTC",
        quote="USDC",
        settlement_currency="USDC",
        contract_size=Decimal("1"),
        amount_step=Decimal("0.0001"),
        min_amount=Decimal("0.001"),
        tick_size=Decimal("0.1"),
        expiration_utc=NOW + timedelta(days=30),
        active=True,
        maker_commission=None,
        taker_commission=None,
        fee_currency=None,
        fee_provenance="public metadata",
        metadata_timestamp=NOW,
        metadata_hash="c" * 64,
        instrument_state="open",
        source=SOURCE,
    )

    def levels(price: str) -> tuple[BookLevel, ...]:
        return tuple(BookLevel(Decimal(price), Decimal("0.001")) for _ in range(20))

    spot_book = DatedBook(
        instrument_id=spot.instrument_id,
        bids=levels("99900"),
        asks=levels("100000"),
        market_timestamp=NOW - timedelta(seconds=1),
        received_timestamp=NOW,
        source=SOURCE,
        instrument_state="open",
        mark_price=Decimal("100000"),
        index_price=Decimal("100000"),
        sequence=1,
        content_hash="b" * 64,
    )
    future_book = DatedBook(
        instrument_id=future.instrument_id,
        bids=levels("120000"),
        asks=levels("120100"),
        market_timestamp=NOW - timedelta(seconds=1),
        received_timestamp=NOW,
        source=SOURCE,
        instrument_state="open",
        mark_price=Decimal("120000"),
        index_price=Decimal("100000"),
        sequence=2,
        content_hash="d" * 64,
    )

    decision = evaluate_entry(
        ((spot, future, spot_book, future_book),),
        capital=Decimal("4000"),
        fee_schedule=FeeSchedule.standard_deribit(),
        margin_policy=MarginPolicy.deribit_ssm_linear_btc(),
        now=NOW,
    )

    assert len(spot_book.asks) == BOOK_DEPTH_LEVELS_PER_SIDE
    assert len(future_book.bids) == BOOK_DEPTH_LEVELS_PER_SIDE
    assert decision.quantity <= sum((level.quantity for level in spot_book.asks), Decimal("0"))
    assert decision.quantity <= sum((level.quantity for level in future_book.bids), Decimal("0"))
    assert decision.action in {"OPEN", "WAIT"}
