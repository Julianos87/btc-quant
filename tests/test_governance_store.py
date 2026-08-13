from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from multiprocessing import get_context
from threading import Barrier

import pytest

from btcquant.research.governance import (
    DatasetProvenance,
    TrialBudgetExceeded,
    DatasetRole,
    HoldoutInvalidated,
)
from btcquant.research.governance_store import (
    DuplicateTrial,
    GovernanceStore,
    GovernanceStoreError,
    result_fingerprint,
)

from test_search_gates import complete_spec


HASH_A = "a" * 64
HASH_B = "b" * 64


def make_dataset(
    *, dataset_id: str, start: str, end: str, role: DatasetRole, venue: str
) -> DatasetProvenance:
    return DatasetProvenance(
        dataset_id=dataset_id,
        venue=venue,
        network="mainnet",
        symbol="BTC/USDC:USDC" if venue == "hyperliquid" else "BTC/USDT:USDT",
        role=role,
        start=start,
        end=end,
        rows_or_events=10,
        sha256=HASH_A if dataset_id.endswith("a") else HASH_B,
        cutoff=end,
        manifest="fixture-manifest.json",
        already_seen=False,
    )


def trial_args(spec, parameters=None):
    return {
        "spec": spec,
        "parameters": parameters or {"lookback": 20},
        "dataset_fingerprint": HASH_A,
        "split_fingerprint": HASH_B,
    }


def _process_reserve_worker(path: str, lookback: int, queue) -> None:
    spec = replace(complete_spec(), maximum_trial_budget=1)
    try:
        with GovernanceStore(path) as store:
            store.reserve_trial(**trial_args(spec, {"lookback": lookback}))
    except TrialBudgetExceeded:
        queue.put("EXHAUSTED")
    else:
        queue.put("RESERVED")


def test_store_is_separate_and_configured_for_durable_sqlite(tmp_path) -> None:
    path = tmp_path / "governance.sqlite3"
    with GovernanceStore(path) as store:
        assert store.path == path
        assert store._connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert store._connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert store._connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert (
            store._connection.execute(
                "SELECT value FROM governance_metadata WHERE key='schema_version'"
            ).fetchone()[0]
            == "1"
        )
    with pytest.raises(GovernanceStoreError):
        GovernanceStore(tmp_path / "btcquant.db")


def test_execute_trial_reserves_before_callback_and_persists_result(tmp_path) -> None:
    path = tmp_path / "governance.sqlite3"
    spec = complete_spec()
    observed = []
    with GovernanceStore(path) as store:
        result = store.execute_trial(
            spec,
            {"lookback": 20},
            dataset_fingerprint=HASH_A,
            split_fingerprint=HASH_B,
            evaluator=lambda reservation: (
                observed.append(store.get_trial(reservation.trial_id)["status"])
                or {"metrics": {"sharpe": 0.5}, "value": 1}
            ),
        )
        assert result["value"] == 1
        assert observed == ["RUNNING"]
    with GovernanceStore(path) as reopened:
        assert reopened.get_trial(f"{spec.experiment_id}:trial:000001")["status"] == "SUCCEEDED"


def test_trial_reservation_survives_restart_and_failed_result(tmp_path) -> None:
    path = tmp_path / "governance.sqlite3"
    spec = replace(complete_spec(), maximum_trial_budget=2)
    first = GovernanceStore(path)
    first.reserve_trial(**trial_args(spec))
    first.close()

    reopened = GovernanceStore(path)
    assert reopened.trial_count(spec.experiment_id) == 1
    row = reopened.get_trial(f"{spec.experiment_id}:trial:000001")
    assert row is not None and row["status"] == "RESERVED"
    reopened.start_trial(row["trial_id"])
    reopened.finish_trial(row["trial_id"], status="FAILED", failure_reason="crash")
    reopened.close()

    final = GovernanceStore(path)
    assert final.trial_count(spec.experiment_id) == 1
    assert final.get_trial(row["trial_id"])["status"] == "FAILED"
    final.close()


