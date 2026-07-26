from __future__ import annotations

import ccxt
import pytest

from btcquant.execution.ccxt_broker import CcxtBroker
from btcquant.execution.resilience import CircuitOpen, RetryPolicy


def test_read_retry_uses_exponential_backoff_then_recovers():
    sleeps: list[float] = []
    calls = 0

    def flaky():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise TimeoutError
        return "ok"

    policy = RetryPolicy(attempts=4, base_delay=0.5, sleep=sleeps.append)

    assert policy.call(flaky, retry_on=(TimeoutError,)) == "ok"
    assert calls == 3
    assert sleeps == [0.5, 1.0]


def test_circuit_opens_and_half_opens_after_cooldown():
    now = [0.0]
    policy = RetryPolicy(
        attempts=2,
        failure_threshold=2,
        reset_after=10,
        sleep=lambda _delay: None,
        monotonic=lambda: now[0],
    )

    def fail():
        raise TimeoutError("offline")

    with pytest.raises(TimeoutError):
        policy.call(fail, retry_on=(TimeoutError,))
    with pytest.raises(CircuitOpen):
        policy.call(fail, retry_on=(TimeoutError,))

    now[0] = 11
    assert policy.call(lambda: "recovered", retry_on=(TimeoutError,)) == "recovered"


def test_market_order_timeout_is_never_retried():
    broker = CcxtBroker.__new__(CcxtBroker)
    broker.symbol = "BTC/USDT"
    broker._order_seq = 0
    broker._round_qty = lambda qty: qty
    broker._check_min_notional = lambda qty, price: None
    calls = 0

    class Exchange:
        def create_order(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            raise ccxt.RequestTimeout("résultat ambigu")

    broker.exchange = Exchange()

    with pytest.raises(ccxt.RequestTimeout):
        broker._market_order("buy", 1.0, 100.0, "stable-intent")

    assert calls == 1
