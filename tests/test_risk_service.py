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


# ── types aux frontières ────────────────────────────────────────────────────
# Bug de production trouvé le 27/07/2026 : l'équity arrive en `numpy.float64`
# (elle dérive d'un prix lu dans une Series pandas). `np.float64` hérite de
# `float` et traverse JSON sans bruit, mais `np.bool_` n'hérite PAS de `bool` :
# `halted` devenait donc non sérialisable dès la première position ouverte, et
# CHAQUE checkpoint échouait ensuite en silence, avalé par la boucle principale.


def test_transition_flags_are_plain_python_bools_even_with_numpy_equity():
    import json

    import numpy as np

    service = PortfolioRiskService(RiskConfig(initial_capital=10_000.0))
    transition = service.evaluate(
        PortfolioRiskState(peak_equity=10_000.0, day=None, day_start_equity=10_000.0),
        equity=np.float64(10_050.0),
        day="2026-07-27",
    )

    assert type(transition.state.halted) is bool
    assert type(transition.state.daily_lockout) is bool
    assert type(transition.state.peak_equity) is float
    assert type(transition.state.day_start_equity) is float
    json.dumps(
        {
            "halted": transition.state.halted,
            "daily_lockout": transition.state.daily_lockout,
            "peak_equity": transition.state.peak_equity,
        }
    )


def test_numpy_equity_still_triggers_the_halt():
    """La normalisation de type ne doit pas neutraliser le coupe-circuit."""
    import numpy as np

    service = PortfolioRiskService(RiskConfig(initial_capital=10_000.0, max_drawdown_halt=0.20))
    transition = service.evaluate(
        PortfolioRiskState(peak_equity=10_000.0, day="2026-07-27", day_start_equity=10_000.0),
        equity=np.float64(7_000.0),
        day="2026-07-27",
    )

    assert transition.state.halted is True
    assert transition.halt_triggered is True
