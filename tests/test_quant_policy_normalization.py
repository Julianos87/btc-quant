import json
import pytest
from dataclasses import replace

from btcquant.research.quant_policy import (
    DSR_N_CONVENTION,
    ParameterNeighborhoodSpec,
    stressed_funding_pnl,
    DSR_PASS_THRESHOLD,
    PSR_BENCHMARK_SHARPE,
    PSR_PASS_THRESHOLD,
    PolicySource,
    NeighborOutcome,
    neighbor_stability_passes,
    positive_fold,
    positive_fold_fraction,
    positive_fold_pnl_concentration,
    proposed_policy_v1,
    generate_neighbors,
    passes_probability_gate,
    trend_governance_policy_v1_proposal,
    carry_governance_policy_v1_proposal,
    validate_policy_shape,
)
from btcquant.research.quant_statistics import (
    DSRStatisticalCohort,
    SharpeScale,
    require_comparable_dsr_cohort,
    StatisticalQualification,
    StatisticalStatus,
    probabilistic_sharpe_ratio,
)


def test_probability_gates_require_qualified_result_and_095() -> None:
    result = probabilistic_sharpe_ratio(
        observed_sharpe=1.0,
        benchmark_sharpe=0.0,
        n_observations=252,
        skewness=0.0,
        raw_kurtosis=3.0,
        return_sampling_frequency="1D_UTC",
    )
    assert passes_probability_gate(result, PSR_PASS_THRESHOLD)
    assert passes_probability_gate(result, DSR_PASS_THRESHOLD)
    assert not passes_probability_gate(replace(result, probability=0.949), 0.95)
    assert not passes_probability_gate(
        StatisticalQualification(
            status=StatisticalStatus.NOT_QUALIFIABLE,
            probability=1.0,
            observed_sharpe_per_period=None,
            benchmark_sharpe_per_period=None,
            denominator=None,
            n_observations=1,
            return_sampling_frequency="1D_UTC",
            dependence_status="ASSUMED_IID",
        ),
        0.95,
    )


def test_dsr_counter_labels_and_conservative_n_are_distinct() -> None:
    policy = proposed_policy_v1()
    counters = policy.dsr_counters.counters(100, 37)
    assert counters.raw_attempted_trials == 100
    assert counters.valid_sr_trials == 37
    assert counters.dsr_n == 100
    assert counters.dsr_n_convention == DSR_N_CONVENTION
    assert policy.dsr_counters.raw_attempted_trials_label == "RAW_ATTEMPTED_TRIALS"
    assert policy.dsr_counters.valid_sr_trials_label == "VALID_SR_TRIALS"
    assert policy.dsr_counters.dsr_n_label == "DSR_N"
    assert policy.psr_benchmark_sharpe == PSR_BENCHMARK_SHARPE


def test_daily_utc_coverage_replaces_theoretical_equity_calendar_counts() -> None:
    policy = proposed_policy_v1()
    dumped = json.dumps(policy.to_dict(), sort_keys=True)
    assert policy.trend.return_coverage.return_sampling_frequency == "1D_UTC"
    assert policy.carry.return_coverage.return_sampling_frequency == "1D_UTC"
    assert policy.trend.return_coverage.minimum_valid_return_coverage == 0.95
    assert policy.carry.return_coverage.minimum_valid_return_coverage == 0.95
    assert "minimum_return_observations" not in dumped


def test_trend_holdout_requires_duration_and_trade_count_and_caps_at_24_months() -> None:
    closure = proposed_policy_v1().trend.holdout_closure
    assert closure.status(elapsed_months=17, completed_trades=30) == "OPEN"
    assert closure.status(elapsed_months=18, completed_trades=30) == "CLOSE_ELIGIBLE"
    assert closure.status(elapsed_months=24, completed_trades=29) == "INSUFFICIENT_SAMPLE"


def test_carry_holdout_requires_24_months_and_20_round_trips_and_caps_at_36() -> None:
    closure = proposed_policy_v1().carry.holdout_closure
    assert closure.status(elapsed_months=24, completed_trades=19) == "OPEN"
    assert closure.status(elapsed_months=24, completed_trades=20) == "CLOSE_ELIGIBLE"
    assert closure.status(elapsed_months=36, completed_trades=19) == "INSUFFICIENT_SAMPLE"
    assert "not PSR/DSR" in proposed_policy_v1().carry.economic_event_role


