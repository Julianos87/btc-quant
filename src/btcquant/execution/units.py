"""Normalisation décimale aux frontières exchange.

Le domaine historique reste en ``float`` pour NumPy/pandas. En revanche, les
valeurs rendues par les filtres de précision d'un exchange sont comparées en
``Decimal`` afin de ne pas réintroduire une quantité ou un notionnel invalide
par erreur binaire juste avant l'appel externe.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation


def decimal_value(value: object, *, name: str, positive: bool = False) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{name} n'est pas un nombre décimal valide") from error
    if not number.is_finite():
        raise ValueError(f"{name} doit être fini")
    if positive and number <= 0:
        raise ValueError(f"{name} doit être strictement positif")
    return number


def exchange_float(value: object, *, name: str, positive: bool = False) -> float:
    """Convertit une valeur déjà normalisée par l'exchange pour l'adaptateur."""

    return float(decimal_value(value, name=name, positive=positive))


def decimal_notional(qty: object, price: object) -> Decimal:
    return decimal_value(qty, name="qty", positive=True) * decimal_value(
        price,
        name="price",
        positive=True,
    )