def test_nan_or_invalid_trial_remains_counted(tmp_path) -> None:
    path = tmp_path / "governance.sqlite3"
    spec = complete_spec()
    with GovernanceStore(path) as store:
        reservation = store.reserve_trial(**trial_args(spec))
        store.finish_trial(
            reservation.trial_id,
            status="INVALID_RESULT",
            result={"reason": "non_finite_metric"},
        )
    with GovernanceStore(path) as reopened:
        assert reopened.trial_count(spec.experiment_id) == 1
        assert reopened.get_trial(reservation.trial_id)["status"] == "INVALID_RESULT"


def test_last_budget_slot_is_concurrency_safe(tmp_path) -> None:
    path = tmp_path / "governance.sqlite3"
    spec = complete_spec()
    spec = replace(spec, maximum_trial_budget=1)
    barrier = Barrier(2)
    with GovernanceStore(path) as setup:
        setup.register_experiment(spec)

    def reserve(parameters):
        with GovernanceStore(path) as store:
            barrier.wait()
            try:
                return store.reserve_trial(**trial_args(spec, parameters))
            except TrialBudgetExceeded:
                return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(reserve, [{"lookback": 20}, {"lookback": 55}]))
    assert sum(result is not None for result in results) == 1


def test_last_budget_slot_is_process_safe(tmp_path) -> None:
    path = tmp_path / "governance.sqlite3"
    spec = replace(complete_spec(), maximum_trial_budget=1)
    with GovernanceStore(path) as setup:
        setup.register_experiment(spec)
    context = get_context("spawn")
    queue = context.Queue()
    processes = [
        context.Process(target=_process_reserve_worker, args=(str(path), lookback, queue))
        for lookback in (20, 55)
    ]
    for process in processes:
        process.start()
    results = [queue.get(timeout=15) for _ in processes]
    for process in processes:
        process.join(timeout=15)
    assert all(process.exitcode == 0 for process in processes)
    assert results.count("RESERVED") == 1
    assert results.count("EXHAUSTED") == 1


def test_duplicate_search_and_explicit_reproduction_have_distinct_semantics(tmp_path) -> None:
    with GovernanceStore(tmp_path / "governance.sqlite3") as store:
        spec = complete_spec()
        first = store.reserve_trial(**trial_args(spec))
        with pytest.raises(DuplicateTrial):
            store.reserve_trial(**trial_args(spec))
        reproduction = store.begin_reproduction(**trial_args(spec))
        assert reproduction.trial_id == first.trial_id
        assert not reproduction.is_new
        assert store.trial_count(spec.experiment_id) == 1
        with pytest.raises(DuplicateTrial, match="déjà enregistré"):
            store.begin_reproduction(**trial_args(spec, {"lookback": 55}))


def test_no_delete_or_reset_api_exists() -> None:
    assert not hasattr(GovernanceStore, "delete_trial")
    assert not hasattr(GovernanceStore, "reset_trial_budget")
    assert not hasattr(GovernanceStore, "clear_failed_trials")


def test_seen_dataset_interval_cannot_become_blind(tmp_path) -> None:
    seen = make_dataset(
        dataset_id="hyperliquid-v1-a",
        start="2026-01-14T12:00:00Z",
        end="2026-08-10T19:00:00Z",
        role=DatasetRole.SEEN_EXECUTION_PARITY_DATA,
        venue="hyperliquid",
    )
    blind = replace(seen, role=DatasetRole.BLIND_FORWARD_OOS, dataset_id="future-a")
    with GovernanceStore(tmp_path / "governance.sqlite3") as store:
        store.register_seen_dataset(seen, purpose="diagnostic")
        with pytest.raises(Exception, match="déjà enregistré"):
            store.reserve_blind_dataset(blind, experiment_id="exp")


def test_binance_cannot_be_reserved_as_hyperliquid_final_blind(tmp_path) -> None:
    blind = make_dataset(
        dataset_id="binance-future-a",
        start="2027-01-01T00:00:00Z",
        end="2027-01-02T00:00:00Z",
        role=DatasetRole.BLIND_FORWARD_OOS,
        venue="binance",
    )
    with GovernanceStore(tmp_path / "governance.sqlite3") as store:
        with pytest.raises(Exception, match="Hyperliquid"):
            store.reserve_blind_dataset(blind, experiment_id="exp", purpose="HYPERLIQUID_FINAL_OOS")


