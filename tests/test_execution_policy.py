import pytest

from btcquant.execution.execution_policy import (
    ExecutionEvidence,
    ExecutionPolicy,
    ExecutionQualificationPolicy,
    ExecutionSnapshot,
    RebalanceBuffer,
    evaluate_execution_evidence,
)


def snapshot(
    *,
    bid: float = 100.0,
    ask: float = 100.01,
    funding: float = 0.0,
    seconds_to_funding: float | None = 1_000.0,
) -> ExecutionSnapshot:
    return ExecutionSnapshot(
        bid=bid,
        ask=ask,
        funding_rate_8h=funding,
        seconds_to_funding=seconds_to_funding,
    )


def test_non_urgent_entry_uses_post_only_at_touch() -> None:
    policy = ExecutionPolicy()

    buy = policy.decide(side="BUY", notional=1_000, snapshot=snapshot())
    sell = policy.decide(side="SELL", notional=1_000, snapshot=snapshot())

    assert (buy.action, buy.limit_price) == ("POST_ONLY", 100.0)
    assert (sell.action, sell.limit_price) == ("POST_ONLY", 100.01)
    assert buy.timeout_seconds == 30.0


def test_small_adjustment_is_held_for_grouping() -> None:
    decision = ExecutionPolicy(min_adjustment_notional=50).decide(
        side="BUY",
        notional=49.99,
        snapshot=snapshot(),
    )

    assert decision.action == "HOLD"


def test_rebalance_buffer_nets_then_releases_adjustments() -> None:
    buffer = RebalanceBuffer(min_notional=50)

    assert buffer.add(30) is None
    assert buffer.add(-10) is None
    assert buffer.add(35) == 55
    assert buffer.pending_notional == 0


def test_excessive_spread_waits() -> None:
    decision = ExecutionPolicy(max_entry_spread_bps=2).decide(
        side="BUY",
        notional=1_000,
        snapshot=snapshot(bid=100, ask=100.05),
    )

    assert decision.action == "WAIT_SPREAD"


@pytest.mark.parametrize(
    ("side", "funding"),
    [("BUY", 0.0001), ("SELL", -0.0001)],
)
def test_adverse_imminent_funding_waits(side: str, funding: float) -> None:
    decision = ExecutionPolicy().decide(
        side=side,
        notional=1_000,
        snapshot=snapshot(funding=funding, seconds_to_funding=120),
    )

    assert decision.action == "WAIT_FUNDING"


def test_favorable_funding_does_not_delay_entry() -> None:
    decision = ExecutionPolicy().decide(
        side="BUY",
        notional=1_000,
        snapshot=snapshot(funding=-0.001, seconds_to_funding=10),
    )

    assert decision.action == "POST_ONLY"


def test_urgent_exit_is_market_even_with_bad_spread_and_small_size() -> None:
    decision = ExecutionPolicy().decide(
        side="SELL",
        notional=10,
        snapshot=snapshot(bid=100, ask=101, funding=-0.01, seconds_to_funding=1),
        urgent=True,
    )

    assert decision.action == "MARKET"


def test_invalid_book_is_rejected() -> None:
    with pytest.raises(ValueError, match="Carnet invalide"):
        ExecutionSnapshot(bid=101, ask=100)


def test_execution_qualification_rejects_missing_evidence() -> None:
    result = evaluate_execution_evidence(
        ExecutionEvidence(
            observation_days=0,
            eligible_intents=0,
            post_only_fills=0,
            fallback_orders=0,
            p95_fill_seconds=None,
            mean_all_in_cost_bps=None,
            p95_slippage_bps=None,
        )
    )

    assert result["passed"] is False
    assert not any(result["checks"].values())
    assert result["post_only_fill_rate"] is None


def test_execution_qualification_accepts_complete_evidence() -> None:
    policy = ExecutionQualificationPolicy()
    result = evaluate_execution_evidence(
        ExecutionEvidence(
            observation_days=30,
            eligible_intents=100,
            post_only_fills=75,
            fallback_orders=25,
            p95_fill_seconds=25,
            mean_all_in_cost_bps=6.5,
            p95_slippage_bps=4.0,
        ),
        policy,
    )

    assert result["passed"] is True
    assert all(result["checks"].values())
