"""Politique pure de risque portefeuille, sans ordre ni notification."""

from __future__ import annotations

from dataclasses import dataclass

from ..risk import RiskConfig


@dataclass(frozen=True)
class PortfolioRiskState:
    peak_equity: float
    day: str | None
    day_start_equity: float
    halted: bool = False
    daily_lockout: bool = False


@dataclass(frozen=True)
class PortfolioRiskTransition:
    state: PortfolioRiskState
    equity: float
    halt_triggered: bool
    lockout_triggered: bool


class PortfolioRiskService:
    def __init__(self, config: RiskConfig) -> None:
        self.config = config

    def evaluate(
        self,
        state: PortfolioRiskState,
        *,
        equity: float,
        day: str,
    ) -> PortfolioRiskTransition:
        peak = max(state.peak_equity, equity)
        new_day = day != state.day
        day_start = equity if new_day else state.day_start_equity
        previous_lockout = False if new_day else state.daily_lockout

        drawdown_breached = equity < peak * (1.0 - self.config.max_drawdown_halt)
        halted = state.halted or drawdown_breached
        halt_triggered = halted and not state.halted

        daily_breached = self.config.daily_loss_limit is not None and equity < day_start * (
            1.0 - self.config.daily_loss_limit
        )
        lockout = previous_lockout or daily_breached
        lockout_triggered = lockout and not previous_lockout

        return PortfolioRiskTransition(
            state=PortfolioRiskState(
                peak_equity=peak,
                day=day,
                day_start_equity=day_start,
                halted=halted,
                daily_lockout=lockout,
            ),
            equity=equity,
            halt_triggered=halt_triggered,
            lockout_triggered=lockout_triggered,
        )
