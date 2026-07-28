"""Lance le carry en paper trading (funding réels, exécution simulée).

Usage : btcquant-carry [--config environments/paper/config.yaml]
"""

import argparse
import logging
import os
import signal
import threading
from dataclasses import replace
from pathlib import Path

from btcquant.config import carry_policy_from_config, load_config
from btcquant.execution.carry_runner import CarryRunner
from btcquant.execution.venue import Venue

ROOT = Path(os.environ.get("BTCQUANT_ROOT", Path.cwd())).resolve()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default=ROOT / "environments" / "paper" / "config.yaml",
    )
    cfg = load_config(parser.parse_known_args()[0].config)
    configured_policy = carry_policy_from_config(cfg)
    parser.add_argument(
        "--capital",
        type=float,
        default=None,
        help="surcharge exceptionnelle du capital configuré",
    )
    parser.add_argument("--leverage", type=float, default=None)
    parser.add_argument(
        "--borrow-rate",
        type=float,
        default=None,
        help="coût annuel des fonds empruntés pour financer la jambe spot "
        f"(configuré à {configured_policy.borrow_rate_ann * 100:.0f} %%). "
        "Sans effet à --leverage 1.",
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

    policy = replace(
        configured_policy,
        capital=args.capital if args.capital is not None else configured_policy.capital,
        leverage=args.leverage if args.leverage is not None else configured_policy.leverage,
        borrow_rate_ann=(
            args.borrow_rate if args.borrow_rate is not None else configured_policy.borrow_rate_ann
        ),
    )
    execution = cfg["execution"]
    runner = CarryRunner(
        policy=policy,
        state_file=ROOT / execution["state_file"],
        legacy_state_file=ROOT / "state" / "carry_state.json",
        live_broker=None,
        venue=Venue(execution["live_exchange"], execution["live_symbol"]),
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
