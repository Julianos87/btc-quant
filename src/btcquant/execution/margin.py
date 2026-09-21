"""Shared cross-margin model for the Trend PAPER account.

The venue-specific tier table is not embedded as if it were immutable. The
runner uses a conservative, explicitly unqualified fallback until the venue
metadata endpoint is wired into the data port. This module prevents three
virtual slots from spending three copies of the same collateral.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from collections.abc import Iterable


@dataclass(frozen=True)
class MarginSnapshot:
    collateral: float
    equity: float
    gross_notional: float
    net_notional: float
    initial_margin: float
    maintenance_margin: float
    available_initial_margin: float
    liquidatable: bool
    qualified: bool
    model: str
    source: str

    def as_dict(self) -> dict[str, object]:
        return {
            "collateral": self.collateral,
            "equity": self.equity,
            "gross_notional": self.gross_notional,
            "net_notional": self.net_notional,
            "initial_margin": self.initial_margin,
            "maintenance_margin": self.maintenance_margin,
            "available_initial_margin": self.available_initial_margin,
            "liquidatable": self.liquidatable,
            "qualified": self.qualified,
            "model": self.model,
            "source": self.source,
        }


class SharedCrossMarginModel:
    """Conservative shared account model for linear perp slots."""

    def __init__(
        self,
        *,
        max_leverage: float,
        venue: str,
        maintenance_margin_rate: float = 0.0125,
        qualified: bool = False,
        source: str = "official-venue-rule-plus-missing-live-tier-metadata",
    ) -> None:
        if not math.isfinite(max_leverage) or max_leverage <= 0:
            raise ValueError("max_leverage doit être strictement positif")
        if not 0 < maintenance_margin_rate < 1:
            raise ValueError("maintenance_margin_rate doit être dans ]0, 1[")
        self.max_leverage = float(max_leverage)
        self.maintenance_margin_rate = float(maintenance_margin_rate)
        self.venue = venue
        self.qualified = bool(qualified)
        self.source = source
        self.model = f"{venue.lower()}-cross-conservative"

    def evaluate(
        self,
        *,
        collateral: float,
        positions: Iterable[tuple[int, float, float, float]],
        mark_price: float,
    ) -> MarginSnapshot:
        if not math.isfinite(collateral) or not math.isfinite(mark_price) or mark_price <= 0:
            raise ValueError("collateral et mark_price doivent être finis, mark_price positif")
        gross = 0.0
        net = 0.0
        unrealized = 0.0
        for direction, qty, entry_price, current_qty_price in positions:
            if direction not in (-1, 1) or qty < 0 or entry_price <= 0 or current_qty_price <= 0:
                raise ValueError("position de marge invalide")
            notional = qty * current_qty_price
            gross += notional
            net += direction * notional
            unrealized += direction * qty * (mark_price - entry_price)
        equity = collateral + unrealized
        initial = gross / self.max_leverage
        maintenance = gross * self.maintenance_margin_rate
        return MarginSnapshot(
            collateral=float(collateral),
            equity=float(equity),
            gross_notional=float(gross),
            net_notional=float(net),
            initial_margin=float(initial),
            maintenance_margin=float(maintenance),
            available_initial_margin=float(equity - initial),
            liquidatable=bool(gross > 0 and equity <= maintenance),
            qualified=self.qualified,
            model=self.model,
            source=self.source,
        )

    def max_additional_qty(
        self,
        snapshot: MarginSnapshot,
        *,
        price: float,
    ) -> float:
        """Quantity that fits the shared initial-margin pool."""

        if price <= 0 or snapshot.available_initial_margin <= 0:
            return 0.0
        return snapshot.available_initial_margin * self.max_leverage / price
