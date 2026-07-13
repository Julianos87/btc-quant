"""Lance le bot en continu (paper par défaut, live après validation UNIQUEMENT).

Paper  : python scripts/run_live.py
Live   : passer execution.mode à "live" dans config.yaml (testnet d'abord !)
         et définir BINANCE_API_KEY / BINANCE_API_SECRET dans l'environnement.
"""

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btcquant.config import build_strategies, load_config, risk_from_config
from btcquant.execution import CcxtBroker, PaperBroker
from btcquant.execution.runner import LiveRunner, StrategySlot


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=ROOT / "config.yaml")
    args = parser.parse_args()

    from logging.handlers import RotatingFileHandler

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            RotatingFileHandler(ROOT / "state" / "runner.log", encoding="utf-8",
                                maxBytes=5_000_000, backupCount=3),
        ],
    )
    cfg = load_config(args.config)
    risk = risk_from_config(cfg)
    exec_cfg = cfg["execution"]

    strategies = build_strategies(cfg)
    markets = {market for _, _, market in strategies}
    if len(markets) > 1:
        raise SystemExit("Mélange spot/perp non supporté dans un même runner : "
                         "activer des stratégies d'un seul marché.")
    market = markets.pop()

    if exec_cfg["mode"] == "live":
        broker = CcxtBroker(cfg["exchange"], cfg["symbol"],
                            testnet=exec_cfg.get("testnet", True), market=market,
                            leverage=int(risk.max_leverage))
        if not exec_cfg.get("testnet", True):
            answer = input("⚠ MODE LIVE RÉEL (argent réel). Taper 'JE CONFIRME' pour continuer : ")
            if answer.strip() != "JE CONFIRME":
                print("Abandon.")
                return
    else:
        fee = cfg["costs"]["perp_fee_rate"] if market == "perp" else cfg["costs"]["fee_rate"]
        broker = PaperBroker(fee, cfg["costs"]["slippage_bps"])

    slots = [
        StrategySlot(strategy, fraction, risk.initial_capital * fraction)
        for strategy, fraction, _ in strategies
    ]
    runner = LiveRunner(
        slots=slots,
        broker=broker,
        risk=risk,
        exchange_id=cfg["exchange"],
        symbol=cfg["symbol"],
        state_file=ROOT / exec_cfg["state_file"],
        poll_buffer_seconds=exec_cfg.get("poll_buffer_seconds", 20),
        # perp : funding simulé par barre comme dans le backtest (taux live,
        # repli sur la constante de config si l'API funding est muette)
        funding_rate_8h=cfg["costs"].get("funding_rate_8h", 0.0) if market == "perp" else 0.0,
    )
    runner.run_forever()


if __name__ == "__main__":
    main()
