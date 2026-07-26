"""Point d'entrée WSGI de production du dashboard."""

from dashboard.app import app, start_warm_loop

# Gunicorn importe ce module dans son worker. Le cache réseau reste ainsi
# asynchrone et n'est démarré ni lors des tests ni dans le processus master.
start_warm_loop()

__all__ = ["app"]
