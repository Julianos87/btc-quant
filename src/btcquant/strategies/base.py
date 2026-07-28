"""Contrat commun des stratégies.

Le moteur (backtest comme live) applique le protocole suivant, qui garantit
l'absence de look-ahead :

1. `prepare(df)` ajoute les colonnes d'indicateurs — chaque valeur en t ne
   dépend que des barres <= t.
2. À la CLÔTURE de la barre t, le moteur appelle `entry_signal` / `exit_signal`
   / `trailing_stop` avec la ligne t.
3. Les ordres qui en découlent sont exécutés à l'OUVERTURE de la barre t+1
   (avec slippage), les stops sont surveillés en intrabar.

Les stratégies peuvent être long/flat (spot) ou long-short (perpétuels) :
`entry_signal` retourne +1 (long), -1 (short) ou 0 (rien).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import IntEnum

import pandas as pd


class Direction(IntEnum):
    """Sens signé d’une position ; sérialisable comme entier aux frontières."""

    SHORT = -1
    LONG = 1


@dataclass
class Position:
    """Position ouverte. `qty` est toujours positive, `direction` = +1/-1."""

    entry_time: pd.Timestamp
    entry_price: float
    qty: float
    stop_price: float
    direction: Direction = Direction.LONG
    bars_held: int = 0
    #: plus haut close depuis l'entrée pour un long, plus bas pour un short
    best_close: float = field(default=0.0)
    #: quantité de la tranche initiale, référence stable pour les renforts
    initial_qty: float = field(default=0.0)
    #: dernier prix d'ajout; l'étape ATR suivante repart de ce niveau
    last_add_price: float = field(default=0.0)
    pyramid_adds: int = 0

    def __post_init__(self) -> None:
        self.direction = Direction(self.direction)
        if self.initial_qty <= 0:
            self.initial_qty = self.qty
        if self.last_add_price <= 0:
            self.last_add_price = self.entry_price

    def unrealized(self, price: float) -> float:
        return self.direction * self.qty * (price - self.entry_price)


class Strategy(ABC):
    """Stratégie mono-actif. Long/flat par défaut, long-short si entry_signal
    retourne aussi -1."""

    name: str = "strategy"
    timeframe: str = "1h"

    def __init__(self, **params) -> None:
        # Un paramètre inconnu est TOUJOURS une erreur, jamais une extension
        # silencieuse : `donchain=20` au lieu de `donchian=20` laisserait sinon
        # tourner la stratégie sur sa valeur par défaut, et un ensemble de trois
        # horizons deviendrait trois copies du même horizon sans le moindre
        # signal. Le coût d'une faute de frappe se paie en argent, pas en trace.
        defaults = self.default_params()
        unknown = sorted(set(params) - set(defaults))
        if unknown:
            raise ValueError(
                f"{type(self).__name__} : paramètre(s) inconnu(s) {unknown} ; "
                f"attendus parmi {sorted(defaults)}"
            )
        self.params = {**defaults, **params}

    @staticmethod
    @abstractmethod
    def default_params() -> dict: ...

    @abstractmethod
    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        """Ajoute les colonnes d'indicateurs. Ne modifie pas `df` en place."""

    @abstractmethod
    def entry_signal(self, row: pd.Series) -> int:
        """+1 : ouvrir un long, -1 : ouvrir un short, 0 : rien.
        (Un bool est accepté pour les stratégies long/flat : True == 1.)"""

    @abstractmethod
    def initial_stop(self, row: pd.Series, entry_price: float, direction: int = 1) -> float:
        """Stop initial pour une entrée exécutée à `entry_price`."""

    def position_size_multiplier(self, row: pd.Series, direction: int) -> float:
        """Multiplicateur de taille propre au signal, borné dans [0, 1]."""

        return 1.0

    def pyramid_fraction(self, row: pd.Series, position: Position) -> float:
        """Fraction de la tranche initiale à ajouter; zéro désactive le renfort."""

        return 0.0

    def trailing_stop(self, row: pd.Series, position: Position) -> float | None:
        """Nouveau stop suiveur. Le moteur ne fait que le resserrer :
        il le monte pour un long, le descend pour un short."""
        return None

    def exit_signal(self, row: pd.Series, position: Position) -> bool:
        """Sortie discrétionnaire (retournement de régime, stop temporel…)."""
        return False

    def warmup_bars(self) -> int:
        """Nombre de barres nécessaires avant que les indicateurs soient valides."""
        return 250
