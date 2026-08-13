"""Gates that separate a diagnostic harness from a real search."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .governance import (
    DIAGNOSTIC_LABEL,
    DatasetRole,
    ExperimentSpec,
    GovernanceIncomplete,
    GovernanceError,
)


def _contains_decision_required(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().upper() in {
            "DECISION_REQUIRED",
            "GOVERNANCE_INCOMPLETE",
            "NOT_IMPLEMENTED",
            "UNSET",
            "TODO",
            "INCOMPLETE",
            "UNKNOWN",
        }
    if isinstance(value, Mapping):
        return any(_contains_decision_required(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_contains_decision_required(item) for item in value)
    return False


def governance_missing_fields(spec: ExperimentSpec) -> tuple[str, ...]:
    """Retourne les décisions obligatoires manquantes ou non résolues."""

    missing: list[str] = []
    if spec.maximum_trial_budget <= 0:
        missing.append("trial_budget")
    if not spec.selection_metric or _contains_decision_required(spec.selection_metric):
        missing.append("selection_metric")
    if not spec.sample_sufficiency_policy or _contains_decision_required(
        spec.sample_sufficiency_policy
    ):
        missing.append("sample_sufficiency_policy")
    if not spec.cost_assumptions or _contains_decision_required(spec.cost_assumptions):
        missing.append("cost_model")
    if not spec.fee_assumptions or _contains_decision_required(spec.fee_assumptions):
        missing.append("fee_model")
    if not spec.slippage_assumptions or _contains_decision_required(spec.slippage_assumptions):
        missing.append("slippage_model")
    if not spec.impact_assumptions or _contains_decision_required(spec.impact_assumptions):
        missing.append("impact_model")
    if not spec.stress_tests or _contains_decision_required(spec.stress_tests):
        missing.append("cost_stress_policy")
    if (
        not spec.split_policy
        or spec.split_policy.get("shuffle")
        or _contains_decision_required(spec.split_policy)
    ):
        missing.append("split_policy")
    if not spec.parameter_space or _contains_decision_required(spec.parameter_space):
        missing.append("parameter_space")
    if not spec.candidate_selection_rule or _contains_decision_required(
        spec.candidate_selection_rule
    ):
        missing.append("candidate_selection_rule")
    if not spec.multiple_testing_policy or _contains_decision_required(
        spec.multiple_testing_policy
    ):
        missing.append("multiple_testing_policy")
    promotion_gates = spec.promotion_gates or spec.acceptance_rules
    if (
        not promotion_gates
        or all(value is None for value in promotion_gates.values())
        or _contains_decision_required(promotion_gates)
    ):
        missing.append("promotion_gates")
    if _contains_decision_required(spec.holdout_policy):
        missing.append("holdout_policy")
    if not spec.code_provenance:
        missing.append("code_provenance")
    if DatasetRole.BLIND_FORWARD_OOS in spec.dataset_roles.values():
        missing.append("development_search_must_not_use_blind_holdout")
    return tuple(dict.fromkeys(missing))


def validate_search_ready(spec: ExperimentSpec) -> None:
    """Bloque une recherche de sélection tant que la gouvernance est incomplète."""

    missing = governance_missing_fields(spec)
    if missing:
        raise GovernanceIncomplete("GOVERNANCE_INCOMPLETE: " + ", ".join(missing))
    if spec.split_policy.get("type", "expanding") not in {"expanding", "rolling"}:
        raise GovernanceError("split non chronologique")


def require_diagnostic_label(label: str | None) -> None:
    """Le mode diagnostic doit être explicitement marqué à chaque appel."""

    if label != DIAGNOSTIC_LABEL:
        raise GovernanceIncomplete("le mode diagnostic exige le label exact: " + DIAGNOSTIC_LABEL)


def refuse_ungoverned_search(entrypoint: str) -> None:
    """Block legacy standalone selectors until they use the durable store."""

    raise GovernanceIncomplete(
        f"{entrypoint}: recherche réelle bloquée; utiliser le durable governance store"
    )
