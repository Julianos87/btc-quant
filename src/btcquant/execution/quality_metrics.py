"""Mesures de qualité d'exécution, partagées par la santé et la qualification.

Ces deux consommateurs décidaient sur des copies séparées et identiques du même
calcul : le tableau de bord d'exploitation et le portail de promotion vers le
testnet pouvaient donc diverger dès qu'une seule des deux était corrigée. Ce
module est désormais leur unique implémentation.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def order_slippage_bps(order: dict[str, Any]) -> float | None:
    """Slippage signé d'un ordre exécuté, en points de base, ou None.

    Positif = défavorable : un achat rempli au-dessus de son prix de référence,
    une vente remplie en dessous. Un ordre sans référence, sans prix ou sans
    quantité remplie n'a pas de slippage mesurable — il est écarté plutôt
    qu'assimilé à zéro, qui compterait comme une exécution parfaite.
    """

    reference = order.get("reference_price")
    price = order.get("price")
    if reference is None or price is None:
        return None
    reference_value = float(reference)
    if reference_value <= 0 or float(order["filled_qty"]) <= 0:
        return None
    ratio = float(price) / reference_value
    signed = ratio - 1.0 if str(order["side"]).upper() == "BUY" else 1.0 - ratio
    return signed * 10_000.0


def slippages_bps(orders: Iterable[dict[str, Any]]) -> list[float]:
    """Slippages mesurables d'une série d'ordres, dans leur ordre d'origine."""

    measured = (order_slippage_bps(order) for order in orders)
    return [value for value in measured if value is not None]


def percentile(values: list[float], fraction: float) -> float | None:
    """Percentile par rang le plus proche (None si aucun échantillon).

    Sans interpolation : sur les petits échantillons d'une campagne, un p95
    interpolé lisserait justement l'exécution aberrante qu'on cherche à voir.
    """

    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * fraction + 0.5)))
    return ordered[index]
