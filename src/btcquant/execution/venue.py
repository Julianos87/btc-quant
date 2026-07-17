"""Abstraction de la venue de données live (Binance ou Hyperliquid).

Depuis le 17/07/2026, les runners paper consomment Hyperliquid ; la référence
backtest (walk-forward 2019-2026) reste calculée sur les données Binance —
l'historique Hyperliquid accessible ne remonte qu'à avril 2024.

Différences normalisées ici :
- funding : Binance paie toutes les 8 h, Hyperliquid toutes les HEURES.
  `funding_rate_8h()` renvoie toujours un taux équivalent 8 h (la convention
  du backtest et des filtres funding_long_max/short_min des stratégies) ;
  `funding_history()` renvoie les paiements natifs, à annualiser avec
  `payments_per_year`.
- prix : sur Hyperliquid, fetch_ticker recharge le contexte de TOUS les
  marchés (~12 s mesurés) → le prix est lu sur la dernière bougie 1m (~0,5 s).
"""

from __future__ import annotations

import time

import ccxt
import pandas as pd


class Venue:
    def __init__(self, exchange_id: str, symbol: str) -> None:
        self.exchange_id = exchange_id
        self.symbol = symbol
        self.is_hourly_funding = exchange_id == "hyperliquid"
        klass = getattr(ccxt, exchange_id)
        self.exchange: ccxt.Exchange = klass({"enableRateLimit": True, "timeout": 30_000})
        if self.is_hourly_funding:
            self.funding_exchange = self.exchange
            self.payments_per_day = 24
        else:
            # binance : les données OHLCV viennent du spot (comme le backtest),
            # le funding du marché perpétuel USDT-M
            self.funding_exchange = ccxt.binanceusdm({"enableRateLimit": True, "timeout": 30_000})
            self.payments_per_day = 3

    @property
    def payments_per_year(self) -> int:
        return self.payments_per_day * 365

    # ── prix & bougies ───────────────────────────────────────────────────────
    def last_price(self) -> float:
        if self.is_hourly_funding:
            candles = self.exchange.fetch_ohlcv(self.symbol, "1m", limit=1)
            return float(candles[-1][4])
        return float(self.exchange.fetch_ticker(self.symbol)["last"])

    def fetch_ohlcv(self, timeframe: str, limit: int = 1000) -> list[list]:
        return self.exchange.fetch_ohlcv(self.symbol, timeframe, limit=limit)

    # ── funding ──────────────────────────────────────────────────────────────
    def funding_rate_8h(self) -> float:
        """Taux de funding courant, ramené à une période de 8 h."""
        if self.is_hourly_funding:
            # pas de fetch_funding_rate sur hyperliquid : dernier paiement de
            # l'historique, ×8 pour l'équivalent 8 h
            since = int((time.time() - 3 * 3600) * 1000)
            hist = self.funding_exchange.fetch_funding_rate_history(self.symbol, since=since)
            if not hist:
                raise ccxt.ExchangeError("historique de funding vide")
            return float(hist[-1]["fundingRate"]) * 8.0
        return float(self.funding_exchange.fetch_funding_rate(self.symbol)["fundingRate"])

    def funding_history(self, days: float) -> pd.Series:
        """Paiements de funding des `days` derniers jours (taux par période
        NATIVE, un point par paiement réel), indexés par horodatage UTC."""
        since = int((time.time() - days * 86_400) * 1000)
        rows = self.funding_exchange.fetch_funding_rate_history(self.symbol, since=since)
        return pd.Series(
            [float(r["fundingRate"]) for r in rows],
            index=pd.DatetimeIndex(
                [pd.Timestamp(r["timestamp"], unit="ms", tz="UTC") for r in rows]
            ),
        ).sort_index()
