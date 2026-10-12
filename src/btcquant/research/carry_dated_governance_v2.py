"""Versioned governance contract for the dated-carry 20-level collection run."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from btcquant.research.carry_dated_governance import build_experiment_spec as build_v1_spec
from btcquant.research.governance import DatasetProvenance, ExperimentSpec
from btcquant.research.governance_store import GovernanceStore
from btcquant.research.search_gates import validate_search_ready

EXPERIMENT_ID = "deribit_btc_usdc_dated_cash_carry_20261012_v2"
PROTOCOL_VERSION = "carry_btc_dated_v2"
COLLECTION_PROFILE_VERSION = "carry_dated_collection_v2"
POLL_INTERVAL_SECONDS = 30
BOOK_DEPTH_LEVELS_PER_SIDE = 20
RETENTION_TARGET_DAYS = 700
MINIMUM_RETENTION_AFTER_FIRST_ENTRY_DAYS = 560
MINIMUM_COMPLETED_CYCLE_DURATION_DAYS = 14


def build_experiment_spec(
    *,
    dataset: DatasetProvenance,
    base_git_sha: str,
    created_at: datetime,
) -> ExperimentSpec:
    """Build v2 while reusing v1's frozen economic gates byte-for-byte.

    The only changed dimensions are protocol identity and data acquisition. A
    seen development manifest is permitted for preregistration; it cannot be
    used as forward evidence or promoted to blind holdout.
    """

    prior = build_v1_spec(
        dataset=dataset,
        base_git_sha=base_git_sha,
        created_at=created_at,
    )
    collection = {
        "profile_version": COLLECTION_PROFILE_VERSION,
        "poll_interval_seconds": POLL_INTERVAL_SECONDS,
        "book_depth_levels_per_side": BOOK_DEPTH_LEVELS_PER_SIDE,
        "persist_exact_runner_books": True,
        "single_authoritative_book_writer": True,
        "compact_pair_poll_index": True,
        "max_projected_retained_dataset_gib": 4.0,
        "max_peak_storage_including_compaction_gib": 5.0,
        "book_universe": "BTC_USDC spot plus runner-selected earliest eligible future; held pair while open",
        "finite_depth_only": True,
        "minimum_completed_cycle_duration_days": MINIMUM_COMPLETED_CYCLE_DURATION_DAYS,
        "retention_target_days_from_collector_start": RETENTION_TARGET_DAYS,
        "minimum_retention_days_after_first_entry": MINIMUM_RETENTION_AFTER_FIRST_ENTRY_DAYS,
        "no_silent_purge_or_auto_downsampling": True,
    }
    spec = replace(
        prior,
        protocol_version=PROTOCOL_VERSION,
        experiment_id=EXPERIMENT_ID,
        feature_policy={**prior.feature_policy, "collection_policy": collection},
        parameter_space={
            "frozen_policy_version": [PROTOCOL_VERSION],
            "collection_profile_version": [COLLECTION_PROFILE_VERSION],
            "poll_interval_seconds": [POLL_INTERVAL_SECONDS],
            "book_depth_levels_per_side": [BOOK_DEPTH_LEVELS_PER_SIDE],
            "minimum_completed_cycle_duration_days": [
                MINIMUM_COMPLETED_CYCLE_DURATION_DAYS
            ],
            "retention_target_days": [RETENTION_TARGET_DAYS],
            "minimum_retention_days_after_first_entry": [
                MINIMUM_RETENTION_AFTER_FIRST_ENTRY_DAYS
            ],
        },
        sample_sufficiency_policy={
            **(prior.sample_sufficiency_policy or {}),
            "retention_target_days_from_collector_start": RETENTION_TARGET_DAYS,
            "minimum_history_days_after_first_entry": MINIMUM_RETENTION_AFTER_FIRST_ENTRY_DAYS,
            "book_poll_interval_seconds": POLL_INTERVAL_SECONDS,
            "book_depth_levels_per_side": BOOK_DEPTH_LEVELS_PER_SIDE,
            "minimum_completed_cycle_duration_days": MINIMUM_COMPLETED_CYCLE_DURATION_DAYS,
        },
        code_provenance={
            **prior.code_provenance,
            "collection_profile_version": COLLECTION_PROFILE_VERSION,
            "exact_runner_books_required": True,
        },
    )
    validate_search_ready(spec)
    return spec


def register_experiment(store: GovernanceStore, spec: ExperimentSpec) -> str:
    """Register v2 separately; v1 remains immutable in the same store."""

    if spec.experiment_id != EXPERIMENT_ID or spec.protocol_version != PROTOCOL_VERSION:
        raise ValueError("specification incompatible avec le protocole Carry daté v2")
    validate_search_ready(spec)
    return store.register_experiment(spec)


__all__ = [
    "BOOK_DEPTH_LEVELS_PER_SIDE",
    "COLLECTION_PROFILE_VERSION",
    "EXPERIMENT_ID",
    "MINIMUM_COMPLETED_CYCLE_DURATION_DAYS",
    "MINIMUM_RETENTION_AFTER_FIRST_ENTRY_DAYS",
    "POLL_INTERVAL_SECONDS",
    "PROTOCOL_VERSION",
    "RETENTION_TARGET_DAYS",
    "build_experiment_spec",
    "register_experiment",
]
