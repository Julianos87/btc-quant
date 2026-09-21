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

import hashlib
import json
import logging
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NoReturn

import pandas as pd

from ..domain import (
    BarDecision,
    EntryRequested,
    ExitRequested,
    PyramidRequested,
    StopTightened,
    decide_bar_close,
)
from ..domain.execution import ExecutionSimulator
from ..carry import funding_event_id
from ..indicators import bars_per_year, realized_vol
from ..backup import assert_writer_recovery_clear
from ..notify import notify
from ..risk import RiskConfig, position_size
from ..strategies.base import Direction, Position, Strategy
from .broker import Broker, Fill
from .atomic_financial_writer import StateStoreAtomicFinancialWriter
from .clock import SystemClock
from .data_quality import validate_closed_ohlcv
from .errors import ReconciliationRequired
from .external_settlement_runtime import ExternalSettlementRuntime
from .funding_service import FundingService
from .instance_lock import EngineInstanceLock
from .order_service import OrderExecutionService, SubmittedOrder, SubmitMarketCommand
from .paper_execution_evidence import (
    PaperExecutionEvidenceContext,
    build_paper_execution_evidence,
)
from .reconciliation_coordinator import (
    OrderReconciliationCoordinator,
    ReconciliationResult,
    ReconciliationStatus,
)
from .financial_application_plan import (
    FinancialApplicationPlan,
    position_generation_from_payload,
    sha256_json,
)
from .order_state import FinancialTransitionType, LogicalOrderIdentity
from .ports import ClockPort, MarketDataPort, Notifier
from .position_accounting import PositionAccountingService
from .protective_stops import ProtectiveStopService, StopDecision, StopDecisionKind
from .margin import MarginSnapshot, SharedCrossMarginModel
from .recovery import recover_interrupted_orders
from .risk_service import PortfolioRiskService, PortfolioRiskState
from .state_contract import (
    PositionState,
    TrendSlotState,
    TrendStatePayload,
    stop_protection_mode_from_broker,
    validate_trend_state,
)
from .state_store import StateStore, database_path
from .venue import Venue

log = logging.getLogger(__name__)

