"""Exceptions métier stables de la couche d'exécution."""


class ExecutionError(RuntimeError):
    """Erreur d'exécution exploitable par les entrypoints."""


class ReconciliationRequired(ExecutionError):
    """État externe ambigu nécessitant une intervention humaine."""


class FinancialTransitionAlreadyReserved(ReconciliationRequired):
    """Une transition identique possède déjà une intention durable."""

    def __init__(
        self,
        logical_order_key: str,
        order_id: int,
        local_state: str,
        external_state: str | None,
    ) -> None:
        self.logical_order_key = logical_order_key
        self.order_id = order_id
        self.local_state = local_state
        self.external_state = external_state
        super().__init__(
            f"Transition financière déjà réservée par l'ordre {order_id} "
            f"(local={local_state}, externe={external_state or 'NON_OBSERVÉ'})"
        )


class OrderIdentityCollision(ExecutionError):
    """Une empreinte d'intention désigne deux clés logiques différentes."""


class AccountingIdentityCollision(ExecutionError):
    """Une clé de funding existante désigne des données comptables différentes."""

    def __init__(self, event_key: str, detail: str) -> None:
        self.event_key = event_key
        self.detail = detail
        super().__init__(f"Collision d'identité comptable pour {event_key}: {detail}")


class InvalidOrderStateTransition(ExecutionError):
    """Une mise à jour tenterait d'inventer ou de perdre un état externe."""


class EngineInstanceAlreadyRunning(ExecutionError):
    """Le verrou OS indique qu'une autre instance du moteur est active."""


class MigrationRequiredError(ExecutionError):
    """La base est ancienne et exige une migration explicitement autorisée."""

    def __init__(self, database: str, current_version: int | None, target_version: int) -> None:
        self.database = database
        self.current_version = current_version
        self.target_version = target_version
        current = "inconnue" if current_version is None else str(current_version)
        super().__init__(
            f"Migration explicite requise pour {database}: "
            f"schéma actuel={current}, cible={target_version}"
        )
