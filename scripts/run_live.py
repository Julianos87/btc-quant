"""Compatibilité : préférer la commande installée `btcquant-trend`."""

from btcquant.entrypoints.trend import main


if __name__ == "__main__":
    main()
