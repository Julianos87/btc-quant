"""Exceptions métier stables de la couche d'exécution."""


class ExecutionError(RuntimeError):
    """Erreur d'exécution exploitable par les entrypoints."""


class ReconciliationRequired(ExecutionError):
    """État externe ambigu nécessitant une intervention humaine."""


class AmbiguousOrder(ReconciliationRequired, TimeoutError):
    """Le broker ne permet pas de prouver l'état terminal d'un ordre."""


class NonTerminalOrder(ReconciliationRequired):
    """L'exchange n'a pas confirmé un état terminal de l'ordre."""
