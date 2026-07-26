from .base import Position, Strategy
from .trend_ls import TrendLS

STRATEGY_REGISTRY: dict[str, type[Strategy]] = {
    "trend_ls": TrendLS,
}

__all__ = ["Position", "Strategy", "STRATEGY_REGISTRY", "TrendLS"]
