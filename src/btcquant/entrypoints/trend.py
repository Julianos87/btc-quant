"""Lance le bot en continu en paper trading.

Paper  : btcquant-trend --config environments/paper/config.yaml
Live   : désactivé tant que le moteur d'exécution transactionnel n'est pas livré.
"""

import argparse
import logging
import os
import signal
import threading
from pathlib import Path

from btcquant.config import (
    build_strategies,
    execution_config_from_config,
    load_config,
    risk_from_config,
)
from btcquant.domain import ExecutionSimulator
from btcquant.execution import CcxtBroker, PaperBroker
from btcquant.execution.broker import Broker
from btcquant.execution.clock import SystemClock
from btcquant.execution.runner import LiveRunner, StrategySlot
from btcquant.execution.venue import Venue

ROOT = Path(os.environ.get("BTCQUANT_ROOT", Path.cwd())).resolve()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=ROOT / "environments" / "dev" / "config.yaml")
    args = parser.parse_args()

    from logging.handlers import RotatingFileHandler

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            RotatingFileHandler(
                ROOT / "state" / "runner.log", encoding="utf-8", maxBytes=5_000_000, backupCount=3
            ),
        ],
    )
    cfg = load_config(args.config)
    risk = risk_from_config(cfg)
    exec_cfg = cfg["execution"]

    strategies = build_strategies(cfg)
    markets = {market for _, _, market in strategies}
    if len(markets) > 1:
        raise SystemExit(
            "Mélange spot/perp non supporté dans un même runner : "
            "activer des stratégies d'un seul marché."
        )
    market = markets.pop()

    mode = exec_cfg["mode"]
    broker: Broker
    if mode == "paper":
        fee = cfg["costs"]["perp_fee_rate"] if market == "perp" else cfg["costs"]["fee_rate"]
        broker = PaperBroker(simulator=ExecutionSimulator(execution_config_from_config(cfg, fee)))
    elif mode == "testnet":
        live_exchange = exec_cfg.get("live_exchange")
        if live_exchange != "hyperliquid" or market != "perp":
            raise SystemExit("SÉCURITÉ : seul Hyperliquid testnet perp est autorisé pour trend.")
        broker = CcxtBroker(
            "hyperliquid",
            exec_cfg.get("live_symbol", "BTC/USDC:USDC"),
            testnet=True,
            market="perp",
            leverage=1,
            qualification_state_path=ROOT
            / exec_cfg.get("qualification_state_file", "state/btcquant.db"),
        )
    else:  # protégé aussi par load_config, défense en profondeur
        raise SystemExit("SÉCURITÉ : l'argent réel reste désactivé.")

    slots = [
        StrategySlot(strategy, fraction, risk.initial_capital * fraction)
        for strategy, fraction, _ in strategies
    ]
    runner = LiveRunner(
        slots=slots,
        broker=broker,
        risk=risk,
        # venue live séparée de la venue backtest : les runners consomment
        # Hyperliquid (execution.live_exchange), le backtest reste sur les
        # données Binance téléchargées (exchange:)
        exchange_id=exec_cfg.get("live_exchange", cfg["exchange"]),
        symbol=exec_cfg.get("live_symbol", cfg["symbol"]),
        state_file=ROOT / exec_cfg["state_file"],
        legacy_state_file=(
            ROOT / exec_cfg["legacy_state_file"] if exec_cfg.get("legacy_state_file") else None
        ),
        poll_buffer_seconds=exec_cfg.get("poll_buffer_seconds", 20),
        # perp : funding simulé par barre comme dans le backtest (taux live,
        # repli sur la constante de config si l'API funding est muette)
        funding_rate_8h=cfg["costs"].get("funding_rate_8h", 0.0) if market == "perp" else 0.0,
        venue=Venue(
            exec_cfg.get("live_exchange", cfg["exchange"]),
            exec_cfg.get("live_symbol", cfg["symbol"]),
            testnet=mode == "testnet",
        ),
        clock=SystemClock(),
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
