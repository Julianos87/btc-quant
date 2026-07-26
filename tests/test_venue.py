"""Tests de l'abstraction de venue (Hyperliquid vs Binance) : conversion du
funding horaire en équivalent 8 h, annualisation du carry, prix via bougie."""

import sys
import time
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btcquant.execution.carry_runner import CarryRunner
from btcquant.execution.venue import Venue


class _StubExchange:
    """Réponses ccxt minimales, sans réseau."""

    def __init__(self, funding_rate: float = 1.25e-5, price: float = 60_000.0):
        self.funding_rate = funding_rate
        self.price = price

    def fetch_funding_rate_history(self, symbol, since=None, limit=None):
        now = int(time.time() * 1000)
        return [
            {"fundingRate": self.funding_rate, "timestamp": now - i * 3_600_000}
            for i in range(5, 0, -1)
        ]

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        return [[int(time.time() * 1000), self.price, self.price, self.price, self.price, 1.0]]


def _stub_venue(v: Venue, **kw) -> Venue:
    stub = _StubExchange(**kw)
    v.exchange = stub
    v.funding_exchange = stub
    return v


def test_hyperliquid_funding_converted_to_8h():
    """Le taux horaire Hyperliquid est ramené à l'équivalent 8 h — la
    convention des filtres funding_long_max/short_min et du backtest."""
    v = _stub_venue(Venue("hyperliquid", "BTC/USDC:USDC"), funding_rate=1e-5)
    assert v.funding_rate_8h() == pytest.approx(8e-5)
    assert v.payments_per_day == 24
    assert v.payments_per_year == 24 * 365


def test_binance_venue_keeps_8h_conventions():
    v = Venue("binance", "BTC/USDT")
    assert v.payments_per_day == 3
    assert v.payments_per_year == 3 * 365


def test_hyperliquid_price_from_candle():
    """Pas de fetch_ticker sur Hyperliquid (~12 s) : prix = clôture 1m."""
    v = _stub_venue(Venue("hyperliquid", "BTC/USDC:USDC"), price=61_234.5)
    assert v.last_price() == pytest.approx(61_234.5)


def test_funding_history_sorted_series():
    v = _stub_venue(Venue("hyperliquid", "BTC/USDC:USDC"))
    s = v.funding_history(1)
    assert isinstance(s, pd.Series)
    assert s.index.is_monotonic_increasing
    assert len(s) == 5


def test_carry_annualizes_hourly_funding(tmp_path):
    """Funding horaire constant de 1.25e-5 → ~10,9 %/an : le carry doit entrer
    (seuil 3 %) et payer le coût de bascule, avec l'annualisation ×24×365
    (l'ancienne convention 8 h, ×3×365, n'aurait vu que ~1,4 % → jamais entré)."""
    runner = CarryRunner(
        initial_capital=4000.0, leverage=3.0, state_file=tmp_path / "carry_state.json"
    )
    _stub_venue(runner.venue, funding_rate=1.25e-5)
    assert not runner.in_position
    runner._tick()
    assert runner.in_position
    assert runner.equity == pytest.approx(4000.0 * (1 - runner.switch_cost))
