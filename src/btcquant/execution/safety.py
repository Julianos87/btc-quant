"""Gardes centrales empêchant toute émission d'ordre externe non qualifiée.

Ce verrou ne doit être retiré qu'avec le futur journal d'ordres transactionnel,
la reprise sur incident et une validation testnet formalisée.
"""

import hmac
import os
from pathlib import Path

from .readiness import require_passed_qualification
from .state_store import StateStore

TESTNET_CONFIRMATION = "I_ACCEPT_TESTNET_ORDERS"


def require_live_execution_enabled(
    *,
    testnet: bool,
    state_path: str | Path = "state/btcquant.db",
) -> None:
    if not testnet:
        raise RuntimeError("SÉCURITÉ : l'exécution avec argent réel reste désactivée")
    try:
        require_passed_qualification(StateStore(state_path))
    except RuntimeError as error:
        raise RuntimeError(f"Safety Baseline — {error}") from error
    confirmation = os.environ.get("BTCQUANT_ENABLE_TESTNET", "")
    if not hmac.compare_digest(confirmation, TESTNET_CONFIRMATION):
        raise RuntimeError(
            "SÉCURITÉ : qualification valide, mais testnet non confirmé ; définir "
            f"BTCQUANT_ENABLE_TESTNET={TESTNET_CONFIRMATION} pour cette session uniquement"
        )
