from __future__ import annotations

import pandas as pd
import pytest

from btcquant.execution.broker import Fill
from btcquant.execution.position_accounting import PositionAccountingService
from btcquant.strategies.base import Direction, Position


def _position(direction=1):
    return Position(
        entry_time=pd.Timestamp("2030-01-01T00:00:00Z"),
        entry_price=100,
        qty=2,
        stop_price=90 if direction == 1 else 110,
        direction=direction,
    )


def test_position_normalizes_and_rejects_invalid_direction():
    assert _position().direction is Direction.LONG
    with pytest.raises(ValueError, match="3 is not a valid Direction"):
        Position(
            entry_time=pd.Timestamp("2026-01-01", tz="UTC"),
            entry_price=100.0,
            qty=1.0,
            stop_price=90.0,
            direction=3,
        )


def test_entry_records_fee_and_position_without_mutating_cash():
    result = PositionAccountingService.open_position(
        Fill(101, 2, 1.5),
        entry_time=pd.Timestamp("2030-01-01T01:00:00Z"),
        stop_price=90,
        direction=1,
    )

    assert result.position.entry_price == 101
    assert result.position.qty == 2
    assert result.cash_delta == -1.5
    assert result.entry_fee == 1.5


@pytest.mark.parametrize(
    "direction, exit_price, expected",
    [(1, 110, 18.0), (-1, 90, 18.0)],
)
def test_full_exit_is_symmetric_and_includes_both_fees(direction, exit_price, expected):
    result = PositionAccountingService.close_position(
        _position(direction),
        Fill(exit_price, 2, 1),
        entry_fee=1,
    )

    assert result.cash_delta == 19
    assert result.trade_pnl == expected
    assert result.remaining_position is None
    assert result.remaining_entry_fee == 0


def test_partial_exit_prorates_entry_fee_and_preserves_original_position():
    position = _position()
    result = PositionAccountingService.close_position(
        position,
        Fill(110, 0.5, 0.25),
        entry_fee=2,
    )

    assert position.qty == 2
    assert result.trade_pnl == pytest.approx(4.25)
    assert result.remaining_position is not None
    assert result.remaining_position.qty == pytest.approx(1.5)
    assert result.remaining_entry_fee == pytest.approx(1.5)


def test_overfill_is_rejected():
    with pytest.raises(ValueError, match="incompatible"):
        PositionAccountingService.close_position(
            _position(),
            Fill(110, 3, 1),
            entry_fee=1,
        )
