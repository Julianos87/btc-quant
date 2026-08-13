from btcquant.research.governance_store import GovernanceStore
from btcquant.research.quant_statistics_registry import (
    durable_dsr_inputs,
    qualify_dsr_from_registry,
)

from test_governance_store import trial_args
from test_search_gates import complete_spec


def test_dsr_population_is_derived_from_durable_trials_including_failures(tmp_path) -> None:
    spec = complete_spec()
    with GovernanceStore(tmp_path / "governance.sqlite3") as store:
        first = store.reserve_trial(**trial_args(spec, {"lookback": 20}))
        store.finish_trial(first.trial_id, status="SUCCEEDED", metrics={"sharpe": 0.10})
        second = store.reserve_trial(**trial_args(spec, {"lookback": 55}))
        store.finish_trial(second.trial_id, status="SUCCEEDED", metrics={"sharpe": 0.30})
        third = store.reserve_trial(**trial_args(spec, {"lookback": 100}))
        store.finish_trial(third.trial_id, status="INVALID_RESULT", result={"reason": "nan"})

        inputs = durable_dsr_inputs(store, spec.experiment_id)
        assert inputs["raw_attempted_trials"] == 3
        assert inputs["valid_trial_sharpes"] == (0.10, 0.30)
        assert inputs["invalid_or_missing_sharpe"] == 1
        assert inputs["dsr_n"] == 3
        assert inputs["dsr_n_convention"] == "CONSERVATIVE_RAW_TRIAL_UPPER_BOUND"
        assert inputs["dsr_n_policy"] == "CONSERVATIVE_RAW_TRIAL_UPPER_BOUND"

        result = qualify_dsr_from_registry(
            store,
            experiment_id=spec.experiment_id,
            observed_sharpe=0.40,
            n_observations=252,
            skewness=0.0,
            raw_kurtosis=3.0,
            return_sampling_frequency="daily",
        )
        assert result.raw_attempted_trials == 3
        assert result.valid_trial_sharpes == 2
        assert result.dsr_n == 3
        assert result.dsr_n_convention == "CONSERVATIVE_RAW_TRIAL_UPPER_BOUND"


def test_dsr_registry_adapter_does_not_accept_caller_trial_count() -> None:
    import inspect

    signature = inspect.signature(qualify_dsr_from_registry)
    assert "raw_attempted_trials" not in signature.parameters
    assert "trial_sharpes" not in signature.parameters


def test_registry_rejects_mixed_dataset_cohorts(tmp_path) -> None:
    import pytest

    spec = complete_spec()
    with GovernanceStore(tmp_path / "governance.sqlite3") as store:
        first = store.reserve_trial(**trial_args(spec, {"lookback": 20}))
        store.finish_trial(first.trial_id, status="SUCCEEDED", metrics={"sharpe": 0.10})
        second_args = trial_args(spec, {"lookback": 55})
        second_args["dataset_fingerprint"] = "c" * 64
        second = store.reserve_trial(**second_args)
        store.finish_trial(second.trial_id, status="SUCCEEDED", metrics={"sharpe": 0.30})
        with pytest.raises(ValueError, match="cohort mismatch"):
            durable_dsr_inputs(store, spec.experiment_id)
