from .base import Position, Strategy
from .intraday_breakout import IntradayBreakout
from .trend_ls import TrendLS
from .trend_swing import TrendSwing

STRATEGY_REGISTRY: dict[str, type[Strategy]] = {
    "trend_swing": TrendSwing,
    "intraday_breakout": IntradayBreakout,
    "trend_ls": TrendLS,
}

__all__ = ["Strategy", "Position", "TrendSwing", "IntradayBreakout", "TrendLS", "STRATEGY_REGISTRY"]
