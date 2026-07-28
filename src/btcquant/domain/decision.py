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
        if not can_enter or halted:
            return BarDecision(position=None)
        direction = int(strategy.entry_signal(row))
        if direction not in (-1, 0, 1):
            raise ValueError(f"Direction de signal invalide : {direction!r}")
        if direction == -1 and not allow_short:
            direction = 0
        events: tuple[DecisionEvent, ...] = (
            (EntryRequested(Direction(direction)),) if direction else ()
        )
        return BarDecision(position=None, events=events)

    if position.direction not in (-1, 1):
        raise ValueError(f"Direction de position invalide : {position.direction!r}")

    close = float(row["close"])
    updated = replace(position)
    updated.bars_held += 1
    if updated.direction == 1:
        updated.best_close = max(updated.best_close, close)
    else:
        updated.best_close = min(updated.best_close, close)

    mutable_events: list[DecisionEvent] = []
    if funding_rate:
        amount = funding_amount(updated, float(funding_rate), close)
        mutable_events.append(FundingAccrued(rate=float(funding_rate), amount=amount))

    proposed_stop = strategy.trailing_stop(row, updated)
    if proposed_stop is not None:
        stop_tightened = (updated.direction == 1 and proposed_stop > updated.stop_price) or (
            updated.direction == -1 and proposed_stop < updated.stop_price
        )
        if stop_tightened:
            previous_stop = updated.stop_price
            updated.stop_price = float(proposed_stop)
            mutable_events.append(
                StopTightened(previous_price=previous_stop, new_price=updated.stop_price)
            )

    if halted:
        mutable_events.append(ExitRequested(reason="kill_switch"))
    elif strategy.exit_signal(row, updated):
        mutable_events.append(ExitRequested(reason="signal"))
    else:
        pyramid_fraction = float(strategy.pyramid_fraction(row, updated))
        if pyramid_fraction < 0 or pyramid_fraction > 1:
            raise ValueError("Fraction de renfort invalide")
        if pyramid_fraction:
            mutable_events.append(PyramidRequested(fraction=pyramid_fraction))

    return BarDecision(position=updated, events=tuple(mutable_events))
