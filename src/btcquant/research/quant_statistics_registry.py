"""Durable-registry adapter for PSR/DSR.

The pure statistical functions accept explicit inputs for independent testing.
This adapter is the governed production-facing path: it derives the raw
attempt count and finite successful Sharpe estimates from the durable
GovernanceStore, so a caller cannot replace the multiple-testing population
with a convenient displayed count.
"""

from __future__ import annotations

import math
from typing import Any

import json

from .governance_store import GovernanceStore
from .quant_statistics import (
    DependenceStatus,
    DeflatedSharpeQualification,
    DSRStatisticalCohort,
    SharpeScale,
    deflated_sharpe_ratio,
)


def _trial_cohort(
    store: GovernanceStore, experiment_id: str, row: dict[str, Any]
) -> DSRStatisticalCohort:
    spec = store.get_experiment_spec(experiment_id)
    policy = spec.multiple_testing_policy or {}
    roles = "|".join(f"{key}:{spec.dataset_roles[key].value}" for key in sorted(spec.dataset_roles))
    warmup = json.dumps(
        {
            "warmup": spec.warmup_policy,
            "purge": spec.purge_policy,
            "embargo": spec.embargo_policy,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return DSRStatisticalCohort(
        strategy_family=spec.strategy_family,
        experiment_id=experiment_id,
        protocol_fingerprint=str(row["experiment_fingerprint"]),
        dataset_identity=str(row["dataset_fingerprint"]),
        dataset_role=roles,
        evaluation_split_specification=str(row["split_fingerprint"]),
        evaluation_interval_policy=json.dumps(
            spec.data_cutoffs, sort_keys=True, separators=(",", ":")
        ),
        return_sampling_frequency=str(policy.get("return_sampling_frequency", "1D_UTC")),
        sharpe_convention=str(policy.get("sharpe_convention", "EXCESS_RETURN_OVER_EQUITY")),
        sharpe_scale=SharpeScale(str(policy.get("sharpe_scale", SharpeScale.PER_PERIOD.value))),
        cost_model=str(row["cost_model_fingerprint"]),
        objective_definition=spec.selection_metric,
        warmup_purge_embargo_semantics=warmup,
    )


def durable_dsr_inputs(
    store: GovernanceStore,
    experiment_id: str,
    *,
    cohort: DSRStatisticalCohort | None = None,
) -> dict[str, Any]:
    """Return only finite Sharpe values from one exact durable cohort."""

    raw_count = store.trial_count(experiment_id)
    valid_sharpes: list[float] = []
    cohort_rows: list[DSRStatisticalCohort] = []
    for sequence in range(1, raw_count + 1):
        row = store.get_trial(f"{experiment_id}:trial:{sequence:06d}")
        if row is None:
            raise RuntimeError("trial sequence gap in durable governance store")
        row_cohort = _trial_cohort(store, experiment_id, row)
        cohort_rows.append(row_cohort)
        result = row.get("metrics_json")
        if not result:
            continue
        metrics = json.loads(str(result))
        value = metrics.get("sharpe") if isinstance(metrics, dict) else None
        if isinstance(value, (int, float)) and math.isfinite(float(value)):
            valid_sharpes.append(float(value))
    cohort_fingerprint: str | None
    if cohort_rows:
        target_cohort = cohort_rows[0] if cohort is None else cohort
        if any(not target_cohort.comparable_to(item) for item in cohort_rows):
            raise ValueError("DSR cohort mismatch: trials cannot be mixed")
        cohort_fingerprint = target_cohort.fingerprint
    else:
        cohort_fingerprint = cohort.fingerprint if cohort is not None else None
    return {
        "raw_attempted_trials": raw_count,
        "valid_trial_sharpes": tuple(valid_sharpes),
        "valid_sr_trials": len(valid_sharpes),
        "invalid_or_missing_sharpe": raw_count - len(valid_sharpes),
        "dsr_n": raw_count,
        "dsr_n_convention": "CONSERVATIVE_RAW_TRIAL_UPPER_BOUND",
        "dsr_n_policy": "CONSERVATIVE_RAW_TRIAL_UPPER_BOUND",
        "statistical_cohort_fingerprint": cohort_fingerprint,
    }


def qualify_dsr_from_registry(
    store: GovernanceStore,
    *,
    experiment_id: str,
    observed_sharpe: float,
    n_observations: int,
    skewness: float,
    raw_kurtosis: float,
    return_sampling_frequency: str,
    sharpe_scale: SharpeScale = SharpeScale.PER_PERIOD,
    periods_per_year: float | None = None,
    dependence_status: DependenceStatus = "ASSUMED_IID",
    cohort: DSRStatisticalCohort | None = None,
) -> DeflatedSharpeQualification:
    """Evaluate DSR using only the durable trial population."""

    inputs = durable_dsr_inputs(store, experiment_id, cohort=cohort)
    return deflated_sharpe_ratio(
        observed_sharpe=observed_sharpe,
        trial_sharpes=inputs["valid_trial_sharpes"],
        raw_attempted_trials=inputs["raw_attempted_trials"],
        n_observations=n_observations,
        skewness=skewness,
        raw_kurtosis=raw_kurtosis,
        return_sampling_frequency=return_sampling_frequency,
        sharpe_scale=sharpe_scale,
        periods_per_year=periods_per_year,
        dependence_status=dependence_status,
    )
