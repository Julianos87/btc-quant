"""Réconciliation état local ↔ exchange (mode live uniquement).

Au démarrage du runner en live, compare la position nette locale (somme des
sous-systèmes) à la position réelle sur l'exchange. Tout écart est signalé
(log + Telegram) : c'est la protection contre les doubles positions ou les
positions orphelines après un crash. Fail-closed : une erreur ou un écart
interdit au runner de démarrer.
"""

from __future__ import annotations

import logging

from ..notify import notify
from .broker import Broker

log = logging.getLogger(__name__)

TOLERANCE_BTC = 1e-5


def reconcile(broker: Broker, slots: list, symbol: str) -> bool:
    """Retourne True si l'état local est cohérent avec l'exchange."""
    if not broker.supports_position_reconciliation:
        return True  # broker papier : rien à réconcilier

    local_net = sum((s.position.direction * s.position.qty) for s in slots if s.position)
    try:
        remote_net = broker.net_position(symbol)
    except Exception as e:
        msg = f"⛔ Réconciliation impossible ({e}) — démarrage live interdit"
        log.error(msg)
        notify(msg)
        return False

    diff = local_net - remote_net
    if abs(diff) <= TOLERANCE_BTC:
        log.info("Réconciliation OK : net local %.6f = exchange %.6f", local_net, remote_net)
        return True

    msg = (
        f"⚠ RÉCONCILIATION : écart détecté ! État local {local_net:+.6f} BTC, "
        f"exchange {remote_net:+.6f} BTC (diff {diff:+.6f}). "
        f"Vérifier manuellement avant de laisser trader."
    )
    log.error(msg)
    notify(msg)
    return False
