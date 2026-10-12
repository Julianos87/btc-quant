"""Pre-register the frozen dated-carry experiment against real data provenance."""

from __future__ import annotations

from datetime import UTC, datetime

from btcquant.research.governance import (
    DatasetProvenance,
    DatasetRole,
    ExperimentSpec,
    GovernanceError,
    validate_dataset_role,
)
from btcquant.research.governance_store import GovernanceStore
from btcquant.research.search_gates import validate_search_ready

EXPERIMENT_ID = "deribit_btc_usdc_dated_cash_carry_20261012_v1"
PROTOCOL_VERSION = "carry_btc_dated_v1"


def build_experiment_spec(
    *,
    dataset: DatasetProvenance,
    base_git_sha: str,
    created_at: datetime,
) -> ExperimentSpec:
    """Build the single-candidate spec from a real immutable source manifest.

    A seen development capture may register this protocol, but cannot be used
    as a blind holdout or economic qualification. The caller must provide an
    actual manifest identity and complete file hash; this function never
    manufactures dataset provenance.
    """

    validate_dataset_role(
        dataset,
        DatasetRole.SEEN_RESEARCH_DATA,
        target_venue="deribit",
        purpose="DATED_CARRY_V1_DEVELOPMENT_PREREGISTRATION",
    )
    if dataset.network.lower() not in {"mainnet", "mainnet_public_data_only"}:
        raise GovernanceError("le protocole v1 exige une source de marché public mainnet")
    if not dataset.manifest or dataset.rows_or_events <= 0:
        raise GovernanceError("un manifest réel avec au moins un événement est obligatoire")
    if created_at.tzinfo is None or created_at.utcoffset() != UTC.utcoffset(created_at):
        raise GovernanceError("created_at doit être timezone-aware UTC")
    created = created_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
    cutoff = dataset.cutoff
    spec = ExperimentSpec(
        protocol_version=PROTOCOL_VERSION,
        experiment_id=EXPERIMENT_ID,
        created_at=created,
        base_git_sha=base_git_sha,
        strategy_family="btc_spot_long_linear_dated_future_short",
        target_venue="deribit",
        target_network="mainnet_public_data_only",
        dataset_ids=(dataset.dataset_id,),
        dataset_roles={dataset.dataset_id: DatasetRole.SEEN_RESEARCH_DATA},
        dataset_hashes={dataset.dataset_id: dataset.sha256},
        data_cutoffs={dataset.dataset_id: cutoff},
        feature_policy={"selection": "none", "market_inputs": "timestamped_depth_books_and_official_settlements"},
        warmup_policy={"not_applicable": "event-driven dated contract lifecycle"},
        split_policy={"type": "expanding", "shuffle": False, "cycles": "chronological_non_overlapping"},
        purge_policy={"mode": "purge_overlapping_entry_to_expiry_cycles"},
        embargo_policy={"mode": "freeze_code_before_forward_holdout"},
        cost_assumptions={
            "fee_model_version": "deribit_standard_taker_delivery_2026-09-25_v1",
            "spot_fee_currency": "unknown; compare BASE and QUOTE scenarios",
            "margin_model_version": "deribit_ssm_linear_futures_tier4_2026-09-28_v1",
            "entry_prices": "walk observed asks/bids; no synthetic fill",
            "terminal_spot_exit": "observed spot bids after official expiry settlement",
        },
        fee_assumptions={
            "spot_taker_rate": "0.0005; public standard-tier assumption",
            "future_taker_rate": "0.00035; public standard-tier assumption",
            "delivery_rate": "0.00025; public standard-tier assumption",
            "spot_exit_taker_rate": "0.0005; public standard-tier assumption",
            "personal_tier_eligibility": "unqualified",
        },
        slippage_assumptions={"entry": "depth-weighted order-book walk", "terminal_basis": "unobserved until exit; separate +5% adverse stress"},
        impact_assumptions={"entry": "finite observed depth only; no extrapolation", "future_exit": "observed asks for protective close"},
        parameter_space={"frozen_policy_version": ["carry_btc_dated_v1"]},
        search_method="single frozen policy; no parameter search or candidate ranking",
        random_seed=20261012,
        maximum_trial_budget=1,
        selection_metric="net_return_on_total_allocated_capital_after_reserve_and_stress",
        secondary_metrics=("max_drawdown", "cash_immobilized", "unbalanced_events", "settlement_backlog"),
        acceptance_rules={
            "development_cycles_min": 30,
            "forward_holdout_cycles_min": 10,
            "regimes_min": 3,
            "lower_one_sided_95pct_bound_after_reserve_and_stress": "> 0",
            "max_drawdown_fraction_of_allocated_capital": "<= 0.10",
            "accounting_or_hedge_violations": 0,
        },
        stress_tests={
            "btc_up_before_settlement": "0.25; separate derivative cash test",
            "terminal_spot_index_basis": "-0.05 spot versus official index; separate from reserve",
            "usdc_usd": "0.99 for USD-equivalent reporting only",
            "commission_multiplier": "1.5",
            "book_execution_cost_multiplier": "2",
            "depth_loss": "entry refused; exit residual reported",
            "settlement_missing_delayed_corrected": "pending; no fabricated value",
            "one_leg_partial_or_rejected": "unbalanced lifecycle replay required",
        },
        holdout_policy={
            "role": "BLIND_FORWARD_OOS",
            "count": 10,
            "seal_before_outcomes": True,
            "seen_diagnostic_capture_excluded": True,
        },
        code_provenance={
            "code_sha": base_git_sha,
            "candidate_selection_enabled": False,
            "fixture_results_are_profit_evidence": False,
        },
        sample_sufficiency_policy={
            "development_completed_non_overlapping_cycles_min": 30,
            "untouched_forward_holdout_cycles_min": 10,
            "causal_volatility_regimes_min": 3,
            "insufficient_status": "INSUFFICIENT_EVIDENCE",
        },
        candidate_selection_rule="no selection; one frozen candidate; choose earliest eligible expiry and tie-break by instrument_id before inspecting premium",
        multiple_testing_policy={"trial_budget": 1, "adaptive_search": False, "multiple_testing_adjustment": "not_applicable_single_candidate"},
        promotion_gates={
            "development_lower_bound_after_frozen_stress": "> 0",
            "blind_holdout_lower_bound_after_frozen_stress": "> 0",
            "max_drawdown_fraction": "<= 0.10",
            "violations": 0,
        },
    )
    validate_search_ready(spec)
    return spec


def register_experiment(
    store: GovernanceStore,
    spec: ExperimentSpec,
) -> str:
    """Validate and durably register one experiment before any trial runs."""

    validate_search_ready(spec)
    return store.register_experiment(spec)


__all__ = ["EXPERIMENT_ID", "PROTOCOL_VERSION", "build_experiment_spec", "register_experiment"]
