from dataclasses import replace
from datetime import timedelta

import pandas as pd
import pytest

from btcquant.research.governance import DIAGNOSTIC_LABEL, GovernanceError, GovernanceIncomplete
from btcquant.research.governed_walkforward import governed_walk_forward
from btcquant.research.search_gates import (
    governance_missing_fields,
    require_diagnostic_label,
    validate_search_ready,
)

from test_quant_governance import make_spec


def complete_spec():
    return replace(
        make_spec(),
        sample_sufficiency_policy={
            "mode": "family_specific",
            "thresholds": {"trend": {"min_trades": 5}, "carry": {"min_trades": 2}},
        },
        candidate_selection_rule="pre_registered_max_train_metric_then_stability",
        multiple_testing_policy={"trial_budget": "fixed", "advanced_metric": "optional_diagnostic"},
        stress_tests={"cost_scenarios": ["base", "stress"]},
        holdout_policy={"close_rule": "fixed_calendar_end"},
        promotion_gates={"sharpe": 0.0, "max_drawdown": -0.5},
    )


def test_unresolved_governance_blocks_real_search() -> None:
    spec = make_spec()
    assert "sample_sufficiency_policy" in governance_missing_fields(spec)
    with pytest.raises(GovernanceIncomplete, match="GOVERNANCE_INCOMPLETE"):
        validate_search_ready(spec)
    with pytest.raises(GovernanceIncomplete):
        governed_walk_forward(
            pd.DataFrame(
                {"close": range(16)},
                index=pd.date_range("2026-01-01T00:00Z", periods=16, freq="h"),
            ),
            [{"kind": "candidate"}],
            spec=spec,
            train_duration=timedelta(hours=4),
            evaluation_duration=timedelta(hours=3),
            warmup_duration=timedelta(hours=1),
            purge_duration=timedelta(0),
            embargo_duration=timedelta(0),
            evaluator=lambda *_: {"sharpe": 1.0, "evaluation_metrics": {}},
        )


def test_diagnostic_label_is_required() -> None:
    with pytest.raises(GovernanceIncomplete):
        require_diagnostic_label("diagnostic")


def test_diagnostic_mode_requires_exact_label_and_complete_spec_can_pass_gate() -> None:
    spec = complete_spec()
    assert spec.fingerprint == replace(spec, created_at="2027-01-01T00:00:00Z").fingerprint
    validate_search_ready(spec)
    with pytest.raises(GovernanceError, match="durable search adapter"):
        governed_walk_forward(
            pd.DataFrame(
                {"close": range(16)},
                index=pd.date_range("2026-01-01T00:00Z", periods=16, freq="h"),
            ),
            [{"kind": "candidate"}],
            spec=spec,
            train_duration=timedelta(hours=4),
            evaluation_duration=timedelta(hours=3),
            warmup_duration=timedelta(hours=1),
            purge_duration=timedelta(0),
            embargo_duration=timedelta(0),
            evaluator=lambda *_: {"sharpe": 1.0, "evaluation_metrics": {}},
        )
    assert DIAGNOSTIC_LABEL.startswith("DIAGNOSTIC GOVERNANCE")
