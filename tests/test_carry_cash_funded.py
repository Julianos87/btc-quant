"""Cash-funded Carry must reserve margin without inventing a loan."""

from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

from btcquant.config import (
    CarryExecutionConfig,
    carry_execution_from_config,
    carry_policy_from_config,
    load_config,
)
from btcquant.domain.execution import ExecutionConfig
from btcquant.execution.carry_paper import CarryAccountingState, CarryPaperError, check_margin
from btcquant.execution.carry_runner import CarryRunner
from test_audit_carry_paper_regressions import _market, _QualifiedVenue

CONFIG = Path(__file__).resolve().parents[1] / "environments/paper/carry_cash.yaml"


def make_runner(tmp_path):
    cfg = load_config(CONFIG)
    return CarryRunner(
        policy=carry_policy_from_config(cfg),
        carry_execution=carry_execution_from_config(cfg),
        state_file=tmp_path / "cash.db",
        venue=_QualifiedVenue(_market()),
        paper_execution_config=ExecutionConfig(
            fee_rate=0.0005, liquidity_model="order_book", max_volume_participation=1.0
        ),
        notifier=lambda _: True,
    )


def test_cash_plan_reserves_margin_and_buffer_and_scales_to_losses(tmp_path):
    runner = make_runner(tmp_path)
    assert runner.two_leg_spec.qualified
    plan = runner._two_leg_plan(_market())
    assert plan.qualified and plan.borrow_principal == 0
    assert plan.spot_qty == plan.perp_qty
    assert plan.spot_qty / 0.00001 == pytest.approx(round(plan.spot_qty / 0.00001))
    assert plan.spot_notional >= 10 and plan.perp_notional >= 10
    assert plan.spot_fee_rate == pytest.approx(0.001)
    assert plan.perp_fee_rate == pytest.approx(0.0005)
    expected_entry_cost = plan.spot_notional * (0.001 + 5 / 10_000) + plan.perp_notional * (
        0.0005 + 5 / 10_000
    )
    assert plan.estimated_entry_cost == pytest.approx(expected_entry_cost)
    assert plan.target_notional < 4000
    assert plan.total_cash_required == pytest.approx(3920)
    assert plan.perp_initial_margin > 0 and plan.estimated_entry_cost > 0
    runner.two_leg_balance.cash_available = 2000
    smaller = runner._two_leg_plan(_market())
    assert smaller.target_notional == pytest.approx(plan.target_notional / 2)
    assert smaller.total_cash_required == pytest.approx(1960)
    assert smaller.borrow_principal == 0


def test_cash_entry_funding_restart_exit_never_creates_debt(tmp_path):
    runner = make_runner(tmp_path)
    market = _market()
    runner._open_two_leg(smooth_ann=1.0, market=market, decision_timestamp=market.timestamp)
    assert runner.in_position and not runner.accounting_uncertain
    assert runner.borrow_principal == 0
    assert runner.spot_qty == pytest.approx(runner.perp_qty)
    ts = market.timestamp + pd.Timedelta(hours=1)
    funding = pd.Series([0.0001], index=[ts])
    runner._apply_two_leg_funding(funding)
    assert runner.two_leg_balance.accrued_interest == 0
    restored = make_runner(tmp_path)
    assert restored.in_position and restored.borrow_principal == 0
    equity = restored.equity
    restored._apply_two_leg_funding(funding)
    assert restored.equity == pytest.approx(equity)
    restored._close_two_leg(
        reason="funding_exit", market=_market(timestamp=str(ts)), decision_timestamp=ts
    )
    assert not restored.in_position and restored.borrow_principal == 0
    assert restored.two_leg_balance.cash_locked == 0
    assert not restored.accounting_uncertain


def test_cash_refuses_net_loss_without_erasing_reserve(tmp_path):
    runner = make_runner(tmp_path)
    market = _market()
    runner._open_two_leg(smooth_ann=0.0784, market=market, decision_timestamp=market.timestamp)
    assert not runner.in_position and runner.borrow_principal == 0
    economics = runner.two_leg_journal[-1]["economics"]
    assert economics["borrow_expected"] == 0
    assert economics["uncertainty_reserve"] > 0
    assert economics["net_expected"] < 0


