from __future__ import annotations

from datetime import timedelta

import pandas as pd
import pytest

from btcquant.research.governance import DIAGNOSTIC_LABEL, GovernanceError
from btcquant.research.governed_walkforward import governed_walk_forward

from test_quant_governance import make_spec


def test_governed_walkforward_counts_every_candidate_and_keeps_oos_flat() -> None:
    index = pd.date_range("2026-01-01T00:00Z", periods=30, freq="h")
    frame = pd.DataFrame({"close": range(len(index))}, index=index)
    candidates = [{"kind": "stable"}, {"kind": "failed"}, {"kind": "other"}]
    seen: list[tuple[dict, int, int]] = []

    def evaluator(parameters, train, evaluation):
        seen.append((dict(parameters), len(train), len(evaluation)))
        if parameters["kind"] == "failed":
            raise RuntimeError("synthetic failed trial")
        score = 1.0 if parameters["kind"] == "stable" else 0.5
        return {"sharpe": score, "evaluation_metrics": {"return": score}}

    spec = make_spec(budget=100)
    result = governed_walk_forward(
        frame,
        candidates,
        spec=spec,
        train_duration=timedelta(hours=8),
        evaluation_duration=timedelta(hours=4),
        warmup_duration=timedelta(hours=2),
        purge_duration=timedelta(hours=1),
        embargo_duration=timedelta(hours=1),
        evaluator=evaluator,
        run_mode="diagnostic",
        diagnostic_label=DIAGNOSTIC_LABEL,
    )
    assert result.trials_attempted == len(result.folds) * len(candidates)
    assert all(
        item.status == "FAILED"
        for item in result.trial_registry.records
        if item.parameters["kind"] == "failed"
    )
    assert all(fold.selected_parameters == {"kind": "stable"} for fold in result.folds)
    assert all(train_count > 0 and eval_count > 0 for _, train_count, eval_count in seen)
    assert all(split.initial_position == "FLAT" for split in result.split_definitions)


def test_governed_walkforward_refuses_budget_overflow() -> None:
    index = pd.date_range("2026-01-01T00:00Z", periods=16, freq="h")
    frame = pd.DataFrame({"close": range(len(index))}, index=index)

    with pytest.raises(GovernanceError, match="budget"):
        governed_walk_forward(
            frame,
            [{"kind": "a"}, {"kind": "b"}],
            spec=make_spec(budget=1),
            train_duration=timedelta(hours=4),
            evaluation_duration=timedelta(hours=3),
            warmup_duration=timedelta(hours=1),
            purge_duration=timedelta(0),
            embargo_duration=timedelta(0),
            evaluator=lambda *_: {"sharpe": 1.0, "evaluation_metrics": {}},
            run_mode="diagnostic",
            diagnostic_label=DIAGNOSTIC_LABEL,
        )
