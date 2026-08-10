"""Identité et états persistants d'une intention d'ordre externe.

La clé logique décrit la décision financière. Elle est conservée intégralement
en SQLite. L'identifiant d'intention est son empreinte SHA-256 complète et sert
de racine au ``client_order_id`` propre à chaque exchange.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, UTC
from enum import StrEnum


class FinancialTransitionType(StrEnum):
    ENTER_LONG = "ENTER_LONG"
    ENTER_SHORT = "ENTER_SHORT"
    EXIT = "EXIT"
    ADD = "ADD"
    REDUCE = "REDUCE"


def _canonical_timestamp(value: str) -> str:
    candidate = value[:-1] + "+00:00" if value[-1:] in {"Z", "z"} else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return value
    if parsed.tzinfo is None:
        return value
    return parsed.astimezone(UTC).isoformat()


def _canonical_position_generation(value: str) -> str:
    prefix, separator, initial_qty = value.partition("|initial_qty=")
    if not separator or not prefix.startswith("entry="):
        return value
    return f"entry={_canonical_timestamp(prefix.removeprefix('entry='))}|initial_qty={initial_qty}"


class LocalOrderState(StrEnum):
    INTENT_CREATED = "INTENT_CREATED"
    SUBMITTING = "SUBMITTING"
    AWAITING_EXTERNAL = "AWAITING_EXTERNAL"
    PENDING_RECONCILIATION = "PENDING_RECONCILIATION"
    TERMINAL = "TERMINAL"


class ExternalOrderState(StrEnum):
    OPEN = "OPEN"
    PARTIAL_OPEN = "PARTIAL_OPEN"
    FILLED = "FILLED"
    PARTIAL_TERMINAL = "PARTIAL_TERMINAL"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"

    @property
    def is_terminal(self) -> bool:
        return self in {
            ExternalOrderState.FILLED,
            ExternalOrderState.PARTIAL_TERMINAL,
            ExternalOrderState.CANCELED,
            ExternalOrderState.REJECTED,
            ExternalOrderState.EXPIRED,
        }


@dataclass(frozen=True)
class LogicalOrderIdentity:
    """Identité stable d'une transition financière unique.

    ``decision_checkpoint`` doit provenir de l'état métier observé (barre de
    décision, génération de position, seuil de liquidation), jamais de l'heure
    d'exécution du processus.
    """

    engine: str
    slot: str
    decision_checkpoint: str
    transition_type: FinancialTransitionType
    position_generation: str | None = None
    transition_sequence: int = 0

    def __post_init__(self) -> None:
        for name in ("engine", "slot", "decision_checkpoint"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} doit être une chaîne non vide")
            object.__setattr__(self, name, value.strip())
        transition_type = FinancialTransitionType(self.transition_type)
        object.__setattr__(self, "transition_type", transition_type)
        if self.position_generation is not None and not self.position_generation.strip():
            raise ValueError("position_generation doit être non vide lorsqu'elle est fournie")
        if self.position_generation is not None:
            position_generation = _canonical_position_generation(self.position_generation.strip())
            object.__setattr__(self, "position_generation", position_generation)
        decision_checkpoint = _canonical_timestamp(self.decision_checkpoint.strip())
        object.__setattr__(self, "decision_checkpoint", decision_checkpoint)
        position_transition = {
            FinancialTransitionType.EXIT,
            FinancialTransitionType.ADD,
            FinancialTransitionType.REDUCE,
        }
        entry_transition = {
            FinancialTransitionType.ENTER_LONG,
            FinancialTransitionType.ENTER_SHORT,
        }
        if transition_type in position_transition and self.position_generation is None:
            raise ValueError(f"{transition_type.value} exige une position_generation non nulle")
        if transition_type in entry_transition and self.position_generation is not None:
            raise ValueError(f"{transition_type.value} ne doit pas avoir de position_generation")
        if (
            isinstance(self.transition_sequence, bool)
            or not isinstance(self.transition_sequence, int)
            or self.transition_sequence < 0
        ):
            raise ValueError("transition_sequence doit être un entier positif ou nul")

    @property
    def logical_key(self) -> str:
        return json.dumps(
            {
                "decision_checkpoint": self.decision_checkpoint,
                "engine": self.engine,
                "position_generation": self.position_generation,
                "slot": self.slot,
                "transition_sequence": self.transition_sequence,
                "transition_type": self.transition_type.value,
                "version": 1,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )

    @property
    def intent_id(self) -> str:
        digest = hashlib.sha256(self.logical_key.encode("utf-8")).hexdigest()
        return f"btq-mkt-{digest}"
