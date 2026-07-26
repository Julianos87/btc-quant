"""Stratégies expérimentales, non autorisées dans le runtime live."""

from .intraday_breakout import IntradayBreakout
from .trend_swing import TrendSwing

RESEARCH_STRATEGY_REGISTRY = {
    "trend_swing": TrendSwing,
    "intraday_breakout": IntradayBreakout,
}

__all__ = ["IntradayBreakout", "RESEARCH_STRATEGY_REGISTRY", "TrendSwing"]
