from __future__ import annotations

from datetime import timedelta

import pandas as pd
import pytest

from btcquant.research.governance import (
    CandidateLifecycle,
    CandidateState,
    DatasetProvenance,
    DatasetRole,
    ExperimentInvalidated,
    ExperimentRegistry,
    ExperimentSpec,
    GovernanceError,
    HoldoutInvalidated,
    HoldoutSeal,
    HoldoutStatus,
    TrialBudgetExceeded,
    TrialRegistry,
    assess_family_sample_sufficiency,
    assess_sample_sufficiency,
    assert_flat_evaluation_start,
    assert_prefix_invariant,
    build_result_manifest,
    canonical_json,
    derive_max_information_lookback,
    evaluate_parameter_stability,
    fold_data,
    generate_time_folds,
    parameter_fingerprint,
    reject_non_temporal_split,
    validate_dataset_role,
    validate_required_metrics,
    validate_time_index,
)


BASE_SHA = "a" * 40
HASH_A = "b" * 64
HASH_B = "c" * 64


def make_spec(*, budget: int = 3, parameter_space: dict | None = None) -> ExperimentSpec:
    return ExperimentSpec(
        protocol_version="lot5-v1",
        experiment_id="exp-governance-001",
        created_at="2026-08-12T00:00:00Z",
        base_git_sha=BASE_SHA,
        strategy_family="synthetic_fixture",
        target_venue="hyperliquid",
        target_network="mainnet",
        dataset_ids=("hl-v1",),
        dataset_roles={"hl-v1": DatasetRole.SEEN_EXECUTION_PARITY_DATA},
        dataset_hashes={"hl-v1": HASH_A},
        data_cutoffs={"hl-v1": "2026-08-10T20:00:00Z"},
        feature_policy={"causal": True},
        warmup_policy={"mode": "derived"},
        split_policy={"type": "expanding", "shuffle": False},
        purge_policy={"mode": "derived"},
        embargo_policy={"mode": "fixed_before_opening"},
        cost_assumptions={"fee": 0.00045, "slippage_bps": 5.0},
        fee_assumptions={"model": "HYPERLIQUID_BASE_PERP_TAKER_V1"},
        slippage_assumptions={"scenario": "FIXED_BPS_SCENARIO"},
        impact_assumptions={"scenario": "FIXED_BPS_SCENARIO"},
        parameter_space=parameter_space or {"lookback": [20, 55]},
        search_method="pre_registered_grid",
        random_seed=None,
        maximum_trial_budget=budget,
        selection_metric="sharpe",
        secondary_metrics=("cagr", "max_drawdown"),
        acceptance_rules={"sharpe": None},
        stress_tests={"cost_multiplier": "DECISION_REQUIRED"},
        holdout_policy={"close_rule": "fixed_calendar_end_pre_registered"},
        code_provenance={"scope": "research_governance"},
    )


def test_sample_sufficiency_is_family_specific() -> None:
    trend = assess_family_sample_sufficiency(
        strategy_family="trend",
        policies={"trend": {"min_trades": 2}},
        observations=10,
        trades=1,
        elapsed=timedelta(days=30),
    )
    carry = assess_family_sample_sufficiency(
        strategy_family="carry",
        policies={"carry": {"min_trades": 2}},
        observations=10,
        trades=2,
        elapsed=timedelta(days=30),
    )
    assert trend.status == "INSUFFICIENT_SAMPLE"
    assert carry.status == "SUFFICIENT"


def test_canonical_spec_and_trial_ids_are_deterministic() -> None:
    first = make_spec()
    second = make_spec()
    assert first.fingerprint == second.fingerprint
    assert canonical_json(first.to_dict()) == canonical_json(second.to_dict())
    registry = TrialRegistry(first)
    one = registry.record_trial({"lookback": 20}, status="FAILED", result={"error": "fixture"})
    two = registry.record_trial({"lookback": 55}, status="COMPLETED", metrics={"sharpe": 1.0})
    assert one.trial_id.endswith("000001")
    assert two.trial_id.endswith("000002")
    assert registry.attempted == 2


def test_experiment_mutation_requires_new_identity() -> None:
    registry = ExperimentRegistry()
    original = make_spec()
    registry.register(original)
    mutated = make_spec(parameter_space={"lookback": [10, 20]})
    with pytest.raises(ExperimentInvalidated):
        registry.register(mutated)


def test_trial_budget_counts_failures_and_refuses_next_attempt() -> None:
    registry = TrialRegistry(make_spec(budget=1))
    registry.record_trial({}, status="FAILED", result={"error": "crash"})
    with pytest.raises(TrialBudgetExceeded):
        registry.record_trial({}, status="COMPLETED")
    assert registry.records[0].status == "FAILED"


