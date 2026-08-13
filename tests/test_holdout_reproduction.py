from btcquant.research.governance import HoldoutSeal, HoldoutStatus


def test_spent_holdout_same_identity_is_reproducible_without_reset() -> None:
    seal = HoldoutSeal(
        candidate_fingerprint="a" * 64,
        parameter_fingerprint="b" * 64,
        experiment_fingerprint="c" * 64,
        code_sha="d" * 40,
        dataset_policy={"role": "BLIND_FORWARD_OOS"},
        holdout_start_rule="fixed",
        holdout_end_rule="fixed",
        metrics={},
        acceptance_gates={"sharpe": "required"},
        cost_assumptions={"fee": 0.00045},
    )

    seal.open()
    seal.validate_identity(
        candidate_fingerprint="a" * 64,
        parameter_fingerprint="b" * 64,
        experiment_fingerprint="c" * 64,
        code_sha="d" * 40,
        cost_assumptions={"fee": 0.00045},
    )
    seal.evaluate({"sharpe": 0.5})

    seal.validate_identity(
        candidate_fingerprint="a" * 64,
        parameter_fingerprint="b" * 64,
        experiment_fingerprint="c" * 64,
        code_sha="d" * 40,
        cost_assumptions={"fee": 0.00045},
    )

    assert seal.status is HoldoutStatus.SPENT
