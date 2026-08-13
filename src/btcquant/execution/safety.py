"""Gardes centrales empêchant toute émission d'ordre externe non qualifiée.

Ce verrou ne doit être retiré qu'avec le futur journal d'ordres transactionnel,
la reprise sur incident et une validation testnet formalisée.
"""

import hmac
import os
import sqlite3
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
        require_passed_qualification(StateStore(state_path, initialize=False, read_only=True))
    except (FileNotFoundError, PermissionError, OSError, sqlite3.Error, RuntimeError) as error:
        raise RuntimeError(
            f"Safety Baseline — qualification state unavailable or invalid: {error}"
        ) from error
    confirmation = os.environ.get("BTCQUANT_ENABLE_TESTNET", "")
    if not hmac.compare_digest(confirmation, TESTNET_CONFIRMATION):
        raise RuntimeError(
            "SÉCURITÉ : qualification valide, mais testnet non confirmé ; définir "
            f"BTCQUANT_ENABLE_TESTNET={TESTNET_CONFIRMATION} pour cette session uniquement"
        )
