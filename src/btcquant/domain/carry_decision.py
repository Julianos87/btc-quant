"""Noyau pur de décision du cash-and-carry.

La décision est prise juste après l'observation d'un paiement de funding. Elle
modifie donc la position qui sera exposée au paiement suivant : le backtest
décale cet état d'une période, tandis que le runner comptabilise le paiement
courant avant d'appeler cette fonction.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum


class CarryAction(StrEnum):
    HOLD = "HOLD"
    OPEN = "OPEN"
    CLOSE = "CLOSE"


@dataclass(frozen=True)
class CarryDecision:
    """Transition déterministe, sans I/O ni connaissance du broker."""

    in_position: bool
    action: CarryAction
    reason: str | None = None


def decide_carry_payment(
    *,
    in_position: bool,
    smooth_ann: float,
    enter_ann: float,
    exit_ann: float,
    halted: bool = False,
    entry_blocked: bool = False,
) -> CarryDecision:
    """Décide la position applicable au prochain paiement de funding.

    Les comparaisons restent strictes : égalité au seuil = maintien. Un signal
    indisponible (`NaN`) ne doit jamais provoquer une transition. Le lockout
    journalier bloque seulement une nouvelle entrée ; une sortie de funding
    reste toujours autorisée.
    """

    if halted:
        if in_position:
            return CarryDecision(False, CarryAction.CLOSE, "kill_switch")
        return CarryDecision(False, CarryAction.HOLD)

    if math.isnan(smooth_ann):
        return CarryDecision(in_position, CarryAction.HOLD)

    if in_position and smooth_ann < exit_ann:
        return CarryDecision(False, CarryAction.CLOSE, "funding_exit")
    if not in_position and not entry_blocked and smooth_ann > enter_ann:
        return CarryDecision(True, CarryAction.OPEN, "funding_entry")
    return CarryDecision(in_position, CarryAction.HOLD)
