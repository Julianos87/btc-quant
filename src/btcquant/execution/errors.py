"""Exceptions métier stables de la couche d'exécution."""


class ExecutionError(RuntimeError):
    """Erreur d'exécution exploitable par les entrypoints."""


class ReconciliationRequired(ExecutionError):
    """État externe ambigu nécessitant une intervention humaine."""
