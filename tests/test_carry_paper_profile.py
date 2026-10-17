"""The autonomous profile is a hypothetical credit scenario, never live authority."""

from dataclasses import replace
from pathlib import Path

import pytest

from btcquant.config import load_config, carry_execution_from_config, carry_policy_from_config
from btcquant.execution.carry_runner import CarryRunner
from test_audit_carry_paper_regressions import _market, _QualifiedVenue


CONFIG = Path(__file__).resolve().parents[1] / "environments/paper/config.yaml"


def test_simulated_financing_cannot_be_used_in_testnet():
    cfg = load_config(CONFIG)
    cfg["execution"]["mode"] = "testnet"
    with pytest.raises(ValueError, match="réservé"):
        carry_execution_from_config(cfg)


def test_simulated_financing_cannot_attach_external_broker(tmp_path):
    cfg = carry_execution_from_config(load_config(CONFIG))
    with pytest.raises(ValueError, match="broker externe"):
        CarryRunner(carry_execution=cfg, live_broker=object(), state_file=tmp_path / "unused.db")
    assert not (tmp_path / "unused.db").exists()


def test_paper_profile_balances_credit_and_refuses_unprofitable_entry(tmp_path):
    cfg = load_config(CONFIG)
    market = _market()
    runner = CarryRunner(
        policy=carry_policy_from_config(cfg),
        carry_execution=carry_execution_from_config(cfg),
        state_file=tmp_path / "carry.db",
        venue=_QualifiedVenue(market),
        notifier=lambda _: True,
    )
    plan = runner._two_leg_plan(market)
    assert runner.two_leg_spec.qualified and plan.qualified
    assert plan.target_notional == 12000
    assert plan.borrow_principal > 8000  # also finances reserved margin and costs
    assert plan.total_cash_required == pytest.approx(4000)
    runner._open_two_leg(smooth_ann=0.03, market=market, decision_timestamp=market.timestamp)
    assert not runner.in_position
    assert runner.two_leg_journal[-1]["reason"] == "net_expected_non_positive"
    assert runner.two_leg_journal[-1]["economics"]["qualified"] is False
    assert runner._state_payload()["financing_mode"] == "paper_simulated"
    assert runner.equity == 4000


def test_carry_checkpoint_keeps_recent_explanation_without_trimming_event_history(tmp_path):
    cfg = load_config(CONFIG)
    market = _market()
    database = tmp_path / "carry.db"

    def make_runner():
        return CarryRunner(
            policy=carry_policy_from_config(cfg),
            carry_execution=carry_execution_from_config(cfg),
            state_file=database,
            venue=_QualifiedVenue(market),
            notifier=lambda _: True,
        )

    runner = make_runner()
    for sequence in range(20):
        runner._two_leg_persist("carry_observation", {"sequence": sequence})
    runner._two_leg_persist(
        "carry_entry_blocked",
        {
            "reason": "net_expected_non_positive",
            "economics": {"funding_expected": 4.0},
        },
    )
    runner._two_leg_persist("checkpoint", {"reason": "routine"})

    payload = runner.store.load_engine_state("carry")
    journal = payload["two_leg"]["journal"]
    assert len(journal) == 2
    assert any(item.get("economics", {}).get("funding_expected") == 4.0 for item in journal)
    assert journal[-1]["event_type"] == "checkpoint"
    assert len(runner.store.read_events("carry")) >= 22

    restored = make_runner()
    assert len(restored.two_leg_journal) == 2
    assert restored.two_leg_journal[-1]["event_type"] == "checkpoint"
    for sequence in range(150):
        restored._two_leg_persist("carry_observation", {"sequence": sequence})

    resumed = make_runner()
    resumed_journal = resumed.store.load_engine_state("carry")["two_leg"]["journal"]
    assert len(resumed_journal) <= 3
    assert any(
        item.get("reason") == "net_expected_non_positive"
        and item.get("economics", {}).get("funding_expected") == 4.0
        for item in resumed_journal
    )
    assert len(resumed.store.read_events("carry")) >= 172


def test_simulated_financing_requires_explicit_provenance():
    config = carry_execution_from_config(load_config(CONFIG))
    with pytest.raises(ValueError, match="provenance"):
        replace(config, financing_source="HYPERLIQUID_VERIFIED")


def test_paper_3x_entry_interest_restart_and_exit(tmp_path):
    import pandas as pd
    from btcquant.domain.execution import ExecutionConfig

    cfg = load_config(CONFIG)
    market = _market()

    def make_runner():
        return CarryRunner(
            policy=carry_policy_from_config(cfg),
            carry_execution=carry_execution_from_config(cfg),
            state_file=tmp_path / "carry.db",
            venue=_QualifiedVenue(market),
            paper_execution_config=ExecutionConfig(
                fee_rate=0.001,
                slippage_bps=5,
                liquidity_model="order_book",
                max_volume_participation=1.0,
            ),
            notifier=lambda _: True,
        )

    runner = make_runner()
    runner._open_two_leg(smooth_ann=1.0, market=market, decision_timestamp=market.timestamp)
    assert runner.in_position
    assert runner.spot_qty == pytest.approx(abs(runner.perp_qty))
    assert runner.borrow_principal > 8000
    after = market.timestamp + pd.Timedelta(hours=1)
    funding = pd.Series([0.0001], index=[after])
    runner._apply_two_leg_funding(funding)
    assert runner.two_leg_balance.accrued_interest > 0
    restored = make_runner()
    assert restored.in_position and not restored.accounting_uncertain
    equity = restored.equity
    restored._apply_two_leg_funding(funding)
    assert restored.equity == pytest.approx(equity)  # no duplicate funding after restart
    restored._close_two_leg(
        reason="funding_exit", market=_market(timestamp=str(after)), decision_timestamp=after
    )
    assert not restored.in_position
    assert restored.borrow_principal == pytest.approx(0)