def test_cost_stress_is_component_specific_and_haircuts_favorable_funding() -> None:
    policy = proposed_policy_v1()
    trend = next(item for item in policy.trend.cost_stress.scenarios if item.required)
    carry = next(item for item in policy.carry.cost_stress.scenarios if item.required)
    severe = next(
        item for item in policy.carry.cost_stress.scenarios if item.name == "SEVERE_DIAGNOSTIC"
    )
    assert trend.slippage_multiplier == 1.5
    assert trend.impact_multiplier == 1.5
    assert trend.fee_assumption == "STRUCTURAL_ADVERSE_FEE_RATE"
    assert trend.fee_multiplier == 1.25
    assert carry.fee_multiplier == 1.25
    assert carry.borrow_multiplier == 1.5
    assert carry.favorable_funding_haircut == 0.75
    assert severe.borrow_multiplier == 2.0
    assert severe.favorable_funding_haircut == 0.50


def test_fold_semantics_and_positive_pnl_concentration_are_fail_closed() -> None:
    policy = proposed_policy_v1()
    assert positive_fold(0.0) is False
    assert positive_fold(0.01) is True
    assert positive_fold_fraction((1.0, 0.0, -1.0)) == 1 / 3
    assert policy.trend.fold_consistency.positive_fold_definition.startswith("net fold return")
    assert policy.trend.fold_consistency.worst_fold_metric == "NET_RETURN"
    assert policy.trend.fold_consistency.worst_fold_floor == -0.20
    assert positive_fold_pnl_concentration((3.0, 2.0)) == 0.6
    assert positive_fold_pnl_concentration(()) is None
    assert positive_fold_pnl_concentration((-1.0,)) is None


def test_neighbor_generation_is_pre_registered_and_boundary_safe() -> None:
    assert generate_neighbors(1, kind="numeric_discrete", lower=1, upper=3) == (2,)
    assert generate_neighbors(10.0, kind="numeric_continuous") == (9.0, 11.0)
    assert generate_neighbors("b", kind="categorical", categories=("c", "a", "b")) == ("a", "c")


def test_neighbor_stability_requires_economic_core_and_80_percent_metric_rule() -> None:
    passing = NeighborOutcome(True, True, 0.8)
    failing = NeighborOutcome(True, False, 1.0)
    assert neighbor_stability_passes(1.0, (passing, passing, passing, passing))
    assert not neighbor_stability_passes(1.0, (passing, passing, passing, failing))
    assert not neighbor_stability_passes(0.0, (passing, passing, passing, passing))
    assert not neighbor_stability_passes(1.0, ())


def test_policy_json_and_family_fingerprints_are_canonical_and_source_tagged() -> None:
    policy = proposed_policy_v1()
    trend_json = trend_governance_policy_v1_proposal()
    carry_json = carry_governance_policy_v1_proposal()
    trend_payload = json.loads(trend_json)
    carry_payload = json.loads(carry_json)
    assert trend_payload["policy_id"] == "TREND_GOVERNANCE_POLICY_V1"
    assert carry_payload["policy_id"] == "CARRY_GOVERNANCE_POLICY_V1"
    assert trend_payload["status"] == "PROPOSED"
    assert trend_payload["fingerprint"] == policy.family_fingerprint("Trend")
    assert carry_payload["fingerprint"] == policy.family_fingerprint("Carry")
    assert trend_json == trend_governance_policy_v1_proposal()
    assert set(policy.provenance_map().values()) <= {item.value for item in PolicySource}
    assert any(
        value == PolicySource.PRIMARY_REFERENCE.value for value in policy.provenance_map().values()
    )
    validate_policy_shape(policy)


