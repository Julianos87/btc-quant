"""Runner du cash-and-carry (paper trading).

Boucle :
- toutes les 5 minutes, récupère l'historique récent des funding réels
  (binanceusdm, public) et calcule le signal : funding lissé `smooth_days`
  jours annualisé > enter_ann → position ON ; < exit_ann → position OFF ;
- en position, chaque paiement de funding réellement versé (toutes les 8 h)
  est crédité : équity × taux × levier ;
- chaque bascule ON/OFF coûte 2 jambes × (frais + slippage) × levier ;
- état persisté en JSON (reprise après redémarrage).

Mode live : non implémenté volontairement — l'exécution double-jambe
(spot + perp simultanés, gestion de marge) sera un jalon séparé, à valider
sur testnet. Ce runner paper utilise les VRAIS taux de funding, donc ses
résultats sont directement comparables au backtest.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import ccxt
import pandas as pd

from ..notify import notify

log = logging.getLogger(__name__)

PAYMENTS_PER_YEAR = 3 * 365
TICK_SECONDS = 300


class CarryRunner:
    def __init__(
        self,
        symbol_perp: str = "BTC/USDT:USDT",
        initial_capital: float = 4000.0,
        leverage: float = 3.0,
        enter_ann: float = 0.03,
        exit_ann: float = 0.0,
        smooth_days: int = 14,
        fee_rate: float = 0.0005,
        slippage_bps: float = 5.0,
        state_file: str | Path = "state/carry_state.json",
        live_broker=None,
    ) -> None:
        self.symbol = symbol_perp
        self.leverage = leverage
        self.enter_ann = enter_ann
        self.exit_ann = exit_ann
        self.smooth_days = smooth_days
        self.switch_cost = 2 * (fee_rate + slippage_bps / 10_000.0) * leverage
        self.state_path = Path(state_file)
        self.exchange = ccxt.binanceusdm({"enableRateLimit": True, "timeout": 30_000})
        self.live_broker = live_broker  # CarryBroker en mode réel, None en paper
        self.equity = initial_capital
        self.in_position = False
        self.qty = 0.0  # BTC détenu (live)
        self.last_funding_ts: pd.Timestamp | None = None
        self._load_state()
        if self.live_broker is not None:
            self.live_broker.reconcile()

    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        raw = json.loads(self.state_path.read_text())
        self.equity = raw["equity"]
        self.in_position = raw["in_position"]
        self.qty = raw.get("qty", 0.0)
        self.last_funding_ts = (
            pd.Timestamp(raw["last_funding_ts"]) if raw.get("last_funding_ts") else None
        )
        log.info("État carry rechargé : équity %.2f, position %s", self.equity, self.in_position)

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(
                {
                    "equity": self.equity,
                    "in_position": self.in_position,
                    "qty": self.qty,
                    "last_funding_ts": str(self.last_funding_ts) if self.last_funding_ts is not None else None,
                }
            )
        )

    def _recent_funding(self) -> pd.Series:
        limit = self.smooth_days * 3 + 10
        rows = self.exchange.fetch_funding_rate_history(self.symbol, limit=limit)
        return pd.Series(
            [float(r["fundingRate"]) for r in rows],
            index=pd.DatetimeIndex([pd.Timestamp(r["timestamp"], unit="ms", tz="UTC") for r in rows]),
        ).sort_index()

    def _tick(self) -> None:
        funding = self._recent_funding()
        if funding.empty:
            return

        # 1. créditer les paiements réels survenus depuis le dernier passage
        if self.in_position:
            new = funding if self.last_funding_ts is None else funding[funding.index > self.last_funding_ts]
            for ts, rate in new.items():
                gain = self.equity * rate * self.leverage
                self.equity += gain
                log.info("[CARRY] Funding %s : %+.4f%% -> %+.2f USDT (équity %.2f)",
                         ts, rate * 100, gain, self.equity)
        self.last_funding_ts = funding.index[-1]

        # 2. signal sur funding lissé
        smooth_ann = funding.tail(self.smooth_days * 3).mean() * PAYMENTS_PER_YEAR
        if not self.in_position and smooth_ann > self.enter_ann:
            if self.live_broker is not None:  # jambes réelles avant de basculer l'état
                res = self.live_broker.open_position(self.equity * self.leverage)
                if res is None:
                    return  # ouverture échouée, on reste flat (déjà notifié)
                self.qty = res["qty"]
            self.equity *= 1.0 - self.switch_cost
            self.in_position = True
            log.info("[CARRY] ENTRÉE (funding lissé %.1f%%/an) — coût %.2f%%, équity %.2f",
                     smooth_ann * 100, self.switch_cost * 100, self.equity)
            notify(f"🔵 Carry — position OUVERTE (funding lissé {smooth_ann:.1%}/an), "
                   f"équity {self.equity:,.2f} $")
        elif self.in_position and smooth_ann < self.exit_ann:
            if self.live_broker is not None:
                self.live_broker.close_position(self.qty)
                self.qty = 0.0
            self.equity *= 1.0 - self.switch_cost
            self.in_position = False
            log.info("[CARRY] SORTIE (funding lissé %.1f%%/an) — équity %.2f",
                     smooth_ann * 100, self.equity)
            notify(f"⚪ Carry — position FERMÉE (funding lissé {smooth_ann:.1%}/an devenu "
                   f"défavorable), équity {self.equity:,.2f} $")

        self._save_state()
        self._append_equity()

    def _append_equity(self) -> None:
        """Historique d'équity (une ligne par tick, ~5 min)."""
        path = self.state_path.parent / "equity_carry.csv"
        is_new = not path.exists()
        with open(path, "a", encoding="utf-8") as fh:
            if is_new:
                fh.write("ts,equity\n")
            fh.write(f"{pd.Timestamp.now(tz='UTC').isoformat()},{self.equity:.2f}\n")

    def run_forever(self) -> None:
        mode = "LIVE" if self.live_broker is not None else "PAPER"
        log.info("Carry runner (%s) démarré : %s, levier %.1fx, entrée >%.0f%%/an, sortie <%.0f%%/an",
                 mode, self.symbol, self.leverage, self.enter_ann * 100, self.exit_ann * 100)
        while True:
            try:
                self._tick()
            except Exception:
                log.exception("Erreur carry (on continue)")
            time.sleep(TICK_SECONDS)
