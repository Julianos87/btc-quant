from math import isclose, sqrt
from statistics import NormalDist

from btcquant.research.quant_statistics import (
    SharpeScale,
    StatisticalStatus,
    deflated_sharpe_ratio,
    probabilistic_sharpe_ratio,
)


def test_psr_matches_independent_primary_formula_with_raw_kurtosis() -> None:
    observed = 0.58
    benchmark = 0.20
    n = 60
    skew = -0.72
    kurtosis = 5.78
    denominator = sqrt(1 - skew * observed + ((kurtosis - 1) / 4) * observed**2)
    expected = NormalDist().cdf((observed - benchmark) * sqrt(n - 1) / denominator)

    result = probabilistic_sharpe_ratio(
        observed_sharpe=observed,
        benchmark_sharpe=benchmark,
        n_observations=n,
        skewness=skew,
        raw_kurtosis=kurtosis,
        return_sampling_frequency="daily",
    )

    assert result.status is StatisticalStatus.QUALIFIED
    assert result.probability is not None
    assert isclose(result.probability, expected, rel_tol=0, abs_tol=1e-15)
    assert result.observed_sharpe_per_period == observed
    assert result.benchmark_sharpe_per_period == benchmark


def test_psr_annualized_and_per_period_contracts_are_equivalent() -> None:
    per_period = probabilistic_sharpe_ratio(
        observed_sharpe=1.2,
        benchmark_sharpe=0.0,
        n_observations=252,
        skewness=0.1,
        raw_kurtosis=3.4,
        return_sampling_frequency="daily",
    )
    annualized = probabilistic_sharpe_ratio(
        observed_sharpe=1.2 * sqrt(252),
        benchmark_sharpe=0.0,
        n_observations=252,
        skewness=0.1,
        raw_kurtosis=3.4,
        return_sampling_frequency="daily",
        sharpe_scale=SharpeScale.ANNUALIZED,
        periods_per_year=252,
    )

    assert per_period.probability == annualized.probability


def test_psr_fails_closed_for_small_samples_bad_moments_and_dependence() -> None:
    common = {
        "observed_sharpe": 0.5,
        "benchmark_sharpe": 0.0,
        "skewness": 0.0,
        "raw_kurtosis": 3.0,
        "return_sampling_frequency": "hourly",
    }
    assert (
        probabilistic_sharpe_ratio(n_observations=1, **common).status
        is StatisticalStatus.NOT_QUALIFIABLE
    )
    assert (
        probabilistic_sharpe_ratio(n_observations=3, **common).status
        is StatisticalStatus.NOT_QUALIFIABLE
    )
    bad_moments = dict(common)
    bad_moments["raw_kurtosis"] = -1
    assert (
        probabilistic_sharpe_ratio(n_observations=20, **bad_moments).status
        is StatisticalStatus.NOT_QUALIFIABLE
    )
    assert (
        probabilistic_sharpe_ratio(
            n_observations=20,
            dependence_status="SERIAL_DEPENDENCE_DETECTED",
            **common,
        ).status
        is StatisticalStatus.ASSUMPTION_NOT_SATISFIED
    )


def test_dsr_uses_expected_max_formula_and_raw_attempt_count() -> None:
    trials = (0.10, 0.30, 0.50)
    raw_attempts = 5
    trial_mean = sum(trials) / len(trials)
    trial_std = (sum((value - trial_mean) ** 2 for value in trials) / 2) ** 0.5
    euler = 0.5772156649015329
    max_z = ((1 - euler) * NormalDist().inv_cdf(1 - 1 / raw_attempts)) + (
        euler * NormalDist().inv_cdf(1 - 1 / (raw_attempts * 2.718281828459045))
    )
    expected_max = trial_mean + trial_std * max_z

    result = deflated_sharpe_ratio(
        observed_sharpe=0.60,
        trial_sharpes=trials,
        raw_attempted_trials=raw_attempts,
        n_observations=252,
        skewness=0.0,
        raw_kurtosis=3.0,
        return_sampling_frequency="daily",
    )

    assert result.status is StatisticalStatus.QUALIFIED
    assert result.raw_attempted_trials == raw_attempts
    assert result.valid_trial_sharpes == len(trials)
    assert result.expected_max_sharpe_per_period is not None
    assert isclose(result.expected_max_sharpe_per_period, expected_max, rel_tol=0, abs_tol=1e-15)


def test_dsr_never_maps_failed_trials_to_zero_and_requires_dispersion() -> None:
    result = deflated_sharpe_ratio(
        observed_sharpe=1.0,
        trial_sharpes=(0.5,),
        raw_attempted_trials=5,
        n_observations=100,
        skewness=0.0,
        raw_kurtosis=3.0,
        return_sampling_frequency="daily",
    )
    assert result.status is StatisticalStatus.NOT_QUALIFIABLE
    assert result.raw_attempted_trials == 5
    assert result.valid_trial_sharpes == 1

    no_dispersion = deflated_sharpe_ratio(
        observed_sharpe=1.0,
        trial_sharpes=(0.5, 0.5),
        raw_attempted_trials=5,
        n_observations=100,
        skewness=0.0,
        raw_kurtosis=3.0,
        return_sampling_frequency="daily",
    )
    assert no_dispersion.status is StatisticalStatus.NOT_QUALIFIABLE