def holdout_identity(*, candidate: str = "candidate-a", cost: float = 0.00045):
    return {
        "candidate_fingerprint": candidate,
        "experiment_fingerprint": "experiment-a",
        "code_sha": "a" * 40,
        "venue": "hyperliquid",
        "symbol": "BTC/USDC:USDC",
        "start_rule": "first full UTC hour after freeze",
        "end_rule": "fixed calendar duration",
        "cost_assumptions": {"fee": cost},
        "metrics": {"sharpe": "pre_registered"},
        "promotion_gates": {"sharpe": 0.0},
        "sample_sufficiency_rule": {"mode": "family_specific"},
        "dataset_usage_id": "blind-usage",
    }


def reserve_holdout(store: GovernanceStore) -> str:
    future = make_dataset(
        dataset_id="hyperliquid-future-a",
        start="2027-01-01T00:00:00Z",
        end="2027-02-01T00:00:00Z",
        role=DatasetRole.BLIND_FORWARD_OOS,
        venue="hyperliquid",
    )
    usage_id = store.reserve_blind_dataset(future, experiment_id="experiment-a")
    store.reserve_holdout(
        holdout_id="holdout-1",
        candidate_fingerprint="candidate-a",
        experiment_fingerprint="experiment-a",
        code_sha="a" * 40,
        venue="hyperliquid",
        symbol="BTC/USDC:USDC",
        start_rule="first full UTC hour after freeze",
        end_rule="fixed calendar duration",
        cost_assumptions={"fee": 0.00045},
        metrics={"sharpe": "pre_registered"},
        promotion_gates={"sharpe": 0.0},
        sample_sufficiency_rule={"mode": "family_specific"},
        dataset_usage_id=usage_id,
    )
    return usage_id


def test_holdout_pending_recovery_and_atomic_result_exposure(tmp_path) -> None:
    path = tmp_path / "governance.sqlite3"
    identity = holdout_identity()
    with GovernanceStore(path) as store:
        usage_id = reserve_holdout(store)
        identity["dataset_usage_id"] = usage_id
        assert store.get_holdout("holdout-1")["status"] == "BLIND_RESERVED"
        store.begin_holdout_evaluation("holdout-1", identity=identity)
        with pytest.raises(RuntimeError):
            store.evaluate_holdout(
                "holdout-1",
                identity=identity,
                evaluator=lambda: (_ for _ in ()).throw(RuntimeError("crash")),
            )
        assert store.get_holdout("holdout-1")["status"] == "PENDING"
        with pytest.raises(HoldoutInvalidated):
            store.begin_holdout_evaluation(
                "holdout-1", identity=holdout_identity(candidate="other")
            )

        observed_during_compute = []

        def evaluator():
            observed_during_compute.append(store.get_holdout("holdout-1")["result"])
            return {"sharpe": 0.5, "pnl": 12.0}

        result = store.evaluate_holdout("holdout-1", identity=identity, evaluator=evaluator)
        assert observed_during_compute == [{}]
        assert result == {"sharpe": 0.5, "pnl": 12.0}
        assert store.get_holdout("holdout-1")["status"] == "SPENT"

    with GovernanceStore(path) as reopened:
        called = []
        reproduced = reopened.evaluate_holdout(
            "holdout-1",
            identity=identity,
            evaluator=lambda: called.append("must-not-run") or {"bad": 1},
        )
        assert reproduced == {"sharpe": 0.5, "pnl": 12.0}
        assert called == []
        with pytest.raises(HoldoutInvalidated):
            reopened.evaluate_holdout(
                "holdout-1",
                identity=holdout_identity(candidate="other"),
                evaluator=lambda: {"sharpe": 1.0},
            )


def test_semantic_result_fingerprint_ignores_execution_time() -> None:
    assert result_fingerprint(
        {"generated_at": "2026-01-01T00:00:00Z", "sharpe": 0.5, "metrics": {"pnl": 2}}
    ) == result_fingerprint(
        {"generated_at": "2027-01-01T00:00:00Z", "metrics": {"pnl": 2}, "sharpe": 0.5}
    )
