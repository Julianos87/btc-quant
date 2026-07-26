"""Notifications Telegram (optionnelles).

Activation : définir TELEGRAM_BOT_TOKEN et TELEGRAM_CHAT_ID dans
l'environnement (fichier .env sur le VPS). Sans ces variables, notify()
est un no-op silencieux — le bot fonctionne normalement sans Telegram.

Création du bot : parler à @BotFather sur Telegram (/newbot), récupérer le
token ; envoyer un message au bot puis lire le chat_id via
https://api.telegram.org/bot<TOKEN>/getUpdates

Usage CLI (watchdog, scripts) : python -m btcquant.notify "message"
"""

from __future__ import annotations

import json
import logging
import os
import sys
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)


def notify(text: str) -> bool:
    """Envoie `text` sur Telegram. Retourne False si non configuré ou en échec."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False
    try:
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.load(resp).get("ok", False)
    except Exception as e:  # une notification ratée ne doit jamais casser le bot
        # Ne jamais sérialiser l'exception complète : urllib peut y inclure
        # l'URL, qui contient le token Telegram.
        log.warning("Notification Telegram échouée (%s)", type(e).__name__)
        return False


if __name__ == "__main__":
    message = " ".join(sys.argv[1:]) or "(message vide)"
    ok = notify(message)
    print("envoyé" if ok else "non envoyé (TELEGRAM_* non configurés ou erreur)")
