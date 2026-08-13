"""Primary-reference PSR/DSR primitives for governed research.

The implementation follows Bailey and López de Prado's definitions in:

* The Sharpe Ratio Efficient Frontier (PSR), Appendix 1;
* The Deflated Sharpe Ratio (DSR), Eq. 2 and Appendix 2.

The functions deliberately do not estimate serial-correlation corrections. A
caller must declare the return sampling frequency and the dependence
assumption; an explicitly detected serial-dependence violation fails closed.
The DSR trial count is a raw attempted count supplied by the durable
governance store in the governed path, never a value inferred from a caller's
displayed result.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from math import isfinite, sqrt
from collections.abc import Sequence
from statistics import NormalDist, mean, stdev
from typing import Literal

from .governance import sha256_canonical


PRIMARY_PSR_REFERENCE = "https://www.davidhbailey.com/dhbpapers/sharpe-frontier.pdf"
PRIMARY_DSR_REFERENCE = "https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf"
EULER_MASCHERONI = 0.5772156649015329
PSR_PASS_THRESHOLD = 0.95
DSR_PASS_THRESHOLD = 0.95
DSR_N_CONVENTION = "CONSERVATIVE_RAW_TRIAL_UPPER_BOUND"


class StatisticalStatus(StrEnum):
    QUALIFIED = "QUALIFIED"
    NOT_QUALIFIABLE = "NOT_QUALIFIABLE"
    ASSUMPTION_NOT_SATISFIED = "ASSUMPTION_NOT_SATISFIED"


class SharpeScale(StrEnum):
    PER_PERIOD = "PER_PERIOD"
    ANNUALIZED = "ANNUALIZED"


@dataclass(frozen=True)
class DSRStatisticalCohort:
    """Exact comparability contract for the Sharpe population used by DSR."""

    strategy_family: str
    experiment_id: str
    protocol_fingerprint: str
    dataset_identity: str
    dataset_role: str
    evaluation_split_specification: str
    evaluation_interval_policy: str
    return_sampling_frequency: str
    sharpe_convention: str
    sharpe_scale: SharpeScale
    cost_model: str
    objective_definition: str
    warmup_purge_embargo_semantics: str

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value.strip()
            for value in (
                self.strategy_family,
                self.experiment_id,
                self.protocol_fingerprint,
                self.dataset_identity,
                self.dataset_role,
                self.evaluation_split_specification,
                self.evaluation_interval_policy,
                self.return_sampling_frequency,
                self.sharpe_convention,
                self.cost_model,
                self.objective_definition,
                self.warmup_purge_embargo_semantics,
            )
        ):
            raise ValueError("DSR cohort fields must be non-empty")

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["sharpe_scale"] = self.sharpe_scale.value
        return payload

    @property
    def fingerprint(self) -> str:
        return sha256_canonical(self.to_dict())

    def comparable_to(self, other: DSRStatisticalCohort) -> bool:
        return self.to_dict() == other.to_dict()


def require_comparable_dsr_cohort(
    cohorts: Sequence[DSRStatisticalCohort],
) -> DSRStatisticalCohort:
    """Return the sole cohort or fail closed instead of mixing populations."""

    if len(cohorts) < 2 or any(not isinstance(item, DSRStatisticalCohort) for item in cohorts):
        raise ValueError("DSR requires at least two comparable statistical cohorts")
    first = cohorts[0]
    if any(not first.comparable_to(item) for item in cohorts[1:]):
        raise ValueError("DSR statistical cohort mismatch")
    return first


DependenceStatus = Literal[
    "ASSUMED_IID",
    "STATIONARY_ERGODIC_LIMITATION",
    "SERIAL_DEPENDENCE_DETECTED",
]


@dataclass(frozen=True)
class StatisticalQualification:
    """A PSR/DSR result with all scale and assumption metadata attached."""

    status: StatisticalStatus
    probability: float | None
    observed_sharpe_per_period: float | None
    benchmark_sharpe_per_period: float | None
    denominator: float | None
    n_observations: int
    return_sampling_frequency: str
    dependence_status: DependenceStatus
    reason: str | None = None


@dataclass(frozen=True)
class DeflatedSharpeQualification(StatisticalQualification):
    """DSR result including the durable-trial population used as reference."""

    raw_attempted_trials: int = 0
    valid_trial_sharpes: int = 0
    expected_max_sharpe_per_period: float | None = None
    trial_sharpe_mean_per_period: float | None = None
    trial_sharpe_std_per_period: float | None = None
    dsr_n: int = 0
    dsr_n_convention: str = DSR_N_CONVENTION

    @property
    def dsr_n_policy(self) -> str:
        return self.dsr_n_convention


def _not_qualifiable(
    *,
    n_observations: int,
    return_sampling_frequency: str,
    dependence_status: DependenceStatus,
    reason: str,
) -> StatisticalQualification:
    return StatisticalQualification(
        status=StatisticalStatus.NOT_QUALIFIABLE,
        probability=None,
        observed_sharpe_per_period=None,
        benchmark_sharpe_per_period=None,
        denominator=None,
        n_observations=n_observations,
        return_sampling_frequency=return_sampling_frequency,
        dependence_status=dependence_status,
        reason=reason,
    )


def _scale_sharpe(
    value: float,
    *,
    scale: SharpeScale,
    periods_per_year: float | None,
) -> float:
    if not isfinite(value):
        raise ValueError("Sharpe non fini")
    if scale is SharpeScale.PER_PERIOD:
        return value
    if periods_per_year is None or not isfinite(periods_per_year) or periods_per_year <= 0:
        raise ValueError("periods_per_year doit être strictement positif")
    return value / sqrt(periods_per_year)


def probabilistic_sharpe_ratio(
    *,
    observed_sharpe: float,
    benchmark_sharpe: float,
    n_observations: int,
    skewness: float,
    raw_kurtosis: float,
    return_sampling_frequency: str,
    sharpe_scale: SharpeScale = SharpeScale.PER_PERIOD,
    periods_per_year: float | None = None,
    dependence_status: DependenceStatus = "ASSUMED_IID",
) -> StatisticalQualification:
    """Return PSR using the paper's raw-kurtosis convention.

    The formula is:

    Phi((SR - SR_ref) * sqrt(n-1) /
    sqrt(1 - skew*SR + (kurtosis-1)*SR**2/4)).

    skewness is the standardized third central moment and raw_kurtosis is the
    standardized fourth central moment (normal == 3), matching the reference
    implementation. Sharpe values are converted to the declared sampling
    period before the formula is evaluated.
    """

    if not return_sampling_frequency.strip():
        return _not_qualifiable(
            n_observations=n_observations,
            return_sampling_frequency=return_sampling_frequency,
            dependence_status=dependence_status,
            reason="return_sampling_frequency manquante",
        )
    if dependence_status == "SERIAL_DEPENDENCE_DETECTED":
        return StatisticalQualification(
            status=StatisticalStatus.ASSUMPTION_NOT_SATISFIED,
            probability=None,
            observed_sharpe_per_period=None,
            benchmark_sharpe_per_period=None,
            denominator=None,
            n_observations=n_observations,
            return_sampling_frequency=return_sampling_frequency,
            dependence_status=dependence_status,
            reason="la formule primaire IID/non-normale n'est pas une correction de dépendance",
        )
    if not isinstance(n_observations, int) or n_observations < 4:
        return _not_qualifiable(
            n_observations=n_observations,
            return_sampling_frequency=return_sampling_frequency,
            dependence_status=dependence_status,
            reason="au moins quatre observations sont requises pour des moments exploitables",
        )
    if not isfinite(skewness) or not isfinite(raw_kurtosis) or raw_kurtosis < 0:
        return _not_qualifiable(
            n_observations=n_observations,
            return_sampling_frequency=return_sampling_frequency,
            dependence_status=dependence_status,
            reason="moments non finis ou kurtosis négative",
        )
    try:
        observed = _scale_sharpe(
            observed_sharpe, scale=sharpe_scale, periods_per_year=periods_per_year
        )
        benchmark = _scale_sharpe(
            benchmark_sharpe, scale=sharpe_scale, periods_per_year=periods_per_year
        )
    except ValueError as exc:
        return _not_qualifiable(
            n_observations=n_observations,
            return_sampling_frequency=return_sampling_frequency,
            dependence_status=dependence_status,
            reason=str(exc),
        )
    denominator_squared = 1.0 - skewness * observed + ((raw_kurtosis - 1.0) / 4.0) * observed**2
    if not isfinite(denominator_squared) or denominator_squared <= 0:
        return _not_qualifiable(
            n_observations=n_observations,
            return_sampling_frequency=return_sampling_frequency,
            dependence_status=dependence_status,
            reason="dénominateur PSR non strictement positif",
        )
    denominator = sqrt(denominator_squared)
    z = (observed - benchmark) * sqrt(n_observations - 1) / denominator
    probability = NormalDist().cdf(z)
    return StatisticalQualification(
        status=StatisticalStatus.QUALIFIED,
        probability=probability,
        observed_sharpe_per_period=observed,
        benchmark_sharpe_per_period=benchmark,
        denominator=denominator,
        n_observations=n_observations,
        return_sampling_frequency=return_sampling_frequency,
        dependence_status=dependence_status,
    )


def _failed_dsr(
    *,
    raw_attempted_trials: int,
    valid_trial_sharpes: int,
    n_observations: int,
    return_sampling_frequency: str,
    dependence_status: DependenceStatus,
    reason: str,
) -> DeflatedSharpeQualification:
    return DeflatedSharpeQualification(
        status=StatisticalStatus.NOT_QUALIFIABLE,
        probability=None,
        observed_sharpe_per_period=None,
        benchmark_sharpe_per_period=None,
        denominator=None,
        n_observations=n_observations,
        return_sampling_frequency=return_sampling_frequency,
        dependence_status=dependence_status,
        reason=reason,
        raw_attempted_trials=raw_attempted_trials,
        valid_trial_sharpes=valid_trial_sharpes,
        expected_max_sharpe_per_period=None,
        trial_sharpe_mean_per_period=None,
        trial_sharpe_std_per_period=None,
        dsr_n=raw_attempted_trials,
        dsr_n_convention=DSR_N_CONVENTION,
    )


def deflated_sharpe_ratio(
    *,
    observed_sharpe: float,
    trial_sharpes: Sequence[float],
    raw_attempted_trials: int,
    n_observations: int,
    skewness: float,
    raw_kurtosis: float,
    return_sampling_frequency: str,
    sharpe_scale: SharpeScale = SharpeScale.PER_PERIOD,
    periods_per_year: float | None = None,
    dependence_status: DependenceStatus = "ASSUMED_IID",
) -> DeflatedSharpeQualification:
    """Return DSR against the expected maximum of durable trial results.

    raw_attempted_trials must come from the durable registry. Failed or
    invalid trials count toward multiple testing but are never mapped to a
    zero Sharpe; trial_sharpes contains finite successful estimates only.
    """

    valid_count = len(trial_sharpes)
    if not isinstance(raw_attempted_trials, int) or raw_attempted_trials < 2:
        return _failed_dsr(
            raw_attempted_trials=raw_attempted_trials,
            valid_trial_sharpes=valid_count,
            n_observations=n_observations,
            return_sampling_frequency=return_sampling_frequency,
            dependence_status=dependence_status,
            reason="au moins deux trials tentés sont requis pour DSR",
        )
    if valid_count < 2:
        return _failed_dsr(
            raw_attempted_trials=raw_attempted_trials,
            valid_trial_sharpes=valid_count,
            n_observations=n_observations,
            return_sampling_frequency=return_sampling_frequency,
            dependence_status=dependence_status,
            reason="au moins deux Sharpe de trials valides sont requis pour estimer la dispersion",
        )
    if raw_attempted_trials < valid_count or any(
        not isinstance(value, (int, float)) or not isfinite(float(value)) for value in trial_sharpes
    ):
        return _failed_dsr(
            raw_attempted_trials=raw_attempted_trials,
            valid_trial_sharpes=valid_count,
            n_observations=n_observations,
            return_sampling_frequency=return_sampling_frequency,
            dependence_status=dependence_status,
            reason="population de trials incohérente ou Sharpe non fini",
        )
    try:
        scaled_trials = [
            _scale_sharpe(float(value), scale=sharpe_scale, periods_per_year=periods_per_year)
            for value in trial_sharpes
        ]
    except ValueError as exc:
        return _failed_dsr(
            raw_attempted_trials=raw_attempted_trials,
            valid_trial_sharpes=valid_count,
            n_observations=n_observations,
            return_sampling_frequency=return_sampling_frequency,
            dependence_status=dependence_status,
            reason=str(exc),
        )
    trial_mean = mean(scaled_trials)
    trial_std = stdev(scaled_trials)
    if not isfinite(trial_std) or trial_std <= 0:
        return _failed_dsr(
            raw_attempted_trials=raw_attempted_trials,
            valid_trial_sharpes=valid_count,
            n_observations=n_observations,
            return_sampling_frequency=return_sampling_frequency,
            dependence_status=dependence_status,
            reason="dispersion des Sharpe de trials nulle ou non finie",
        )
    quantile_a = NormalDist().inv_cdf(1.0 - 1.0 / raw_attempted_trials)
    quantile_b = NormalDist().inv_cdf(1.0 - 1.0 / (raw_attempted_trials * 2.718281828459045))
    expected_max = trial_mean + trial_std * (
        (1.0 - EULER_MASCHERONI) * quantile_a + EULER_MASCHERONI * quantile_b
    )
    psr = probabilistic_sharpe_ratio(
        observed_sharpe=observed_sharpe,
        benchmark_sharpe=expected_max,
        n_observations=n_observations,
        skewness=skewness,
        raw_kurtosis=raw_kurtosis,
        return_sampling_frequency=return_sampling_frequency,
        sharpe_scale=sharpe_scale,
        periods_per_year=periods_per_year,
        dependence_status=dependence_status,
    )
    return DeflatedSharpeQualification(
        status=psr.status,
        probability=psr.probability,
        observed_sharpe_per_period=psr.observed_sharpe_per_period,
        benchmark_sharpe_per_period=psr.benchmark_sharpe_per_period,
        denominator=psr.denominator,
        n_observations=psr.n_observations,
        return_sampling_frequency=psr.return_sampling_frequency,
        dependence_status=psr.dependence_status,
        reason=psr.reason,
        raw_attempted_trials=raw_attempted_trials,
        valid_trial_sharpes=valid_count,
        expected_max_sharpe_per_period=expected_max,
        trial_sharpe_mean_per_period=trial_mean,
        trial_sharpe_std_per_period=trial_std,
        dsr_n=raw_attempted_trials,
        dsr_n_convention=DSR_N_CONVENTION,
    )
