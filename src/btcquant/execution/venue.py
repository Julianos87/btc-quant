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

from .resilience import RetryPolicy

NETWORK_ERRORS = (ccxt.NetworkError, ccxt.ExchangeNotAvailable, ccxt.RequestTimeout)
FUNDING_HISTORY_PAGE_LIMIT = 1000


class Venue:
    def __init__(self, exchange_id: str, symbol: str, *, testnet: bool = False) -> None:
        self.exchange_id = exchange_id
        self.symbol = symbol
        self.is_hourly_funding = exchange_id == "hyperliquid"
        klass = getattr(ccxt, exchange_id)
        self.exchange: ccxt.Exchange = klass({"enableRateLimit": True, "timeout": 30_000})
        if testnet:
            if exchange_id != "hyperliquid":
                raise ValueError("Seul le sandbox Hyperliquid est pris en charge par Venue")
            self.exchange.set_sandbox_mode(True)
        self._retry = RetryPolicy()
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
            candles = self._retry.call(
                self.exchange.fetch_ohlcv,
                self.symbol,
                "1m",
                limit=1,
                retry_on=NETWORK_ERRORS,
            )
            return float(candles[-1][4])
        ticker = self._retry.call(self.exchange.fetch_ticker, self.symbol, retry_on=NETWORK_ERRORS)
        return float(ticker["last"])

    def fetch_ohlcv(self, timeframe: str, limit: int = 1000) -> list[list]:
        return self._retry.call(
            self.exchange.fetch_ohlcv,
            self.symbol,
            timeframe,
            limit=limit,
            retry_on=NETWORK_ERRORS,
        )

    # ── funding ──────────────────────────────────────────────────────────────
    def funding_rate_8h(self) -> float:
        """Taux de funding courant, ramené à une période de 8 h."""
        if self.is_hourly_funding:
            # pas de fetch_funding_rate sur hyperliquid : dernier paiement de
            # l'historique, ×8 pour l'équivalent 8 h
            since = int((time.time() - 3 * 3600) * 1000)
            hist = self._retry.call(
                self.funding_exchange.fetch_funding_rate_history,
                self.symbol,
                since=since,
                retry_on=NETWORK_ERRORS,
            )
            if not hist:
                raise ccxt.ExchangeError("historique de funding vide")
            return float(hist[-1]["fundingRate"]) * 8.0
        funding = self._retry.call(
            self.funding_exchange.fetch_funding_rate,
            self.symbol,
            retry_on=NETWORK_ERRORS,
        )
        return float(funding["fundingRate"])

    def funding_history(self, days: float) -> pd.Series:
        """Paiements de funding des `days` derniers jours (taux par période
        NATIVE, un point par paiement réel), indexés par horodatage UTC."""
        since = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days)
        return self.funding_history_since(since)

    def funding_history_since(self, since: pd.Timestamp) -> pd.Series:
        """Paiements natifs depuis ``since``, toutes les pages dédupliquées.

        Une seule réponse CCXT peut être plus courte que l'arriéré demandé,
        notamment après plusieurs semaines d'arrêt sur une venue horaire.
        Le curseur avance jusqu'à une page vide ou non progressive.
        """

        since = pd.Timestamp(since)
        since = since.tz_localize("UTC") if since.tzinfo is None else since.tz_convert("UTC")
        since_ms = int(since.timestamp() * 1000)
        cursor_ms = since_ms
        payments: dict[int, float] = {}
        while True:
            rows = self._retry.call(
                self.funding_exchange.fetch_funding_rate_history,
                self.symbol,
                since=cursor_ms,
                limit=FUNDING_HISTORY_PAGE_LIMIT,
                retry_on=NETWORK_ERRORS,
            )
            if not rows:
                break
            latest_ms = max(int(row["timestamp"]) for row in rows)
            for row in rows:
                timestamp_ms = int(row["timestamp"])
                if timestamp_ms >= since_ms:
                    payments[timestamp_ms] = float(row["fundingRate"])
            next_cursor_ms = latest_ms + 1
            if next_cursor_ms <= cursor_ms:
                break
            cursor_ms = next_cursor_ms

        timestamps = sorted(payments)
        return pd.Series(
            [payments[timestamp] for timestamp in timestamps],
            index=pd.DatetimeIndex(
                [pd.Timestamp(timestamp, unit="ms", tz="UTC") for timestamp in timestamps]
            ),
            dtype=float,
        )