def test_time_series_contract_rejects_naive_duplicate_and_shuffle() -> None:
    with pytest.raises(GovernanceError):
        validate_time_index(pd.date_range("2026-01-01", periods=3, freq="h"))
    duplicate = pd.DatetimeIndex(["2026-01-01T00:00Z", "2026-01-01T01:00Z", "2026-01-01T01:00Z"])
    with pytest.raises(GovernanceError):
        validate_time_index(duplicate)
    with pytest.raises(GovernanceError):
        reject_non_temporal_split(split_type="random", shuffle=False)
    with pytest.raises(GovernanceError):
        reject_non_temporal_split(split_type="expanding", shuffle=True)


def test_chronological_expanding_folds_use_real_timestamps_and_purge() -> None:
    index = pd.date_range("2026-01-01T00:00Z", periods=20, freq="h")
    folds = generate_time_folds(
        index,
        train_duration=timedelta(hours=8),
        evaluation_duration=timedelta(hours=4),
        warmup_duration=timedelta(hours=3),
        purge_duration=timedelta(hours=1),
        embargo_duration=timedelta(hours=1),
    )
    assert len(folds) == 3
    assert folds[0].initial_position == "FLAT"
    assert folds[0].train_points == 8
    assert folds[0].evaluation_points == 4
    assert folds[0].purge_duration_seconds == 3600
    assert folds[1].train_points > folds[0].train_points
    frame = pd.DataFrame({"close": range(len(index))}, index=index)
    context, evaluation = fold_data(frame, folds[0])
    assert context.index[-1] < evaluation.index[0]
    assert evaluation.index[0].tz is not None
    assert_flat_evaluation_start(None)
    with pytest.raises(GovernanceError):
        assert_flat_evaluation_start({"qty": 1})


def test_lookback_is_derived_from_strategy_contract() -> None:
    class FixtureStrategy:
        def warmup_bars(self) -> int:
            return 55

    assert derive_max_information_lookback(FixtureStrategy(), "4h") == timedelta(hours=220)


def test_prefix_invariance_rejects_future_dependent_result() -> None:
    index = pd.date_range("2026-01-01T00:00Z", periods=4, freq="h")
    prefix = pd.Series([1.0, 2.0], index=index[:2])
    extended = pd.Series([1.0, 2.0, 3.0, 4.0], index=index)
    assert_prefix_invariant(lambda value: value, prefix, extended, index[1])

    def future_dependent(value: pd.Series) -> pd.Series:
        return value + value.iloc[-1]

    with pytest.raises(GovernanceError):
        assert_prefix_invariant(future_dependent, prefix, extended, index[1])


def test_parameter_stability_reports_plateau_and_single_fold_concentration() -> None:
    stable = evaluate_parameter_stability(
        1.0,
        [0.98, 1.02],
        all_candidate_scores=[1.0, 0.99, 0.4],
        candidate_fold_scores=[0.9, 1.1, 1.0],
    )
    assert stable.candidate_rank == 1
    assert stable.performance_concentration == pytest.approx(1.1 / 3.0)
    assert stable.stable is None
    sharp = evaluate_parameter_stability(1.0, [0.1], candidate_fold_scores=[3.0, 0.0, 0.0])
    assert sharp.performance_concentration == 1.0
    assert evaluate_parameter_stability(1.0, [0.99], max_neighbor_drop=0.02).stable


def test_candidate_lifecycle_is_fail_closed() -> None:
    candidate = CandidateLifecycle("candidate-1", parameter_fingerprint({"p": 1}))
    candidate.transition(CandidateState.REGISTERED)
    candidate.transition(CandidateState.DEVELOPMENT)
    candidate.freeze()
    candidate.transition(CandidateState.BLIND_OOS_PENDING)
    with pytest.raises(GovernanceError):
        candidate.transition(CandidateState.DRAFT)
    candidate.transition(CandidateState.BLIND_OOS_EVALUATED)
    candidate.transition(CandidateState.REJECTED)
    with pytest.raises(GovernanceError):
        candidate.transition(CandidateState.QUANT_RESEARCH_PASS)


def test_holdout_seal_is_single_use_and_mutation_invalidates() -> None:
    seal = HoldoutSeal(
        candidate_fingerprint=HASH_A,
        parameter_fingerprint=HASH_B,
        experiment_fingerprint=HASH_A,
        code_sha=BASE_SHA,
        dataset_policy={"venue": "hyperliquid", "role": DatasetRole.BLIND_FORWARD_OOS.value},
        holdout_start_rule="first full UTC hour after freeze",
        holdout_end_rule="fixed calendar duration pre-registered",
        metrics={},
        acceptance_gates={"sharpe": "required"},
        cost_assumptions={"fee": 0.00045},
    )
    seal.open()
    seal.validate_identity(
        candidate_fingerprint=HASH_A,
        parameter_fingerprint=HASH_B,
        experiment_fingerprint=HASH_A,
        code_sha=BASE_SHA,
        cost_assumptions={"fee": 0.00045},
    )
    seal.evaluate({"sharpe": 0.5})
    assert seal.status is HoldoutStatus.SPENT
    # Reproduction of a spent holdout is not a reset to UNSEEN.
    with pytest.raises(HoldoutInvalidated):
        seal.open()


