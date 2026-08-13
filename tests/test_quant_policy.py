from dataclasses import FrozenInstanceError, replace

import pytest

from btcquant.research.quant_policy import (
    PolicyStatus,
    proposed_policy_v1,
    validate_policy_shape,
)
from btcquant.research.governance import GovernanceIncomplete


def test_policy_v1_is_numeric_family_specific_and_valid_shape() -> None:
    policy = proposed_policy_v1()
    validate_policy_shape(policy)

    assert policy.status is PolicyStatus.PROPOSED
    assert policy.confidence_level == 0.95
    assert policy.psr_benchmark_sharpe == 0.0
    assert policy.dsr_required is True
    assert policy.trend.minimum_elapsed_days == 365
    assert policy.carry.minimum_elapsed_days == 730
    assert policy.carry.holdout_months > policy.trend.holdout_months
    assert policy.trend.cost_stress.required_pass_multiplier == 1.5
    assert policy.carry.sample_rule_note.startswith("1D UTC valid-return coverage")


def test_policy_fingerprint_ignores_nothing_semantic_and_is_immutable() -> None:
    policy = proposed_policy_v1()
    same = proposed_policy_v1()
    changed = replace(policy, confidence_level=0.90)

    assert policy.fingerprint == same.fingerprint
    assert policy.fingerprint != changed.fingerprint
    with pytest.raises(FrozenInstanceError):
        policy.confidence_level = 0.90  # type: ignore[misc]


def test_proposed_policy_cannot_authorize_real_search() -> None:
    with pytest.raises(GovernanceIncomplete, match="not approved/frozen"):
        proposed_policy_v1().validate_for_real_search()


def test_placeholders_cannot_bypass_real_search_gate() -> None:
    policy = replace(
        proposed_policy_v1(), status=PolicyStatus.APPROVED, pbo_status="DECISION_REQUIRED"
    )
    with pytest.raises(GovernanceIncomplete, match="placeholders"):
        policy.validate_for_real_search()
