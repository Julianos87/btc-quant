"""Boucle d'exécution live/paper.

Principes :
- mêmes stratégies et même séquencement que le backtest : décision à la
  clôture de barre, exécution immédiate après (le live "ouvre" la barre
  suivante au moment où elle démarre) ;
- chaque stratégie a son propre sous-compte virtuel (fraction du capital),
  sa position et son stop, persistés transactionnellement dans SQLite → le
  bot peut redémarrer sans perdre le fil ;
- si le broker le permet (live ccxt), le stop est un vrai ordre côté
  exchange, remonté à chaque ratchet du stop suiveur ; sinon (paper) il est
  surveillé à chaque tick (60 s) ;
- coupe-circuits globaux : drawdown max → liquidation totale et arrêt ;
  perte journalière → plus d'entrées jusqu'au jour UTC suivant.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, NoReturn

import pandas as pd

from ..domain import (
    BarDecision,
    EntryRequested,
    ExitRequested,
    StopTightened,
    decide_bar_close,
    funding_amount,
)
from ..indicators import bars_per_year, realized_vol
from ..notify import notify
from ..risk import RiskConfig, position_size
from ..strategies.base import Direction, Position, Strategy
from .broker import Broker, Fill
from .clock import SystemClock
from .data_quality import validate_closed_ohlcv
from .errors import ReconciliationRequired
from .funding_service import FundingService
from .order_service import OrderExecutionService
from .position_accounting import PositionAccountingService
from .ports import ClockPort, MarketDataPort, Notifier
from .protective_stops import ProtectiveStopService, StopDecisionKind
from .risk_service import PortfolioRiskService, PortfolioRiskState
from .recovery import recover_interrupted_orders
from .state_store import StateStore, database_path
from .venue import Venue

log = logging.getLogger(__name__)

TIMEFRAME_SECONDS = {"1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "12h": 43200, "1d": 86400}
TICK_SECONDS = 60
VOL_LOOKBACK = 30
FUNDING_POLL_SECONDS = 300


class StrategySlot:
    def __init__(self, strategy: Strategy, capital_fraction: float, initial_cash: float) -> None:
        self.strategy = strategy
        del capital_fraction
        self.cash = initial_cash
        self.position: Position | None = None
        self.stop_order_id: str | None = None
        self.last_bar_ts: pd.Timestamp | None = None
        #: frais d'entrée de la position ouverte — inclus dans le PnL du trade
        #: à la sortie (même convention que le backtest)
        self.entry_fee: float = 0.0

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
        legacy_state_file: str | Path | None = None,
        poll_buffer_seconds: int = 20,
        funding_rate_8h: float = 0.0,
        venue: MarketDataPort | None = None,
        notifier: Notifier = notify,
        clock: ClockPort | None = None,
        funding_service: FundingService | None = None,
        stop_service: ProtectiveStopService | None = None,
        risk_service: PortfolioRiskService | None = None,
        order_service: OrderExecutionService | None = None,
        accounting_service: PositionAccountingService | None = None,
    ) -> None:
        self.slots = slots
        self.broker = broker
        self.risk = risk
        self.symbol = symbol
        self.state_path = Path(state_file)
        self.legacy_state_path = (
            Path(legacy_state_file) if legacy_state_file is not None else self.state_path
        )
        self.store = StateStore(database_path(self.state_path))
        if self.store.path.name == "btcquant.db":
            self.store.migrate_legacy_journals(self.state_path.parent)
        self.poll_buffer = poll_buffer_seconds
        #: taux de repli quand le funding live est indisponible ; 0.0 = pas de
        #: funding simulé (spot). En perp, le funding est débité/crédité à
        #: chaque barre comme dans le backtest (les longs paient un taux > 0).
        self.funding_rate_8h = funding_rate_8h
        self._halt_notified = False
        # venue de données live (prix, bougies, funding) — accès public, sans
        # clés ; normalise les conventions (funding 8 h vs horaire), voir venue.py
        self.venue: MarketDataPort = venue or Venue(exchange_id, symbol)
        self.notifier = notifier
        self.clock: ClockPort = clock or SystemClock()
        self.funding_service = funding_service or FundingService(
            self.venue,
            self.clock,
            poll_seconds=FUNDING_POLL_SECONDS,
        )
        self.stop_service = stop_service or ProtectiveStopService(self.broker)
        self.risk_service = risk_service or PortfolioRiskService(self.risk)
        self.order_service = order_service or OrderExecutionService(self.store, self.broker)
        self.accounting_service = accounting_service or PositionAccountingService()
        self.peak_equity = sum(s.cash for s in slots)
        self.halted = False
        self.day: str | None = None
        self.day_start_equity = self.peak_equity
        self.daily_lockout = False
        self.reconciliation_required = False
        self.last_funding_ts: pd.Timestamp | None = None
        self._load_state()
        if self.reconciliation_required:
            raise ReconciliationRequired(
                "État trend marqué RECONCILIATION_REQUIRED : démarrage interdit"
            )
        recovery = recover_interrupted_orders(
            self.store,
            self.broker,
            "trend",
            external=self.broker.supports_stop_orders,
        )
        if not recovery.can_start:
            details = (
                f"manuel={recovery.manual_order_ids}, "
                f"erreurs_lookup={sorted(recovery.lookup_errors)}"
            )
            self.store.record_incident(
                "execution:trend:recovery_blocked",
                engine="trend",
                severity="CRITICAL",
                kind="recovery_blocked",
                message=f"Reprise trend bloquée : {details}",
                context={
                    "manual_order_ids": recovery.manual_order_ids,
                    "lookup_error_order_ids": sorted(recovery.lookup_errors),
                },
            )
            raise RuntimeError(
                f"Ordre(s) indéterminé(s) après crash ({details}) : "
                "réconciliation manuelle requise, démarrage interdit"
            )
        self.store.resolve_incident("execution:trend:recovery_blocked")

    # ── persistance ──────────────────────────────────────────────────────────
    def _load_state(self) -> None:
        self.store.migrate_legacy_json("trend", self.legacy_state_path)
        raw = self.store.load_engine_state("trend")
        if raw is None:
            return
        for slot in self.slots:
            s = raw.get("slots", {}).get(slot.strategy.name)
            if not s:
                continue
            slot.cash = s["cash"]
            slot.stop_order_id = s.get("stop_order_id")
            slot.entry_fee = s.get("entry_fee", 0.0)
            slot.last_bar_ts = pd.Timestamp(s["last_bar_ts"]) if s.get("last_bar_ts") else None
            if s.get("position"):
                p = s["position"]
                slot.position = Position(
                    entry_time=pd.Timestamp(p["entry_time"]),
                    entry_price=p["entry_price"],
                    qty=p["qty"],
                    stop_price=p["stop_price"],
                    direction=Direction(p.get("direction", 1)),
                    bars_held=p["bars_held"],
                    best_close=p["best_close"],
                )
        self.peak_equity = raw.get("peak_equity", self.peak_equity)
        self.halted = raw.get("halted", False)
        self.day = raw.get("day")
        self.day_start_equity = raw.get("day_start_equity", self.day_start_equity)
        self.daily_lockout = raw.get("daily_lockout", False)
        self.reconciliation_required = raw.get("reconciliation_required", False)
        if raw.get("last_funding_ts"):
            self.last_funding_ts = pd.Timestamp(raw["last_funding_ts"])
        log.info("État trend rechargé depuis %s", self.store.path)

    def _state_payload(self) -> dict[str, Any]:
        raw: dict[str, Any] = {
            "slots": {},
            "peak_equity": self.peak_equity,
            "halted": self.halted,
            "day": self.day,
            "day_start_equity": self.day_start_equity,
            "daily_lockout": self.daily_lockout,
            "reconciliation_required": self.reconciliation_required,
            "last_funding_ts": (
                self.last_funding_ts.isoformat() if self.last_funding_ts is not None else None
            ),
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
                "entry_fee": slot.entry_fee,
                "last_bar_ts": str(slot.last_bar_ts) if slot.last_bar_ts is not None else None,
            }
        return raw

    def _save_state(self) -> None:
        self.store.save_engine_state("trend", self._state_payload())

    def _require_manual_reconciliation(
        self,
        message: str,
        *,
        slot: StrategySlot | None = None,
        context: dict[str, Any] | None = None,
    ) -> NoReturn:
        self.reconciliation_required = True
        payload = {"slot": slot.strategy.name if slot else None, **(context or {})}
        self.store.record_incident(
            "execution:trend:protective_order_uncertain",
            engine="trend",
            severity="CRITICAL",
            kind="protective_order_uncertain",
            message=message,
            context=payload,
        )
        self._save_state()
        self.notifier(f"⛔ TREND : {message}. Réconciliation manuelle requise.")
        raise ReconciliationRequired(message)

    def _monitor_exchange_stops(self) -> None:
        """Rapproche chaque stop à chaque tick et échoue fermé en cas d'ambiguïté."""

        if not self.broker.supports_stop_orders:
            return
        for slot in self.slots:
            pos = slot.position
            decision = self.stop_service.inspect(
                stop_id=slot.stop_order_id,
                qty=pos.qty if pos else None,
                stop_price=pos.stop_price if pos else None,
                direction=pos.direction if pos else None,
            )
            if decision.kind == StopDecisionKind.NOOP:
                continue
            if decision.kind == StopDecisionKind.UNCERTAIN:
                self._require_manual_reconciliation(
                    decision.message or "État du stop protecteur incertain",
                    slot=slot,
                    context=decision.context,
                )
            if decision.kind == StopDecisionKind.REPLACED:
                slot.stop_order_id = decision.replacement_stop_id
                if decision.previous_stop_id is not None:
                    self.store.append_event(
                        "trend",
                        "protective_order_recreated",
                        {
                            "slot": slot.strategy.name,
                            "previous_stop_id": decision.previous_stop_id,
                            "replacement_stop_id": decision.replacement_stop_id,
                            "previous_status": decision.previous_status,
                        },
                        aggregate_type="strategy",
                        aggregate_id=slot.strategy.name,
                    )
                self._save_state()
                continue
            assert pos is not None
            assert decision.snapshot is not None
            assert decision.previous_stop_id is not None
            snapshot = decision.snapshot
            stop_id = decision.previous_stop_id
            fill_price = snapshot.average_price or pos.stop_price
            pnl = pos.direction * pos.qty * (fill_price - pos.entry_price) - snapshot.fee
            slot.cash += pnl
            trade = self._trade_payload(
                slot,
                pos,
                fill_price,
                pnl - slot.entry_fee,
                "stop_exchange",
            )
            filled_qty = pos.qty
            side = "SELL" if pos.direction == 1 else "BUY"
            slot.position = None
            slot.stop_order_id = None
            slot.entry_fee = 0.0
            self.store.record_observed_fill_and_checkpoint(
                engine="trend",
                slot=slot.strategy.name,
                intent_id=f"observed-stop-{stop_id}",
                broker_order_id=snapshot.broker_order_id,
                side=side,
                requested_qty=filled_qty,
                filled_qty=filled_qty,
                price=fill_price,
                fee=snapshot.fee,
                reason="stop_exchange",
                state=self._state_payload(),
                trade=trade,
            )
            self.store.resolve_incident("execution:trend:protective_order_uncertain")
            self.notifier(
                f"{'🟩' if trade['pnl'] >= 0 else '🟥'} {slot.strategy.name} — "
                f"stop exchange exécuté @ {fill_price:,.0f} $ : {trade['pnl']:+,.2f} $"
            )

    def _apply_funding_payments(self, mark_price: float) -> None:
        """Applique chaque paiement natif une fois, selon son horodatage."""

        poll = self.funding_service.poll(self.last_funding_ts)
        if poll is None:
            return
        if poll.initialized:
            self.last_funding_ts = poll.checkpoint
            self.store.save_engine_state(
                "trend",
                self._state_payload(),
                event_type="funding_checkpoint_initialized",
                event_payload={"last_funding_ts": poll.checkpoint.isoformat()},
            )
            return
        if not poll.payments:
            return
        applied: list[dict[str, Any]] = []
        for payment in poll.payments:
            slot_amounts: dict[str, float] = {}
            for slot in self.slots:
                if slot.position is None:
                    continue
                amount = funding_amount(slot.position, payment.rate, mark_price)
                slot.cash -= amount
                slot_amounts[slot.strategy.name] = amount
            applied.append(
                {
                    "ts": payment.timestamp.isoformat(),
                    "rate": payment.rate,
                    "amounts": slot_amounts,
                }
            )
        self.last_funding_ts = poll.checkpoint
        self.store.save_engine_state(
            "trend",
            self._state_payload(),
            event_type="funding_payments_applied",
            event_payload={"payments": applied},
        )

    def _execute_market_order(
        self,
        slot: StrategySlot,
        side: str,
        qty: float,
        ref_price: float,
        reason: str,
        available_volume: float | None = None,
    ) -> tuple[Fill, int, str]:
        submitted = self.order_service.submit_market(
            engine="trend",
            slot=slot.strategy.name,
            side=side,
            qty=qty,
            reference_price=ref_price,
            reason=reason,
            available_volume=available_volume,
        )
        return submitted.fill, submitted.order_id, submitted.status

    # ── données ──────────────────────────────────────────────────────────────
    def _fetch_frame(self, strategy: Strategy) -> pd.DataFrame:
        # 1000 barres (max d'un appel) et non warmup+60 : une EMA200 n'est pas
        # convergée à 280 barres (~45 % du poids initial subsiste) — mesuré :
        # 3,1 % des barres avaient un régime EMA différent du backtest à 280
        # barres, 0 % à 1000. Le surcoût réseau est négligeable.
        limit = 1000
        raw = self.venue.fetch_ohlcv(strategy.timeframe, limit=limit)
        df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df = df.set_index("timestamp").astype(float)
        return validate_closed_ohlcv(
            df,
            timeframe_seconds=TIMEFRAME_SECONDS[strategy.timeframe],
            now=self.clock.utc_now(),
        )

    def _last_price(self) -> float:
        return self.venue.last_price()

    # ── exécution ────────────────────────────────────────────────────────────
    def _exit_position(
        self,
        slot: StrategySlot,
        ref_price: float,
        reason: str,
        available_volume: float | None = None,
    ) -> None:
        assert slot.position is not None
        pos = slot.position
        # clôture : on vend un long, on rachète un short
        side = "SELL" if pos.direction == 1 else "BUY"
        fill, order_id, order_status = self._execute_market_order(
            slot,
            side,
            pos.qty,
            ref_price,
            reason,
            available_volume,
        )
        if fill.qty <= 0:
            self.store.complete_order_and_checkpoint(
                order_id,
                engine="trend",
                state=self._state_payload(),
                status=order_status,
                filled_qty=fill.qty,
                price=fill.price,
                fee=fill.fee,
                broker_order_id=fill.broker_order_id,
            )
            # ordre non exécuté : on GARDE la position (nouvel essai au tick
            # suivant via soft stop / condition de sortie toujours vraie)
            log.error(
                "[%s] Sortie non exécutée (fill nul) — position conservée", slot.strategy.name
            )
            self.notifier(f"⚠ {slot.strategy.name} : ordre de sortie non exécuté, on retentera")
            return
        accounting = self.accounting_service.close_position(
            pos,
            fill,
            entry_fee=slot.entry_fee,
        )
        slot.cash += accounting.cash_delta
        partial = accounting.partial
        trade_pnl = accounting.trade_pnl
        slot.entry_fee = accounting.remaining_entry_fee
        log.info(
            "[%s] Sortie %s (%s) : %.6f @ %.2f",
            slot.strategy.name,
            "LONG" if pos.direction == 1 else "SHORT",
            reason,
            fill.qty,
            fill.price,
        )
        trade = self._trade_payload(
            slot,
            pos,
            fill.price,
            trade_pnl,
            reason,
            qty=fill.qty,
        )
        if partial:
            assert accounting.remaining_position is not None
            slot.position = accounting.remaining_position
            pos = accounting.remaining_position
            if slot.stop_order_id and self.broker.supports_stop_orders:
                # Le stop existant reste actif jusqu'à ce que son remplaçant
                # protège effectivement la quantité résiduelle.
                previous_stop_id = slot.stop_order_id
                replacement_id = self.broker.place_stop(pos.qty, pos.stop_price, pos.direction)
                if replacement_id is not None:
                    self.broker.cancel_stop(previous_stop_id)
                    slot.stop_order_id = replacement_id
            log.warning(
                "[%s] Sortie PARTIELLE : %.6f restant en position", slot.strategy.name, pos.qty
            )
        else:
            if slot.stop_order_id and self.broker.supports_stop_orders:
                self.broker.cancel_stop(slot.stop_order_id)
                slot.stop_order_id = None
            slot.position = None
        self.store.complete_order_and_checkpoint(
            order_id,
            engine="trend",
            state=self._state_payload(),
            status=order_status,
            filled_qty=fill.qty,
            price=fill.price,
            fee=fill.fee,
            broker_order_id=fill.broker_order_id,
            trade=trade,
        )
        self.notifier(
            f"{'🟩' if trade_pnl >= 0 else '🟥'} {slot.strategy.name} — sortie "
            f"{'LONG' if pos.direction == 1 else 'SHORT'} ({reason}) @ {fill.price:,.0f} $ : "
            f"{trade_pnl:+,.2f} $"
        )
        if partial:
            self.notifier(f"⚠ {slot.strategy.name} : sortie partielle, {pos.qty:.6f} BTC restants")

    def _enter_position(
        self, slot: StrategySlot, row: pd.Series, ref_price: float, direction: int
    ) -> None:
        stop = slot.strategy.initial_stop(row, ref_price, direction)
        rvol = row.get("_rvol")
        qty = position_size(
            slot.cash,
            ref_price,
            stop,
            float(rvol) if pd.notna(rvol) else None,
            self.risk,
            direction=direction,
        )
        live_balance = self.broker.free_quote_balance()
        if live_balance is not None:
            qty = min(qty, live_balance * 0.99 / ref_price)
        if qty <= 0:
            return
        side = "BUY" if direction == 1 else "SELL"
        fill, order_id, order_status = self._execute_market_order(
            slot,
            side,
            qty,
            ref_price,
            "entry",
            float(row["volume"]) if pd.notna(row.get("volume")) else None,
        )
        if fill.qty <= 0:
            self.store.complete_order_and_checkpoint(
                order_id,
                engine="trend",
                state=self._state_payload(),
                status=order_status,
                filled_qty=fill.qty,
                price=fill.price,
                fee=fill.fee,
                broker_order_id=fill.broker_order_id,
            )
            log.error("[%s] Entrée non exécutée", slot.strategy.name)
            return
        accounting = self.accounting_service.open_position(
            fill,
            entry_time=self.clock.utc_now(),
            stop_price=stop,
            direction=direction,
        )
        slot.cash += accounting.cash_delta
        slot.entry_fee = accounting.entry_fee
        slot.position = accounting.position
        if self.broker.supports_stop_orders:
            slot.stop_order_id = self.broker.place_stop(fill.qty, stop, direction)
        self.store.complete_order_and_checkpoint(
            order_id,
            engine="trend",
            state=self._state_payload(),
            status=order_status,
            filled_qty=fill.qty,
            price=fill.price,
            fee=fill.fee,
            broker_order_id=fill.broker_order_id,
        )
        log.info(
            "[%s] Entrée %s : %.6f @ %.2f, stop %.2f",
            slot.strategy.name,
            "LONG" if direction == 1 else "SHORT",
            fill.qty,
            fill.price,
            stop,
        )
        self.notifier(
            f"{'📈' if direction == 1 else '📉'} {slot.strategy.name} — entrée "
            f"{'LONG' if direction == 1 else 'SHORT'} {fill.qty:.5f} BTC @ {fill.price:,.0f} $, "
            f"stop {stop:,.0f} $"
        )

    def _process_bar(self, slot: StrategySlot) -> BarDecision | None:
        df = self._fetch_frame(slot.strategy)
        if df.empty:
            return None
        last_ts = df.index[-1]
        if slot.last_bar_ts is not None and last_ts <= slot.last_bar_ts:
            return None  # pas de nouvelle barre clôturée
        data = slot.strategy.prepare(df)
        data["_rvol"] = realized_vol(
            data["close"], VOL_LOOKBACK, bars_per_year(slot.strategy.timeframe)
        )
        data["funding"] = float("nan")
        try:
            # toujours en équivalent 8 h (convention des filtres et du backtest),
            # quelle que soit la périodicité native de la venue
            data.loc[data.index[-1], "funding"] = self.venue.funding_rate_8h()
        except Exception as e:  # le filtre funding devient neutre, on ne bloque pas le bot
            if self.funding_rate_8h:
                data.loc[data.index[-1], "funding"] = self.funding_rate_8h
            else:
                log.warning("Funding indisponible (%s) : filtre funding neutre sur cette barre", e)
        row = data.iloc[-1]

        if slot.position is not None:
            pos = slot.position
            # 1. décision métier pure (aucun ordre, aucune mutation, aucun I/O)
            decision = decide_bar_close(
                slot.strategy,
                row,
                pos,
                halted=self.halted,
            )
            assert decision.position is not None
            next_position = decision.position
            stop_event = next(
                (event for event in decision.events if isinstance(event, StopTightened)),
                None,
            )
            exit_event = next(
                (event for event in decision.events if isinstance(event, ExitRequested)),
                None,
            )
            # 2. barre marquée traitée + mutations locales (jamais rejouées)
            slot.last_bar_ts = last_ts
            pos.best_close = next_position.best_close
            pos.bars_held = next_position.bars_held
            # 3. appels externes, at-most-once (un échec ici ne rejoue pas la
            # barre ; l'ancien stop exchange continue de protéger en attendant)
            if stop_event and slot.stop_order_id and self.broker.supports_stop_orders:
                previous_stop_id = slot.stop_order_id
                replacement_id = self.broker.place_stop(
                    pos.qty,
                    stop_event.new_price,
                    pos.direction,
                )
                if replacement_id is not None:
                    self.broker.cancel_stop(previous_stop_id)
                    slot.stop_order_id = replacement_id
                    pos.stop_price = stop_event.new_price
            elif stop_event:
                pos.stop_price = stop_event.new_price
            if exit_event:
                self._exit_position(
                    slot,
                    row["close"],
                    exit_event.reason,
                    float(row["volume"]) if pd.notna(row.get("volume")) else None,
                )
            return decision
        else:
            slot.last_bar_ts = last_ts
            can_enter = not self.halted and not self.daily_lockout
            decision = decide_bar_close(
                slot.strategy,
                row,
                None,
                can_enter=can_enter,
            )
            for event in decision.events:
                if isinstance(event, EntryRequested):
                    self._enter_position(slot, row, row["close"], event.direction)
            return decision

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
        today = str(self.clock.utc_now().date())
        transition = self.risk_service.evaluate(
            PortfolioRiskState(
                peak_equity=self.peak_equity,
                day=self.day,
                day_start_equity=self.day_start_equity,
                halted=self.halted,
                daily_lockout=self.daily_lockout,
            ),
            equity=equity,
            day=today,
        )
        state = transition.state
        self.peak_equity = state.peak_equity
        self.day = state.day
        self.day_start_equity = state.day_start_equity
        self.halted = state.halted
        self.daily_lockout = state.daily_lockout
        if transition.halt_triggered:
            log.error(
                "KILL SWITCH drawdown : équity %.2f < %.2f",
                equity,
                self.peak_equity * (1.0 - self.risk.max_drawdown_halt),
            )
            self.notifier(
                f"⛔ KILL-SWITCH : drawdown maximal atteint (équity {equity:,.0f} $). "
                f"Liquidation et arrêt du moteur trend."
            )
        if transition.lockout_triggered:
            log.warning("Limite de perte journalière atteinte : plus d'entrées aujourd'hui")
            self.notifier(
                f"🔒 Lockout journalier : perte du jour > {self.risk.daily_loss_limit:.0%} "
                f"(équity {equity:,.0f} $). Plus d'entrées trend avant demain 00:00 UTC."
            )

    def _liquidate_if_halted(self, price: float) -> None:
        """Tente la liquidation à chaque tick dès que le kill switch est actif."""
        if not self.halted:
            return
        for slot in self.slots:
            if slot.position is not None:
                self._exit_position(slot, price, "kill_switch")

    def _trade_payload(
        self,
        slot: StrategySlot,
        pos: Position,
        exit_price: float,
        pnl: float,
        reason: str,
        *,
        qty: float | None = None,
    ) -> dict[str, Any]:
        return {
            "exit_ts": self.clock.utc_now().isoformat(),
            "entry_ts": pos.entry_time.isoformat(),
            "strategy": slot.strategy.name,
            "direction": "LONG" if pos.direction == 1 else "SHORT",
            "qty": pos.qty if qty is None else qty,
            "entry_price": pos.entry_price,
            "exit_price": exit_price,
            "pnl": pnl,
            "bars_held": pos.bars_held,
            "reason": reason,
        }

    def _append_equity(self, price: float) -> None:
        """Historique d'équity marquée au marché (une ligne par tick, ~60 s)."""
        total = sum(s.equity(price) for s in self.slots)
        self.store.append_equity("trend", total)

    # ── boucle principale ────────────────────────────────────────────────────
    def run_forever(self, stop_event: threading.Event | None = None) -> None:
        stop_event = stop_event or threading.Event()
        last_price: float | None = None
        log.info(
            "Runner démarré : %s, stratégies %s",
            self.symbol,
            [s.strategy.name for s in self.slots],
        )
        if self.broker.supports_stop_orders:  # live : vérifier la cohérence avant de trader
            from .reconcile import reconcile

            if not reconcile(self.broker, self.slots, self.symbol):
                raise RuntimeError("Réconciliation live échouée : runner arrêté (fail-closed)")
        try:
            while not stop_event.is_set():
                try:
                    price = self._last_price()
                    last_price = price
                    self._apply_funding_payments(price)
                    self._monitor_exchange_stops()
                    # Le risque est évalué avant toute stratégie : un kill switch
                    # liquide au tick courant, sans attendre la prochaine barre.
                    self._update_kill_switches(price)
                    self._liquidate_if_halted(price)
                    self._check_soft_stops(price)
                    for slot in self.slots:
                        tf_s = TIMEFRAME_SECONDS[slot.strategy.timeframe]
                        now = self.clock.time()
                        seconds_into_bar = now % tf_s
                        if seconds_into_bar >= self.poll_buffer:
                            # une nouvelle barre a-t-elle été clôturée depuis le dernier passage ?
                            current_bar_start = pd.Timestamp(
                                now - seconds_into_bar, unit="s", tz="UTC"
                            )
                            if (
                                slot.last_bar_ts is None
                                or slot.last_bar_ts < current_bar_start - pd.Timedelta(seconds=tf_s)
                            ):
                                self._process_bar(slot)
                    if self.halted and all(s.position is None for s in self.slots):
                        # on reste vivant en veille (state maintenu frais pour le
                        # watchdog) au lieu de sortir : avec Restart=always, un
                        # return provoquerait une boucle de redémarrage infinie
                        if not self._halt_notified:
                            log.error(
                                "Kill switch actif et positions liquidées : moteur en veille "
                                "(intervention manuelle requise pour reprendre)."
                            )
                            self._halt_notified = True
                        self._save_state()
                        self._append_equity(price)
                        stop_event.wait(TICK_SECONDS)
                        continue
                    self._save_state()
                    self._append_equity(price)
                except ReconciliationRequired:
                    log.critical("Arrêt fail-closed : réconciliation manuelle requise")
                    raise
                except Exception:
                    log.exception("Erreur dans la boucle principale (on continue)")
                stop_event.wait(TICK_SECONDS)
        finally:
            self._save_state()
            if last_price is not None:
                self._append_equity(last_price)
            log.info("Runner arrêté proprement ; checkpoint final enregistré")
