"""Sortie console tolérante à l'encodage de l'hôte.

Le dépôt écrit en français et utilise des caractères hors Latin-1 (flèches,
filets, symboles d'état). Sur une console Windows en CP1252 — le poste de
développement — un simple `print` d'un `→` fait planter le script avec un
``UnicodeEncodeError``, après avoir éventuellement déjà produit des effets.

Deux réponses coexistaient : les services systemd imposaient
``PYTHONIOENCODING=utf-8``, et la CLI de readiness translittérait quelques
caractères à la main. Ce module est la réponse unique : il force UTF-8 sur les
flux standards, et remplace les caractères non représentables au lieu
d'interrompre le programme.
"""

from __future__ import annotations

import sys
from typing import IO, Any


def _reconfigure(stream: IO[Any] | None) -> None:
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:  # flux redirigé vers un objet sans encodage (tests)
        return
    try:
        reconfigure(encoding="utf-8", errors="replace")
    except (ValueError, OSError):
        # Flux déjà détaché ou non reconfigurable : mieux vaut un affichage
        # dégradé qu'une erreur au démarrage d'un outil de diagnostic.
        pass


def enable_utf8_output() -> None:
    """Rend `stdout`/`stderr` sûrs pour du texte français accentué et symbolique.

    Idempotent, sans effet si les flux sont déjà en UTF-8.
    """

    _reconfigure(sys.stdout)
    _reconfigure(sys.stderr)