def test_numeric_fee_vectors_and_funding_stress_are_exact_and_adverse() -> None:
    policy = proposed_policy_v1()
    trend = {item.name: item for item in policy.trend.cost_stress.scenarios}
    carry = {item.name: item for item in policy.carry.cost_stress.scenarios}
    assert (
        trend["BASELINE"].fee_multiplier,
        trend["BASELINE"].slippage_multiplier,
        trend["BASELINE"].impact_multiplier,
    ) == (1.0, 1.0, 1.0)
    assert (
        trend["MODERATE_REQUIRED"].fee_multiplier,
        trend["MODERATE_REQUIRED"].slippage_multiplier,
        trend["MODERATE_REQUIRED"].impact_multiplier,
    ) == (1.25, 1.5, 1.5)
    assert (
        trend["SEVERE_DIAGNOSTIC"].fee_multiplier,
        trend["SEVERE_DIAGNOSTIC"].slippage_multiplier,
        trend["SEVERE_DIAGNOSTIC"].impact_multiplier,
    ) == (1.5, 2.0, 2.0)
    assert (
        carry["MODERATE_REQUIRED"].fee_multiplier,
        carry["MODERATE_REQUIRED"].borrow_multiplier,
        carry["MODERATE_REQUIRED"].favorable_funding_haircut,
    ) == (1.25, 1.5, 0.75)
    assert (
        carry["SEVERE_DIAGNOSTIC"].fee_multiplier,
        carry["SEVERE_DIAGNOSTIC"].borrow_multiplier,
        carry["SEVERE_DIAGNOSTIC"].favorable_funding_haircut,
    ) == (1.5, 2.0, 0.50)
    assert stressed_funding_pnl(10.0, 0.75) == 7.5
    assert stressed_funding_pnl(-10.0, 0.75) == -10.0


def test_parameter_neighborhoods_use_declared_domain_or_explicit_delta() -> None:
    discrete = ParameterNeighborhoodSpec(
        "lookback", "DISCRETE_ORDERED", (10, 20, 40, 80), "ADJACENT_DOMAIN_VALUES"
    )
    relative = ParameterNeighborhoodSpec(
        "threshold", "CONTINUOUS", (0.09, 0.10, 0.11), "RELATIVE_DELTA", relative_delta=0.10
    )
    absolute = ParameterNeighborhoodSpec(
        "period", "CONTINUOUS", (8.0, 10.0, 12.0), "ABSOLUTE_DELTA", absolute_delta=2.0
    )
    categorical = ParameterNeighborhoodSpec(
        "mode",
        "CATEGORICAL",
        ("a", "b", "c"),
        "REGISTERED_ALTERNATIVES",
        categories=("a", "b", "c"),
    )
    assert discrete.neighbors(20) == (10, 40)
    assert relative.neighbors(0.10) == (0.09, 0.11)
    assert absolute.neighbors(10.0) == (8.0, 12.0)
    assert categorical.neighbors("b") == ("a", "c")
    assert generate_neighbors(20, spec=discrete) == (10, 40)


def test_parameter_specification_mutation_changes_fingerprint() -> None:
    policy = proposed_policy_v1()
    spec = ParameterNeighborhoodSpec(
        "lookback", "DISCRETE_ORDERED", (10, 20, 40), "ADJACENT_DOMAIN_VALUES"
    )
    stability = replace(policy.trend.parameter_stability, neighborhood_specs=(spec,))
    trend = replace(policy.trend, parameter_stability=stability)
    mutated = replace(policy, trend=trend)
    assert mutated.fingerprint != policy.fingerprint
    assert policy.trend.parameter_stability.core_gates == (
        "net_return",
        "sample_sufficiency",
        "fold_consistency",
        "drawdown",
        "required_cost_stress",
    )
    assert policy.trend.parameter_stability.statistical_gates_included is False


def _cohort() -> DSRStatisticalCohort:
    return DSRStatisticalCohort(
        strategy_family="Trend",
        experiment_id="exp-1",
        protocol_fingerprint="p" * 64,
        dataset_identity="hl-v1",
        dataset_role="SEEN_EXECUTION_PARITY_DATA",
        evaluation_split_specification="expanding:train=365d:test=90d",
        evaluation_interval_policy="fixed_2026_window",
        return_sampling_frequency="1D_UTC",
        sharpe_convention="excess_return_over_equity",
        sharpe_scale=SharpeScale.PER_PERIOD,
        cost_model="hl-v1-qualified-costs",
        objective_definition="NET_RETURN_AFTER_COSTS",
        warmup_purge_embargo_semantics="warmup=30d;purge=1d;embargo=1d",
    )


def test_dsr_cohort_accepts_identical_and_rejects_protocol_mismatches() -> None:
    cohort = _cohort()
    assert require_comparable_dsr_cohort((cohort, cohort)).fingerprint == cohort.fingerprint
    for field, value in (
        ("return_sampling_frequency", "1h"),
        ("cost_model", "other-costs"),
        ("evaluation_split_specification", "rolling:train=90d"),
        ("dataset_role", "BLIND_FORWARD_OOS"),
        ("strategy_family", "Carry"),
    ):
        changed = replace(cohort, **{field: value})
        with pytest.raises(ValueError, match="cohort mismatch"):
            require_comparable_dsr_cohort((cohort, changed))


