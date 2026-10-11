"""Funding outages must preserve protection, retryability and accounting cursors."""

from types import SimpleNamespace

import pandas as pd
import pytest

from btcquant.execution.errors import ReconciliationRequired
from btcquant.execution.funding_service import FundingService, contiguous_funding
from test_audit_carry_paper_regressions import _market, _QualifiedVenue, _qualified_runner


def _runner(tmp_path):
    return _qualified_runner(tmp_path, _QualifiedVenue(_market(str(pd.Timestamp.now(tz="UTC")))))


def _history():
    return pd.Series(
        0.0001, index=pd.date_range(end=pd.Timestamp.now(tz="UTC").floor("h"), periods=49, freq="h")
    )


def test_flat_signal_gap_recovers_automatically_and_after_restart(tmp_path):
    runner = _runner(tmp_path)
    good = _history()
    runner._recent_funding = lambda: good.drop(good.index[-5])
    runner._tick()
    assert not runner.accounting_uncertain and not runner.in_position
    restored = _runner(tmp_path)
    restored._recent_funding = lambda: good
    decisions = []
    restored._open_two_leg = lambda **kw: decisions.append(kw)
    restored._tick()
    assert len(decisions) == 1
    assert not restored.accounting_uncertain


@pytest.mark.parametrize("failure", ["empty", "network"])
def test_risk_and_timeout_run_when_funding_unavailable(tmp_path, failure):
    runner = _runner(tmp_path)

    def history():
        if failure == "network":
            raise TimeoutError("funding API unavailable")
        return pd.Series(dtype=float)

    runner._recent_funding = history
    calls = []
    runner._update_kill_switches = lambda: calls.append("risk")
    runner._enforce_two_leg_timeout = lambda **kw: calls.append("timeout") or False
    runner._open_two_leg = lambda **kw: pytest.fail("entry during funding outage")
    runner._tick()
    assert calls == ["timeout", "risk"]
    assert not runner.accounting_uncertain


def test_stale_funding_never_opens_position_but_records_equity(tmp_path):
    runner = _runner(tmp_path)
    stale = _history()
    stale.index -= pd.Timedelta(days=3)
    runner._recent_funding = lambda: stale
    runner._open_two_leg = lambda **kw: pytest.fail("stale entry")
    runner._tick()
    with runner.store._connect() as connection:
        assert (
            connection.execute(
                "select count(*) from equity_samples where engine='carry'"
            ).fetchone()[0]
            == 1
        )
    assert not runner.accounting_uncertain


def test_failed_accounting_checkpoint_does_not_reach_clean_shutdown_save(tmp_path):
    import threading

    runner = _runner(tmp_path)
    durable = runner.store.load_engine_state("carry")

    def fail(*args, **kwargs):
        raise OSError("disk failed")

    runner.store.save_engine_state = fail
    runner._tick = lambda: runner._two_leg_persist("carry_funding_payment", {"event_id": "test"})
    with pytest.raises(ReconciliationRequired):
        runner.run_forever(threading.Event())
    assert runner.store.load_engine_state("carry") == durable


def test_missing_payment_is_retried_before_later_payments_with_timestamp_jitter():
    start = pd.Timestamp("2026-10-11T00:00:00.060Z")
    now = start + pd.Timedelta(hours=4)
    clock = SimpleNamespace(monotonic=lambda: 1.0, utc_now=lambda: now)
    stamps = [pd.Timestamp(f"2026-10-11T0{h}:00:00.{ms:03d}Z") for h, ms in ((1, 2), (3, 8))]
    rows = pd.Series([0.001, 0.003], index=stamps)
    venue = SimpleNamespace(
        funding_history_since=lambda since: rows, native_funding_interval=pd.Timedelta(hours=1)
    )
    service = FundingService(venue, clock, poll_seconds=0)
    first = service.poll(start)
    assert [p.rate for p in first.payments] == [0.001]
    assert first.checkpoint == stamps[0]
    assert first.missing_timestamp == pd.Timestamp("2026-10-11T02:00:00Z")
    rows.loc[pd.Timestamp("2026-10-11T02:00:00.090Z")] = 0.002
    second = service.poll(first.checkpoint)
    assert [p.rate for p in second.payments] == [0.002, 0.003]
    assert second.missing_timestamp is None
    assert service.poll(second.checkpoint).payments == ()


