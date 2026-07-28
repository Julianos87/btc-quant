"""Collecteur shadow du carnet mainnet, strictement sans ordre."""

from __future__ import annotations

import argparse
import logging
import time
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from ..console import enable_utf8_output
from ..execution.shadow import BookTop, ShadowCollector, ShadowConfig, ShadowStore
from ..execution.venue import Venue

DEFAULT_DATABASE = Path("state/execution-shadow.db")
log = logging.getLogger(__name__)


class HyperliquidPublicBook:
    """Adaptateur public sans clé API et sans méthode d'exécution."""

    def __init__(self, symbol: str = "BTC/USDC:USDC") -> None:
        self.venue = Venue("hyperliquid", symbol, testnet=False)
        self._funding_rate_8h = 0.0
        self._funding_refreshed_at = 0.0

    def _funding(self) -> float:
        now = time.monotonic()
        if now - self._funding_refreshed_at >= 300.0:
            try:
                self._funding_rate_8h = self.venue.funding_rate_8h()
                self._funding_refreshed_at = now
            except Exception as error:
                log.warning("Funding shadow indisponible, dernière valeur conservée : %s", error)
        return self._funding_rate_8h

    def top(self) -> BookTop:
        book = self.venue.fetch_order_book(limit=20)
        if not book.get("bids") or not book.get("asks"):
            raise RuntimeError("Carnet Hyperliquid vide")
        bid, bid_qty = map(float, book["bids"][0][:2])
        ask, ask_qty = map(float, book["asks"][0][:2])
        observed_at = datetime.now(UTC)
        timestamp = pd.Timestamp(observed_at)
        next_hour = timestamp.floor("h") + pd.Timedelta(hours=1)
        return BookTop(
            observed_at=observed_at,
            bid=bid,
            ask=ask,
            bid_qty=bid_qty,
            ask_qty=ask_qty,
            funding_rate_8h=self._funding(),
            seconds_to_funding=float((next_hour - timestamp).total_seconds()),
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE,
        help="base SQLite shadow séparée du track record paper",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    run = subcommands.add_parser("run", help="collecte continue")
    run.add_argument("--quote-interval-seconds", type=float, default=60.0)
    run.add_argument("--poll-interval-seconds", type=float, default=2.0)
    run.add_argument("--maker-timeout-seconds", type=float, default=30.0)
    run.add_argument("--notional", type=float, default=1_000.0)
    subcommands.add_parser("status", help="résumé JSON en lecture seule")
    return parser


def main() -> None:
    enable_utf8_output()
    args = _parser().parse_args()
    store = ShadowStore(args.database)
    if args.command == "status":
        print(store.summary_json())
        return
    logging.basicConfig(level=logging.INFO)
    config = ShadowConfig(
        quote_interval_seconds=args.quote_interval_seconds,
        poll_interval_seconds=args.poll_interval_seconds,
        maker_timeout_seconds=args.maker_timeout_seconds,
        notional=args.notional,
    )
    log.info(
        "Shadow mainnet démarré : aucune clé, aucun ordre, base=%s",
        args.database,
    )
    ShadowCollector(HyperliquidPublicBook(), store, config).run_forever()
