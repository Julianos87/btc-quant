"""Stratégies expérimentales, non autorisées dans le runtime live."""

from .intraday_breakout import IntradayBreakout
from .range_mean_reversion import RangeMeanReversion
from .trend_swing import TrendSwing

RESEARCH_STRATEGY_REGISTRY = {
    "trend_swing": TrendSwing,
    "intraday_breakout": IntradayBreakout,
    "range_mean_reversion": RangeMeanReversion,
}

__all__ = [
    "IntradayBreakout",
    "RangeMeanReversion",
    "RESEARCH_STRATEGY_REGISTRY",
    "TrendSwing",
]
