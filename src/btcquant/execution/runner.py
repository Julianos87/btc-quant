"""Boucle d'exécution live/paper.

Principes :
- mêmes stratégies et même séquencement que le backtest : décision à la
  clôture de barre, exécution immédiate après (le live "ouvre" la barre
  suivante au moment où elle démarre) ;
- chaque stratégie a son propre sous-compte virtuel (fraction du capital),
  sa position et son stop, persistés dans un fichier d'état JSON → le bot
  peut redémarrer sans perdre le fil ;
- si le broker le permet (live ccxt), le stop est un vrai ordre côté
  exchange, remonté à chaque ratchet du stop suiveur ; sinon (paper) il est
  surveillé à chaque tick (60 s) ;
- coupe-circuits globaux : drawdown max → liquidation totale et arrêt ;
  perte journalière → plus d'entrées jusqu'au jour UTC suivant.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import ccxt
import pandas as pd

from ..data import _make_exchange
from ..indicators import bars_per_year, realized_vol
from ..notify import notify
from ..risk import RiskConfig, position_size
from ..strategies.base import Position, Strategy
from .broker import Broker

log = logging.getLogger(__name__)

TIMEFRAME_SECONDS = {"1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "12h": 43200, "1d": 86400}
TICK_SECONDS = 60
VOL_LOOKBACK = 30


class StrategySlot:
    def __init__(self, strategy: Strategy, capital_fraction: float, initial_cash: float) -> None:
        self.strategy = strategy
        self.capital_fraction = capital_fraction
        self.cash = initial_cash
        self.position: Position | None = None
        self.stop_order_id: str | None = None
        self.last_bar_ts: pd.Timestamp | None = None

    def equity(self, price: float) -> float:
        """Comptabilité sur marge : équity = cash + PnL latent (valide spot 1x
        et futures, longs comme shorts)."""
        return self.cash + (self.position.unrealized(price) if self.position else 0.0)


class LiveRunner:
    def __init__(
        self,
        slots: list[StrategySlot],
        broker: Broker,
        risk: RiskConfig,
        exchange_id: str,
        symbol: str,
        state_file: str | Path,
        poll_buffer_seconds: int = 20,
    ) -> None:
        self.slots = slots
        self.broker = broker
        self.risk = risk
        self.symbol = symbol
        self.state_path = Path(state_file)
        self.poll_buffer = poll_buffer_seconds
        self.data_exchange = _make_exchange(exchange_id)  # accès public, sans clés
        # taux de funding courant (public, futures USDT-M) pour les filtres
        self.funding_exchange = ccxt.binanceusdm({"enableRateLimit": True, "timeout": 30_000})
        self.peak_equity = sum(s.cash for s in slots)
        self.halted = False
        self.day: str | None = None
        self.day_start_equity = self.peak_equity
        self.daily_lockout = False
        self._load_state()

    # ── persistance ──────────────────────────────────────────────────────────
    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        raw = json.loads(self.state_path.read_text())
        for slot in self.slots:
            s = raw.get("slots", {}).get(slot.strategy.name)
            if not s:
                continue
            slot.cash = s["cash"]
            slot.stop_order_id = s.get("stop_order_id")
            slot.last_bar_ts = pd.Timestamp(s["last_bar_ts"]) if s.get("last_bar_ts") else None
            if s.get("position"):
                p = s["position"]
                slot.position = Position(
                    entry_time=pd.Timestamp(p["entry_time"]),
                    entry_price=p["entry_price"],
                    qty=p["qty"],
                    stop_price=p["stop_price"],
                    direction=p.get("direction", 1),
                    bars_held=p["bars_held"],
                    best_close=p["best_close"],
                )
        self.peak_equity = raw.get("peak_equity", self.peak_equity)
        self.halted = raw.get("halted", False)
        self.day = raw.get("day")
        self.day_start_equity = raw.get("day_start_equity", self.day_start_equity)
        self.daily_lockout = raw.get("daily_lockout", False)
        log.info("État rechargé depuis %s", self.state_path)

    def _save_state(self) -> None:
        raw = {
            "slots": {},
            "peak_equity": self.peak_equity,
            "halted": self.halted,
            "day": self.day,
            "day_start_equity": self.day_start_equity,
            "daily_lockout": self.daily_lockout,
        }
        for slot in self.slots:
            pos = None
            if slot.position:
                pos = {
                    "entry_time": str(slot.position.entry_time),
                    "entry_price": slot.position.entry_price,
                    "qty": slot.position.qty,
                    "stop_price": slot.position.stop_price,
                    "direction": slot.position.direction,
                    "bars_held": slot.position.bars_held,
                    "best_close": slot.position.best_close,
                }
            raw["slots"][slot.strategy.name] = {
                "cash": slot.cash,
                "position": pos,
                "stop_order_id": slot.stop_order_id,
                "last_bar_ts": str(slot.last_bar_ts) if slot.last_bar_ts is not None else None,
            }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(raw, indent=2))

    # ── données ──────────────────────────────────────────────────────────────
    def _fetch_frame(self, strategy: Strategy) -> pd.DataFrame:
        limit = min(1000, strategy.warmup_bars() + 60)
        raw = self.data_exchange.fetch_ohlcv(self.symbol, strategy.timeframe, limit=limit)
        df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df = df.set_index("timestamp").astype(float)
        return df.iloc[:-1]  # écarte la bougie en cours

    def _last_price(self) -> float:
        return float(self.data_exchange.fetch_ticker(self.symbol)["last"])

    # ── exécution ────────────────────────────────────────────────────────────
    def _exit_position(self, slot: StrategySlot, ref_price: float, reason: str) -> None:
        assert slot.position is not None
        pos = slot.position
        if slot.stop_order_id and self.broker.supports_stop_orders:
            self.broker.cancel_stop(slot.stop_order_id)
            slot.stop_order_id = None
        # clôture : on vend un long, on rachète un short
        if pos.direction == 1:
            fill = self.broker.market_sell(pos.qty, ref_price)
        else:
            fill = self.broker.market_buy(pos.qty, ref_price)
        pnl = pos.direction * fill.qty * (fill.price - pos.entry_price) - fill.fee
        slot.cash += pnl
        log.info("[%s] Sortie %s (%s) : %.6f @ %.2f", slot.strategy.name,
                 "LONG" if pos.direction == 1 else "SHORT", reason, fill.qty, fill.price)
        self._record_trade(slot, pos, fill.price, pnl, reason)
        notify(f"{'🟩' if pnl >= 0 else '🟥'} {slot.strategy.name} — sortie "
               f"{'LONG' if pos.direction == 1 else 'SHORT'} ({reason}) @ {fill.price:,.0f} $ : "
               f"{pnl:+,.2f} $")
        slot.position = None

    def _enter_position(self, slot: StrategySlot, row: pd.Series, ref_price: float, direction: int) -> None:
        stop = slot.strategy.initial_stop(row, ref_price, direction)
        rvol = row.get("_rvol")
        qty = position_size(
            slot.cash, ref_price, stop,
            float(rvol) if pd.notna(rvol) else None, self.risk,
            direction=direction,
        )
        live_balance = self.broker.free_quote_balance()
        if live_balance is not None:
            qty = min(qty, live_balance * 0.99 / ref_price)
        if qty <= 0:
            return
        if direction == 1:
            fill = self.broker.market_buy(qty, ref_price)
        else:
            fill = self.broker.market_sell(qty, ref_price)
        if fill.qty <= 0:
            log.error("[%s] Entrée non exécutée", slot.strategy.name)
            return
        slot.cash -= fill.fee
        slot.position = Position(
            entry_time=pd.Timestamp.now(tz="UTC"),
            entry_price=fill.price,
            qty=fill.qty,
            stop_price=stop,
            direction=direction,
            best_close=fill.price,
        )
        if self.broker.supports_stop_orders:
            slot.stop_order_id = self.broker.place_stop(fill.qty, stop, direction)
        log.info("[%s] Entrée %s : %.6f @ %.2f, stop %.2f", slot.strategy.name,
                 "LONG" if direction == 1 else "SHORT", fill.qty, fill.price, stop)
        notify(f"{'📈' if direction == 1 else '📉'} {slot.strategy.name} — entrée "
               f"{'LONG' if direction == 1 else 'SHORT'} {fill.qty:.5f} BTC @ {fill.price:,.0f} $, "
               f"stop {stop:,.0f} $")

    def _process_bar(self, slot: StrategySlot) -> None:
        df = self._fetch_frame(slot.strategy)
        if df.empty:
            return
        last_ts = df.index[-1]
        if slot.last_bar_ts is not None and last_ts <= slot.last_bar_ts:
            return  # pas de nouvelle barre clôturée
        data = slot.strategy.prepare(df)
        data["_rvol"] = realized_vol(
            data["close"], VOL_LOOKBACK, bars_per_year(slot.strategy.timeframe)
        )
        data["funding"] = float("nan")
        try:
            fr = self.funding_exchange.fetch_funding_rate(self.symbol)
            data.loc[data.index[-1], "funding"] = float(fr["fundingRate"])
        except Exception as e:  # le filtre funding devient neutre, on ne bloque pas le bot
            log.warning("Funding indisponible (%s) : filtre funding neutre sur cette barre", e)
        row = data.iloc[-1]
        slot.last_bar_ts = last_ts

        if slot.position is not None:
            # le stop exchange a-t-il été exécuté pendant qu'on ne regardait pas ?
            pos = slot.position
            if slot.stop_order_id and self.broker.supports_stop_orders:
                status = self.broker.stop_status(slot.stop_order_id)
                if status.get("status") == "closed":
                    fill_price = float(status.get("average") or pos.stop_price)
                    pnl = pos.direction * pos.qty * (fill_price - pos.entry_price)
                    slot.cash += pnl
                    log.info("[%s] Stop exchange exécuté @ %.2f", slot.strategy.name, fill_price)
                    self._record_trade(slot, pos, fill_price, pnl, "stop_exchange")
                    slot.position = None
                    slot.stop_order_id = None
                    return
            pos.bars_held += 1
            if pos.direction == 1:
                pos.best_close = max(pos.best_close, row["close"])
            else:
                pos.best_close = min(pos.best_close, row["close"])
            new_stop = slot.strategy.trailing_stop(row, pos)
            tightened = new_stop is not None and (
                (pos.direction == 1 and new_stop > pos.stop_price)
                or (pos.direction == -1 and new_stop < pos.stop_price)
            )
            if tightened:
                pos.stop_price = new_stop
                if slot.stop_order_id and self.broker.supports_stop_orders:
                    self.broker.cancel_stop(slot.stop_order_id)
                    slot.stop_order_id = self.broker.place_stop(pos.qty, new_stop, pos.direction)
            if self.halted or slot.strategy.exit_signal(row, pos):
                self._exit_position(slot, row["close"], "kill_switch" if self.halted else "signal")
        else:
            can_enter = not self.halted and not self.daily_lockout
            if can_enter:
                direction = int(slot.strategy.entry_signal(row))
                if direction != 0:
                    self._enter_position(slot, row, row["close"], direction)

    def _check_soft_stops(self, price: float) -> None:
        if self.broker.supports_stop_orders:
            return
        for slot in self.slots:
            if slot.position is None:
                continue
            hit = (
                price <= slot.position.stop_price
                if slot.position.direction == 1
                else price >= slot.position.stop_price
            )
            if hit:
                self._exit_position(slot, price, "stop")

    def _update_kill_switches(self, price: float) -> None:
        equity = sum(s.equity(price) for s in self.slots)
        self.peak_equity = max(self.peak_equity, equity)
        today = str(pd.Timestamp.now(tz="UTC").date())
        if today != self.day:
            self.day = today
            self.day_start_equity = equity
            self.daily_lockout = False
        if equity < self.peak_equity * (1.0 - self.risk.max_drawdown_halt):
            if not self.halted:
                log.error("KILL SWITCH drawdown : équity %.2f < %.2f", equity,
                          self.peak_equity * (1.0 - self.risk.max_drawdown_halt))
                notify(f"⛔ KILL-SWITCH : drawdown maximal atteint (équity {equity:,.0f} $). "
                       f"Liquidation et arrêt du moteur trend.")
            self.halted = True
        if (
            self.risk.daily_loss_limit is not None
            and equity < self.day_start_equity * (1.0 - self.risk.daily_loss_limit)
            and not self.daily_lockout
        ):
            log.warning("Limite de perte journalière atteinte : plus d'entrées aujourd'hui")
            self.daily_lockout = True

    def _record_trade(self, slot: StrategySlot, pos: Position, exit_price: float,
                      pnl: float, reason: str) -> None:
        """Journal structuré des trades clôturés — la matière première pour
        comparer le paper trading au backtest."""
        path = self.state_path.parent / "trades.csv"
        is_new = not path.exists()
        with open(path, "a", encoding="utf-8") as fh:
            if is_new:
                fh.write("exit_ts,entry_ts,strategy,direction,qty,entry_price,exit_price,pnl,bars_held,reason\n")
            fh.write(
                f"{pd.Timestamp.now(tz='UTC').isoformat()},{pos.entry_time.isoformat()},"
                f"{slot.strategy.name},{'LONG' if pos.direction == 1 else 'SHORT'},"
                f"{pos.qty:.8f},{pos.entry_price:.2f},{exit_price:.2f},{pnl:.2f},"
                f"{pos.bars_held},{reason}\n"
            )

    def _append_equity(self, price: float) -> None:
        """Historique d'équity marquée au marché (une ligne par tick, ~60 s)."""
        path = self.state_path.parent / "equity_trend.csv"
        is_new = not path.exists()
        with open(path, "a", encoding="utf-8") as fh:
            if is_new:
                fh.write("ts,equity\n")
            total = sum(s.equity(price) for s in self.slots)
            fh.write(f"{pd.Timestamp.now(tz='UTC').isoformat()},{total:.2f}\n")

    # ── boucle principale ────────────────────────────────────────────────────
    def run_forever(self) -> None:
        log.info(
            "Runner démarré : %s, stratégies %s",
            self.symbol, [s.strategy.name for s in self.slots],
        )
        if self.broker.supports_stop_orders:  # live : vérifier la cohérence avant de trader
            from .reconcile import reconcile
            reconcile(self.broker, self.slots, self.symbol)
        while True:
            try:
                price = self._last_price()
                self._check_soft_stops(price)
                for slot in self.slots:
                    tf_s = TIMEFRAME_SECONDS[slot.strategy.timeframe]
                    now = time.time()
                    seconds_into_bar = now % tf_s
                    if seconds_into_bar >= self.poll_buffer:
                        # une nouvelle barre a-t-elle été clôturée depuis le dernier passage ?
                        current_bar_start = pd.Timestamp(now - seconds_into_bar, unit="s", tz="UTC")
                        if slot.last_bar_ts is None or slot.last_bar_ts < current_bar_start - pd.Timedelta(seconds=tf_s):
                            self._process_bar(slot)
                self._update_kill_switches(price)
                if self.halted and all(s.position is None for s in self.slots):
                    self._save_state()
                    log.error("Kill switch actif et positions liquidées : arrêt du runner.")
                    return
                self._save_state()
                self._append_equity(price)
            except Exception:
                log.exception("Erreur dans la boucle principale (on continue)")
            time.sleep(TICK_SECONDS)
