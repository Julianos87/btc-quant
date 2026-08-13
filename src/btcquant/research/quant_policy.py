"""Canonical quantitative-governance policy proposal.

This module contains governance thresholds, not strategy parameters. The
values are deliberately PROPOSED in Lot 5.3. They can be reviewed and
fingerprinted, but the real-search gate must reject them until an independent
decision changes the status to APPROVED or FROZEN.

The PSR/DSR formulas and their statistical limitations come from Bailey and
López de Prado's primary papers. The numeric business/risk thresholds are
BTCQuant structural governance proposals and are not universal academic
results.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from math import isclose, isfinite
from pathlib import Path
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .governance import GovernanceError, GovernanceIncomplete, sha256_canonical
from .quant_statistics import (
    DSR_PASS_THRESHOLD,
    PSR_PASS_THRESHOLD,
    StatisticalQualification,
    StatisticalStatus,
)


QUANT_POLICY_VERSION = "QUANT_POLICY_V1"
PSR_BENCHMARK_SHARPE = 0.0
RETURN_SAMPLING_FREQUENCY = "1D_UTC"
DSR_N_CONVENTION = "CONSERVATIVE_RAW_TRIAL_UPPER_BOUND"
FREEZE_ARTIFACT_SCHEMA_VERSION = 1
FREEZE_LIFECYCLE_VERSION = "PROPOSED_APPROVED_FROZEN_V1"
SEMANTIC_POLICY_HASH_ALGORITHM = "SHA256_CANONICAL_JSON_V1"
FROZEN_POLICY_BASE_GIT_SHA = "9924bd9182375f9d247dfcd6cb7931b5adc10e17"
DEFAULT_FREEZE_ARTIFACT = (
    Path(__file__).resolve().parents[3] / "audit" / "governance" / "quant_policy_v1.freeze.json"
)


class PolicyStatus(StrEnum):
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    FROZEN = "FROZEN"


class PolicySource(StrEnum):
    PRIMARY_REFERENCE = "PRIMARY_REFERENCE"
    STRUCTURAL_RISK_POLICY = "STRUCTURAL_RISK_POLICY"
    OPERATIONAL_DECISION = "OPERATIONAL_DECISION"
    EMPIRICAL_SEEN_DATA_CONTEXT = "EMPIRICAL_SEEN_DATA_CONTEXT"


_PLACEHOLDERS = {
    "",
    "NONE",
    "TODO",
    "UNSET",
    "UNKNOWN",
    "DECISION_REQUIRED",
    "GOVERNANCE_INCOMPLETE",
    "NOT_IMPLEMENTED",
    "INCOMPLETE",
}


@dataclass(frozen=True)
class DSRTrialCounterPolicy:
    raw_attempted_trials_label: str
    valid_sr_trials_label: str
    dsr_n_label: str
    dsr_n_convention: str
    raw_attempted_definition: str
    valid_sr_definition: str
    independent_trial_limitation: str

    def counters(self, raw_attempted_trials: int, valid_sr_trials: int) -> DSRTrialCounters:
        if raw_attempted_trials < 0 or valid_sr_trials < 0:
            raise ValueError("DSR counters must be non-negative")
        if valid_sr_trials > raw_attempted_trials:
            raise ValueError("VALID_SR_TRIALS cannot exceed RAW_ATTEMPTED_TRIALS")
        return DSRTrialCounters(
            raw_attempted_trials=raw_attempted_trials,
            valid_sr_trials=valid_sr_trials,
            dsr_n=raw_attempted_trials,
            dsr_n_convention=self.dsr_n_convention,
        )


@dataclass(frozen=True)
class DSRTrialCounters:
    raw_attempted_trials: int
    valid_sr_trials: int
    dsr_n: int
    dsr_n_convention: str


@dataclass(frozen=True)
class ReturnCoveragePolicy:
    return_sampling_frequency: str
    minimum_valid_return_coverage: float
    valid_return_definition: str


@dataclass(frozen=True)
class ParameterNeighborhoodSpec:
    """Pre-registered neighborhood for one optimizable parameter."""

    name: str
    parameter_type: str
    declared_domain: tuple[int | float | str, ...]
    neighbor_method: str
    relative_delta: float | None = None
    absolute_delta: float | None = None
    categories: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        allowed = {"DISCRETE_ORDERED", "CONTINUOUS", "CATEGORICAL"}
        if self.parameter_type not in allowed:
            raise ValueError("unknown parameter neighborhood type")
        if not self.name or not self.declared_domain:
            raise ValueError("parameter name and declared domain are required")
        if len(set(self.declared_domain)) != len(self.declared_domain):
            raise ValueError("declared domain must be unique")
        if (
            self.parameter_type == "DISCRETE_ORDERED"
            and self.neighbor_method != "ADJACENT_DOMAIN_VALUES"
        ):
            raise ValueError("discrete neighborhoods must use domain adjacency")
        if self.parameter_type == "CONTINUOUS":
            if self.neighbor_method == "RELATIVE_DELTA":
                if self.relative_delta is None or not 0 < self.relative_delta < 1:
                    raise ValueError("relative delta must be in (0, 1)")
            elif self.neighbor_method == "ABSOLUTE_DELTA":
                if self.absolute_delta is None or not self.absolute_delta > 0:
                    raise ValueError("absolute delta must be positive")
            else:
                raise ValueError("continuous neighborhood method must be explicit")
        if self.parameter_type == "CATEGORICAL":
            if self.neighbor_method != "REGISTERED_ALTERNATIVES" or not self.categories:
                raise ValueError("categorical alternatives must be registered")

    def neighbors(self, value: int | float | str) -> tuple[int | float | str, ...]:
        if value not in self.declared_domain:
            raise ValueError("candidate is outside the declared domain")
        if self.parameter_type == "DISCRETE_ORDERED":
            index = self.declared_domain.index(value)
            indices = (index - 1, index + 1)
            return tuple(
                self.declared_domain[item]
                for item in indices
                if 0 <= item < len(self.declared_domain)
            )
        if self.parameter_type == "CATEGORICAL":
            return tuple(sorted(item for item in self.categories if item != value))
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError("continuous neighborhood requires numeric value")
        delta = (
            self.relative_delta if self.neighbor_method == "RELATIVE_DELTA" else self.absolute_delta
        )
        assert delta is not None
        candidates = (
            (value * (1.0 - delta), value * (1.0 + delta))
            if self.neighbor_method == "RELATIVE_DELTA"
            else (value - delta, value + delta)
        )
        return tuple(
            next(
                (
                    declared
                    for declared in self.declared_domain
                    if isinstance(declared, (int, float))
                    and isclose(float(declared), float(item), rel_tol=1e-12, abs_tol=1e-12)
                ),
                item,
            )
            for item in candidates
            if any(
                isinstance(declared, (int, float))
                and isclose(float(declared), float(item), rel_tol=1e-12, abs_tol=1e-12)
                for declared in self.declared_domain
            )
        )

    @property
    def fingerprint(self) -> str:
        return sha256_canonical(asdict(self))


@dataclass(frozen=True)
class FoldConsistencyPolicy:
    positive_fold_definition: str
    minimum_positive_fold_fraction: float
    worst_fold_metric: str
    worst_fold_floor: float
    maximum_pnl_concentration: float
    pnl_concentration_formula: str
    nonpositive_denominator_result: str

    @property
    def worst_fold_return_floor(self) -> float:
        """Backward-compatible alias; the metric is explicitly NET_RETURN."""

        return self.worst_fold_floor


@dataclass(frozen=True)
class CostStressScenario:
    name: str
    scope: str
    required: bool
    fee_assumption: str
    fee_multiplier: float
    slippage_multiplier: float
    impact_multiplier: float
    borrow_multiplier: float
    favorable_funding_haircut: float
    basis_assumption: str
    basis_multiplier: float
    funding_stress_rule: str


@dataclass(frozen=True)
class CostStressPolicy:
    scenarios: tuple[CostStressScenario, ...]
    required_scenario: str

    @property
    def required_pass_multiplier(self) -> float:
        """Compatibility view for older diagnostics; use component vectors."""

        required = next(item for item in self.scenarios if item.name == self.required_scenario)
        return required.slippage_multiplier

    @property
    def multipliers(self) -> tuple[float, ...]:
        return tuple(sorted({item.slippage_multiplier for item in self.scenarios}))


@dataclass(frozen=True)
class ParameterStabilityPolicy:
    neighborhood_rule: str
    neighborhood_specs_required_before_search: bool
    neighborhood_specs: tuple[ParameterNeighborhoodSpec, ...]
    core_gates: tuple[str, ...]
    statistical_gates_included: bool
    numeric_discrete_rule: str
    numeric_continuous_rule: str
    categorical_rule: str
    boundary_rule: str
    minimum_neighbor_pass_fraction: float
    relative_metric_tolerance: float
    nonpositive_candidate_rule: str


@dataclass(frozen=True)
class HoldoutClosurePolicy:
    minimum_duration_months: int
    minimum_completed_trades: int
    hard_cap_months: int
    close_rule: str
    hard_cap_result: str

    @property
    def holdout_months(self) -> int:
        """Backward-compatible alias for the minimum duration."""

        return self.minimum_duration_months

    def status(self, *, elapsed_months: int, completed_trades: int) -> str:
        """Evaluate time/count rules, never performance."""

        if elapsed_months < 0 or completed_trades < 0:
            raise ValueError("holdout counters must be non-negative")
        if (
            elapsed_months >= self.minimum_duration_months
            and completed_trades >= self.minimum_completed_trades
        ):
            return "CLOSE_ELIGIBLE"
        if elapsed_months >= self.hard_cap_months:
            return self.hard_cap_result
        return "OPEN"


@dataclass(frozen=True)
class FamilyPolicy:
    strategy_family: str
    return_coverage: ReturnCoveragePolicy
    minimum_elapsed_days: int
    minimum_trades: int
    minimum_economic_events: int
    economic_event_role: str
    holdout_closure: HoldoutClosurePolicy
    maximum_drawdown: float
    fold_consistency: FoldConsistencyPolicy
    cost_stress: CostStressPolicy
    parameter_stability: ParameterStabilityPolicy
    sample_rule_note: str

    @property
    def holdout_months(self) -> int:
        return self.holdout_closure.minimum_duration_months


@dataclass(frozen=True)
class NeighborOutcome:
    economically_valid: bool
    core_gates_pass: bool
    primary_metric: float


@dataclass(frozen=True)
class QuantGovernancePolicy:
    """Immutable, fully fingerprinted policy contract."""

    policy_version: str
    status: PolicyStatus
    confidence_level: float
    dsr_required: bool
    psr_pass_threshold: float
    dsr_pass_threshold: float
    psr_benchmark_sharpe: float
    return_sampling_frequency: str
    dsr_counters: DSRTrialCounterPolicy
    dsr_cohort_fields: tuple[str, ...]
    dsr_minimum_valid_comparable_trials: int
    serial_dependence_policy: str
    trial_budget_rule: str
    selection_metric: str
    development_gates: tuple[str, ...]
    blind_promotion_gates: tuple[str, ...]
    trend: FamilyPolicy
    carry: FamilyPolicy
    pbo_status: str
    rationale_sources: tuple[str, ...]

    def _raw_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        return payload

    def _semantic_dict(self) -> dict[str, Any]:
        """Return lifecycle-independent quantitative policy content."""

        payload = self.to_dict()
        # Status is governance metadata. Keep the approved V1 identity stable
        # while PROPOSED -> APPROVED -> FROZEN transitions occur.
        payload["status"] = PolicyStatus.PROPOSED.value
        return payload

    def provenance_map(self) -> dict[str, str]:
        """Attach a source classification to every leaf in the policy dump."""

        output: dict[str, str] = {}

        def source_for(path: str) -> PolicySource:
            if "dsr_counters" in path or "serial_dependence_policy" in path:
                return PolicySource.PRIMARY_REFERENCE
            if "return_sampling_frequency" in path:
                return PolicySource.OPERATIONAL_DECISION
            if path.endswith("pbo_status") or path.endswith("rationale_sources"):
                return PolicySource.OPERATIONAL_DECISION
            if ".trend." in path or ".carry." in path:
                return PolicySource.STRUCTURAL_RISK_POLICY
            if path.endswith("psr_pass_threshold") or path.endswith("dsr_pass_threshold"):
                return PolicySource.OPERATIONAL_DECISION
            if path.endswith("psr_benchmark_sharpe"):
                return PolicySource.OPERATIONAL_DECISION
            if path.endswith("policy_version") or path.endswith("status"):
                return PolicySource.OPERATIONAL_DECISION
            return PolicySource.OPERATIONAL_DECISION

        def visit(value: Any, path: str) -> None:
            if isinstance(value, Mapping):
                for key, item in value.items():
                    visit(item, f"{path}.{key}")
            elif isinstance(value, (tuple, list)):
                for index, item in enumerate(value):
                    visit(item, f"{path}[{index}]")
            else:
                output[path.removeprefix("policy.")] = source_for(path).value

        visit(self._raw_dict(), "policy")
        return {key: value for key, value in sorted(output.items())}

    def to_dict(self) -> dict[str, Any]:
        payload = self._raw_dict()
        payload["provenance"] = self.provenance_map()
        return payload

    @property
    def fingerprint(self) -> str:
        return sha256_canonical(self._semantic_dict())

    def family_payload(self, family: str) -> dict[str, Any]:
        normalized = family.lower()
        if normalized not in {"trend", "carry"}:
            raise ValueError("family must be Trend or Carry")
        payload = {
            "policy_id": f"{normalized.upper()}_GOVERNANCE_POLICY_V1",
            "policy_version": self.policy_version,
            "status": self.status.value,
            "statistical": {
                "psr_pass_threshold": self.psr_pass_threshold,
                "dsr_pass_threshold": self.dsr_pass_threshold,
                "confidence_level": self.confidence_level,
                "dsr_required": self.dsr_required,
                "psr_benchmark_sharpe": self.psr_benchmark_sharpe,
                "return_sampling_frequency": self.return_sampling_frequency,
                "dsr_counters": asdict(self.dsr_counters),
                "dsr_cohort_fields": self.dsr_cohort_fields,
                "dsr_minimum_valid_comparable_trials": self.dsr_minimum_valid_comparable_trials,
                "serial_dependence_policy": self.serial_dependence_policy,
            },
            "family": asdict(getattr(self, normalized)),
            "provenance": self.provenance_map(),
        }
        return payload

    def family_fingerprint(self, family: str) -> str:
        payload = self.family_payload(family)
        payload["status"] = PolicyStatus.PROPOSED.value
        return sha256_canonical(payload)

    def canonical_family_json(self, family: str) -> str:
        payload = self.family_payload(family)
        payload["fingerprint"] = self.family_fingerprint(family)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def completeness_issues(self) -> tuple[str, ...]:
        """Return executable policy fields that are empty or semantically vague."""

        forbidden_phrases = (
            "explicit adverse",
            "appropriate",
            "as needed",
            "reasonable",
            "core gates",
        )
        found: list[str] = []

        def visit(value: Any, path: str) -> None:
            if isinstance(value, str):
                normalized = value.strip().upper()
                lowered = value.strip().lower()
                if normalized in _PLACEHOLDERS or any(
                    phrase in lowered for phrase in forbidden_phrases
                ):
                    found.append(path)
            elif isinstance(value, Mapping):
                for key, item in value.items():
                    visit(item, f"{path}.{key}")
            elif isinstance(value, (tuple, list)):
                for index, item in enumerate(value):
                    visit(item, f"{path}[{index}]")

        visit(self.to_dict(), "policy")
        return tuple(found)

    def placeholder_paths(self) -> tuple[str, ...]:
        return self.completeness_issues()

    def approve(
        self, governance_store: Any | None = None, *, base_git_sha: str | None = None
    ) -> QuantGovernancePolicy:
        """Return APPROVED only after complete deterministic validation."""

        if self.status is not PolicyStatus.PROPOSED:
            raise GovernanceError("only PROPOSED policy can transition to APPROVED")
        validate_policy_shape(self)
        issues = self.completeness_issues()
        if issues:
            raise GovernanceIncomplete("policy completeness failure: " + ", ".join(issues))
        approved = replace(self, status=PolicyStatus.APPROVED)
        if governance_store is not None:
            if base_git_sha is None:
                raise GovernanceError("base_git_sha is required for durable approval")
            governance_store.record_policy_transition(
                policy_version=approved.policy_version,
                trend_fingerprint=approved.family_fingerprint("Trend"),
                carry_fingerprint=approved.family_fingerprint("Carry"),
                combined_fingerprint=approved.fingerprint,
                previous_state=PolicyStatus.PROPOSED.value,
                new_state=PolicyStatus.APPROVED.value,
                base_git_sha=base_git_sha,
            )
        return approved

    def freeze(
        self, governance_store: Any | None = None, *, base_git_sha: str | None = None
    ) -> QuantGovernancePolicy:
        """Return FROZEN only from APPROVED and optionally seal its fingerprint."""

        if self.status is not PolicyStatus.APPROVED:
            raise GovernanceError("only APPROVED policy can transition to FROZEN")
        validate_policy_shape(self)
        issues = self.completeness_issues()
        if issues:
            raise GovernanceIncomplete("policy completeness failure: " + ", ".join(issues))
        frozen = replace(self, status=PolicyStatus.FROZEN)
        if governance_store is not None:
            if base_git_sha is None:
                raise GovernanceError("base_git_sha is required for durable freeze")
            governance_store.record_policy_transition(
                policy_version=frozen.policy_version,
                trend_fingerprint=frozen.family_fingerprint("Trend"),
                carry_fingerprint=frozen.family_fingerprint("Carry"),
                combined_fingerprint=frozen.fingerprint,
                previous_state=PolicyStatus.APPROVED.value,
                new_state=PolicyStatus.FROZEN.value,
                base_git_sha=base_git_sha,
            )
            governance_store.record_policy_fingerprint(
                policy_version=frozen.policy_version,
                fingerprint=frozen.fingerprint,
                status=PolicyStatus.FROZEN.value,
            )
        return frozen

    def derive_new_version(self, policy_version: str, **changes: Any) -> QuantGovernancePolicy:
        """A changed frozen policy must use a new version and new fingerprint."""

        if self.status is PolicyStatus.FROZEN and policy_version == self.policy_version:
            raise GovernanceError("FROZEN policy is immutable; use a new policy version")
        return replace(self, policy_version=policy_version, status=PolicyStatus.PROPOSED, **changes)

    def validate_for_real_search(
        self,
        governance_store: Any | None = None,
        *,
        expected_base_git_sha: str | None = None,
        freeze_artifact_path: str | Path | None = None,
        recovery_root: str | Path | None = None,
    ) -> None:
        """Authorize real search only with a portable and durable freeze proof."""

        placeholders = self.placeholder_paths()
        if placeholders:
            raise GovernanceIncomplete(
                "GOVERNANCE_INCOMPLETE: policy placeholders at " + ", ".join(placeholders)
            )
        if self.status is not PolicyStatus.FROZEN:
            raise GovernanceIncomplete(
                "GOVERNANCE_INCOMPLETE: policy not approved/frozen; REAL_SEARCH requires FROZEN policy"
            )
        if governance_store is None:
            raise GovernanceIncomplete("GOVERNANCE_INCOMPLETE: durable governance store required")
        if expected_base_git_sha is None:
            raise GovernanceIncomplete("GOVERNANCE_INCOMPLETE: expected base Git SHA required")
        if not getattr(governance_store, "path_is_explicit", False):
            raise GovernanceIncomplete(
                "GOVERNANCE_INCOMPLETE: REAL_SEARCH requires an explicitly configured governance DB"
            )
        try:
            artifact_policy = frozen_policy_v1(
                freeze_artifact_path,
                expected_base_git_sha=expected_base_git_sha,
            )
            if (
                self.policy_version != artifact_policy.policy_version
                or self.fingerprint != artifact_policy.fingerprint
                or self.family_fingerprint("Trend") != artifact_policy.family_fingerprint("Trend")
                or self.family_fingerprint("Carry") != artifact_policy.family_fingerprint("Carry")
            ):
                raise GovernanceError("policy object does not match freeze artifact")
            governance_store.verify_policy_freeze(
                policy_version=self.policy_version,
                trend_fingerprint=self.family_fingerprint("Trend"),
                carry_fingerprint=self.family_fingerprint("Carry"),
                combined_fingerprint=self.fingerprint,
                base_git_sha=expected_base_git_sha,
            )
            configured_db = os.environ.get("BTCQUANT_GOVERNANCE_DB")
            if not configured_db:
                raise GovernanceIncomplete(
                    "GOVERNANCE_INCOMPLETE: BTCQUANT_GOVERNANCE_DB is required for REAL_SEARCH"
                )
            if governance_store.resolved_path() != Path(configured_db).expanduser().resolve():
                raise GovernanceIncomplete(
                    "GOVERNANCE_INCOMPLETE: governance DB is not the configured canonical store"
                )
            configured_recovery_root = os.environ.get("BTCQUANT_RECOVERY_ROOT")
            if not configured_recovery_root:
                raise GovernanceIncomplete(
                    "GOVERNANCE_INCOMPLETE: BTCQUANT_RECOVERY_ROOT is required for REAL_SEARCH"
                )
            if recovery_root is not None and (
                Path(recovery_root).expanduser().resolve()
                != Path(configured_recovery_root).expanduser().resolve()
            ):
                raise GovernanceIncomplete(
                    "GOVERNANCE_INCOMPLETE: recovery root is not the configured canonical root"
                )
            from btcquant.backup import ResearchRecoveryRequired, assert_research_recovery_clear

            try:
                assert_research_recovery_clear(configured_recovery_root)
            except ResearchRecoveryRequired as exc:
                raise GovernanceIncomplete(
                    "GOVERNANCE_INCOMPLETE: research recovery reconciliation is required"
                ) from exc
        except GovernanceIncomplete:
            raise
        except GovernanceError as exc:
            raise GovernanceIncomplete(
                "GOVERNANCE_INCOMPLETE: durable freeze verification failed"
            ) from exc
        if not self.dsr_required:
            raise GovernanceIncomplete("GOVERNANCE_INCOMPLETE: DSR required policy missing")
        if self.pbo_status_is_placeholder:
            raise GovernanceIncomplete("GOVERNANCE_INCOMPLETE: multiple-testing policy incomplete")

    @property
    def pbo_status_is_placeholder(self) -> bool:
        return self.pbo_status.strip().upper() in {"", "DECISION_REQUIRED", "INCOMPLETE"}


def passes_probability_gate(result: StatisticalQualification, threshold: float) -> bool:
    """Return true only for a finite qualified probability at/above threshold."""

    return (
        result.status is StatisticalStatus.QUALIFIED
        and result.probability is not None
        and isfinite(result.probability)
        and 0.0 <= result.probability <= 1.0
        and result.probability >= threshold
    )


def positive_fold(net_return: float) -> bool:
    return isfinite(net_return) and net_return > 0.0


def positive_fold_fraction(net_returns: Sequence[float]) -> float | None:
    if not net_returns or any(not isfinite(value) for value in net_returns):
        return None
    return sum(positive_fold(value) for value in net_returns) / len(net_returns)


def worst_fold_passes(net_returns: Sequence[float], floor: float) -> bool:
    return (
        bool(net_returns)
        and all(isfinite(value) for value in net_returns)
        and min(net_returns) >= floor
    )


def positive_fold_pnl_concentration(positive_fold_pnls: Sequence[float]) -> float | None:
    """Best positive-fold PnL divided by the sum of positive-fold PnL only."""

    if not positive_fold_pnls or any(
        not isfinite(value) or value <= 0 for value in positive_fold_pnls
    ):
        return None
    denominator = sum(positive_fold_pnls)
    if denominator <= 0:
        return None
    return max(positive_fold_pnls) / denominator


def stressed_funding_pnl(native_funding_pnl: float, favorable_income_haircut: float) -> float:
    """Apply funding stress without improving a negative funding outcome."""

    if not isfinite(native_funding_pnl) or not 0 < favorable_income_haircut <= 1:
        raise ValueError("funding stress inputs must be finite and bounded")
    return (
        native_funding_pnl * favorable_income_haircut
        if native_funding_pnl > 0
        else native_funding_pnl
    )


def generate_neighbors(
    value: int | float | str,
    *,
    spec: ParameterNeighborhoodSpec | None = None,
    kind: str | None = None,
    lower: float | None = None,
    upper: float | None = None,
    categories: Iterable[str] = (),
) -> tuple[int | float | str, ...]:
    """Generate neighbors; real SEARCH must pass an explicit specification."""

    if spec is not None:
        return spec.neighbors(value)
    if kind == "numeric_discrete":
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError("numeric_discrete requires an integer")
        candidates: Iterable[int | float | str] = (value - 1, value + 1)
    elif kind == "numeric_continuous":
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value == 0:
            raise ValueError("continuous zero requires an explicit scale")
        candidates = (value * 0.90, value * 1.10)
    elif kind == "categorical":
        if not isinstance(value, str):
            raise ValueError("categorical requires a string")
        candidates = tuple(sorted({item for item in categories if item != value}))
    else:
        raise ValueError("real SEARCH requires an explicit ParameterNeighborhoodSpec")
    result: list[int | float | str] = []
    for candidate in candidates:
        if lower is not None and candidate < lower:  # type: ignore[operator]
            continue
        if upper is not None and candidate > upper:  # type: ignore[operator]
            continue
        if candidate not in result:
            result.append(candidate)
    return tuple(result)


def neighbor_stability_passes(
    candidate_score: float,
    neighbors: Sequence[NeighborOutcome],
    *,
    minimum_fraction: float = 0.80,
    metric_ratio: float = 0.80,
) -> bool:
    """Apply the pre-registered 80%-of-neighbors gate, fail closed otherwise."""

    if not isfinite(candidate_score) or candidate_score <= 0:
        return False
    if not neighbors or not 0 < minimum_fraction <= 1 or not 0 < metric_ratio <= 1:
        return False
    passed = sum(
        outcome.economically_valid
        and outcome.core_gates_pass
        and isfinite(outcome.primary_metric)
        and outcome.primary_metric >= candidate_score * metric_ratio
        for outcome in neighbors
    )
    return passed / len(neighbors) >= minimum_fraction


def _scenario(
    name: str,
    scope: str,
    *,
    required: bool,
    fee_multiplier: float,
    fee_assumption: str,
    slippage: float,
    impact: float,
    borrow: float,
    funding_haircut: float,
    basis: str,
    basis_multiplier: float = 1.0,
    funding_rule: str = "POSITIVE_INCOME_HAIRCUT_ONLY; NEGATIVE_COST_UNCHANGED",
) -> CostStressScenario:
    return CostStressScenario(
        name=name,
        scope=scope,
        required=required,
        fee_assumption=fee_assumption,
        fee_multiplier=fee_multiplier,
        slippage_multiplier=slippage,
        impact_multiplier=impact,
        borrow_multiplier=borrow,
        favorable_funding_haircut=funding_haircut,
        basis_assumption=basis,
        basis_multiplier=basis_multiplier,
        funding_stress_rule=funding_rule,
    )


def _trend_cost_stress() -> CostStressPolicy:
    return CostStressPolicy(
        scenarios=(
            _scenario(
                "BASELINE",
                "qualified_v1_costs",
                required=False,
                fee_multiplier=1.0,
                fee_assumption="BASELINE_FEE_RATE",
                slippage=1.0,
                impact=1.0,
                borrow=1.0,
                funding_haircut=1.0,
                basis="NOT_APPLICABLE",
            ),
            _scenario(
                "MODERATE_REQUIRED",
                "execution",
                required=True,
                fee_multiplier=1.25,
                fee_assumption="STRUCTURAL_ADVERSE_FEE_RATE",
                slippage=1.5,
                impact=1.5,
                borrow=1.0,
                funding_haircut=1.0,
                basis="NOT_APPLICABLE",
            ),
            _scenario(
                "SEVERE_DIAGNOSTIC",
                "execution",
                required=False,
                fee_multiplier=1.5,
                fee_assumption="STRUCTURAL_ADVERSE_FEE_RATE",
                slippage=2.0,
                impact=2.0,
                borrow=1.0,
                funding_haircut=1.0,
                basis="NOT_APPLICABLE",
            ),
        ),
        required_scenario="MODERATE_REQUIRED",
    )


def _carry_cost_stress() -> CostStressPolicy:
    return CostStressPolicy(
        scenarios=(
            _scenario(
                "BASELINE",
                "qualified_v1_costs",
                required=False,
                fee_multiplier=1.0,
                fee_assumption="BASELINE_FEE_RATE",
                slippage=1.0,
                impact=1.0,
                borrow=1.0,
                funding_haircut=1.0,
                basis="NOT_MODELED_IN_CURRENT_ENGINE",
            ),
            _scenario(
                "MODERATE_REQUIRED",
                "execution_borrow_funding",
                required=True,
                fee_multiplier=1.25,
                fee_assumption="STRUCTURAL_ADVERSE_FEE_RATE",
                slippage=1.5,
                impact=1.5,
                borrow=1.5,
                funding_haircut=0.75,
                basis="NOT_MODELED_IN_CURRENT_ENGINE",
            ),
            _scenario(
                "SEVERE_DIAGNOSTIC",
                "execution_borrow_funding",
                required=False,
                fee_multiplier=1.5,
                fee_assumption="STRUCTURAL_ADVERSE_FEE_RATE",
                slippage=2.0,
                impact=2.0,
                borrow=2.0,
                funding_haircut=0.50,
                basis="NOT_MODELED_IN_CURRENT_ENGINE",
            ),
        ),
        required_scenario="MODERATE_REQUIRED",
    )


def _stability() -> ParameterStabilityPolicy:
    return ParameterStabilityPolicy(
        neighborhood_rule="all parameter specifications are registered before SEARCH and every neighbor consumes one SEARCH trial",
        neighborhood_specs_required_before_search=True,
        neighborhood_specs=(),
        core_gates=(
            "net_return",
            "sample_sufficiency",
            "fold_consistency",
            "drawdown",
            "required_cost_stress",
        ),
        statistical_gates_included=False,
        numeric_discrete_rule="ADJACENT_DOMAIN_VALUES from the pre-registered ordered domain",
        numeric_continuous_rule="RELATIVE_DELTA or ABSOLUTE_DELTA declared per parameter",
        categorical_rule="REGISTERED_ALTERNATIVES declared per parameter",
        boundary_rule="only admissible declared neighbors; empty neighborhood FAIL_CLOSED",
        minimum_neighbor_pass_fraction=0.80,
        relative_metric_tolerance=0.20,
        nonpositive_candidate_rule="candidate primary metric <= 0 => FAIL_CLOSED",
    )


def _family(
    *,
    strategy_family: str,
    minimum_elapsed_days: int,
    minimum_trades: int,
    minimum_economic_events: int,
    economic_event_role: str,
    minimum_duration_months: int,
    hard_cap_months: int,
    worst_fold_floor: float,
    positive_fold_fraction: float,
    cost_stress: CostStressPolicy,
    sample_rule_note: str,
) -> FamilyPolicy:
    return FamilyPolicy(
        strategy_family=strategy_family,
        return_coverage=ReturnCoveragePolicy(
            return_sampling_frequency=RETURN_SAMPLING_FREQUENCY,
            minimum_valid_return_coverage=0.95,
            valid_return_definition="finite daily UTC return after the declared data-integrity contract",
        ),
        minimum_elapsed_days=minimum_elapsed_days,
        minimum_trades=minimum_trades,
        minimum_economic_events=minimum_economic_events,
        economic_event_role=economic_event_role,
        holdout_closure=HoldoutClosurePolicy(
            minimum_duration_months=minimum_duration_months,
            minimum_completed_trades=minimum_trades,
            hard_cap_months=hard_cap_months,
            close_rule="elapsed >= minimum_duration AND completed_trades >= minimum_completed_trades",
            hard_cap_result="INSUFFICIENT_SAMPLE",
        ),
        maximum_drawdown=-0.30,
        fold_consistency=FoldConsistencyPolicy(
            positive_fold_definition="net fold return after all modeled costs > 0",
            minimum_positive_fold_fraction=positive_fold_fraction,
            worst_fold_metric="NET_RETURN",
            worst_fold_floor=worst_fold_floor,
            maximum_pnl_concentration=0.50,
            pnl_concentration_formula="max(positive_fold_pnl) / sum(positive_fold_pnl)",
            nonpositive_denominator_result="FAIL_CLOSED",
        ),
        cost_stress=cost_stress,
        parameter_stability=_stability(),
        sample_rule_note=sample_rule_note,
    )


def proposed_policy_v1() -> QuantGovernancePolicy:
    """Return the normalized numeric proposal; never APPROVED/FROZEN."""

    return QuantGovernancePolicy(
        policy_version=QUANT_POLICY_VERSION,
        status=PolicyStatus.PROPOSED,
        confidence_level=0.95,
        dsr_required=True,
        psr_pass_threshold=PSR_PASS_THRESHOLD,
        dsr_pass_threshold=DSR_PASS_THRESHOLD,
        psr_benchmark_sharpe=PSR_BENCHMARK_SHARPE,
        return_sampling_frequency=RETURN_SAMPLING_FREQUENCY,
        dsr_counters=DSRTrialCounterPolicy(
            raw_attempted_trials_label="RAW_ATTEMPTED_TRIALS",
            valid_sr_trials_label="VALID_SR_TRIALS",
            dsr_n_label="DSR_N",
            dsr_n_convention=DSR_N_CONVENTION,
            raw_attempted_definition="all durable SEARCH trials, including FAILED, INVALID_RESULT and ABORTED",
            valid_sr_definition="finite Sharpe estimates from completed trials in the same declared DSR statistical cohort only",
            independent_trial_limitation="the primary DSR reference concerns independent trials; raw N is a conservative upper-bound convention, not an estimate of independent N",
        ),
        dsr_cohort_fields=(
            "strategy_family",
            "experiment_id",
            "protocol_fingerprint",
            "dataset_identity",
            "dataset_role",
            "evaluation_split_specification",
            "evaluation_interval_policy",
            "return_sampling_frequency",
            "sharpe_convention",
            "sharpe_scale",
            "cost_model",
            "objective_definition",
            "warmup_purge_embargo_semantics",
        ),
        dsr_minimum_valid_comparable_trials=2,
        serial_dependence_policy="BTCQUANT_CONSERVATIVE_POLICY: pre-register sampling_frequency and report stationarity/dependence diagnostics; material serial dependence => NOT_QUALIFIABLE",
        trial_budget_rule="RAW_ATTEMPTED_TRIALS consume the fixed budget permanently",
        selection_metric="NET_RETURN_AFTER_FEES_SLIPPAGE_IMPACT_WITH_ROBUSTNESS_GATES",
        development_gates=(
            "PSR >= 0.95 vs pre-registered benchmark",
            "DSR >= 0.95 using CONSERVATIVE_RAW_TRIAL_UPPER_BOUND",
            "family-specific daily UTC coverage and holdout sample sufficiency",
            "positive net-return fold consistency",
            "component cost stress",
            "parameter stability",
        ),
        blind_promotion_gates=(
            "single frozen candidate only",
            "PSR/DSR pass thresholds and explicit statistical qualification",
            "economic and drawdown gate",
            "fold, concentration and cost-stress gates",
            "no optional stopping",
        ),
        trend=_family(
            strategy_family="Trend",
            minimum_elapsed_days=365,
            minimum_trades=30,
            minimum_economic_events=30,
            economic_event_role="coverage/context only; never PSR/DSR sample size",
            minimum_duration_months=18,
            hard_cap_months=24,
            worst_fold_floor=-0.20,
            positive_fold_fraction=0.75,
            cost_stress=_trend_cost_stress(),
            sample_rule_note="1D UTC valid-return coverage >= 0.95; no theoretical equity-calendar observation minimum",
        ),
        carry=_family(
            strategy_family="Carry",
            minimum_elapsed_days=730,
            minimum_trades=20,
            minimum_economic_events=1000,
            economic_event_role="temporal/economic coverage gate only; not PSR/DSR N or independent observations",
            minimum_duration_months=24,
            hard_cap_months=36,
            worst_fold_floor=-0.25,
            positive_fold_fraction=0.67,
            cost_stress=_carry_cost_stress(),
            sample_rule_note="1D UTC valid-return coverage >= 0.95; funding events preserve coverage evidence but are not independent returns",
        ),
        pbo_status="OPTIONAL_DIAGNOSTIC_NOT_A_PROMOTION_GATE",
        rationale_sources=(
            "PSR/DSR formula and IID/non-normal limitation: Bailey-López de Prado primary papers",
            "thresholds, coverage, holdout, cost, fold and stability values: STRUCTURAL_RISK_POLICY",
            "1D UTC and operational closure rules: OPERATIONAL_DECISION",
            "venue/data availability context only: EMPIRICAL_SEEN_DATA_CONTEXT",
        ),
    )


def freeze_artifact_payload(
    policy: QuantGovernancePolicy,
    *,
    base_git_sha: str = FROZEN_POLICY_BASE_GIT_SHA,
) -> dict[str, Any]:
    """Build the portable, non-runtime freeze artifact payload."""

    if policy.status not in {PolicyStatus.APPROVED, PolicyStatus.FROZEN}:
        raise GovernanceError("only an approved or frozen policy can be sealed")
    return {
        "artifact_schema_version": FREEZE_ARTIFACT_SCHEMA_VERSION,
        "policy_version": policy.policy_version,
        "status": PolicyStatus.FROZEN.value,
        "base_git_sha": base_git_sha,
        "trend_fingerprint": policy.family_fingerprint("Trend"),
        "carry_fingerprint": policy.family_fingerprint("Carry"),
        "combined_fingerprint": policy.fingerprint,
        "freeze_lifecycle_version": FREEZE_LIFECYCLE_VERSION,
        "semantic_policy_hash_algorithm": SEMANTIC_POLICY_HASH_ALGORITHM,
    }


def _read_freeze_artifact(path: str | Path | None) -> dict[str, Any]:
    artifact_path = Path(path) if path is not None else DEFAULT_FREEZE_ARTIFACT
    try:
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GovernanceError(f"freeze artifact unavailable or invalid: {artifact_path}") from exc
    if not isinstance(payload, dict):
        raise GovernanceError("freeze artifact must be a JSON object")
    return payload


def frozen_policy_v1(
    artifact_path: str | Path | None = None,
    *,
    expected_base_git_sha: str = FROZEN_POLICY_BASE_GIT_SHA,
) -> QuantGovernancePolicy:
    """Load QUANT_POLICY_V1 only after validating its committed freeze artifact."""

    proposed = proposed_policy_v1()
    artifact = _read_freeze_artifact(artifact_path)
    expected = freeze_artifact_payload(proposed.approve(), base_git_sha=expected_base_git_sha)
    if artifact != expected:
        raise GovernanceError("freeze artifact does not match QUANT_POLICY_V1")
    return replace(proposed, status=PolicyStatus.FROZEN)


def trend_governance_policy_v1_proposal() -> str:
    return proposed_policy_v1().canonical_family_json("Trend")


def carry_governance_policy_v1_proposal() -> str:
    return proposed_policy_v1().canonical_family_json("Carry")


def validate_policy_shape(policy: QuantGovernancePolicy) -> None:
    """Validate numeric shape and normalization without approving the policy."""

    if policy.confidence_level != policy.psr_pass_threshold:
        raise ValueError("confidence_level must equal PSR threshold")
    if not policy.dsr_required:
        raise ValueError("DSR must be required")
    if policy.psr_pass_threshold != PSR_PASS_THRESHOLD:
        raise ValueError("PSR threshold must be 0.95")
    if policy.dsr_pass_threshold != DSR_PASS_THRESHOLD:
        raise ValueError("DSR threshold must be 0.95")
    if policy.psr_benchmark_sharpe != PSR_BENCHMARK_SHARPE:
        raise ValueError("PSR benchmark must be zero")
    if policy.return_sampling_frequency != RETURN_SAMPLING_FREQUENCY:
        raise ValueError("return frequency must be 1D_UTC")
    if policy.dsr_counters.dsr_n_convention != DSR_N_CONVENTION:
        raise ValueError("DSR_N convention is not conservative raw upper bound")
    if policy.dsr_minimum_valid_comparable_trials < 2:
        raise ValueError("DSR requires two comparable Sharpe trials")
    expected_cohort_fields = (
        "strategy_family",
        "experiment_id",
        "protocol_fingerprint",
        "dataset_identity",
        "dataset_role",
        "evaluation_split_specification",
        "evaluation_interval_policy",
        "return_sampling_frequency",
        "sharpe_convention",
        "sharpe_scale",
        "cost_model",
        "objective_definition",
        "warmup_purge_embargo_semantics",
    )
    if policy.dsr_cohort_fields != expected_cohort_fields:
        raise ValueError("DSR cohort fields are incomplete or reordered")
    if len(policy.development_gates) == 0 or len(policy.blind_promotion_gates) == 0:
        raise ValueError("gates manquantes")
    for family in (policy.trend, policy.carry):
        coverage = family.return_coverage
        if coverage.return_sampling_frequency != RETURN_SAMPLING_FREQUENCY:
            raise ValueError("family frequency must be 1D_UTC")
        if not 0 < coverage.minimum_valid_return_coverage <= 1:
            raise ValueError("coverage invalide")
        if family.minimum_elapsed_days <= 0 or family.minimum_trades <= 0:
            raise ValueError("sample policy non positive")
        if family.minimum_economic_events <= 0:
            raise ValueError("economic event policy non positive")
        closure = family.holdout_closure
        if not 0 < closure.minimum_duration_months < closure.hard_cap_months:
            raise ValueError("holdout closure/cap invalide")
        if closure.minimum_completed_trades != family.minimum_trades:
            raise ValueError("holdout trade count must match family policy")
        if not -1.0 <= family.maximum_drawdown < 0:
            raise ValueError("drawdown policy invalide")
        fold = family.fold_consistency
        if fold.worst_fold_metric != "NET_RETURN":
            raise ValueError("worst fold must be NET_RETURN")
        if not 0 < fold.minimum_positive_fold_fraction <= 1:
            raise ValueError("fold fraction invalide")
        if not -1.0 <= fold.worst_fold_floor <= 1:
            raise ValueError("worst fold floor invalide")
        if not 0 < fold.maximum_pnl_concentration <= 1:
            raise ValueError("concentration invalide")
        stress = family.cost_stress
        if not stress.scenarios or stress.required_scenario not in {
            scenario.name for scenario in stress.scenarios
        }:
            raise ValueError("cost stress non pré-enregistré")
        for scenario in stress.scenarios:
            if any(
                not isfinite(value) or value <= 0
                for value in (
                    scenario.slippage_multiplier,
                    scenario.impact_multiplier,
                    scenario.borrow_multiplier,
                    scenario.fee_multiplier,
                    scenario.basis_multiplier,
                )
            ):
                raise ValueError("cost multiplier invalide")
            if not 0 < scenario.favorable_funding_haircut <= 1:
                raise ValueError("funding haircut invalide")
            if (
                scenario.funding_stress_rule
                != "POSITIVE_INCOME_HAIRCUT_ONLY; NEGATIVE_COST_UNCHANGED"
            ):
                raise ValueError("funding stress direction is not fail-closed")
        stability = family.parameter_stability
        if stability.core_gates != (
            "net_return",
            "sample_sufficiency",
            "fold_consistency",
            "drawdown",
            "required_cost_stress",
        ):
            raise ValueError("stability gates are not explicit")
        if stability.statistical_gates_included:
            raise ValueError("statistical gates must be explicitly excluded from stability")
        if not 0 < stability.minimum_neighbor_pass_fraction <= 1:
            raise ValueError("neighbor fraction invalide")
        if not 0 < stability.relative_metric_tolerance < 1:
            raise ValueError("neighbor metric ratio invalide")
