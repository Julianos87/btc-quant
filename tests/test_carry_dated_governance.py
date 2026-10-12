from __future__ import annotations

from datetime import UTC, datetime

import pytest

from btcquant.research.carry_dated_governance import (
    EXPERIMENT_ID,
    build_experiment_spec,
    register_experiment,
)
from btcquant.research.governance import DatasetProvenance, DatasetRole, GovernanceError
from btcquant.research.governance_store import GovernanceStore
from btcquant.research.search_gates import validate_search_ready


def provenance(role: DatasetRole = DatasetRole.SEEN_RESEARCH_DATA) -> DatasetProvenance:
    return DatasetProvenance(
        dataset_id="deribit-diagnostic-capture-20261012T071632Z",
        venue="deribit",
        network="mainnet_public_data_only",
        symbol="BTC_USDC dated futures + BTC_USDC spot",
        role=role,
        start="2026-10-12T07:16:32Z",
        end="2026-10-12T07:16:32Z",
        rows_or_events=3,
        sha256="a" * 64,
        cutoff="2026-10-12T07:16:32Z",
        manifest="DIAGNOSTIC_DATED_CARRY_20261012T071632Z.json",
        already_seen=True,
    )


def test_spec_passes_gate_and_is_registered_in_separate_durable_store(tmp_path) -> None:
    spec = build_experiment_spec(
        dataset=provenance(),
        base_git_sha="b" * 40,
        created_at=datetime(2026, 10, 12, 10, 0, tzinfo=UTC),
    )
    db_path = tmp_path / "carry-dated-governance.sqlite3"

    validate_search_ready(spec)
    with GovernanceStore(db_path) as store:
        fingerprint = register_experiment(store, spec)
        stored = store.get_experiment_spec(EXPERIMENT_ID)
        status = store._connection.execute(
            "SELECT status FROM experiments WHERE experiment_id=?", (EXPERIMENT_ID,)
        ).fetchone()[0]

    assert status == "REGISTERED"
    assert stored.fingerprint == spec.fingerprint
    assert fingerprint == spec.fingerprint
    assert stored.dataset_roles[stored.dataset_ids[0]] is DatasetRole.SEEN_RESEARCH_DATA
    assert stored.promotion_gates["blind_holdout_lower_bound_after_frozen_stress"] == "> 0"


def test_blind_or_wrong_venue_data_cannot_be_used_for_development_registration() -> None:
    with pytest.raises(GovernanceError):
        build_experiment_spec(
            dataset=provenance(DatasetRole.BLIND_FORWARD_OOS),
            base_git_sha="b" * 40,
            created_at=datetime(2026, 10, 12, 10, 0, tzinfo=UTC),
        )


def test_single_candidate_spec_freezes_selection_and_keeps_economic_gates() -> None:
    spec = build_experiment_spec(
        dataset=provenance(),
        base_git_sha="b" * 40,
        created_at=datetime(2026, 10, 12, 10, 0, tzinfo=UTC),
    )

    assert spec.maximum_trial_budget == 1
    assert spec.candidate_selection_rule.startswith("no selection")
    assert spec.sample_sufficiency_policy["untouched_forward_holdout_cycles_min"] == 10
    assert spec.acceptance_rules["development_cycles_min"] == 30
