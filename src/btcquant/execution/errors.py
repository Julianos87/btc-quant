"""Exceptions métier stables de la couche d'exécution."""


class ExecutionError(RuntimeError):
    """Erreur d'exécution exploitable par les entrypoints."""


class ReconciliationRequired(ExecutionError):
    """État externe ambigu nécessitant une intervention humaine."""


class RemotePositionUnavailable(ExecutionError):
    """La position distante ne peut pas être établie avec certitude."""