def test_policy_completeness_and_approval_freeze_are_machine_readable(tmp_path) -> None:
    policy = proposed_policy_v1()
    assert policy.completeness_issues() == ()
    assert "dsr_cohort_fields" in policy.canonical_family_json("Trend")
    expected_base = "9924bd9182375f9d247dfcd6cb7931b5adc10e17"
    assert (
        policy.family_fingerprint("Trend")
        == "d2a7fbd2e215d3db375dcfa0d30ec6c42e809c6eee624c1d4c0496ac2d42e5a8"
    )
    assert (
        policy.family_fingerprint("Carry")
        == "ba1030b3cc206605654b1ab8e6e563acbef47e7ce311b8527a021bf7c6545083"
    )
    assert policy.fingerprint == "50c0c8a7c2f08fec705456567df7fb4a9de79e6a940ae04d0bc39886cbfb57e4"

    from btcquant.research.governance_store import GovernanceStore

    with GovernanceStore(tmp_path / "governance.sqlite3") as store:
        approved = policy.approve(store, base_git_sha=expected_base)
        frozen = approved.freeze(store, base_git_sha=expected_base)
        assert approved.status.value == "APPROVED"
        assert frozen.status.value == "FROZEN"
        assert policy.fingerprint == approved.fingerprint == frozen.fingerprint
        assert (
            policy.family_fingerprint("Trend")
            == approved.family_fingerprint("Trend")
            == frozen.family_fingerprint("Trend")
        )
        assert (
            policy.family_fingerprint("Carry")
            == approved.family_fingerprint("Carry")
            == frozen.family_fingerprint("Carry")
        )
        assert store.get_policy_fingerprint(policy.policy_version) == {
            "fingerprint": frozen.fingerprint,
            "status": "FROZEN",
        }
        events = store._connection.execute(
            "SELECT event_type, payload_json FROM governance_events ORDER BY event_id"
        ).fetchall()
        assert [row[0] for row in events] == [
            "POLICY_LIFECYCLE_TRANSITION",
            "POLICY_LIFECYCLE_TRANSITION",
            "POLICY_FINGERPRINT_RECORDED",
        ]
        approval_payload = json.loads(events[0][1])
        assert approval_payload["previous_state"] == "PROPOSED"
        assert approval_payload["new_state"] == "APPROVED"
        assert approval_payload["base_git_sha"] == expected_base
        assert approval_payload["combined_fingerprint"] == policy.fingerprint
        with pytest.raises(Exception, match="fingerprint"):
            store.record_policy_fingerprint(
                policy_version=policy.policy_version, fingerprint="0" * 64, status="FROZEN"
            )

        changed = replace(policy, confidence_level=0.94)
        assert changed.fingerprint != policy.fingerprint
        with pytest.raises(Exception, match="immutable"):
            frozen.derive_new_version(policy.policy_version, confidence_level=0.94)
        new_policy = frozen.derive_new_version("QUANT_POLICY_V2", confidence_level=0.94)
        assert new_policy.status.value == "PROPOSED"
        assert new_policy.fingerprint != frozen.fingerprint


def test_frozen_policy_changes_require_a_new_version() -> None:
    policy = proposed_policy_v1()
    frozen = policy.approve().freeze()
    trend = frozen.trend
    changed_scenario = replace(trend.cost_stress.scenarios[0], fee_multiplier=1.01)
    changes = [
        {"confidence_level": 0.94},
        {"trend": replace(trend, maximum_drawdown=-0.25)},
        {
            "trend": replace(
                trend, holdout_closure=replace(trend.holdout_closure, minimum_duration_months=19)
            )
        },
        {
            "trend": replace(
                trend,
                cost_stress=replace(
                    trend.cost_stress,
                    scenarios=(changed_scenario, *trend.cost_stress.scenarios[1:]),
                ),
            )
        },
        {
            "trend": replace(
                trend,
                parameter_stability=replace(
                    trend.parameter_stability, relative_metric_tolerance=0.19
                ),
            )
        },
        {
            "trend": replace(
                trend,
                return_coverage=replace(trend.return_coverage, minimum_valid_return_coverage=0.94),
            )
        },
    ]
    for mutation in changes:
        with pytest.raises(Exception, match="immutable"):
            frozen.derive_new_version(frozen.policy_version, **mutation)
