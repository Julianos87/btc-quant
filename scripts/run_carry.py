"""Compatibilité : préférer la commande installée `btcquant-carry`."""

from btcquant.entrypoints.carry import main


if __name__ == "__main__":
    main()