def test_gap_before_first_returned_payment_does_not_advance_mid_slot_checkpoint():
    start = pd.Timestamp("2026-10-11T00:45:00Z")
    raw = pd.Series([0.003], index=[pd.Timestamp("2026-10-11T03:00:00Z")])
    accepted, missing = contiguous_funding(raw, start, pd.Timedelta(hours=1))
    assert accepted.empty
    assert missing == pd.Timestamp("2026-10-11T01:00:00Z")


def test_old_flat_signal_latch_recovers_without_clearing_real_accounting_uncertainty(tmp_path):
    runner = _runner(tmp_path)
    runner.accounting_uncertain = True
    runner.accounting_uncertainty_reason = "Signal carry two-leg incertain"
    runner._save_state()
    restored = _runner(tmp_path)
    assert not restored.accounting_uncertain
    restored.accounting_uncertain = True
    restored.accounting_uncertainty_reason = "Funding non reconstructible"
    restored._save_state()
    assert _runner(tmp_path).accounting_uncertain


def test_open_position_hard_halt_closes_even_without_funding_and_flags_unsettled_accounts(tmp_path):
    from btcquant.execution.carry_paper import CarryExecutionState
    from test_audit_carry_paper_regressions import _seed_exit_position

    runner = _runner(tmp_path)
    _seed_exit_position(
        runner,
        start=pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=1),
        spot_qty=1,
        perp_qty=1,
        debt=100,
    )
    runner.two_leg_state = CarryExecutionState.HEDGED
    runner.halted = True
    runner.venue.current_carry_market_state = lambda: _market(str(pd.Timestamp.now(tz="UTC")))
    runner._recent_funding = lambda: pd.Series(dtype=float)
    runner._tick()
    assert not runner.in_position
    assert runner.accounting_uncertain
    assert (
        runner.accounting_uncertainty_reason
        == "Sortie de risque avec funding non intégralement vérifié"
    )
    assert runner.store.load_engine_state("carry")["accounting_uncertain"]


def test_trend_funding_outage_keeps_protective_cycle_running_and_recovers(tmp_path):
    import threading
    from btcquant.execution.broker import PaperBroker
    from btcquant.execution.funding_service import FundingPoll
    from test_protective_stop_saga import _runner as trend_runner

    runner, slot = trend_runner(tmp_path / "trend.db", PaperBroker(simulate_exchange_stops=True))

    def fail(_):
        raise TimeoutError("funding unavailable")

    runner.funding_service = SimpleNamespace(poll=fail)
    calls = []
    runner._monitor_exchange_stops = lambda: calls.append("stops")
    runner._maybe_reconcile_position = lambda: None
    runner._update_kill_switches = lambda price: calls.append("risk")
    runner._liquidate_if_halted = lambda price: None
    runner._check_soft_stops = lambda price: None
    runner._process_due_bars = lambda price: None
    runner._run_cycle(100.0, threading.Event())
    assert calls == ["stops", "risk", "stops"]
    assert runner._funding_data_unavailable
    now = pd.Timestamp.now(tz="UTC")
    runner.funding_service = SimpleNamespace(poll=lambda _: FundingPoll(now, (), initialized=True))
    runner._run_cycle(100.0, threading.Event())
    assert not runner._funding_data_unavailable


def test_early_raw_payment_checkpoint_does_not_create_false_gap():
    checkpoint = pd.Timestamp("2026-10-11T00:59:59.990Z")
    raw = pd.Series([0.1, 0.2], index=[checkpoint, pd.Timestamp("2026-10-11T02:00:00.010Z")])
    accepted, missing = contiguous_funding(raw, checkpoint, pd.Timedelta(hours=1))
    assert missing is None
    assert list(accepted) == [0.2]


def test_two_distinct_raw_events_in_one_native_slot_are_not_paid_twice():
    checkpoint = pd.Timestamp("2026-10-11T01:00:00.010Z")
    raw = pd.Series([0.1, 0.2], index=[checkpoint, pd.Timestamp("2026-10-11T01:00:00.020Z")])
    with pytest.raises(ValueError, match="duplicate native"):
        contiguous_funding(raw, checkpoint, pd.Timedelta(hours=1))
