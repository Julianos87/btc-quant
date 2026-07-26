"""Télécharge/actualise l'historique OHLCV défini dans config.yaml."""

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btcquant.config import load_config
from btcquant.data import load_ohlcv


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=ROOT / "config.yaml")
    parser.add_argument("--symbol", default=None, help="ex: ETH/USDT (défaut: config)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)
    symbol = args.symbol or cfg["symbol"]
    df = load_ohlcv(
        cfg["exchange"],
        symbol,
        cfg["data"]["base_timeframe"],
        cfg["data"]["since"],
        data_dir=ROOT / cfg["data"]["dir"],
    )
    print(
        f"\n{symbol} {cfg['data']['base_timeframe']} : {len(df)} bougies, "
        f"{df.index[0]} → {df.index[-1]}"
    )


if __name__ == "__main__":
    main()
