"""Comportement du moteur carry paper : politique, risque et comptabilisation.

Trois invariants sont figés ici, chacun corrigeant un écart mesuré entre ce
que le carry prétendait faire et ce qu'il faisait :

1. le runner exécute EXACTEMENT la politique du backtest publié — les défauts
   divergeaient (entrée 3 % vs 5 %, lissage 14 j vs 7 j), donc aucune référence
   ne décrivait le moteur réel ;
2. le carry possède des coupe-circuits — il gérait 40 % du portefeuille sans
   aucun filet, alors que le trend en avait deux ;
3. les paiements de funding antérieurs à la fenêtre de lissage ne sont plus
   perdus après un arrêt prolongé.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btcquant.carry import (
    PAPER_CARRY_POLICY,
    CarryPolicy,
    backtest_carry,
    elapsed_years_between,
    funding_event_id,
)
from btcquant.execution.carry_runner import CarryRunner


class StubVenue:
    """Venue de funding déterministe, sans réseau."""

    payments_per_day = 3
    native_funding_interval = pd.Timedelta("8h")
    exchange_id = "binance"

    def __init__(self, funding: pd.Series) -> None:
        self.funding = funding
        self.requested_since: list[pd.Timestamp] = []

    @property
    def payments_per_year(self) -> int:
        return self.payments_per_day * 365

    def funding_history(self, days: float) -> pd.Series:
        raise AssertionError("le runner doit repartir du checkpoint, pas d'une fenêtre fixe")

    def funding_history_since(self, since: pd.Timestamp) -> pd.Series:
        self.requested_since.append(pd.Timestamp(since))
        return self.funding[self.funding.index >= pd.Timestamp(since)]


def _funding(n: int, rate: float, end: pd.Timestamp | None = None) -> pd.Series:
    end = end or pd.Timestamp.now(tz="UTC").floor("h")
    index = pd.date_range(end=end, periods=n, freq="8h", tz="UTC")
    return pd.Series([rate] * n, index=index)


def _runner(tmp_path, funding: pd.Series, **policy_kwargs) -> CarryRunner:
    policy = replace(PAPER_CARRY_POLICY, **policy_kwargs)
    return CarryRunner(
        policy=policy,
        state_file=tmp_path / "btcquant.db",
        venue=StubVenue(funding),
        notifier=lambda _message: True,
    )


def _mark_open(runner: CarryRunner, checkpoint: pd.Timestamp) -> None:
    runner.in_position = True
    runner.execution_state = "OPEN"
    runner.entry_equity = runner.equity
    runner.entry_timestamp = checkpoint
    runner.spot_notional = runner.equity * runner.leverage
    runner.perp_notional = runner.spot_notional
    runner.borrow_principal = runner.equity * (runner.leverage - 1.0)
    runner.position_generation = (
        funding_event_id(runner.venue.exchange_id, runner.symbol, checkpoint) + "|position"
    )
    runner.last_funding_ts = checkpoint


# ── 1. politique unique backtest / runner ───────────────────────────────────


def test_runner_defaults_are_the_published_backtest_policy():
    """Les défauts du runner ne sont pas « proches » de ceux du backtest : ce
    sont les mêmes objets. Un futur ajustement ne peut plus n'en toucher qu'un."""
    from inspect import signature

    defaults = signature(backtest_carry).parameters
    for field in ("leverage", "enter_ann", "exit_ann", "smooth_days", "fee_rate"):
        assert defaults[field].default == getattr(PAPER_CARRY_POLICY, field), field


def test_runner_uses_the_policy_switch_cost(tmp_path):
    runner = _runner(tmp_path, _funding(60, 0.0002))
    expected = 2 * (PAPER_CARRY_POLICY.fee_rate + PAPER_CARRY_POLICY.slippage_bps / 10_000.0)
    assert runner.switch_cost == pytest.approx(expected * PAPER_CARRY_POLICY.leverage)


def test_policy_rejects_incoherent_thresholds():
    with pytest.raises(ValueError, match="enter_ann"):
        CarryPolicy(enter_ann=0.0, exit_ann=0.05)
    with pytest.raises(ValueError, match="leverage"):
        CarryPolicy(leverage=0.5)
    with pytest.raises(ValueError, match="smooth_days"):
        CarryPolicy(smooth_days=0)


# ── 2. coupe-circuits ───────────────────────────────────────────────────────


def test_drawdown_halt_closes_the_position_and_stops_the_engine(tmp_path):
    runner = _runner(tmp_path, _funding(60, 0.0002), smooth_days=1)
    runner._tick()
    assert runner.in_position, "le funding positif doit avoir ouvert la position"

    # creux au-delà du seuil catastrophe, sans toucher au signal de funding
    runner.equity = runner.peak_equity * 0.5
    runner._tick()

    assert runner.halted
    assert not runner.in_position, "un kill-switch doit fermer au tick courant"
    incidents = {item["fingerprint"] for item in runner.store.read_incidents(open_only=True)}
    assert "execution:carry:kill_switch" in incidents


def test_halted_engine_never_reopens(tmp_path):
    runner = _runner(tmp_path, _funding(60, 0.0002), smooth_days=1)
    runner.halted = True
    runner._tick()
    assert not runner.in_position


def test_daily_loss_lockout_blocks_entry_without_halting(tmp_path):
    runner = _runner(tmp_path, _funding(60, 0.0002), smooth_days=1)
    runner.day = str(pd.Timestamp.now(tz="UTC").date())
    runner.day_start_equity = runner.equity
    runner.equity *= 0.90  # -10 % sur la journée, au-delà de la limite

    runner._tick()

    assert runner.daily_lockout
    assert not runner.halted, "une perte journalière ne doit pas arrêter le moteur"
    assert not runner.in_position


def test_kill_switch_state_survives_a_restart(tmp_path):
    runner = _runner(tmp_path, _funding(60, 0.0002), smooth_days=1)
    runner.halted = True
    runner.peak_equity = 9_999.0
    runner._save_state()

    revived = _runner(tmp_path, _funding(60, 0.0002), smooth_days=1)

    assert revived.halted
    assert revived.peak_equity == pytest.approx(9_999.0)


# ── 3. rattrapage du funding après un arrêt prolongé ────────────────────────


def test_funding_window_starts_at_the_checkpoint_after_a_long_outage(tmp_path):
    """Une fenêtre fixe de `smooth_days` perdait sans alerte tous les paiements
    d'un arrêt plus long qu'elle. Le runner doit repartir du checkpoint."""
    now = pd.Timestamp.now(tz="UTC").floor("h")
    funding = _funding(300, 0.0002, end=now)  # 100 jours
    runner = _runner(tmp_path, funding)
    outage_start = now - pd.Timedelta(days=60)
    runner.last_funding_ts = outage_start

    runner._recent_funding()

    requested = runner.venue.requested_since[-1]
    assert requested <= outage_start, "la demande doit couvrir tout l'arriéré"


def test_no_payment_is_counted_twice_across_ticks(tmp_path):
    now = pd.Timestamp.now(tz="UTC").floor("h")
    runner = _runner(tmp_path, _funding(90, 0.0002, end=now), smooth_days=1)
    runner._tick()
    equity_after_entry = runner.equity

    runner._tick()  # aucun nouveau paiement disponible

    assert runner.equity == pytest.approx(equity_after_entry)


def test_backlog_is_credited_once_when_the_engine_restarts_in_position(tmp_path):
    now = pd.Timestamp.now(tz="UTC").floor("h")
    rate = 0.0002
    funding = _funding(90, rate, end=now)
    runner = _runner(tmp_path, funding, smooth_days=1)
    missed = funding.index[-4]
    _mark_open(runner, missed)
    start_equity = runner.equity

    runner._apply_funding(runner._recent_funding())

    dt_years = elapsed_years_between(funding.index[-4], funding.index[-3])
    entry_notional = start_equity * PAPER_CARRY_POLICY.leverage
    borrow_principal = start_equity * (PAPER_CARRY_POLICY.leverage - 1.0)
    event_pnl = entry_notional * rate - (
        borrow_principal * PAPER_CARRY_POLICY.borrow_rate_ann * dt_years
    )
    expected = start_equity + 3 * event_pnl
    assert runner.equity == pytest.approx(expected, rel=1e-9)
    assert runner.last_funding_ts == funding.index[-1]


def test_backlog_checkpoint_survives_a_crash_between_payments(tmp_path, monkeypatch):
    funding = _funding(12, 0.0002)
    runner = _runner(tmp_path, funding, smooth_days=1)
    _mark_open(runner, funding.index[-4])
    runner._save_state()

    original_apply = runner.store.apply_carry_accounting_event_and_checkpoint
    calls = 0

    def crash_on_second_payment(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("crash simulé")
        return original_apply(*args, **kwargs)

    monkeypatch.setattr(
        runner.store,
        "apply_carry_accounting_event_and_checkpoint",
        crash_on_second_payment,
    )
    with pytest.raises(RuntimeError, match="crash simulé"):
        runner._apply_funding(funding)

    revived = _runner(tmp_path, funding, smooth_days=1)
    assert revived.last_funding_ts == funding.index[-3]
    revived._apply_funding(funding)

    dt_years = elapsed_years_between(funding.index[-4], funding.index[-3])
    entry_notional = PAPER_CARRY_POLICY.capital * PAPER_CARRY_POLICY.leverage
    borrow_principal = PAPER_CARRY_POLICY.capital * (PAPER_CARRY_POLICY.leverage - 1.0)
    event_pnl = entry_notional * 0.0002 - (
        borrow_principal * PAPER_CARRY_POLICY.borrow_rate_ann * dt_years
    )
    expected = PAPER_CARRY_POLICY.capital + 3 * event_pnl
    assert revived.equity == pytest.approx(expected, rel=1e-9)
    assert revived.last_funding_ts == funding.index[-1]


def test_missing_checkpoint_initializes_without_crediting_legacy_history(tmp_path):
    funding = _funding(90, 0.0002)
    runner = _runner(tmp_path, funding, smooth_days=1)
    runner.in_position = True
    initial_equity = runner.equity

    runner._apply_funding(funding)

    assert runner.equity == initial_equity
    assert runner.accounting_uncertain
    assert runner.last_funding_ts is None
    revived = _runner(tmp_path, funding, smooth_days=1)
    assert revived.accounting_uncertain
    assert revived.last_funding_ts is None
