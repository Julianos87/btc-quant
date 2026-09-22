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

from ..config import HYPERLIQUID_TESTNET_API_URL
from .resilience import RetryPolicy
from .carry_paper import CarryMarketState
from .ports import FundingReference


def _assert_hyperliquid_testnet_endpoint(exchange: object) -> None:
    urls = getattr(exchange, "urls", None)
    api = urls.get("api") if isinstance(urls, dict) else None
    public = api.get("public") if isinstance(api, dict) else None
    private = api.get("private") if isinstance(api, dict) else None
    if public != HYPERLIQUID_TESTNET_API_URL or private != HYPERLIQUID_TESTNET_API_URL:
        raise RuntimeError(
            "Safety Baseline : endpoint Hyperliquid testnet absent ou remplacé par mainnet"
        )


NETWORK_ERRORS = (ccxt.NetworkError, ccxt.ExchangeNotAvailable, ccxt.RequestTimeout)
FUNDING_HISTORY_PAGE_LIMIT = 1000


class Venue:
    def __init__(
        self,
        exchange_id: str,
        symbol: str,
        *,
        testnet: bool = False,
        spot_symbol: str | None = None,
    ) -> None:
        self.exchange_id = exchange_id
        self.symbol = symbol
        self.spot_symbol = spot_symbol or (symbol.split(":", 1)[0] if ":" in symbol else symbol)
        self.is_hourly_funding = exchange_id == "hyperliquid"
        klass = getattr(ccxt, exchange_id)
        self.exchange: ccxt.Exchange = klass({"enableRateLimit": True, "timeout": 30_000})
        if testnet:
            if exchange_id != "hyperliquid":
                raise ValueError("Seul le sandbox Hyperliquid est pris en charge par Venue")
            self.exchange.set_sandbox_mode(True)
            _assert_hyperliquid_testnet_endpoint(self.exchange)
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
            return float(self._retry.call(self._hyperliquid_last_close, retry_on=NETWORK_ERRORS))
        ticker = self._retry.call(self.exchange.fetch_ticker, self.symbol, retry_on=NETWORK_ERRORS)
        last = ticker.get("last") if isinstance(ticker, dict) else None
        if last is None:
            raise ccxt.ExchangeNotAvailable(
                f"{self.exchange_id} ticker.last absent pour {self.symbol}"
            )
        return float(last)

    def _hyperliquid_last_close(self) -> float:
        """Clôture 1m ; une réponse vide est transitoire, pas un IndexError."""

        candles = self.exchange.fetch_ohlcv(self.symbol, "1m", limit=1)
        if not candles:
            raise ccxt.ExchangeNotAvailable(f"{self.exchange_id} bougie 1m vide pour {self.symbol}")
        close = candles[-1][4] if len(candles[-1]) > 4 else None
        if close is None:
            raise ccxt.ExchangeNotAvailable(
                f"{self.exchange_id} clôture 1m absente pour {self.symbol}"
            )
        return float(close)

    def fetch_ohlcv(self, timeframe: str, limit: int = 1000) -> list[list]:
        return self._retry.call(
            self.exchange.fetch_ohlcv,
            self.symbol,
            timeframe,
            limit=limit,
            retry_on=NETWORK_ERRORS,
        )

    def fetch_order_book(self, limit: int = 20) -> dict:
        """Carnet public uniquement ; cette abstraction ne sait pas placer d'ordre."""

        return self._retry.call(
            self.exchange.fetch_order_book,
            self.symbol,
            limit=limit,
            retry_on=NETWORK_ERRORS,
        )

    def current_carry_market_state(self, limit: int = 20) -> CarryMarketState:
        """Return one synchronous public observation for the two paper legs.

        A positive configured latency still requires a recorded post-decision
        tape; this live public snapshot is intentionally only suitable for a
        zero-latency observation cycle.
        """

        spot_book = self._retry.call(
            self.exchange.fetch_order_book,
            self.spot_symbol,
            limit=limit,
            retry_on=NETWORK_ERRORS,
        )
        perp_book = self._retry.call(
            self.exchange.fetch_order_book,
            self.symbol,
            limit=limit,
            retry_on=NETWORK_ERRORS,
        )

        def top(book: dict, side: str) -> float:
            levels = book.get(side)
            if not isinstance(levels, list) or not levels or len(levels[0]) < 1:
                raise ccxt.ExchangeNotAvailable(f"carnet {side} vide")
            return float(levels[0][0])

        spot_bid, spot_ask = top(spot_book, "bids"), top(spot_book, "asks")
        perp_bid, perp_ask = top(perp_book, "bids"), top(perp_book, "asks")
        return CarryMarketState(
            timestamp=pd.Timestamp.now(tz="UTC"),
            spot_bid=spot_bid,
            spot_ask=spot_ask,
            perp_bid=perp_bid,
            perp_ask=perp_ask,
            spot_mark=(spot_bid + spot_ask) / 2.0,
            perp_mark=(perp_bid + perp_ask) / 2.0,
            source="public_order_book_snapshot",
            spot_order_book=spot_book,
            perp_order_book=perp_book,
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

    def funding_reference_price(self, timestamp: pd.Timestamp) -> FundingReference | None:
        """Resolve an explicit as-of spot reference for a funding event.

        Hyperliquid's exact historical oracle series is not exposed by this
        public adapter. PAPER therefore uses the documented conservative
        approximation already used by the carry model: the close of the
        completed spot 1h candle immediately preceding the funding slot. The
        provenance is returned with the value; a missing or non-causal candle
        remains unresolved and the accounting layer blocks.
        """

        event = pd.Timestamp(timestamp)
        event = event.tz_localize("UTC") if event.tzinfo is None else event.tz_convert("UTC")
        slot = event.floor("h")
        if abs(event - slot) > pd.Timedelta(seconds=1):
            return None
        completed_open = slot - pd.Timedelta(hours=1)
        exchange = self.exchange
        fetch = getattr(exchange, "fetch_ohlcv", None)
        if not callable(fetch):
            return None
        since_ms = int((completed_open - pd.Timedelta(hours=1)).timestamp() * 1000)
        rows = self._retry.call(
            fetch,
            self.spot_symbol,
            "1h",
            since=since_ms,
            limit=3,
            retry_on=NETWORK_ERRORS,
        )
        for row in rows or []:
            if not isinstance(row, (list, tuple)) or len(row) < 5:
                continue
            opened = pd.Timestamp(row[0], unit="ms", tz="UTC")
            if opened != completed_open:
                continue
            close = float(row[4])
            if not pd.notna(close) or close <= 0:
                return None
            return {
                "price": close,
                "timestamp": completed_open,
                "source": "HYPERLIQUID_PREVIOUS_1H_CLOSE_APPROXIMATION",
            }
        return None

    def execution_price_after(self, decision_timestamp: pd.Timestamp, latency_ms: int) -> object:
        """No recorded post-decision stream is exposed by this adapter yet.

        Returning ``None`` is intentional: a positive simulated latency must
        not fall back to the current ticker, which could be a future price
        relative to the decision.
        """

        del decision_timestamp, latency_ms
        return None

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
