from __future__ import annotations

from btcquant.execution.risk_service import PortfolioRiskService, PortfolioRiskState
from btcquant.risk import RiskConfig


def _service() -> PortfolioRiskService:
    return PortfolioRiskService(
        RiskConfig(
            initial_capital=1_000,
            max_drawdown_halt=0.20,
            daily_loss_limit=0.05,
        )
    )


def test_new_peak_and_new_day_are_recorded_without_trigger():
    result = _service().evaluate(
        PortfolioRiskState(1_000, "2030-01-01", 1_000),
        equity=1_100,
        day="2030-01-02",
    )

    assert result.state.peak_equity == 1_100
    assert result.state.day_start_equity == 1_100
    assert not result.halt_triggered
    assert not result.lockout_triggered


def test_drawdown_halts_once_and_halt_is_sticky():
    service = _service()
    first = service.evaluate(
        PortfolioRiskState(1_000, "2030-01-01", 1_000),
        equity=790,
        day="2030-01-01",
    )
    second = service.evaluate(first.state, equity=900, day="2030-01-02")

    assert first.halt_triggered
    assert first.state.halted
    assert not second.halt_triggered
    assert second.state.halted


def test_daily_lockout_resets_next_utc_day():
    service = _service()
    first = service.evaluate(
        PortfolioRiskState(1_000, "2030-01-01", 1_000),
        equity=949,
        day="2030-01-01",
    )
    second = service.evaluate(first.state, equity=960, day="2030-01-02")

    assert first.lockout_triggered
    assert first.state.daily_lockout
    assert not second.state.daily_lockout
