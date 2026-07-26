"""Calculs purs de comptabilité des positions et fills."""

from __future__ import annotations

from dataclasses import dataclass, replace

import pandas as pd

from ..strategies.base import Direction, Position
from .broker import Fill


@dataclass(frozen=True)
class EntryAccounting:
    position: Position
    cash_delta: float
    entry_fee: float


@dataclass(frozen=True)
class ExitAccounting:
    cash_delta: float
    trade_pnl: float
    entry_fee_share: float
    remaining_entry_fee: float
    remaining_position: Position | None

    @property
    def partial(self) -> bool:
        return self.remaining_position is not None


class PositionAccountingService:
    @staticmethod
    def open_position(
        fill: Fill,
        *,
        entry_time: pd.Timestamp,
        stop_price: float,
        direction: int,
    ) -> EntryAccounting:
        if fill.qty <= 0:
            raise ValueError("Un fill nul ne peut pas ouvrir une position")
        position = Position(
            entry_time=entry_time,
            entry_price=fill.price,
            qty=fill.qty,
            stop_price=stop_price,
            direction=Direction(direction),
            best_close=fill.price,
        )
        return EntryAccounting(position, cash_delta=-fill.fee, entry_fee=fill.fee)

    @staticmethod
    def close_position(
        position: Position,
        fill: Fill,
        *,
        entry_fee: float,
    ) -> ExitAccounting:
        if fill.qty <= 0 or fill.qty > position.qty + 1e-9:
            raise ValueError("Quantité de sortie incompatible avec la position")
        cash_delta = position.direction * fill.qty * (fill.price - position.entry_price) - fill.fee
        entry_fee_share = entry_fee * (fill.qty / position.qty)
        remaining_qty = position.qty - fill.qty
        remaining = replace(position, qty=remaining_qty) if remaining_qty > 1e-9 else None
        return ExitAccounting(
            cash_delta=cash_delta,
            trade_pnl=cash_delta - entry_fee_share,
            entry_fee_share=entry_fee_share,
            remaining_entry_fee=entry_fee - entry_fee_share if remaining else 0.0,
            remaining_position=remaining,
        )
