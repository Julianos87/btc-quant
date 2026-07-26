"""Compatibilité : préférer la commande installée `btcquant-readiness`."""

from btcquant.entrypoints.readiness import main


if __name__ == "__main__":
    main()
