from dataclasses import replace

import pytest

from btcquant.research.governance import ExperimentInvalidated, ExperimentRegistry

from test_quant_governance import make_spec


def test_cost_model_mutation_requires_a_new_experiment() -> None:
    registry = ExperimentRegistry()
    original = make_spec()
    registry.register(original)
    mutated = replace(
        original,
        cost_assumptions={"fee": 0.0005, "slippage_bps": 5.0},
    )

    with pytest.raises(ExperimentInvalidated):
        registry.register(mutated)


def test_dataset_hash_mutation_requires_a_new_experiment() -> None:
    registry = ExperimentRegistry()
    original = make_spec()
    registry.register(original)
    mutated = replace(
        original,
        dataset_hashes={"hl-v1": "d" * 64},
    )

    with pytest.raises(ExperimentInvalidated):
        registry.register(mutated)
