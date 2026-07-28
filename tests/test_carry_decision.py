"""Parité temporelle du noyau carry partagé par backtest et runner."""

from __future__ import annotations

import math

from btcquant.domain.carry_decision import CarryAction, CarryDecision, decide_carry_payment


def decide(in_position: bool, smooth_ann: float, **kwargs) -> CarryDecision:
    return decide_carry_payment(
        in_position=in_position,
        smooth_ann=smooth_ann,
        enter_ann=0.03,
        exit_ann=0.0,
        **kwargs,
    )


def test_thresholds_are_strict_and_hysteretic():
    assert decide(False, 0.03).action is CarryAction.HOLD
    assert decide(False, 0.031) == CarryDecision(True, CarryAction.OPEN, "funding_entry")

    assert decide(True, 0.0).action is CarryAction.HOLD
    assert decide(True, -0.001) == CarryDecision(False, CarryAction.CLOSE, "funding_exit")


def test_missing_signal_never_changes_the_position():
    flat = decide(False, math.nan)
    invested = decide(True, math.nan)
    assert flat.action is CarryAction.HOLD and not flat.in_position
    assert invested.action is CarryAction.HOLD and invested.in_position


def test_daily_lockout_blocks_only_new_entries():
    blocked = decide(False, 0.10, entry_blocked=True)
    exit_allowed = decide(True, -0.10, entry_blocked=True)
    assert blocked.action is CarryAction.HOLD
    assert exit_allowed.action is CarryAction.CLOSE


def test_halt_is_fail_closed():
    close = decide(True, 0.10, halted=True)
    stay_flat = decide(False, 0.10, halted=True)
    assert close.action is CarryAction.CLOSE
    assert close.reason == "kill_switch"
    assert stay_flat.action is CarryAction.HOLD


def test_decision_at_t_is_exposure_from_the_next_payment():
    """Le backtest décale l'état ; le runner crédite avant de décider."""

    signals = [math.nan, 0.04, 0.05, -0.01, 0.04]
    state = False
    signal_states = []
    runner_exposure_before_decision = []
    for signal in signals:
        runner_exposure_before_decision.append(state)
        state = decide(state, signal).in_position
        signal_states.append(state)

    backtest_applied = [False, *signal_states[:-1]]
    assert runner_exposure_before_decision == backtest_applied
    assert backtest_applied == [False, False, True, True, False]
