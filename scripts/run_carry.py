"""Lance le carry en paper trading (funding réels, exécution simulée).

Usage : python scripts/run_carry.py [--capital 4000] [--leverage 3]
"""

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btcquant.carry import DEFAULT_BORROW_RATE_ANN
from btcquant.execution.carry_runner import CarryRunner


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capital", type=float, default=4000.0, help="40 %% d'un compte de 10 000")
    parser.add_argument("--leverage", type=float, default=3.0)
    parser.add_argument(
        "--borrow-rate", type=float, default=DEFAULT_BORROW_RATE_ANN,
        help="coût annuel des fonds empruntés pour financer la jambe spot "
             f"(défaut {DEFAULT_BORROW_RATE_ANN:.0%}). Sans effet à --leverage 1.",
    )
    parser.add_argument("--live", action="store_true", help="exécution réelle double-jambe (sinon paper)")
    parser.add_argument("--testnet", action="store_true", help="sandbox Binance (avec --live)")
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
    live_broker = None
    if args.live:
        from btcquant.execution.carry_broker import CarryBroker
        if not args.testnet:
            ans = input("⚠ CARRY LIVE RÉEL (argent réel, 2 jambes). Taper 'JE CONFIRME' : ")
            if ans.strip() != "JE CONFIRME":
                print("Abandon."); return
        live_broker = CarryBroker(symbol="BTC/USDT", testnet=args.testnet, leverage=int(args.leverage))

    CarryRunner(
        initial_capital=args.capital,
        leverage=args.leverage,
        borrow_rate_ann=args.borrow_rate,
        state_file=ROOT / "state" / "carry_state.json",
        live_broker=live_broker,
    ).run_forever()


if __name__ == "__main__":
    main()