TIMEFRAME_SECONDS = {"1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "12h": 43200, "1d": 86400}
TICK_SECONDS = 60
VOL_LOOKBACK = 30
FUNDING_POLL_SECONDS = 300
POSITION_RECONCILIATION_SECONDS = 300
STOP_TERMINAL_NO_FILL = {"CANCELED", "REJECTED", "EXPIRED"}
#: Une erreur isolée (réseau, bougie en retard) est normale ; trois échecs
#: consécutifs signalent une panne durable qui doit remonter en incident.
LOOP_FAILURES_BEFORE_INCIDENT = 3


class StrategySlot:
    def __init__(self, strategy: Strategy, capital_fraction: float, initial_cash: float) -> None:
        self.strategy = strategy
        del capital_fraction
        self.cash = initial_cash
        self.position: Position | None = None
        self.stop_order_id: str | None = None
        self.stop_order_local_id: int | None = None
        self.stop_intent_id: str | None = None
        self.stop_transition: dict[str, Any] | None = None
        self.last_bar_ts: pd.Timestamp | None = None
        self.financial_transition_seq = 0
        #: frais d'entrée de la position ouverte — inclus dans le PnL du trade
        #: à la sortie (même convention que le backtest)
        self.entry_fee: float = 0.0
        # Durable position snapshots used to reconstruct exposure at each
        # funding timestamp. A current position is not a historical ledger.
        self.position_timeline: list[dict[str, Any]] = []

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
        external_settlement_runtime: ExternalSettlementRuntime | None = None,
    ) -> None:
        self.slots = slots
        self.broker = broker
        self.risk = risk
        self.exchange_id = exchange_id
        self.symbol = symbol
        self.state_path = Path(state_file)
        self.legacy_state_path = (
            Path(legacy_state_file) if legacy_state_file is not None else self.state_path
        )
        # A restored trading state is never safe to run merely because the
        # SQLite file opens. The backup layer writes an external recovery
        # marker and only explicit reconciliation may clear it.
        assert_writer_recovery_clear(self.state_path.parent)
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
        self.external_settlement_runtime = external_settlement_runtime
        if (
            self.external_settlement_runtime is None
            and ExternalSettlementRuntime.is_qualified_broker(self.broker)
        ):
            self.external_settlement_runtime = ExternalSettlementRuntime(
                self.store,
                self.broker,
                self.clock,
            )
        self.reconciliation_coordinator = OrderReconciliationCoordinator(
            self.store,
            financial_writer=StateStoreAtomicFinancialWriter(self.store),
        )
        self.peak_equity = sum(s.cash for s in slots)
        self.halted = False
        self.day: str | None = None
        self.day_start_equity = self.peak_equity
        self.daily_lockout = False
        self.reconciliation_required = False
        self.last_funding_ts: pd.Timestamp | None = None
        self.execution_context: dict[str, Any] | None = None
        self.margin_model = SharedCrossMarginModel(
            max_leverage=risk.max_leverage,
            venue=exchange_id,
        )
        self._last_margin_snapshot: MarginSnapshot | None = None
        self._last_position_reconciliation_at: float | None = None
        self._startup_lock = EngineInstanceLock(self.store.path, "trend")
        self._startup_lock.acquire()
        self._load_state()
        if self.reconciliation_required:
            raise ReconciliationRequired(
                "État trend marqué RECONCILIATION_REQUIRED : démarrage interdit"
            )
        recovery_can_start: bool
        recovery_manual_order_ids: tuple[int, ...] | list[int]
        recovery_lookup_errors: dict[int, str]
        if self.external_settlement_runtime is not None:
            external_recovery = self.external_settlement_runtime.recover_startup(
                observed_at=self.clock.utc_now().isoformat()
            )
            recovery_can_start = external_recovery.can_start
            recovery_manual_order_ids = external_recovery.manual_order_ids
            recovery_lookup_errors = dict(external_recovery.blocking_reasons)
        else:
            recovery = recover_interrupted_orders(
                self.store,
                self.broker,
                "trend",
                external=self.broker.external_execution,
            )
            recovery_can_start = recovery.can_start
            recovery_manual_order_ids = recovery.manual_order_ids
            recovery_lookup_errors = recovery.lookup_errors
        if not recovery_can_start:
            details = (
                f"manuel={recovery_manual_order_ids}, "
                f"erreurs_lookup={sorted(recovery_lookup_errors)}"
            )
            self.store.record_incident(
                "execution:trend:recovery_blocked",
                engine="trend",
                severity="CRITICAL",
                kind="recovery_blocked",
                message=f"Reprise trend bloquée : {details}",
                context={
                    "manual_order_ids": recovery_manual_order_ids,
                    "lookup_error_order_ids": sorted(recovery_lookup_errors),
                },
            )
            raise RuntimeError(
                f"Ordre(s) indéterminé(s) après crash ({details}) : "
                "réconciliation manuelle requise, démarrage interdit"
            )
        self.store.resolve_incident("execution:trend:recovery_blocked")
        # Recovery may have committed finalization after the initial
        # constructor load. Refresh memory from that durable state before
        # reconstructing any legacy timeline or evaluating a strategy decision.
        self._load_state(reconstruct_legacy_timelines=True)

        self._startup_lock.release()

    # ── persistance ──────────────────────────────────────────────────────────
    def _reconstruct_missing_position_timelines(self, stored: Mapping[str, Any]) -> None:
        """Rebuild legacy timelines from committed financial applications."""

        missing_slots = [
            slot for slot in self.slots if slot.position is not None and not slot.position_timeline
        ]
        if not missing_slots:
            return

        records = self.store.read_financial_position_transitions("trend")
        grouped: dict[str, list[dict[str, Any]]] = {
            slot.strategy.name: [] for slot in missing_slots
        }
        for record in records:
            slot_name = str(record.get("slot") or "")
            if slot_name in grouped:
                grouped[slot_name].append(record)

        repaired_slots: list[str] = []
        application_keys: list[str] = []
        for slot in missing_slots:
            slot_records = grouped[slot.strategy.name]
            if not slot_records:
                # Older/manual checkpoints may predate the financial journal.
                # Leave them untouched; funding will still fail closed because
                # no historical exposure can be proven.
                continue
            timeline: list[dict[str, Any]] = []
            last_position: dict[str, Any] | None = None
            last_direction = 1
            for record in slot_records:
                try:
                    result = json.loads(str(record["result_payload"]))
                    state_after = result["state_after_payload"]
                    if not isinstance(state_after, dict):
                        raise ValueError("state_after_payload non objet")
                    if sha256_json(state_after) != str(record["state_after_sha256"]):
                        raise ValueError("hash state_after divergent")
                    after_slots = state_after["slots"]
                    after_slot = after_slots[slot.strategy.name]
                    if not isinstance(after_slot, dict):
                        raise ValueError("slot state_after non objet")
                    position = after_slot.get("position")
                    if position is not None:
                        if not isinstance(position, dict):
                            raise ValueError("position state_after non objet")
                        qty = float(position["qty"])
                        direction = int(position["direction"])
                        generation = position_generation_from_payload(position)
                        last_position = position
                        last_direction = direction
                    else:
                        qty = 0.0
                        direction = last_direction
                        generation = str(record.get("position_generation") or "FLAT")
                        last_position = None
                    effective_at = str(record["economic_effect_at"])
                    if not effective_at:
                        raise ValueError("economic_effect_at absent")
                    timeline.append(
                        {
                            "effective_at": effective_at,
                            "intent_id": str(record["intent_id"]),
                            "qty": qty,
                            "direction": direction,
                            "generation": generation,
                        }
                    )
                    application_keys.append(str(record["application_key"]))
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                    raise ReconciliationRequired(
                        f"Timeline {slot.strategy.name} non reconstructible depuis "
                        f"l'application financière {record.get('application_key')}"
                    ) from error

            current = slot.position
            if not timeline or last_position is None or current is None:
                raise ReconciliationRequired(
                    f"Timeline {slot.strategy.name} absente ou incompatible avec la position courante"
                )
            if (
                abs(float(last_position["qty"]) - float(current.qty)) > 1e-12
                or int(last_position["direction"]) != int(current.direction)
                or position_generation_from_payload(last_position)
                != self._position_generation(current)
            ):
                raise ReconciliationRequired(
                    f"Timeline {slot.strategy.name} divergente de la position courante"
                )
            slot.position_timeline = timeline
            repaired_slots.append(slot.strategy.name)

        if not repaired_slots:
            return
        before_hash = sha256_json(stored)
        repaired_state = self._state_payload()
        self.store.save_engine_state(
            "trend",
            repaired_state,
            event_type="position_timeline_reconstructed",
            event_payload={
                "source": "financial_fill_applications",
                "slots": repaired_slots,
                "application_keys": application_keys,
                "state_before_sha256": before_hash,
                "state_after_sha256": sha256_json(repaired_state),
            },
        )
        self.store.resolve_incident("accounting:trend:funding_uncertainty")

    def _load_state(self, *, reconstruct_legacy_timelines: bool = False) -> None:
        self.store.migrate_legacy_json("trend", self.legacy_state_path)
        stored = self.store.load_engine_state("trend")
        raw = validate_trend_state(stored) if stored is not None else None
        if raw is None:
            return
        for slot in self.slots:
            s = raw.get("slots", {}).get(slot.strategy.name)
            if not s:
                continue
            slot.cash = s["cash"]
            slot.stop_order_id = s.get("stop_order_id")
            slot.stop_order_local_id = s.get("stop_order_local_id")
            slot.stop_intent_id = s.get("stop_intent_id")
            slot.stop_transition = s.get("stop_transition")
            slot.entry_fee = s.get("entry_fee", 0.0)
            slot.position_timeline = list(s.get("position_timeline", []))
            slot.financial_transition_seq = s.get("financial_transition_seq", 0)
            last_bar_ts = s.get("last_bar_ts")
            slot.last_bar_ts = pd.Timestamp(last_bar_ts) if last_bar_ts else None
            p = s.get("position")
            if p is None:
                slot.position = None
            else:
                slot.position = Position(
                    entry_time=pd.Timestamp(p["entry_time"]),
                    entry_price=p["entry_price"],
                    qty=p["qty"],
                    stop_price=p["stop_price"],
                    direction=Direction(p.get("direction", 1)),
                    bars_held=p["bars_held"],
                    best_close=p["best_close"],
                    initial_qty=p.get("initial_qty", p["qty"]),
                    last_add_price=p.get("last_add_price", p["entry_price"]),
                    pyramid_adds=p.get("pyramid_adds", 0),
                )
                restore_stop = getattr(self.broker, "restore_protective_stop", None)
                if slot.stop_order_id is not None and callable(restore_stop):
                    restore_stop(
                        str(slot.stop_order_id),
                        qty=slot.position.qty,
                        stop_price=slot.position.stop_price,
                        direction=int(slot.position.direction),
                        client_order_id=slot.stop_intent_id,
                    )
        self.peak_equity = raw.get("peak_equity", self.peak_equity)
        self.halted = raw.get("halted", False)
        self.day = raw.get("day")
        self.day_start_equity = raw.get("day_start_equity", self.day_start_equity)
        self.daily_lockout = raw.get("daily_lockout", False)
        self.reconciliation_required = raw.get("reconciliation_required", False)
        context = raw.get("execution_context")
        self.execution_context = dict(context) if isinstance(context, dict) else None
        last_funding_ts = raw.get("last_funding_ts")
        if last_funding_ts:
            self.last_funding_ts = pd.Timestamp(last_funding_ts)
        if reconstruct_legacy_timelines:
            self._reconstruct_missing_position_timelines(raw)
        log.info("État trend rechargé depuis %s", self.store.path)

    def _state_payload(self) -> TrendStatePayload:
        raw: TrendStatePayload = {
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
            "stop_protection_mode": stop_protection_mode_from_broker(
                supports_stop_orders=self.broker.supports_stop_orders
            ),
            "execution_realism": self._execution_realism_payload(),
        }
        if self.execution_context is not None:
            raw["execution_context"] = dict(self.execution_context)
        for slot in self.slots:
            pos: PositionState | None = None
            if slot.position:
                pos = {
                    "entry_time": slot.position.entry_time.isoformat(),
                    "entry_price": slot.position.entry_price,
                    "qty": slot.position.qty,
                    "stop_price": slot.position.stop_price,
                    "direction": int(slot.position.direction),
                    "bars_held": slot.position.bars_held,
                    "best_close": slot.position.best_close,
                    "initial_qty": slot.position.initial_qty,
                    "last_add_price": slot.position.last_add_price,
                    "pyramid_adds": slot.position.pyramid_adds,
                }
            slot_state: TrendSlotState = {
                "cash": slot.cash,
                "position": pos,
                "stop_order_id": slot.stop_order_id,
                "stop_order_local_id": slot.stop_order_local_id,
                "stop_intent_id": slot.stop_intent_id,
                "stop_transition": slot.stop_transition,
                "entry_fee": slot.entry_fee,
                "last_bar_ts": slot.last_bar_ts.isoformat()
                if slot.last_bar_ts is not None
                else None,
                "financial_transition_seq": slot.financial_transition_seq,
                "position_timeline": list(slot.position_timeline),
            }
            raw["slots"][slot.strategy.name] = slot_state
        return raw

    def _save_state(self) -> None:
        self.store.save_engine_state("trend", self._state_payload())

    @staticmethod
    def _stop_intent_id(
        slot: StrategySlot,
        *,
        qty: float,
        stop_price: float,
        direction: int,
    ) -> str:
        """Construit une identité stable pour une génération de stop.

        Le checkpoint de transition reste la source de reprise après une
        réponse perdue. Cette empreinte rend également l'intention
        reproductible avant sa création, sans UUID aléatoire, et empêche de
        confondre deux générations de position ou deux anciens stops.
        """

        position = slot.position
        if position is None:
            position_generation: str | None = None
        else:
            entry_time = position.entry_time
            if entry_time.tzinfo is not None:
                entry_time = entry_time.tz_convert("UTC")
            position_generation = (
                f"entry={entry_time.isoformat()}|initial_qty={position.initial_qty:.17g}"
            )
        identity = json.dumps(
            {
                "direction": int(direction),
                "position_generation": position_generation,
                "previous_stop_id": slot.stop_order_id,
                "qty": float(qty),
                "stop_price": float(stop_price),
                "version": 1,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return f"btq-stop-{digest}"

    def _require_manual_reconciliation(
        self,
        message: str,
        *,
        slot: StrategySlot | None = None,
        context: dict[str, Any] | None = None,
        incident_fingerprint: str = "execution:trend:protective_order_uncertain",
        incident_kind: str = "protective_order_uncertain",
    ) -> NoReturn:
        self.reconciliation_required = True
        payload = {"slot": slot.strategy.name if slot else None, **(context or {})}
        self.store.record_incident(
            incident_fingerprint,
            engine="trend",
            severity="CRITICAL",
            kind=incident_kind,
            message=message,
            context=payload,
        )
        self._save_state()
        self.notifier(f"⛔ TREND : {message}. Réconciliation manuelle requise.")
        raise ReconciliationRequired(message)

    def _begin_stop_replacement(
        self,
        slot: StrategySlot,
        *,
        qty: float,
        stop_price: float,
        direction: int,
        reason: str,
    ) -> None:
        """Journalise puis exécute un remplacement de stop récupérable.

        L'ancien stop reste la protection active jusqu'à confirmation du
        nouveau. Le checkpoint ``PLACING`` précède toujours l'appel exchange.
        """

        if not self.broker.supports_stop_orders:
            return
        if slot.stop_transition is not None:
            self._resume_stop_transition(slot)
            return
        intent_id = self._stop_intent_id(
            slot,
            qty=qty,
            stop_price=stop_price,
            direction=direction,
        )
        slot.stop_transition = {
            "kind": "REPLACE",
            "phase": "PLACING",
            "intent_id": intent_id,
            "previous_stop_id": slot.stop_order_id,
            "previous_local_order_id": slot.stop_order_local_id,
            "replacement_stop_id": None,
            "qty": float(qty),
            "stop_price": float(stop_price),
            "direction": int(direction),
            "reason": reason,
        }
        side = "SELL" if direction == 1 else "BUY"
        try:
            self.store.begin_order_and_checkpoint(
                "trend",
                slot.strategy.name,
                intent_id,
                "STOP",
                side,
                qty,
                reason,
                self._state_payload(),
                reference_price=stop_price,
            )
        except Exception as error:
            # Aucun appel exchange n'a encore eu lieu. Le checkpoint market de
            # la position suffit pour que le redémarrage détecte le stop absent.
            slot.stop_transition = None
            raise ReconciliationRequired(
                "Impossible de journaliser l'intention de stop ; runner arrêté"
            ) from error
        self._resume_stop_transition(slot)

    def _stop_transition_pending(
        self,
        slot: StrategySlot,
        message: str,
        error: BaseException | None = None,
    ) -> NoReturn:
        """Arrête le runner sans rendre la saga irrécupérable.

        Le checkpoint contient tout le contexte nécessaire au prochain
        démarrage. Contrairement à une quantité partiellement remplie, une
        panne réseau n'exige donc pas encore une décision humaine.
        """

        transition = dict(slot.stop_transition or {})
        if error is not None:
            transition["error"] = f"{type(error).__name__}: {error}"
        slot.stop_transition = transition
        fingerprint = f"execution:trend:protective_stop_transition:{slot.strategy.name}"
        self.store.record_incident(
            fingerprint,
            engine="trend",
            severity="CRITICAL",
            kind="protective_stop_transition_pending",
            message=message,
            context=transition,
        )
        try:
            self._save_state()
        except Exception as persistence_error:
            raise ReconciliationRequired(
                "Transition de stop ambiguë et checkpoint impossible"
            ) from persistence_error
        self.notifier(f"⛔ {slot.strategy.name} : {message}. Reprise automatique au redémarrage.")
        raise ReconciliationRequired(message)

    def _prepare_stop_cancellation(self, slot: StrategySlot, *, reason: str) -> None:
        if slot.stop_order_id is None or slot.stop_transition is not None:
            return
        slot.stop_transition = {
            "kind": "CANCEL",
            "phase": "CANCELING",
            "previous_stop_id": slot.stop_order_id,
            "previous_local_order_id": slot.stop_order_local_id,
            "reason": reason,
        }

    def _verify_stop_terminal(
        self,
        slot: StrategySlot,
        transition: dict[str, Any],
        stop_id: str,
        *,
        cancel_error: BaseException | None = None,
    ) -> Any:
        """Prouve la fin de vie de l'ancien stop avant toute promotion.

        cancel_stop ne constitue jamais la preuve : l'ordre peut avoir
        changé d'état entre l'appel d'annulation et sa réponse, et
        OrderNotFound peut recouvrir un fill. Le snapshot par identifiant
        exchange est donc obligatoire, y compris lorsque l'annulation a
        renvoyé sans erreur.
        """

        transition["phase"] = "VERIFY_OLD_TERMINAL"
        try:
            snapshot = self.broker.protective_order_snapshot(stop_id)
        except Exception as lookup_error:
            self._stop_transition_pending(
                slot,
                f"État du stop {stop_id} impossible à prouver après annulation",
                cancel_error or lookup_error,
            )
        if snapshot.status in {"FILLED", "PARTIAL"} or snapshot.filled_qty > 1e-9:
            transition["phase"] = "RECONCILIATION_REQUIRED"
            self._require_manual_reconciliation(
                f"Le stop {stop_id} a potentiellement exécuté la position pendant son remplacement",
                slot=slot,
                context={
                    "stop_order_id": stop_id,
                    "status": snapshot.status,
                    "filled_qty": snapshot.filled_qty,
                    "remaining_qty": snapshot.remaining_qty,
                    "replacement_stop_id": transition.get("replacement_stop_id"),
                },
            )
        if (
            snapshot.status in STOP_TERMINAL_NO_FILL
            and snapshot.filled_qty <= 1e-9
            and snapshot.remaining_qty <= 1e-9
        ):
            return snapshot
        self._stop_transition_pending(
            slot,
            f"Annulation du stop {stop_id} non confirmée (état observé : {snapshot.status})",
            cancel_error,
        )

    def _lookup_stop_placement(self, intent_id: str):
        if not self.broker.supports_order_lookup:
            return None
        return self.broker.lookup_order(intent_id)

    def _resume_stop_cancellation(self, slot: StrategySlot, transition: dict[str, Any]) -> None:
        previous_id = str(transition["previous_stop_id"])
        cancel_error: BaseException | None = None
        try:
            self.broker.cancel_stop(previous_id)
        except Exception as error:
            cancel_error = error
        self._verify_stop_terminal(
            slot,
            transition,
            previous_id,
            cancel_error=cancel_error,
        )
        previous_local = transition.get("previous_local_order_id")
        try:
            if previous_local is not None:
                self.store.complete_order(
                    int(previous_local),
                    status="CANCELED",
                    broker_order_id=previous_id,
                )
        except Exception as error:
            raise ReconciliationRequired(
                "Stop annulé côté exchange, mais journal SQLite non actualisé"
            ) from error
        previous_intent = slot.stop_intent_id
        slot.stop_order_id = None
        slot.stop_order_local_id = None
        slot.stop_intent_id = None
        slot.stop_transition = None
        try:
            self.store.save_engine_state(
                "trend",
                self._state_payload(),
                event_type="protective_order_canceled",
                event_payload={"slot": slot.strategy.name, "stop_order_id": previous_id},
            )
        except Exception as error:
            slot.stop_order_id = previous_id
            slot.stop_order_local_id = int(previous_local) if previous_local is not None else None
            slot.stop_intent_id = previous_intent
            slot.stop_transition = transition
            raise ReconciliationRequired(
                "Stop annulé, mais checkpoint final non enregistré"
            ) from error
        self.store.resolve_incident(
            f"execution:trend:protective_stop_transition:{slot.strategy.name}"
        )

    def _place_replacement_stop(
        self,
        slot: StrategySlot,
        transition: dict[str, Any],
        intent_id: str,
        order_id: int,
    ) -> str:
        replacement_id = transition.get("replacement_stop_id")
        phase = transition["phase"]
        if phase in {"SUBMITTING", "SUBMISSION_AMBIGUOUS"}:
            try:
                snapshot = self._lookup_stop_placement(intent_id)
            except Exception as error:
                self._stop_transition_pending(
                    slot,
                    "Recherche du stop ambigu interrompue",
                    error,
                )
            if snapshot is None:
                self._stop_transition_pending(
                    slot,
                    "Stop potentiellement soumis mais toujours introuvable",
                )
        elif phase != "PLACING":
            if replacement_id is None:
                self._stop_transition_pending(
                    slot,
                    "Phase de stop sans identifiant de remplacement",
                )
            return str(replacement_id)
        else:
            try:
                snapshot = self._lookup_stop_placement(intent_id)
            except Exception as error:
                self._stop_transition_pending(
                    slot,
                    "Recherche du stop protecteur interrompue",
                    error,
                )
        if snapshot is not None:
            if (
                snapshot.filled_qty > 0
                or snapshot.status not in ("OPEN",)
                or (
                    snapshot.requested_qty is not None
                    and abs(snapshot.requested_qty - float(transition["qty"])) > 1e-9
                )
            ):
                self._require_manual_reconciliation(
                    "Ordre stop retrouvé dans un état non protecteur",
                    slot=slot,
                    context={
                        "intent_id": intent_id,
                        "status": snapshot.status,
                        "filled_qty": snapshot.filled_qty,
                    },
                )
            replacement_id = snapshot.broker_order_id
        if replacement_id is None:
            self._persist_stop_phase(slot, "SUBMITTING")
            try:
                replacement_id = self.broker.place_stop(
                    float(transition["qty"]),
                    float(transition["stop_price"]),
                    int(transition["direction"]),
                    client_order_id=intent_id,
                )
            except Exception as placement_error:
                self._persist_stop_phase(slot, "SUBMISSION_AMBIGUOUS")
                try:
                    snapshot = self._lookup_stop_placement(intent_id)
                except Exception as lookup_error:
                    self._stop_transition_pending(
                        slot,
                        "Création du stop ambiguë et lookup indisponible",
                        lookup_error,
                    )
                if (
                    snapshot is None
                    or snapshot.broker_order_id is None
                    or snapshot.status != "OPEN"
                    or snapshot.filled_qty > 0
                ):
                    self._stop_transition_pending(
                        slot,
                        "Création du stop non confirmée",
                        placement_error,
                    )
                replacement_id = snapshot.broker_order_id
        if replacement_id is None:
            self._stop_transition_pending(
                slot,
                "Le broker n'a pas confirmé l'identifiant du stop protecteur",
            )
        replacement_id = str(replacement_id)
        transition["replacement_stop_id"] = replacement_id
        transition["phase"] = "VERIFY_OLD_TERMINAL"
        try:
            self.store.complete_order_and_checkpoint(
                order_id,
                engine="trend",
                state=self._state_payload(),
                status="OPEN",
                broker_order_id=replacement_id,
            )
        except Exception as error:
            transition["replacement_stop_id"] = None
            transition["phase"] = "PLACING"
            raise ReconciliationRequired(
                "Stop créé, mais confirmation SQLite non enregistrée"
            ) from error
        return replacement_id

    def _persist_stop_phase(self, slot: StrategySlot, phase: str) -> None:
        transition = slot.stop_transition
        if transition is None:
            raise ReconciliationRequired("Phase de stop absente pendant la reprise")
        transition["phase"] = phase
        try:
            self._save_state()
        except Exception as error:
            raise ReconciliationRequired(
                f"Phase de stop {phase} non persistée ; runner arrêté"
            ) from error

    def _resume_stop_transition(self, slot: StrategySlot) -> None:
        transition = slot.stop_transition
        if transition is None:
            return
        if transition.get("phase") == "RECONCILIATION_REQUIRED":
            self._require_manual_reconciliation(
                "Saga de stop déjà marquée pour réconciliation manuelle",
                slot=slot,
                context=transition,
            )

        if transition["kind"] == "CANCEL":
            self._resume_stop_cancellation(slot, transition)
            return

        intent_id = str(transition["intent_id"])
        order = self.store.read_order_by_intent(intent_id)
        if order is None:
            self._require_manual_reconciliation(
                "Transition de stop sans intention SQLite",
                slot=slot,
                context={"intent_id": intent_id},
            )
        order_id = int(order["id"])
        replacement_id = self._place_replacement_stop(slot, transition, intent_id, order_id)
        previous_stop_id = transition.get("previous_stop_id")
        if previous_stop_id is not None and str(previous_stop_id) != replacement_id:
            cancel_error: BaseException | None = None
            try:
                self.broker.cancel_stop(str(previous_stop_id))
            except Exception as error:
                cancel_error = error
            self._verify_stop_terminal(
                slot,
                transition,
                str(previous_stop_id),
                cancel_error=cancel_error,
            )
        self._persist_stop_phase(slot, "PROMOTION_PENDING")
        previous_local = transition.get("previous_local_order_id")
        try:
            if previous_stop_id is not None and previous_local is not None:
                self.store.complete_order(
                    int(previous_local),
                    status="CANCELED",
                    broker_order_id=str(previous_stop_id),
                )
        except Exception as error:
            raise ReconciliationRequired(
                "Ancien stop prouvé terminal, mais journal SQLite non actualisé"
            ) from error
        previous_active_id = slot.stop_order_id
        previous_active_local_id = slot.stop_order_local_id
        previous_active_intent = slot.stop_intent_id
        previous_stop_price = slot.position.stop_price if slot.position is not None else None
        slot.stop_order_id = replacement_id
        slot.stop_order_local_id = order_id
        slot.stop_intent_id = intent_id
        if slot.position is not None:
            slot.position.stop_price = float(transition["stop_price"])
        slot.stop_transition = None
        try:
            self.store.save_engine_state(
                "trend",
                self._state_payload(),
                event_type="protective_order_replaced",
                event_payload={
                    "slot": slot.strategy.name,
                    "previous_stop_id": previous_stop_id,
                    "replacement_stop_id": replacement_id,
                    "intent_id": intent_id,
                },
            )
        except Exception as error:
            slot.stop_order_id = previous_active_id
            slot.stop_order_local_id = previous_active_local_id
            slot.stop_intent_id = previous_active_intent
            if slot.position is not None and previous_stop_price is not None:
                slot.position.stop_price = previous_stop_price
            transition["phase"] = "PROMOTION_PENDING"
            slot.stop_transition = transition
            raise ReconciliationRequired(
                "Remplacement externe terminé, mais checkpoint final non enregistré"
            ) from error
        self.store.resolve_incident(
            f"execution:trend:protective_stop_transition:{slot.strategy.name}"
        )

    def _recover_protective_stop_transitions(self) -> None:
        """Termine les transitions persistées avant toute nouvelle décision."""

        for slot in self.slots:
            if slot.stop_transition is not None:
                self._resume_stop_transition(slot)
            elif slot.position is not None and slot.stop_order_id is None:
                self._begin_stop_replacement(
                    slot,
                    qty=slot.position.qty,
                    stop_price=slot.position.stop_price,
                    direction=slot.position.direction,
                    reason="startup_missing_stop",
                )

    def _materialize_filled_stop(
        self,
        slot: StrategySlot,
        decision: StopDecision,
    ) -> None:
        pos = slot.position
        assert pos is not None
        assert decision.snapshot is not None
        assert decision.previous_stop_id is not None
        snapshot = decision.snapshot
        fill_qty = float(snapshot.filled_qty)
        if fill_qty <= 0 or fill_qty > pos.qty + 1e-9:
            self._require_manual_reconciliation(
                "Fill stop incohérent avec la position locale",
                slot=slot,
                context={"filled_qty": fill_qty, "local_qty": pos.qty},
            )
        fill_price = snapshot.average_price or pos.stop_price
        fill = Fill(
            price=fill_price,
            qty=fill_qty,
            fee=float(snapshot.fee),
            broker_order_id=snapshot.broker_order_id,
        )
        try:
            accounting = self.accounting_service.close_position(pos, fill, entry_fee=slot.entry_fee)
        except Exception as error:
            raise ReconciliationRequired(
                "Stop exécuté mais application comptable impossible; arrêt fail-closed"
            ) from error
        slot.cash += accounting.cash_delta
        partial = accounting.partial
        trade = self._trade_payload(
            slot,
            pos,
            fill_price,
            accounting.trade_pnl,
            "stop_exchange",
            qty=fill_qty,
        )
        if partial:
            assert accounting.remaining_position is not None
            slot.position = accounting.remaining_position
        else:
            slot.position = None
        slot.stop_order_id = None
        local_stop_order_id = slot.stop_order_local_id
        slot.stop_order_local_id = None
        slot.stop_intent_id = None
        slot.entry_fee = accounting.remaining_entry_fee
        self.store.record_observed_fill_and_checkpoint(
            engine="trend",
            slot=slot.strategy.name,
            intent_id=f"observed-stop-{snapshot.broker_order_id}",
            broker_order_id=snapshot.broker_order_id,
            side="SELL" if pos.direction == 1 else "BUY",
            requested_qty=float(snapshot.requested_qty),
            filled_qty=fill_qty,
            price=fill_price,
            fee=float(snapshot.fee),
            reason="stop_exchange",
            state=self._state_payload(),
            trade=trade,
        )
        if local_stop_order_id is not None:
            self.store.complete_order(
                local_stop_order_id,
                status="PARTIAL" if partial else "FILLED",
                filled_qty=fill_qty,
                price=fill_price,
                fee=float(snapshot.fee),
                broker_order_id=snapshot.broker_order_id,
            )
        if partial:
            assert slot.position is not None
            self._begin_stop_replacement(
                slot,
                qty=slot.position.qty,
                stop_price=slot.position.stop_price,
                direction=slot.position.direction,
                reason="partial_stop_fill",
            )
        self.store.resolve_incident("execution:trend:protective_order_uncertain")
        self.notifier(
            f"{'🟩' if trade['pnl'] >= 0 else '🟥'} {slot.strategy.name} — "
            f"stop exchange {'partiel ' if partial else ''}exécuté @ {fill_price:,.0f} $ : "
            f"{trade['pnl']:+,.2f} $"
        )

    def _observe_exchange_stop_fills(self) -> None:
        """Matérialise les fills survenus hors processus avant reconcile().

        Cette étape ne crée ni n'annule aucun ordre. Elle permet à une position
        locale encore ouverte de devenir plate lorsque son stop confirmé a été
        exécuté pendant l'arrêt, avant la comparaison de position distante.
        """

        for slot in self.slots:
            if (
                slot.position is None
                or slot.stop_order_id is None
                or slot.stop_transition is not None
            ):
                continue
            decision = self.stop_service.inspect(
                stop_id=slot.stop_order_id,
                qty=slot.position.qty,
                stop_price=slot.position.stop_price,
                direction=slot.position.direction,
            )
            if decision.kind in {StopDecisionKind.FILLED, StopDecisionKind.PARTIAL_FILLED}:
                self._materialize_filled_stop(slot, decision)
            elif decision.kind == StopDecisionKind.UNCERTAIN:
                self._require_manual_reconciliation(
                    decision.message or "État du stop protecteur incertain au démarrage",
                    slot=slot,
                    context=decision.context,
                )

    def _monitor_exchange_stops(self) -> None:
        """Rapproche chaque stop à chaque tick et échoue fermé en cas d'ambiguïté."""

        if not self.broker.supports_stop_orders:
            return
        for slot in self.slots:
            if slot.stop_transition is not None:
                self._resume_stop_transition(slot)
                continue
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
            if decision.kind == StopDecisionKind.REPLACE_REQUIRED:
                assert pos is not None
                self._begin_stop_replacement(
                    slot,
                    qty=pos.qty,
                    stop_price=pos.stop_price,
                    direction=pos.direction,
                    reason="monitor_replacement",
                )
                continue
            if decision.kind in {StopDecisionKind.FILLED, StopDecisionKind.PARTIAL_FILLED}:
                self._materialize_filled_stop(slot, decision)

    def _record_position_transition(
        self,
        slot: StrategySlot,
        submitted: SubmittedOrder,
        *,
        previous_direction: int | None = None,
    ) -> None:
        """Persist the exposure after one durable market transition.

        Funding replay uses this causal timeline rather than the position that
        happens to be open when a payment is polled. A missing timeline is
        intentionally not repaired from the current position: doing so would
        silently include exposure that did not exist at the event timestamp.
        """

        position = slot.position
        if position is not None:
            qty = float(position.qty)
            direction = int(position.direction)
            generation = self._position_generation(position)
        else:
            qty = 0.0
            direction = int(previous_direction or submitted.application_plan.entry_direction or 1)
            generation = submitted.application_plan.identity.position_generation or "FLAT"
        intent_id = submitted.intent_id
        if any(item.get("intent_id") == intent_id for item in slot.position_timeline):
            return
        slot.position_timeline.append(
            {
                "effective_at": submitted.application_plan.planned_effect_at,
                "intent_id": intent_id,
                "qty": qty,
                "direction": direction,
                "generation": generation,
            }
        )
        slot.position_timeline.sort(
            key=lambda item: (str(item["effective_at"]), str(item["intent_id"]))
        )
        self._save_state()

    @staticmethod
    def _exposure_at(
        slot: StrategySlot, timestamp: pd.Timestamp
    ) -> tuple[dict[str, Any] | None, bool]:
        """Return exposure at ``timestamp`` and whether its history is known."""

        timeline = slot.position_timeline
        if not timeline:
            # A non-flat legacy checkpoint without transitions is ambiguous.
            return (None, slot.position is None)
        point = pd.Timestamp(timestamp)
        point = point.tz_localize("UTC") if point.tzinfo is None else point.tz_convert("UTC")
        selected: dict[str, Any] | None = None
        for item in timeline:
            effective = pd.Timestamp(item["effective_at"])
            effective = (
                effective.tz_localize("UTC")
                if effective.tzinfo is None
                else effective.tz_convert("UTC")
            )
            if effective <= point:
                selected = item
            else:
                break
        return selected, True

    def _apply_funding_payments(self, _mark_price: float) -> None:
        """Settle venue funding from event prices and a durable exposure timeline."""

        del _mark_price  # current marks are never funding accounting inputs
        poll = self.funding_service.poll(self.last_funding_ts)
        if poll is None:
            return
        if poll.initialized:
            if any(slot.position is not None for slot in self.slots):
                reason = "Position ouverte sans checkpoint funding durable"
                self.store.record_incident(
                    "accounting:trend:funding_uncertainty",
                    engine="trend",
                    severity="CRITICAL",
                    kind="funding_accounting_uncertainty",
                    message=reason,
                    context={"classification": "MISSING_FUNDING_CHECKPOINT"},
                )
                raise ReconciliationRequired(reason)
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
        for payment in poll.payments:
            details: dict[str, dict[str, Any]] = {}
            total_amount = 0.0
            total_notional = 0.0
            generations: list[str] = []
            active_exposure = False
            for slot in self.slots:
                exposure, known = self._exposure_at(slot, payment.timestamp)
                if not known:
                    reason = (
                        f"Historique de position absent pour le funding "
                        f"{payment.timestamp.isoformat()} ({slot.strategy.name})"
                    )
                    self.store.record_incident(
                        "accounting:trend:funding_uncertainty",
                        engine="trend",
                        severity="CRITICAL",
                        kind="funding_accounting_uncertainty",
                        message=reason,
                        context={
                            "classification": "POSITION_TIMELINE_UNAVAILABLE",
                            "slot": slot.strategy.name,
                            "funding_timestamp": payment.timestamp.isoformat(),
                        },
                    )
                    raise ReconciliationRequired(reason)
                if exposure is None or float(exposure["qty"]) <= 0:
                    details[slot.strategy.name] = {"amount": 0.0, "qty": 0.0}
                    continue
                active_exposure = True
                if (
                    payment.reference_price is None
                    or payment.reference_price <= 0
                    or not payment.reference_price_source
                    or payment.reference_price_timestamp is None
                ):
                    reason = (
                        f"Prix de référence funding indisponible pour "
                        f"{payment.timestamp.isoformat()}"
                    )
                    self.store.record_incident(
                        "accounting:trend:funding_uncertainty",
                        engine="trend",
                        severity="CRITICAL",
                        kind="funding_accounting_uncertainty",
                        message=reason,
                        context={
                            "classification": "FUNDING_REFERENCE_PRICE_UNAVAILABLE",
                            "funding_timestamp": payment.timestamp.isoformat(),
                            "source": payment.reference_price_source,
                        },
                    )
                    raise ReconciliationRequired(reason)
                qty = float(exposure["qty"])
                direction = int(exposure["direction"])
                amount = direction * qty * float(payment.reference_price) * float(payment.rate)
                total_amount += amount
                total_notional += qty * float(payment.reference_price)
                generations.append(
                    f"{slot.strategy.name}:{exposure['generation']}:{qty:.17g}:{direction}"
                )
                details[slot.strategy.name] = {
                    "amount": amount,
                    "qty": qty,
                    "direction": direction,
                    "generation": exposure["generation"],
                }
            # With no active exposure the checkpoint is still durable, but the
            # missing price is not silently turned into a numeric zero.
            source = (
                payment.reference_price_source
                if active_exposure
                else (payment.reference_price_source or "not_required_flat")
            )
            price_timestamp = (
                payment.reference_price_timestamp.isoformat()
                if payment.reference_price_timestamp is not None
                else None
            )
            previous_cash = {slot.strategy.name: slot.cash for slot in self.slots}
            previous_checkpoint = self.last_funding_ts
            for slot in self.slots:
                amount = float(details[slot.strategy.name]["amount"])
                slot.cash -= amount
            self.last_funding_ts = payment.timestamp
            # Carry and Trend share one SQLite funding ledger, but they have
            # independent position generations. Keep the historical carry key
            # format unchanged and namespace Trend events so one engine cannot
            # reject the other engine's valid accounting event.
            event_key = (
                f"trend|{funding_event_id(self.exchange_id, self.symbol, payment.timestamp)}"
            )
            ledger = {
                "event_key": event_key,
                "venue": self.exchange_id,
                "instrument": self.symbol,
                "funding_timestamp": payment.timestamp.isoformat(),
                "native_funding_rate": float(payment.rate),
                "position_generation": "|".join(sorted(generations)) or "FLAT",
                "funding_notional": total_notional,
                "funding_notional_price": payment.reference_price if active_exposure else None,
                "funding_notional_price_source": source,
                "funding_notional_price_timestamp": price_timestamp,
                "funding_pnl": -total_amount,
                "borrow_principal": 0.0,
                "borrow_rate_ann": 0.0,
                "borrow_dt_seconds": 0.0,
                "borrow_cost": 0.0,
                "applied_at": self.clock.utc_now().isoformat(),
            }
            try:
                result = self.store.apply_carry_accounting_event_and_checkpoint(
                    ledger,
                    self._state_payload(),
                    engine="trend",
                    event_payload={
                        "reason": "funding_payment",
                        "event_key": event_key,
                        "payment_ts": payment.timestamp.isoformat(),
                        "native_funding_rate": float(payment.rate),
                        "reference_price": payment.reference_price,
                        "reference_price_source": source,
                        "reference_price_timestamp": price_timestamp,
                        "amounts": details,
                        "funding_pnl": -total_amount,
                    },
                )
            except Exception:
                for slot in self.slots:
                    slot.cash = previous_cash[slot.strategy.name]
                self.last_funding_ts = previous_checkpoint
                raise
            # A previously unresolved funding incident is cleared only after
            # the event is durably applied or proven idempotently replayed.
            self.store.resolve_incident("accounting:trend:funding_uncertainty")
            if result == "replayed":
                self._load_state()
                break

    def _execution_realism_payload(self) -> dict[str, Any]:
        simulator = getattr(self.broker, "simulator", None)
        config = getattr(simulator, "config", None)
        context = self.execution_context or {}
        payload: dict[str, Any] = {
            "simulator_version": "paper-execution-v2",
            "profile": getattr(config, "simulation_profile", "custom"),
            "liquidity_model": getattr(config, "liquidity_model", "aggregate"),
            "latency_ms": getattr(config, "latency_ms", 0),
            "stop_model": (
                "persistent_exchange_style"
                if self.broker.supports_stop_orders
                else "software_tick_or_ohlc"
            ),
            "funding_reference": "event_price_required",
            "margin_model": self.margin_model.model,
            "margin_qualified": self.margin_model.qualified,
            "margin_source": self.margin_model.source,
            "cost_assumptions": {
                "fee_rate": getattr(config, "fee_rate", None),
                "slippage_bps": getattr(config, "slippage_bps", None),
                "market_impact_bps": getattr(config, "market_impact_bps", None),
                "source": "selected_simulation_profile",
            },
            "data_provenance": {
                "reference_price_source": context.get("price_source"),
                "market_timestamp": context.get("market_timestamp"),
                "reception_timestamp": context.get("reception_timestamp"),
                "order_book_source": (
                    context.get("order_book", {}).get("source")
                    if isinstance(context.get("order_book"), dict)
                    else None
                ),
                "order_book_timestamp": (
                    context.get("order_book", {}).get("timestamp")
                    if isinstance(context.get("order_book"), dict)
                    else None
                ),
            },
            "uncertainties": [
                "venue_margin_tiers_not_loaded",
            ],
        }
        if self._last_margin_snapshot is not None:
            payload["last_margin_snapshot"] = self._last_margin_snapshot.as_dict()
        return payload

    def _simulator_latency_ms(self) -> int:
        simulator = getattr(self.broker, "simulator", None)
        config = getattr(simulator, "config", None)
        value = getattr(config, "latency_ms", 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ReconciliationRequired("Configuration de latence PAPER invalide")
        return value

    @staticmethod
    def _checkpoint_timestamp(value: str) -> pd.Timestamp | None:
        """Parse one causal market checkpoint without inventing a timestamp."""

        try:
            timestamp = pd.Timestamp(value)
            return (
                timestamp.tz_localize("UTC")
                if timestamp.tzinfo is None
                else timestamp.tz_convert("UTC")
            )
        except (TypeError, ValueError, OverflowError):
            return None

    def _start_execution_context(self, decision_checkpoint: str, reference_price: float) -> None:
        """Persist the five causal clocks used by one simulated submission.

        The checkpoint is the market/decision time supplied by the strategy.
        Reception and submission are local observations.  They are kept
        separate even when a deterministic test clock makes them equal.
        """

        now = self.clock.utc_now()
        decision_timestamp = self._checkpoint_timestamp(decision_checkpoint)
        decision_iso = (
            decision_timestamp.isoformat() if decision_timestamp is not None else now.isoformat()
        )
        self.execution_context = {
            "market_timestamp": decision_iso,
            "reception_timestamp": now.isoformat(),
            "decision_timestamp": decision_iso,
            "submission_timestamp": now.isoformat(),
            "execution_timestamp": None,
            "execution_timestamp_source": None,
            "latency_ms": self._simulator_latency_ms(),
            "reference_price": float(reference_price),
            "price_source": "decision_reference",
        }

    def _resolve_delayed_price(
        self, decision_checkpoint: str, reference_price: float
    ) -> float | None:
        latency_ms = self._simulator_latency_ms()
        if latency_ms == 0:
            return None
        decision_timestamp = self._checkpoint_timestamp(decision_checkpoint)
        if decision_timestamp is None:
            decision_timestamp = self.clock.utc_now()
        target_timestamp = decision_timestamp + pd.Timedelta(milliseconds=latency_ms)
        resolver = getattr(self.venue, "execution_price_after", None)
        if not callable(resolver):
            raise ReconciliationRequired(
                "Latence PAPER positive mais aucune source de prix post-décision horodatée"
            )
        resolved = resolver(decision_timestamp, latency_ms)
        if isinstance(resolved, dict):
            price = resolved.get("price")
            observed_at = resolved.get("timestamp", target_timestamp)
            source = resolved.get("source")
        elif isinstance(resolved, tuple) and len(resolved) == 3:
            price, observed_at, source = resolved
        else:
            price, observed_at, source = resolved, target_timestamp, None
        if price is None or source is None:
            raise ReconciliationRequired(
                "Prix d'exécution retardé absent ou sans provenance; ordre non soumis"
            )
        try:
            delayed_price = float(price)
            observed_timestamp = pd.Timestamp(observed_at)
            observed_timestamp = (
                observed_timestamp.tz_localize("UTC")
                if observed_timestamp.tzinfo is None
                else observed_timestamp.tz_convert("UTC")
            )
        except (TypeError, ValueError, OverflowError) as error:
            raise ReconciliationRequired("Prix retardé invalide; ordre non soumis") from error
        if (
            not pd.notna(delayed_price)
            or delayed_price <= 0
            or observed_timestamp < target_timestamp
        ):
            raise ReconciliationRequired(
                "Prix retardé antérieur à la latence demandée; ordre non soumis"
            )
        self.execution_context = {
            **(self.execution_context or {}),
            "decision_timestamp": decision_timestamp.isoformat(),
            "target_timestamp": target_timestamp.isoformat(),
            "observed_timestamp": observed_timestamp.isoformat(),
            "delayed_price": delayed_price,
            "price_source": str(source),
            "reference_price": float(reference_price),
        }
        return delayed_price

    def _liquidity_model(self) -> str:
        simulator = getattr(self.broker, "simulator", None)
        config = getattr(simulator, "config", None)
        model = getattr(config, "liquidity_model", "aggregate")
        if model not in {"aggregate", "order_book"}:
            raise ReconciliationRequired(f"Modèle de liquidité PAPER inconnu: {model!r}")
        return str(model)

    def _resolve_order_book(self) -> dict[str, Any] | None:
        if self._liquidity_model() != "order_book":
            return None
        fetch = getattr(self.venue, "fetch_order_book", None)
        if not callable(fetch):
            raise ReconciliationRequired(
                "Simulation order-book activée mais le flux de carnet est indisponible"
            )
        raw = fetch(limit=20)
        if (
            not isinstance(raw, dict)
            or not isinstance(raw.get("bids"), list)
            or not isinstance(raw.get("asks"), list)
        ):
            raise ReconciliationRequired("Carnet enregistré incomplet; ordre non soumis")
        observed_at = self.clock.utc_now()
        book = dict(raw)
        book["timestamp"] = str(book.get("timestamp") or observed_at.isoformat())
        book["snapshot_id"] = str(
            book.get("snapshot_id") or f"{book['timestamp']}:{sha256_json(book)}"
        )
        book["source"] = str(book.get("source") or f"{self.exchange_id}:public_order_book")
        self.execution_context = {
            **(self.execution_context or {}),
            "liquidity_model": "order_book",
            "order_book": book,
        }
        return book

    def _execute_market_order(
        self,
        slot: StrategySlot,
        side: str,
        qty: float,
        ref_price: float,
        reason: str,
        decision_checkpoint: str,
        transition_type: FinancialTransitionType,
        position_generation: str | None,
        available_volume: float | None = None,
        *,
        reduce_only: bool = False,
        volatility_annual: float | None = None,
        entry_direction: int | None = None,
        entry_stop_price: float | None = None,
    ) -> SubmittedOrder:
        qty = self.broker.normalize_market_quantity(qty, ref_price, reduce_only=reduce_only)
        latency_ms = self._simulator_latency_ms()
        delayed_price: float | None = None
        order_book: dict[str, Any] | None = None
        identity = LogicalOrderIdentity(
            engine="trend",
            slot=slot.strategy.name,
            decision_checkpoint=decision_checkpoint,
            transition_type=transition_type,
            position_generation=position_generation,
            transition_sequence=slot.financial_transition_seq,
        )
        persisted_plan = self.store.get_financial_application_plan_by_intent(identity.intent_id)
        if persisted_plan is not None:
            current_payload = self._state_payload()
            durable_payload = self.store.load_engine_state("trend")
            expected_hash = persisted_plan.plan.pre_state_sha256
            if (
                durable_payload is None
                or sha256_json(durable_payload) != expected_hash
                or sha256_json(current_payload) != expected_hash
            ):
                raise ReconciliationRequired(
                    f"Ordre {identity.intent_id}: état courant divergent du "
                    "pre-state du plan financier durable"
                )
            application_plan = persisted_plan.plan
            context = application_plan.pre_state_payload.get("execution_context")
            if latency_ms > 0:
                if not isinstance(context, dict) or context.get("delayed_price") is None:
                    raise ReconciliationRequired(
                        "Plan durable sans prix retardé pour une latence positive"
                    )
                delayed_price = float(context["delayed_price"])
                self.execution_context = dict(context)
            if self._liquidity_model() == "order_book":
                if not isinstance(context, dict) or not isinstance(context.get("order_book"), dict):
                    raise ReconciliationRequired(
                        "Plan durable sans carnet enregistré pour une liquidité order-book"
                    )
                order_book = dict(context["order_book"])
        else:
            self._start_execution_context(decision_checkpoint, ref_price)
            delayed_price = self._resolve_delayed_price(decision_checkpoint, ref_price)
            order_book = self._resolve_order_book()
            application_plan = FinancialApplicationPlan(
                identity=identity,
                side=side,
                requested_qty=qty,
                reference_price=ref_price,
                reason=reason,
                reduce_only=reduce_only,
                planned_effect_at=self.clock.utc_now().isoformat(),
                pre_state_payload=self._state_payload(),
                protection_mode=stop_protection_mode_from_broker(
                    supports_stop_orders=self.broker.supports_stop_orders
                ),
                entry_direction=entry_direction,
                entry_stop_price=entry_stop_price,
            )
        # Reuse the durable plan exactly on retries.
        qty = application_plan.requested_qty
        ref_price = application_plan.reference_price
        side = application_plan.side
        reason = application_plan.reason
        reduce_only = application_plan.reduce_only

        submitted = self.order_service.submit_market(
            SubmitMarketCommand(
                engine="trend",
                slot=slot.strategy.name,
                side=side,
                qty=qty,
                reference_price=ref_price,
                reason=reason,
                decision_checkpoint=decision_checkpoint,
                transition_type=transition_type,
                position_generation=position_generation,
                transition_sequence=slot.financial_transition_seq,
                reduce_only=reduce_only,
                available_volume=available_volume,
                volatility_annual=volatility_annual,
                delayed_price=delayed_price,
                order_book=order_book,
                application_plan=application_plan,
            )
        )
        if not submitted.is_terminal:
            # La barre/décision est checkpointée, mais aucun fill encore actif
            # n'est appliqué au portefeuille local. La reprise devra rapprocher
            # l'intention par son client_order_id avant toute autre émission.
            try:
                self.store.save_engine_state(
                    "trend",
                    self._state_payload(),
                    event_type="order_pending_reconciliation",
                    event_payload={
                        "order_id": submitted.order_id,
                        "logical_order_key": submitted.logical_order_key,
                        "external_state": submitted.external_state.value,
                        "filled_qty": submitted.fill.qty,
                        "remaining_qty": submitted.remaining_qty,
                    },
                )
            except Exception as error:
                raise ReconciliationRequired(
                    f"Ordre {submitted.order_id} non terminal et checkpoint impossible; "
                    "arrêt fail-closed"
                ) from error
            raise ReconciliationRequired(
                f"Ordre {submitted.order_id} non terminal "
                f"({submitted.external_state.value}) : reprise bloquée"
            )
        self.execution_context = {
            **(self.execution_context or {}),
            "execution_timestamp": self.clock.utc_now().isoformat(),
            "execution_timestamp_source": "paper_broker_observation"
            if not self.broker.external_execution
            else "external_submission_observation",
            "execution_price": submitted.fill.price if submitted.fill.qty > 0 else None,
            "execution_qty": submitted.fill.qty,
        }
        return submitted

    def _complete_market_order_and_checkpoint(
        self,
        slot: StrategySlot,
        submitted: SubmittedOrder,
        *,
        trade: dict[str, Any] | None = None,
    ) -> None:
        """Checkpoint terminal et numéro de décision suivante atomiquement."""

        previous_sequence = slot.financial_transition_seq
        slot.financial_transition_seq = submitted.transition_sequence + 1
        try:
            self.store.complete_order_and_checkpoint(
                submitted.order_id,
                engine="trend",
                state=self._state_payload(),
                status=submitted.status,
                filled_qty=submitted.fill.qty,
                remaining_qty=submitted.remaining_qty,
                price=submitted.fill.price,
                fee=submitted.fill.fee,
                broker_order_id=submitted.fill.broker_order_id,
                trade=trade,
                external_state=submitted.external_state,
            )
        except Exception as error:
            slot.financial_transition_seq = previous_sequence
            raise ReconciliationRequired(
                f"Ordre {submitted.order_id} observé mais checkpoint métier impossible; "
                "arrêt fail-closed"
            ) from error
        except BaseException:
            slot.financial_transition_seq = previous_sequence
            raise

    def _reconcile_paper_submission(self, submitted: SubmittedOrder) -> ReconciliationResult:
        """Persist, apply, refresh, and finalize one local PAPER execution.

        SubmittedOrder.broker_result is converted to immutable local
        evidence. E3 remains the sole financial writer; the in-memory slot is
        refreshed exclusively from its committed engine state. A separate
        atomic PAPER finalization transaction then closes a positive terminal
        order, while zero-effect and external retry decisions remain outside
        this path.
        """

        if self.broker.external_execution:
            raise AssertionError("paper reconciliation cannot process an external broker")
        result = submitted.broker_result
        if result is None:
            raise ReconciliationRequired(
                f"Ordre PAPER {submitted.order_id}: réponse broker typée absente; "
                "réconciliation requise"
            )
        evidence = build_paper_execution_evidence(
            PaperExecutionEvidenceContext(
                local_order_id=submitted.order_id,
                intent_id=submitted.intent_id,
                engine="trend",
                instrument=self.symbol,
                side=submitted.application_plan.side,
            ),
            result,
            observed_at=self.clock.utc_now().isoformat(),
        )
        reconciliation = self.reconciliation_coordinator.reconcile(
            submitted.order_id,
            paper_evidence=evidence,
        )
        status = ReconciliationStatus(reconciliation.status)
        if status in {
            ReconciliationStatus.APPLIED,
            ReconciliationStatus.ALREADY_APPLIED,
        }:
            # The durable E3 state is the accounting authority. In
            # particular, an EXIT must clear an obsolete in-memory Position.
            try:
                self._load_state()
                self.store.finalize_paper_order_atomically(submitted.order_id)
                self._load_state()
            except ReconciliationRequired:
                raise
            except Exception as error:
                raise ReconciliationRequired(
                    "État PAPER appliqué mais relecture/finalisation impossible; "
                    f"arrêt fail-closed pour empêcher un checkpoint périmé: {error}"
                ) from error
        return reconciliation

    def _reconcile_external_submission(self, submitted: SubmittedOrder) -> None:
        """Reconcile and apply one qualified external submission durably."""

        runtime = self.external_settlement_runtime
        if runtime is None:
            raise ReconciliationRequired(
                "EXTERNAL_RUNTIME_NOT_QUALIFIED: réconciliation externe indisponible"
            )
        runtime.reconcile_order(
            submitted.order_id,
            observed_at=self.clock.utc_now().isoformat(),
        )
        try:
            self._load_state()
        except ReconciliationRequired:
            raise
        except Exception as error:
            raise ReconciliationRequired(
                "État externe appliqué mais relecture impossible; arrêt fail-closed "
                "pour empêcher un checkpoint périmé"
            ) from error

    @staticmethod
    def _position_generation(position: Position) -> str:
        return (
            f"entry={position.entry_time.isoformat()}|"
            f"initial_qty={format(position.initial_qty, '.17g')}"
        )

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
        volatility_annual: float | None = None,
        *,
        decision_checkpoint: str | None = None,
    ) -> None:
        assert slot.position is not None
        pos = slot.position
        # clôture : on vend un long, on rachète un short
        side = "SELL" if pos.direction == 1 else "BUY"
        checkpoint = decision_checkpoint or (
            f"{reason}:entry={pos.entry_time.isoformat()}:qty={format(pos.qty, '.17g')}"
        )
        submitted = self._execute_market_order(
            slot,
            side,
            pos.qty,
            ref_price,
            reason,
            checkpoint,
            FinancialTransitionType.EXIT,
            self._position_generation(pos),
            available_volume,
            reduce_only=True,
            volatility_annual=volatility_annual,
        )
        if self.external_settlement_runtime is not None:
            self._reconcile_external_submission(submitted)
            self._record_position_transition(slot, submitted, previous_direction=int(pos.direction))
            if self.broker.supports_stop_orders:
                if slot.position is None:
                    self._prepare_stop_cancellation(slot, reason=f"position_closed:{reason}")
                elif slot.position is not None:
                    self._begin_stop_replacement(
                        slot,
                        qty=slot.position.qty,
                        stop_price=slot.position.stop_price,
                        direction=slot.position.direction,
                        reason=f"partial_exit:{reason}",
                    )
            return
        if not self.broker.external_execution:
            self._reconcile_paper_submission(submitted)
            self._record_position_transition(slot, submitted, previous_direction=int(pos.direction))
            if self.broker.supports_stop_orders:
                if slot.position is None:
                    self._prepare_stop_cancellation(slot, reason=f"position_closed:{reason}")
                else:
                    self._begin_stop_replacement(
                        slot,
                        qty=slot.position.qty,
                        stop_price=slot.position.stop_price,
                        direction=slot.position.direction,
                        reason=f"partial_exit:{reason}",
                    )
            return
        fill = submitted.fill
        application_plan = submitted.application_plan
        if fill.qty <= 0:
            self._complete_market_order_and_checkpoint(slot, submitted)
            # ordre non exécuté : on GARDE la position (nouvel essai au tick
            # suivant via soft stop / condition de sortie toujours vraie)
            log.error(
                "[%s] Sortie non exécutée (fill nul) — position conservée", slot.strategy.name
            )
            self.notifier(f"⚠ {slot.strategy.name} : ordre de sortie non exécuté, on retentera")
            return
        try:
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
                exit_ts=application_plan.planned_effect_at,
            )
            if partial:
                assert accounting.remaining_position is not None
                slot.position = accounting.remaining_position
                pos = accounting.remaining_position
                log.warning(
                    "[%s] Sortie PARTIELLE : %.6f restant en position",
                    slot.strategy.name,
                    pos.qty,
                )
            else:
                slot.position = None
                if self.broker.supports_stop_orders:
                    self._prepare_stop_cancellation(slot, reason=f"position_closed:{reason}")
            self._complete_market_order_and_checkpoint(slot, submitted, trade=trade)
        except ReconciliationRequired:
            raise
        except Exception as error:
            raise ReconciliationRequired(
                f"Ordre {submitted.order_id} observé mais application métier impossible; "
                "arrêt fail-closed"
            ) from error
        if partial and self.broker.supports_stop_orders:
            self._begin_stop_replacement(
                slot,
                qty=pos.qty,
                stop_price=pos.stop_price,
                direction=pos.direction,
                reason=f"partial_exit:{reason}",
            )
        elif slot.stop_transition is not None:
            self._resume_stop_transition(slot)
        self.notifier(
            f"{'🟩' if trade_pnl >= 0 else '🟥'} {slot.strategy.name} — sortie "
            f"{'LONG' if pos.direction == 1 else 'SHORT'} ({reason}) @ {fill.price:,.0f} $ : "
            f"{trade_pnl:+,.2f} $"
        )
        if partial:
            self.notifier(f"⚠ {slot.strategy.name} : sortie partielle, {pos.qty:.6f} BTC restants")

    def _enter_position(
        self,
        slot: StrategySlot,
        row: pd.Series,
        ref_price: float,
        direction: int,
        *,
        decision_checkpoint: str,
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
        qty *= slot.strategy.position_size_multiplier(row, direction)
        qty = min(qty, self._shared_margin_quantity_cap(ref_price))
        live_balance = self.broker.free_quote_balance()
        if live_balance is not None:
            qty = min(qty, live_balance * 0.99 / ref_price)
        if qty <= 0:
            return
        side = "BUY" if direction == 1 else "SELL"
        volatility_annual = float(rvol) if pd.notna(rvol) else None
        submitted = self._execute_market_order(
            slot,
            side,
            qty,
            ref_price,
            "entry",
            decision_checkpoint,
            (
                FinancialTransitionType.ENTER_LONG
                if direction == 1
                else FinancialTransitionType.ENTER_SHORT
            ),
            None,
            float(row["volume"]) if pd.notna(row.get("volume")) else None,
            volatility_annual=volatility_annual,
            entry_direction=direction,
            entry_stop_price=stop,
        )
        if self.external_settlement_runtime is not None:
            self._reconcile_external_submission(submitted)
            self._record_position_transition(slot, submitted, previous_direction=direction)
            if self.broker.supports_stop_orders and slot.position is not None:
                self._begin_stop_replacement(
                    slot,
                    qty=slot.position.qty,
                    stop_price=slot.position.stop_price,
                    direction=slot.position.direction,
                    reason="entry_protection",
                )
            return
        if not self.broker.external_execution:
            self._reconcile_paper_submission(submitted)
            self._record_position_transition(slot, submitted, previous_direction=direction)
            if self.broker.supports_stop_orders and slot.position is not None:
                self._begin_stop_replacement(
                    slot,
                    qty=slot.position.qty,
                    stop_price=slot.position.stop_price,
                    direction=slot.position.direction,
                    reason="entry_protection",
                )
            return
        fill = submitted.fill
        application_plan = submitted.application_plan
        assert application_plan.entry_stop_price is not None
        if fill.qty <= 0:
            self._complete_market_order_and_checkpoint(slot, submitted)
            log.error("[%s] Entrée non exécutée", slot.strategy.name)
            return
        try:
            accounting = self.accounting_service.open_position(
                fill,
                entry_time=pd.Timestamp(application_plan.planned_effect_at),
                stop_price=application_plan.entry_stop_price,
                direction=direction,
            )
            slot.cash += accounting.cash_delta
            slot.entry_fee = accounting.entry_fee
            slot.position = accounting.position
            # Le fill market et la position sont d'abord matérialisés
            # atomiquement. La pose du stop possède ensuite sa propre saga ; un
            # crash entre les deux est détecté au redémarrage comme stop manquant.
            self._complete_market_order_and_checkpoint(slot, submitted)
        except ReconciliationRequired:
            raise
        except Exception as error:
            raise ReconciliationRequired(
                f"Ordre {submitted.order_id} observé mais application métier impossible; "
                "arrêt fail-closed"
            ) from error
        if self.broker.supports_stop_orders:
            self._begin_stop_replacement(
                slot,
                qty=fill.qty,
                stop_price=stop,
                direction=direction,
                reason="entry_protection",
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

    def _pyramid_position(
        self,
        slot: StrategySlot,
        row: pd.Series,
        ref_price: float,
        fraction: float,
        *,
        decision_checkpoint: str,
    ) -> None:
        position = slot.position
        if position is None:
            return
        if self.broker.supports_stop_orders and self.broker.external_execution:
            log.warning(
                "[%s] Renfort ignoré : saga de redimensionnement du stop externe "
                "non encore qualifiée",
                slot.strategy.name,
            )
            return
        qty = position.initial_qty * fraction
        max_total_qty = (
            slot.equity(ref_price) * self.risk.max_position_pct * self.risk.max_leverage / ref_price
        )
        qty = min(qty, max_total_qty - position.qty)
        qty = min(qty, self._shared_margin_quantity_cap(ref_price))
        if qty <= 0:
            return
        side = "BUY" if position.direction == 1 else "SELL"
        rvol = row.get("_rvol")
        submitted = self._execute_market_order(
            slot,
            side,
            qty,
            ref_price,
            "pyramid",
            decision_checkpoint,
            FinancialTransitionType.ADD,
            self._position_generation(position),
            float(row["volume"]) if pd.notna(row.get("volume")) else None,
            volatility_annual=(float(rvol) if pd.notna(rvol) else None),
        )
        if self.external_settlement_runtime is not None:
            self._reconcile_external_submission(submitted)
            self._record_position_transition(
                slot, submitted, previous_direction=int(position.direction)
            )
            return
        if not self.broker.external_execution:
            self._reconcile_paper_submission(submitted)
            self._record_position_transition(
                slot, submitted, previous_direction=int(position.direction)
            )
            if self.broker.supports_stop_orders and slot.position is not None:
                self._begin_stop_replacement(
                    slot,
                    qty=slot.position.qty,
                    stop_price=slot.position.stop_price,
                    direction=slot.position.direction,
                    reason="pyramid_protection",
                )
            return
        fill = submitted.fill
        try:
            if fill.qty > 0:
                previous_qty = position.qty
                total_qty = previous_qty + fill.qty
                position.entry_price = (
                    previous_qty * position.entry_price + fill.qty * fill.price
                ) / total_qty
                position.qty = total_qty
                position.last_add_price = fill.price
                position.pyramid_adds += 1
                slot.cash -= fill.fee
                slot.entry_fee += fill.fee
            self._complete_market_order_and_checkpoint(slot, submitted)
        except ReconciliationRequired:
            raise
        except Exception as error:
            raise ReconciliationRequired(
                f"Ordre {submitted.order_id} observé mais application métier impossible; "
                "arrêt fail-closed"
            ) from error

    def _process_bar(
        self,
        slot: StrategySlot,
        execution_price: float,
        *,
        frame: pd.DataFrame | None = None,
        target_ts: pd.Timestamp | None = None,
        allow_trade_actions: bool = True,
    ) -> BarDecision | None:
        """Décide sur la dernière clôture et exécute au prix de marché courant.

        Le backtest remplit les décisions de clôture à l'ouverture de ``t+1``.
        Le runner traite cette clôture après le début de ``t+1`` : son
        équivalent observable est donc le prix courant, jamais ``row["close"]``.
        """

        if execution_price <= 0:
            raise ValueError("execution_price doit être strictement positif")
        df = frame if frame is not None else self._fetch_frame(slot.strategy)
        if df.empty:
            return None
        if target_ts is None:
            last_ts = df.index[-1]
        else:
            eligible = df.index[df.index <= target_ts]
            if len(eligible) == 0:
                return None
            last_ts = eligible[-1]
        if slot.last_bar_ts is not None and last_ts <= slot.last_bar_ts:
            return None  # pas de nouvelle barre clôturée
        if slot.position is not None and self.broker.supports_stop_orders:
            raw_row = df.loc[last_ts]
            observe_bar = getattr(self.broker, "observe_market_bar", None)
            if callable(observe_bar):
                entry_time = slot.position.entry_time
                entry_time = (
                    entry_time.tz_localize("UTC")
                    if entry_time.tzinfo is None
                    else entry_time.tz_convert("UTC")
                )
                if entry_time <= last_ts:
                    observe_bar(
                        float(raw_row["open"]),
                        float(raw_row["high"]),
                        float(raw_row["low"]),
                        available_volume=(
                            float(raw_row["volume"]) if pd.notna(raw_row.get("volume")) else None
                        ),
                    )

        if slot.position is not None and not self.broker.supports_stop_orders:
            position = slot.position
            # A position opened after this candle began cannot safely use the
            # candle's full high/low: part of that range predates the entry.
            # Tick monitoring still protects it until the next complete bar.
            entry_time = position.entry_time
            if entry_time.tzinfo is None:
                entry_time = entry_time.tz_localize("UTC")
            else:
                entry_time = entry_time.tz_convert("UTC")
            if entry_time <= last_ts:
                raw_row = df.loc[last_ts]
                stop_reference = ExecutionSimulator.stop_trigger_price(
                    direction=position.direction,
                    open_price=float(raw_row["open"]),
                    high_price=float(raw_row["high"]),
                    low_price=float(raw_row["low"]),
                    stop_price=position.stop_price,
                )
                if stop_reference is not None:
                    # Mark the bar before submitting. The FinancialApplicationPlan
                    # binds this checkpoint to the atomic PAPER fill writer, so a
                    # restart cannot replay the same wick.
                    slot.last_bar_ts = last_ts
                    self._exit_position(
                        slot,
                        stop_reference,
                        "stop",
                        float(raw_row["volume"]) if pd.notna(raw_row.get("volume")) else None,
                        decision_checkpoint=last_ts.isoformat(),
                    )
                    return BarDecision(
                        position=None,
                        events=(ExitRequested(reason="stop"),),
                    )
        data = slot.strategy.prepare(df)
        data["_rvol"] = realized_vol(
            data["close"], VOL_LOOKBACK, bars_per_year(slot.strategy.timeframe)
        )
        if not allow_trade_actions:
            row = data.loc[last_ts]
            if slot.position is None:
                # Une entrée historique serait exécutée au prix du tick actuel,
                # et non au prix de la barre : elle est donc volontairement
                # ignorée pendant le rattrapage.
                slot.last_bar_ts = last_ts
                return BarDecision(position=None)
            pos = slot.position
            decision = decide_bar_close(
                slot.strategy,
                row,
                pos,
                halted=self.halted,
            )
            assert decision.position is not None
            next_position = decision.position
            slot.last_bar_ts = last_ts
            pos.best_close = next_position.best_close
            pos.bars_held = next_position.bars_held
            stop_event = next(
                (event for event in decision.events if isinstance(event, StopTightened)),
                None,
            )
            if stop_event and not self.broker.supports_stop_orders:
                pos.stop_price = stop_event.new_price
            skipped = tuple(
                event
                for event in decision.events
                if isinstance(event, (ExitRequested, PyramidRequested))
            )
            if skipped:
                log.info(
                    "[%s] Signaux historiques ignorés pendant le rattrapage : %s",
                    slot.strategy.name,
                    ", ".join(type(event).__name__ for event in skipped),
                )
            return decision
        data["funding"] = float("nan")
        funding_available = True
        try:
            # toujours en équivalent 8 h (convention des filtres et du backtest),
            # quelle que soit la périodicité native de la venue
            data.loc[last_ts, "funding"] = self.venue.funding_rate_8h()
        except Exception as e:
            if self.funding_rate_8h:
                data.loc[last_ts, "funding"] = self.funding_rate_8h
            else:
                funding_available = False
                log.warning(
                    "Funding indisponible (%s) : nouvelles expositions bloquées sur cette barre",
                    e,
                )
        uses_funding_filter = (
            slot.strategy.params.get("funding_long_max") is not None
            or slot.strategy.params.get("funding_short_min") is not None
        )
        allow_new_exposure = funding_available or not uses_funding_filter
        row = data.loc[last_ts]

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
            pyramid_event = next(
                (event for event in decision.events if isinstance(event, PyramidRequested)),
                None,
            )
            # 2. barre marquée traitée + mutations locales (jamais rejouées)
            slot.last_bar_ts = last_ts
            pos.best_close = next_position.best_close
            pos.bars_held = next_position.bars_held
            # 3. appels externes, at-most-once (un échec ici ne rejoue pas la
            # barre ; l'ancien stop exchange continue de protéger en attendant)
            if stop_event and pyramid_event:
                pos.stop_price = stop_event.new_price
            elif stop_event and slot.stop_order_id and self.broker.supports_stop_orders:
                self._begin_stop_replacement(
                    slot,
                    qty=pos.qty,
                    stop_price=stop_event.new_price,
                    direction=pos.direction,
                    reason="trailing_stop",
                )
            elif stop_event:
                pos.stop_price = stop_event.new_price
            if exit_event:
                self._exit_position(
                    slot,
                    execution_price,
                    exit_event.reason,
                    float(row["volume"]) if pd.notna(row.get("volume")) else None,
                    (float(row["_rvol"]) if pd.notna(row.get("_rvol")) else None),
                    decision_checkpoint=last_ts.isoformat(),
                )
            elif pyramid_event and not self.daily_lockout and allow_new_exposure:
                self._pyramid_position(
                    slot,
                    row,
                    execution_price,
                    pyramid_event.fraction,
                    decision_checkpoint=last_ts.isoformat(),
                )
            elif pyramid_event:
                log.warning(
                    "[%s] Renfort ignoré : lockout journalier ou funding venue indisponible",
                    slot.strategy.name,
                )
            return decision
        else:
            slot.last_bar_ts = last_ts
            can_enter = not self.halted and not self.daily_lockout and allow_new_exposure
            decision = decide_bar_close(
                slot.strategy,
                row,
                None,
                can_enter=can_enter,
            )
            for event in decision.events:
                if isinstance(event, EntryRequested):
                    self._enter_position(
                        slot,
                        row,
                        execution_price,
                        event.direction,
                        decision_checkpoint=last_ts.isoformat(),
                    )
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

    def _margin_snapshot(self, price: float) -> MarginSnapshot:
        positions = []
        for slot in self.slots:
            if slot.position is not None:
                positions.append(
                    (
                        int(slot.position.direction),
                        float(slot.position.qty),
                        float(slot.position.entry_price),
                        float(price),
                    )
                )
        self._last_margin_snapshot = self.margin_model.evaluate(
            collateral=sum(float(slot.cash) for slot in self.slots),
            positions=positions,
            mark_price=float(price),
        )
        return self._last_margin_snapshot

    def _shared_margin_quantity_cap(self, price: float) -> float:
        return self.margin_model.max_additional_qty(
            self._margin_snapshot(price),
            price=float(price),
        )

    def _update_kill_switches(self, price: float) -> None:
        margin = self._margin_snapshot(price)
        if margin.liquidatable and not self.halted:
            self.halted = True
            self.store.record_incident(
                "execution:trend:margin_liquidation",
                engine="trend",
                severity="CRITICAL",
                kind="paper_margin_liquidation",
                message="Marge de maintenance franchie : liquidation PAPER déclenchée",
                context=margin.as_dict(),
            )
            self.notifier(
                f"⛔ MARGE : maintenance franchie ({margin.equity:,.2f} / "
                f"{margin.maintenance_margin:,.2f} $), liquidation PAPER."
            )
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
        exit_ts: str | None = None,
    ) -> dict[str, Any]:
        return {
            "exit_ts": exit_ts or self.clock.utc_now().isoformat(),
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
    def _record_loop_failure(self, error: BaseException, consecutive: int) -> None:
        """Ouvre un incident quand la boucle échoue de façon répétée.

        Le catch-all de la boucle principale protège le moteur d'une erreur
        transitoire, mais il masquait aussi les pannes durables : un checkpoint
        non sérialisable, par exemple, échouait à chaque tick sans jamais rien
        signaler d'autre qu'une ligne de log. Le watchdog finissait par voir
        l'état périmé, mais sans la cause. On la publie ici.
        """

        if consecutive < LOOP_FAILURES_BEFORE_INCIDENT:
            return
        message = f"{type(error).__name__}: {error}"
        incident = self.store.record_incident(
            "execution:trend:loop_failure",
            engine="trend",
            severity="CRITICAL",
            kind="loop_failure",
            message=f"Boucle trend en échec {consecutive} fois de suite — {message}",
            context={"consecutive": consecutive, "error": message},
        )
        if incident["is_new_or_reopened"]:
            self.notifier(f"⛔ TREND : boucle en échec répété — {message}")

    def _maybe_reconcile_position(self, *, force: bool = False) -> None:
        """Rapproche périodiquement la position sans corriger un écart.

        Le contrôle des stops reste effectué à chaque tick par
        _monitor_exchange_stops. Cette cadence limite les appels de position
        tout en garantissant un contrôle au démarrage et avant de nouvelles
        décisions financières.
        """

        if not self.broker.supports_position_reconciliation:
            return
        now = self.clock.time()
        last = self._last_position_reconciliation_at
        if not force and last is not None and now - last < POSITION_RECONCILIATION_SECONDS:
            return
        self._last_position_reconciliation_at = now
        from .reconcile import inspect_position_reconciliation

        report = inspect_position_reconciliation(self.broker, self.slots, self.symbol)
        if report.ok:
            return
        context = {
            "reconciliation_domain": "position",
            "reason": report.reason,
            "local_net": report.local_net,
            "remote_net": report.remote_net,
            **(report.context or {}),
        }
        if report.reason == "multi_slot_net_attribution_unavailable":
            message = (
                "Réconciliation position impossible : le broker ne permet pas "
                "l'attribution multi-slot ; moteur bloqué"
            )
        elif report.reason == "remote_position_lookup_failed":
            message = "Position exchange inconnue : moteur bloqué"
        else:
            message = (
                f"Divergence position locale/exchange : local={report.local_net!r}, "
                f"remote={report.remote_net!r} ; correction automatique interdite"
            )
        self._require_manual_reconciliation(
            message,
            context=context,
            incident_fingerprint="execution:trend:position_reconciliation_required",
            incident_kind="position_reconciliation_required",
        )

    def _prepare_external_execution(self) -> None:
        if not self.broker.supports_stop_orders:
            return
        self._observe_exchange_stop_fills()
        self._maybe_reconcile_position(force=True)
        # Aucune mutation d'ordre protecteur ne précède le rapprochement de
        # position. Cela évite de poser un stop depuis un état local périmé.
        self._recover_protective_stop_transitions()
        self._monitor_exchange_stops()

    def _process_due_bars(self, price: float) -> None:
        for slot in self.slots:
            timeframe_seconds = TIMEFRAME_SECONDS[slot.strategy.timeframe]
            now = self.clock.time()
            seconds_into_bar = now % timeframe_seconds
            if seconds_into_bar < self.poll_buffer:
                continue
            current_bar_start = pd.Timestamp(
                now - seconds_into_bar,
                unit="s",
                tz="UTC",
            )
            previous_bar_start = current_bar_start - pd.Timedelta(seconds=timeframe_seconds)
            if slot.last_bar_ts is not None and slot.last_bar_ts >= previous_bar_start:
                continue
            frame = self._fetch_frame(slot.strategy)
            if frame.empty:
                continue
            if slot.last_bar_ts is None:
                self._process_bar(slot, price, frame=frame, target_ts=frame.index[-1])
                continue
            checkpoint = pd.Timestamp(slot.last_bar_ts)
            if checkpoint.tzinfo is None:
                checkpoint = checkpoint.tz_localize("UTC")
            else:
                checkpoint = checkpoint.tz_convert("UTC")
            if (
                slot.position is not None
                and not self.broker.supports_stop_orders
                and checkpoint < frame.index[0]
                and checkpoint < previous_bar_start
            ):
                self._require_manual_reconciliation(
                    "Historique PAPER insuffisant pour reconstituer les stops manqués",
                    slot=slot,
                    context={
                        "checkpoint": checkpoint.isoformat(),
                        "oldest_available_bar": frame.index[0].isoformat(),
                    },
                    incident_fingerprint=(
                        f"execution:trend:paper_history_gap:{slot.strategy.name}"
                    ),
                    incident_kind="paper_history_gap",
                )
            due = frame.index[(frame.index > checkpoint) & (frame.index <= previous_bar_start)]
            normal_next_bar = (
                len(due) == 1
                and due[0] == previous_bar_start
                and checkpoint + pd.Timedelta(seconds=timeframe_seconds) == previous_bar_start
            )
            for bar_ts in due:
                process_kwargs = {} if normal_next_bar else {"allow_trade_actions": False}
                self._process_bar(
                    slot,
                    price,
                    frame=frame,
                    target_ts=bar_ts,
                    **process_kwargs,
                )

    def _run_cycle(self, price: float, stop_event: threading.Event) -> bool:
        self._apply_funding_payments(price)
        observe_price = getattr(self.broker, "observe_market_price", None)
        if callable(observe_price):
            observe_price(price)
        self._monitor_exchange_stops()
        self._maybe_reconcile_position()
        # Le risque est évalué avant toute stratégie : un kill switch liquide
        # au tick courant, sans attendre la prochaine barre.
        self._update_kill_switches(price)
        self._liquidate_if_halted(price)
        self._check_soft_stops(price)
        self._process_due_bars(price)
        # A bar can trigger a PAPER exchange stop; reconcile it in the same
        # cycle before persisting the final projections.
        self._monitor_exchange_stops()
        self._save_state()
        self._append_equity(price)
        if not self.halted or any(slot.position is not None for slot in self.slots):
            return False
        if not self._halt_notified:
            log.error(
                "Kill switch actif et positions liquidées : moteur en veille "
                "(intervention manuelle requise pour reprendre)."
            )
            self._halt_notified = True
        stop_event.wait(TICK_SECONDS)
        return True

    def run_forever(self, stop_event: threading.Event | None = None) -> None:
        stop_event = stop_event or threading.Event()
        with EngineInstanceLock(self.store.path, "trend"):
            # Une instance construite avant l'acquisition du lock peut avoir
            # chargé un état obsolète pendant qu'une autre instance tournait.
            # Recharger ici rend l'instance propriétaire de la boucle autoritaire.
            self._load_state()
            if self.reconciliation_required:
                raise ReconciliationRequired(
                    "État trend marqué reconciliation_required : démarrage interdit"
                )
            self._run_forever_owned(stop_event)

    def _run_forever_owned(self, stop_event: threading.Event) -> None:
        last_price: float | None = None
        consecutive_failures = 0
        clean_shutdown = False
        log.info(
            "Runner démarré : %s, stratégies %s",
            self.symbol,
            [s.strategy.name for s in self.slots],
        )
        self._prepare_external_execution()
        try:
            while not stop_event.is_set():
                waited_while_halted = False
                try:
                    price = self._last_price()
                    last_price = price
                    waited_while_halted = self._run_cycle(price, stop_event)
                except ReconciliationRequired:
                    log.critical("Arrêt fail-closed : réconciliation manuelle requise")
                    raise
                except Exception as error:
                    log.exception("Erreur dans la boucle principale (on continue)")
                    consecutive_failures += 1
                    self._record_loop_failure(error, consecutive_failures)
                else:
                    if consecutive_failures:
                        consecutive_failures = 0
                        self.store.resolve_incident("execution:trend:loop_failure")
                if not waited_while_halted:
                    stop_event.wait(TICK_SECONDS)
            clean_shutdown = True
        finally:
            if clean_shutdown:
                self._save_state()
                if last_price is not None:
                    self._append_equity(last_price)
                log.info("Runner arrêté proprement ; checkpoint final enregistré")
            else:
                # Un fill peut déjà avoir muté la mémoire alors que la transaction
                # ordre + checkpoint n'a pas abouti. Sauvegarder ici contournerait
                # précisément l'atomicité et pourrait comptabiliser le fill deux fois.
                log.critical("Arrêt non propre : checkpoint final volontairement ignoré")