def test_holdout_mutation_and_nonfinite_gate_fail_closed() -> None:
    seal = HoldoutSeal(
        candidate_fingerprint=HASH_A,
        parameter_fingerprint=HASH_B,
        experiment_fingerprint=HASH_A,
        code_sha=BASE_SHA,
        dataset_policy={},
        holdout_start_rule="fixed",
        holdout_end_rule="fixed",
        metrics={},
        acceptance_gates={"cagr": "required"},
        cost_assumptions={},
    )
    seal.open()
    with pytest.raises(GovernanceError):
        validate_required_metrics({"cagr": float("nan")}, seal.acceptance_gates)
    with pytest.raises(HoldoutInvalidated):
        seal.validate_identity(
            candidate_fingerprint="d" * 64,
            parameter_fingerprint=HASH_B,
            experiment_fingerprint=HASH_A,
            code_sha=BASE_SHA,
            cost_assumptions={},
        )
    assert seal.status is HoldoutStatus.INVALIDATED


def test_dataset_roles_keep_seen_data_out_of_blind_oos() -> None:
    seen = DatasetProvenance(
        dataset_id="hl-v1",
        venue="hyperliquid",
        network="mainnet",
        symbol="BTC/USDC:USDC",
        role=DatasetRole.SEEN_EXECUTION_PARITY_DATA,
        start="2026-01-14T12:00Z",
        end="2026-08-10T19:00Z",
        rows_or_events=5000,
        sha256=HASH_A,
        cutoff="2026-08-10T20:00Z",
        manifest="audit/baselines/hyperliquid_execution_v1.json",
        already_seen=True,
    )
    with pytest.raises(GovernanceError):
        validate_dataset_role(
            seen,
            DatasetRole.BLIND_FORWARD_OOS,
            target_venue="hyperliquid",
            purpose="HYPERLIQUID_FINAL_OOS_PASS",
        )
    with pytest.raises(GovernanceError):
        DatasetProvenance(
            **{**seen.to_dict(), "role": DatasetRole.BLIND_FORWARD_OOS, "already_seen": True}
        )


def test_sample_sufficiency_does_not_invent_a_threshold() -> None:
    decision = assess_sample_sufficiency(observations=1000, trades=3, elapsed=timedelta(days=30))
    assert decision.status == "DECISION_REQUIRED"
    insufficient = assess_sample_sufficiency(
        observations=100, trades=2, elapsed=timedelta(days=3), min_trades=5
    )
    assert insufficient.status == "INSUFFICIENT_SAMPLE"


def test_result_manifest_is_deterministic_and_contextualizes_trials() -> None:
    spec = make_spec(budget=2)
    trials = TrialRegistry(spec)
    trials.record_trial({"lookback": 20}, status="FAILED", result={"error": "rejected"})
    trials.record_trial({"lookback": 55}, status="COMPLETED", metrics={"sharpe": 0.8})
    start = "2026-01-01T00:00Z"
    index = pd.date_range(start, periods=12, freq="h")
    folds = generate_time_folds(
        index,
        train_duration=timedelta(hours=4),
        evaluation_duration=timedelta(hours=3),
        warmup_duration=timedelta(hours=2),
    )
    dataset = DatasetProvenance(
        dataset_id="synthetic",
        venue="hyperliquid",
        network="mainnet",
        symbol="BTC/USDC:USDC",
        role=DatasetRole.BACKTEST_OOS,
        start=start,
        end=str(index[-1]),
        rows_or_events=len(index),
        sha256=HASH_A,
        cutoff="2026-01-01T12:00Z",
        manifest="synthetic",
    )
    first = build_result_manifest(
        spec=spec,
        candidate_fingerprint=HASH_B,
        code_sha=BASE_SHA,
        datasets=[dataset],
        splits=folds,
        trial_registry=trials,
        selection_rule="pre_registered metric with full trial report",
        metrics={"sharpe": 0.8},
        stability=None,
        holdout_status=HoldoutStatus.UNSEEN,
        generated_at="2026-08-12T00:00Z",
    )
    second = build_result_manifest(
        spec=spec,
        candidate_fingerprint=HASH_B,
        code_sha=BASE_SHA,
        datasets=[dataset],
        splits=folds,
        trial_registry=trials,
        selection_rule="pre_registered metric with full trial report",
        metrics={"sharpe": 0.8},
        stability=None,
        holdout_status=HoldoutStatus.UNSEEN,
        generated_at="2026-08-12T00:00Z",
    )
    assert first["trials_attempted"] == 2
    assert first["report_fingerprint"] == second["report_fingerprint"]
    assert first["report_fingerprint"] != parameter_fingerprint({"lookback": 20})
