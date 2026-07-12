"""Lance le carry en paper trading (funding réels, exécution simulée).

Usage : python scripts/run_carry.py [--capital 4000] [--leverage 3]
"""

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btcquant.execution.carry_runner import CarryRunner


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capital", type=float, default=4000.0, help="40 %% d'un compte de 10 000")
    parser.add_argument("--leverage", type=float, default=3.0)
    args = parser.parse_args()

    from logging.handlers import RotatingFileHandler

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            RotatingFileHandler(ROOT / "state" / "carry.log", encoding="utf-8",
                                maxBytes=5_000_000, backupCount=3),
        ],
    )
    CarryRunner(
        initial_capital=args.capital,
        leverage=args.leverage,
        state_file=ROOT / "state" / "carry_state.json",
    ).run_forever()


if __name__ == "__main__":
    main()
