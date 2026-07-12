"""Réconciliation état local ↔ exchange (mode live uniquement).

Au démarrage du runner en live, compare la position nette locale (somme des
sous-systèmes) à la position réelle sur l'exchange. Tout écart est signalé
(log + Telegram) : c'est la protection contre les doubles positions ou les
positions orphelines après un crash. Non bloquant : on alerte, l'humain tranche.
"""

from __future__ import annotations

import logging

from ..notify import notify
from .broker import Broker

log = logging.getLogger(__name__)

TOLERANCE_BTC = 1e-5


def reconcile(broker: Broker, slots: list, symbol: str) -> bool:
    """Retourne True si l'état local est cohérent avec l'exchange."""
    exchange = getattr(broker, "exchange", None)
    if exchange is None:
        return True  # broker papier : rien à réconcilier

    local_net = sum(
        (s.position.direction * s.position.qty) for s in slots if s.position
    )
    try:
        positions = exchange.fetch_positions([symbol])
        remote_net = 0.0
        for p in positions:
            qty = float(p.get("contracts") or 0.0)
            side = p.get("side")
            remote_net += qty if side == "long" else -qty if side == "short" else 0.0
    except Exception as e:
        log.warning("Réconciliation impossible (%s) — à vérifier manuellement", e)
        return True

    diff = local_net - remote_net
    if abs(diff) <= TOLERANCE_BTC:
        log.info("Réconciliation OK : net local %.6f = exchange %.6f", local_net, remote_net)
        return True

    msg = (f"⚠ RÉCONCILIATION : écart détecté ! État local {local_net:+.6f} BTC, "
           f"exchange {remote_net:+.6f} BTC (diff {diff:+.6f}). "
           f"Vérifier manuellement avant de laisser trader.")
    log.error(msg)
    notify(msg)
    return False
