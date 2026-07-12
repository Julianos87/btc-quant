"""Gestion du risque : dimensionnement des positions et coupe-circuits.

Deux contraintes se cumulent, la plus restrictive gagne :
1. Risque fixe par trade : (équity × risk_per_trade) / distance au stop.
   → une position large quand le stop est serré, petite quand l'ATR gonfle.
2. Ciblage de volatilité : notionnel ≤ équity × vol_target / vol_réalisée.
   → réduit l'exposition dans les régimes de volatilité extrême
   (amélioration documentée du Sharpe).

Plus un plafond spot (max_position_pct, pas de levier) et deux
coupe-circuits : drawdown maximal et perte journalière.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RiskConfig:
    initial_capital: float = 10_000.0
    risk_per_trade: float = 0.0075
    max_position_pct: float = 0.95
    vol_target_annual: float | None = 0.40
    max_drawdown_halt: float = 0.30
    daily_loss_limit: float | None = 0.03
    #: multiplicateur de notionnel maximal (futures uniquement). 1.0 = pas de
    #: levier. Le levier MULTIPLIE gains, pertes et drawdowns à l'identique.
    max_leverage: float = 1.0


def position_size(
    equity: float,
    entry_price: float,
    stop_price: float,
    realized_vol_annual: float | None,
    cfg: RiskConfig,
    direction: int = 1,
) -> float:
    """Quantité (positive, en BTC) à ouvrir, long ou short selon `direction`."""
    if equity <= 0 or entry_price <= 0:
        return 0.0
    stop_distance = direction * (entry_price - stop_price)
    if stop_distance <= 0:
        return 0.0

    # 1. risque fixe par trade
    qty = (equity * cfg.risk_per_trade) / stop_distance

    # 2. plafond de volatilité
    if cfg.vol_target_annual and realized_vol_annual and realized_vol_annual > 0:
        max_notional_vol = equity * cfg.vol_target_annual / realized_vol_annual
        qty = min(qty, max_notional_vol / entry_price)

    # 3. plafond de notionnel (1x spot, ou levier explicite en futures)
    qty = min(qty, equity * cfg.max_position_pct * cfg.max_leverage / entry_price)
    return max(qty, 0.0)


class KillSwitch:
    """Coupe-circuits : drawdown maximal et perte journalière (jour UTC)."""

    def __init__(self, cfg: RiskConfig) -> None:
        self.cfg = cfg
        self.peak_equity = cfg.initial_capital
        self.halted = False
        self._day: object = None
        self._day_start_equity = cfg.initial_capital
        self.daily_lockout = False

    def update(self, equity: float, day: object) -> None:
        """À appeler à chaque clôture de barre avec l'équity marquée au marché."""
        self.peak_equity = max(self.peak_equity, equity)
        if day != self._day:
            self._day = day
            self._day_start_equity = equity
            self.daily_lockout = False

        if equity < self.peak_equity * (1.0 - self.cfg.max_drawdown_halt):
            self.halted = True
        if (
            self.cfg.daily_loss_limit is not None
            and equity < self._day_start_equity * (1.0 - self.cfg.daily_loss_limit)
        ):
            self.daily_lockout = True

    @property
    def can_trade(self) -> bool:
        return not self.halted and not self.daily_lockout
