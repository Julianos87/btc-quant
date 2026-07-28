"""Décisions de clôture de barre, indépendantes de l'infrastructure.

Ce module ne passe aucun ordre et ne persiste rien. À entrées identiques, il
produit le même nouvel état et les mêmes événements. Le backtest et le runner
paper/live peuvent donc partager les règles métier tout en conservant leurs
adaptateurs d'exécution respectifs.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TypeAlias

import pandas as pd

from ..strategies.base import Direction, Position, Strategy


@dataclass(frozen=True)
class EntryRequested:
    """La stratégie demande l'ouverture d'une position."""

    direction: Direction


@dataclass(frozen=True)
class ExitRequested:
    """La position doit être fermée par l'adaptateur d'exécution."""

    reason: str


@dataclass(frozen=True)
class StopTightened:
    """Le stop peut être rapproché, mais jamais éloigné du prix."""

    previous_price: float
    new_price: float


@dataclass(frozen=True)
class PyramidRequested:
    """Ajout demandé, exprimé en fraction de la tranche initiale."""

    fraction: float


@dataclass(frozen=True)
class FundingAccrued:
    """Coût signé du funding pour la barre (négatif = crédit)."""

    rate: float
    amount: float


DecisionEvent: TypeAlias = (
    EntryRequested | ExitRequested | StopTightened | PyramidRequested | FundingAccrued
)


def funding_amount(position: Position, rate: float, mark_price: float) -> float:
    """Montant signé d'un paiement : positif = coût, négatif = crédit."""

    return position.direction * position.qty * mark_price * rate


@dataclass(frozen=True)
class BarDecision:
    """Résultat immuable d'une décision de clôture de barre."""

    position: Position | None
    events: tuple[DecisionEvent, ...] = ()


def _decide_entry(
    strategy: Strategy,
    row: pd.Series,
    *,
    halted: bool,
    can_enter: bool,
    allow_short: bool,
) -> BarDecision:
    if not can_enter or halted:
        return BarDecision(position=None)
    direction = int(strategy.entry_signal(row))
    if direction not in (-1, 0, 1):
        raise ValueError(f"Direction de signal invalide : {direction!r}")
    if direction == -1 and not allow_short:
        direction = 0
    events: tuple[DecisionEvent, ...] = (EntryRequested(Direction(direction)),) if direction else ()
    return BarDecision(position=None, events=events)


def _advance_position(position: Position, close: float) -> Position:
    if position.direction not in (-1, 1):
        raise ValueError(f"Direction de position invalide : {position.direction!r}")
    updated = replace(position)
    updated.bars_held += 1
    if updated.direction == 1:
        updated.best_close = max(updated.best_close, close)
    else:
        updated.best_close = min(updated.best_close, close)
    return updated


def _append_tighter_stop(
    strategy: Strategy,
    row: pd.Series,
    position: Position,
    events: list[DecisionEvent],
) -> None:
    proposed = strategy.trailing_stop(row, position)
    if proposed is None:
        return
    tighter = (position.direction == 1 and proposed > position.stop_price) or (
        position.direction == -1 and proposed < position.stop_price
    )
    if not tighter:
        return
    previous = position.stop_price
    position.stop_price = float(proposed)
    events.append(StopTightened(previous_price=previous, new_price=position.stop_price))


def _append_exit_or_pyramid(
    strategy: Strategy,
    row: pd.Series,
    position: Position,
    events: list[DecisionEvent],
    *,
    halted: bool,
) -> None:
    if halted:
        events.append(ExitRequested(reason="kill_switch"))
        return
    if strategy.exit_signal(row, position):
        events.append(ExitRequested(reason="signal"))
        return
    fraction = float(strategy.pyramid_fraction(row, position))
    if fraction < 0 or fraction > 1:
        raise ValueError("Fraction de renfort invalide")
    if fraction:
        events.append(PyramidRequested(fraction=fraction))


def decide_bar_close(
    strategy: Strategy,
    row: pd.Series,
    position: Position | None,
    *,
    funding_rate: float = 0.0,
    halted: bool = False,
    can_enter: bool = True,
    allow_short: bool = True,
) -> BarDecision:
    """Évalue une barre clôturée sans modifier la position fournie.

    ``funding_rate`` est le taux déjà ramené à la durée de la barre. Son coût
    est positif pour un long qui paie un taux positif et négatif pour un short.
    La priorité de sortie est volontairement donnée au coupe-circuit.
    """

    if position is None:
        return _decide_entry(
            strategy,
            row,
            halted=halted,
            can_enter=can_enter,
            allow_short=allow_short,
        )

    close = float(row["close"])
    updated = _advance_position(position, close)
    mutable_events: list[DecisionEvent] = []
    if funding_rate:
        amount = funding_amount(updated, float(funding_rate), close)
        mutable_events.append(FundingAccrued(rate=float(funding_rate), amount=amount))
    _append_tighter_stop(strategy, row, updated, mutable_events)
    _append_exit_or_pyramid(strategy, row, updated, mutable_events, halted=halted)
    return BarDecision(position=updated, events=tuple(mutable_events))
