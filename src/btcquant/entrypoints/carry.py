"""Lance le carry en paper trading (funding réels, exécution simulée).

Usage : python scripts/run_carry.py [--capital 4000] [--leverage 3]
"""

import argparse
import logging
import os
import signal
import threading
from pathlib import Path

from btcquant.carry import DEFAULT_BORROW_RATE_ANN
from btcquant.execution.carry_runner import CarryRunner
from btcquant.execution.venue import Venue

ROOT = Path(os.environ.get("BTCQUANT_ROOT", Path.cwd())).resolve()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capital", type=float, default=4000.0, help="40 %% d'un compte de 10 000")
    parser.add_argument("--leverage", type=float, default=3.0)
    parser.add_argument(
        "--borrow-rate",
        type=float,
        default=DEFAULT_BORROW_RATE_ANN,
        help="coût annuel des fonds empruntés pour financer la jambe spot "
        f"(défaut {DEFAULT_BORROW_RATE_ANN * 100:.0f} %%). Sans effet à --leverage 1.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="désactivé : réservé au futur moteur d'exécution transactionnel",
    )
    parser.add_argument("--testnet", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    from logging.handlers import RotatingFileHandler

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            RotatingFileHandler(
                ROOT / "state" / "carry.log", encoding="utf-8", maxBytes=5_000_000, backupCount=3
            ),
        ],
    )
    if args.live:
        raise SystemExit(
            "SÉCURITÉ : le carry live/testnet est temporairement désactivé. "
            "Le runner paper reste disponible."
        )

    runner = CarryRunner(
        initial_capital=args.capital,
        leverage=args.leverage,
        borrow_rate_ann=args.borrow_rate,
        state_file=ROOT / "state" / "btcquant.db",
        legacy_state_file=ROOT / "state" / "carry_state.json",
        live_broker=None,
        venue=Venue("hyperliquid", "BTC/USDC:USDC"),
    )
    stop_event = threading.Event()

    def request_stop(signum, _frame):
        logging.getLogger(__name__).info("Signal %s reçu : arrêt propre demandé", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    runner.run_forever(stop_event)


if __name__ == "__main__":
    main()