def test_cash_short_margin_survives_twenty_percent_price_rise(tmp_path):
    runner = make_runner(tmp_path)
    market = _market()
    runner._open_two_leg(smooth_ann=1, market=market, decision_timestamp=market.timestamp)
    margin = check_margin(
        runner.two_leg_balance,
        perp_mark=market.perp_mark * 1.2,
        initial_margin_rate=0.50,
        maintenance_margin_rate=0.05,
    )
    assert margin.qualified and not margin.liquidatable


def test_unexpected_fill_cost_cannot_create_unauthorized_debt():
    balance = CarryAccountingState(initial_cash=100)
    with pytest.raises(CarryPaperError, match="capacité d'emprunt"):
        balance.apply_spot_fill(
            side="BUY", qty=2, price=100, fee=1, event_id="expensive", max_debt_principal=0
        )
    assert balance.cash_available == 100 and balance.debt_principal == 0
    assert balance.spot_qty == 0 and balance.applied_event_ids == []


@pytest.mark.parametrize("value", [0, -0.1, 1.1, float("nan"), float("inf")])
def test_cash_utilization_is_bounded(value):
    with pytest.raises(ValueError, match="cash_utilization"):
        CarryExecutionConfig(cash_utilization=value)


def test_disabled_borrow_rejects_credit_capacity():
    with pytest.raises(ValueError, match="max_borrow"):
        CarryExecutionConfig(borrow_enabled=False, max_borrow=1)


def test_cash_profile_preserves_trend_budget_and_parameters():
    cash = load_config(CONFIG)
    original = load_config(CONFIG.with_name("config.yaml"))
    for section in ("portfolio", "risk", "strategies", "execution", "costs"):
        assert cash[section] == original[section]
    assert cash["carry"]["leverage"] == 1
    assert cash["carry_execution"]["max_borrow"] == 0
    assert cash["carry_execution"]["uncertainty_reserve_rate"] == 0.02
    with pytest.raises(ValueError, match="provenance"):
        replace(carry_execution_from_config(cash), financing_source="REAL_ACCOUNT")


def test_cash_policy_keeps_spot_and_perp_fees_distinct():
    cfg = load_config(CONFIG)
    policy = carry_policy_from_config(cfg)
    assert policy.fee_rate == pytest.approx(0.0005)  # legacy single-rate replay stays stable
    assert policy.spot_execution_fee_rate == pytest.approx(0.001)
    assert policy.perp_execution_fee_rate == pytest.approx(0.0005)


def test_cash_market_gate_checks_instrument_identity_and_source_freshness(tmp_path):
    runner = make_runner(tmp_path)
    current = replace(
        _market(),
        spot_market_id="@142",
        spot_base_name="UBTC",
        spot_quote_asset="USDC",
        spot_token_index=197,
        spot_token_id="0x9fdbda0a5e284c32744d2f17ee5c74b284993463",
        spot_is_canonical=False,
        perp_market_id="0",
        perp_base_name="BTC",
        perp_quote_asset="USDC",
        perp_settle_asset="USDC",
        spot_quantity_step=0.00001,
        perp_quantity_step=0.00001,
        spot_min_notional=10.0,
        perp_min_notional=10.0,
    )
    runner.venue = _QualifiedVenue(current)
    assert runner._two_leg_market() == current

    runner.venue = _QualifiedVenue(replace(current, spot_base_name="BTC"))
    assert runner._two_leg_market() is None

    runner.venue = _QualifiedVenue(
        replace(current, spot_token_id="0x00000000000000000000000000000000")
    )
    assert runner._two_leg_market() is None

    runner.venue = _QualifiedVenue(replace(current, freshness_seconds=15.01))
    assert runner._two_leg_market() is None

    stale = replace(current, freshness_seconds=15.01)
    runner.venue = _QualifiedVenue(stale)
    runner.in_position = True
    assert runner._two_leg_market() == stale


def test_cash_entry_cannot_bypass_freshness_gate(tmp_path):
    runner = make_runner(tmp_path)
    market = replace(_market(), freshness_seconds=15.01)
    runner._open_two_leg(smooth_ann=1.0, market=market, decision_timestamp=market.timestamp)
    assert not runner.in_position
    assert runner.two_leg_journal[-1]["reason"] == "stale_market_data"
