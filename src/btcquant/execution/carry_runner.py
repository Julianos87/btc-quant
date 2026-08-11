"""Runner du cash-and-carry (paper trading).

Boucle :
- toutes les 5 minutes, récupère l'historique récent des funding réels
  (venue publique, Hyperliquid depuis le 17/07/2026) et calcule le signal :
  funding lissé `smooth_days` jours annualisé > enter_ann → position ON ;
  < exit_ann → position OFF ;
- en position, chaque événement de funding réellement publié est crédité sur
  le notionnel perp fixé à l'entrée ; le borrow est débité sur son principal
  fixé et la durée UTC réellement écoulée ;
- chaque bascule ON/OFF coûte 2 jambes × (frais + slippage) × levier ;
- état, ordres et événements persistés dans SQLite (reprise après redémarrage).

Mode live : non implémenté volontairement — l'exécution double-jambe
(spot + perp simultanés, gestion de marge) sera un jalon séparé, à valider
sur testnet. Ce runner paper utilise les vrais taux de funding et la même
décision que le backtest, mais ne simule ni divergence de basis, ni marge, ni
liquidation : sa courbe ne qualifie pas ces risques d'exécution réelle.
"""

from __future__ import annotations

import logging
import threading
import uuid
from pathlib import Path

import pandas as pd

from ..carry import (
    PAPER_CARRY_POLICY,
    CarryPolicy,
    elapsed_years_between,
    funding_event_gaps,
    funding_event_id,
    normalize_funding_events,
    smooth_funding_events,
)
from ..domain.carry_decision import CarryAction, decide_carry_payment
from ..notify import notify
from ..risk import RiskConfig
from .carry_contract import CarrySagaStatus
from .errors import AccountingIdentityCollision
from .ports import MarketDataPort, Notifier
from .risk_service import PortfolioRiskService, PortfolioRiskState
from .state_contract import CarryStatePayload, validate_carry_state
from .state_store import StateStore, database_path
from .venue import Venue

log = logging.getLogger(__name__)

TICK_SECONDS = 300
#: Marge de rattrapage au premier tick d'une base neuve : sans checkpoint, on
#: ne réclame que de quoi amorcer le lissage, jamais tout l'historique.
BOOTSTRAP_MARGIN_DAYS = 1
#: Coupe-circuits du carry. Le drawdown historique du profil x3 financé à 10 %/an
#: est de -11,6 % : un halt à 25 % est une protection catastrophe, qui ne coupe
#: pas un creux normal. La limite journalière est délibérément serrée — une
#: structure delta-neutre qui perd 3 % en un jour ne se comporte plus comme le
#: modèle (basis qui diverge, jambe orpheline, marge appelée).
CARRY_MAX_DRAWDOWN_HALT = 0.25
CARRY_DAILY_LOSS_LIMIT = 0.03


class CarryRunner:
    def __init__(
        self,
        exchange_id: str = "hyperliquid",
        symbol_perp: str = "BTC/USDC:USDC",
        policy: CarryPolicy = PAPER_CARRY_POLICY,
        state_file: str | Path = "state/btcquant.db",
        legacy_state_file: str | Path | None = None,
        live_broker=None,
        venue: MarketDataPort | None = None,
        notifier: Notifier = notify,
        risk: RiskConfig | None = None,
        risk_service: PortfolioRiskService | None = None,
    ) -> None:
        self.symbol = symbol_perp
        #: règles partagées mot pour mot avec `carry.backtest_carry` : c'est la
        #: condition pour que la référence publiée décrive ce moteur.
        self.policy = policy
        self.leverage = policy.leverage
        self.enter_ann = policy.enter_ann
        self.exit_ann = policy.exit_ann
        self.smooth_days = policy.smooth_days
        #: coût annuel du principal emprunté pour financer la jambe spot,
        #: fixé à l'entrée et réduit uniquement lors d'une sortie partielle ;
        #: même convention que `carry.backtest_carry`.
        self.borrow_rate_ann = policy.borrow_rate_ann
        self.switch_cost = policy.switch_cost
        initial_capital = policy.capital
        self.risk = risk or RiskConfig(
            initial_capital=initial_capital,
            max_drawdown_halt=CARRY_MAX_DRAWDOWN_HALT,
            daily_loss_limit=CARRY_DAILY_LOSS_LIMIT,
        )
        self.risk_service = risk_service or PortfolioRiskService(self.risk)
        self.peak_equity = initial_capital
        self.day: str | None = None
        self.day_start_equity = initial_capital
        self.halted = False
        self.daily_lockout = False
        self._halt_notified = False
        self.state_path = Path(state_file)
        self.legacy_state_path = (
            Path(legacy_state_file) if legacy_state_file is not None else self.state_path
        )
        self.store = StateStore(database_path(self.state_path))
        if self.store.path.name == "btcquant.db":
            self.store.migrate_legacy_journals(self.state_path.parent)
        self.venue: MarketDataPort = venue or Venue(exchange_id, symbol_perp)
        self.notifier = notifier
        self.live_broker = live_broker  # adaptateur futur double-jambe, None en paper
        self.equity = initial_capital
        self.in_position = False
        self.execution_state = "FLAT"
        self.qty = 0.0  # BTC détenu (live)
        self.spot_qty = 0.0
        self.perp_qty = 0.0
        self.entry_equity: float | None = None
        self.entry_timestamp: pd.Timestamp | None = None
        self.spot_notional = 0.0
        self.perp_notional = 0.0
        self.borrow_principal = 0.0
        self.position_generation: str | None = None
        self.funding_notional_price: float | None = None
        self.last_funding_ts: pd.Timestamp | None = None
        self.accounting_uncertain = False
        self.accounting_uncertainty_reason: str | None = None
        self._load_state()
        pending = self.store.pending_orders("carry")
        if pending and self.live_broker is not None:
            raise RuntimeError(
                f"{len(pending)} ordre(s) carry indéterminé(s) après crash : "
                "réconciliation manuelle requise"
            )
        for order in pending:
            self.store.complete_order(
                order["id"],
                status="RECOVERED_ABORTED",
                error="Ordre paper interrompu par un crash",
            )
        if self.live_broker is not None and self.execution_state in (
            "OPENING",
            "CLOSING",
            "UNBALANCED",
        ):
            raise RuntimeError(
                f"Carry en état {self.execution_state} : intervention manuelle requise"
            )
        if self.live_broker is not None and self.accounting_uncertain:
            raise RuntimeError("Carry en incertitude comptable : réconciliation manuelle requise")
        if self.live_broker is not None and not self.live_broker.reconcile():
            raise RuntimeError("Réconciliation carry échouée : runner arrêté (fail-closed)")

    def _load_state(self) -> None:
        self.store.migrate_legacy_json("carry", self.legacy_state_path)
        stored = self.store.load_engine_state("carry")
        raw = validate_carry_state(stored) if stored is not None else None
        if raw is None:
            return
        self.equity = raw["equity"]
        self.in_position = raw["in_position"]
        self.execution_state = raw.get("execution_state", "OPEN" if self.in_position else "FLAT")
        self.qty = raw.get("qty", 0.0)
        self.spot_qty = raw.get("spot_qty", self.qty)
        self.perp_qty = raw.get("perp_qty", self.qty)
        self.entry_equity = raw.get("entry_equity")
        entry_timestamp = raw.get("entry_timestamp")
        self.entry_timestamp = pd.Timestamp(entry_timestamp) if entry_timestamp else None
        self.spot_notional = raw.get("spot_notional", 0.0)
        self.perp_notional = raw.get("perp_notional", 0.0)
        self.borrow_principal = raw.get("borrow_principal", 0.0)
        self.position_generation = raw.get("position_generation")
        self.funding_notional_price = raw.get("funding_notional_price")
        last_funding_ts = raw.get("last_funding_ts")
        self.last_funding_ts = pd.Timestamp(last_funding_ts) if last_funding_ts else None
        self.peak_equity = raw.get("peak_equity", self.equity)
        self.day = raw.get("day")
        self.day_start_equity = raw.get("day_start_equity", self.equity)
        self.halted = raw.get("halted", False)
        self.daily_lockout = raw.get("daily_lockout", False)
        self.accounting_uncertain = raw.get("accounting_uncertain", False)
        self.accounting_uncertainty_reason = raw.get("accounting_uncertainty_reason")
        if self.in_position and not self.accounting_uncertain:
            required = (
                self.entry_equity,
                self.entry_timestamp,
                self.position_generation,
                self.perp_notional,
                self.borrow_principal,
                self.last_funding_ts,
            )
            valid_timestamp = all(
                isinstance(value, pd.Timestamp) and value.tzinfo is not None
                for value in (self.entry_timestamp, self.last_funding_ts)
            )
            valid_notional = isinstance(self.perp_notional, (int, float)) and self.perp_notional > 0
            if (
                not valid_timestamp
                or not valid_notional
                or any(value is None for value in required)
            ):
                self._mark_accounting_uncertain(
                    "Checkpoint de position carry incomplet pour la reprise comptable",
                    {"classification": "ACCOUNTING_UNCERTAIN"},
                )
        log.info("État carry rechargé : équity %.2f, position %s", self.equity, self.in_position)

    def _state_payload(self) -> CarryStatePayload:
        return {
            "equity": self.equity,
            "in_position": self.in_position,
            "execution_state": self.execution_state,
            "qty": self.qty,
            "spot_qty": self.spot_qty,
            "perp_qty": self.perp_qty,
            "entry_equity": self.entry_equity,
            "entry_timestamp": (
                str(self.entry_timestamp) if self.entry_timestamp is not None else None
            ),
            "spot_notional": self.spot_notional,
            "perp_notional": self.perp_notional,
            "borrow_principal": self.borrow_principal,
            "position_generation": self.position_generation,
            "funding_notional_price": self.funding_notional_price,
            "last_funding_ts": (
                str(self.last_funding_ts) if self.last_funding_ts is not None else None
            ),
            "peak_equity": self.peak_equity,
            "day": self.day,
            "day_start_equity": self.day_start_equity,
            "halted": self.halted,
            "daily_lockout": self.daily_lockout,
            "accounting_uncertain": self.accounting_uncertain,
            "accounting_uncertainty_reason": self.accounting_uncertainty_reason,
        }

    def _save_state(self) -> None:
        self.store.save_engine_state("carry", self._state_payload())

    def _recent_funding(self) -> pd.Series:
        """Historique couvrant À LA FOIS le lissage et tout l'arriéré non comptabilisé.

        Une fenêtre fixe de ``smooth_days`` perdait DÉFINITIVEMENT, et sans
        alerte, les paiements antérieurs après un arrêt plus long que cette
        fenêtre. On repart donc toujours du checkpoint quand il est plus ancien
        que le besoin de lissage — même invariant que le moteur trend.
        """

        # +1 jour de marge : le lissage a besoin de smooth_days complets même
        # si le premier paiement de la fenêtre tombe juste avant la borne
        smoothing_start = pd.Timestamp.now(tz="UTC") - pd.Timedelta(
            days=self.smooth_days + BOOTSTRAP_MARGIN_DAYS
        )
        if self.last_funding_ts is None:
            return self.venue.funding_history_since(smoothing_start)
        checkpoint = pd.Timestamp(self.last_funding_ts)
        if checkpoint.tzinfo is None:
            checkpoint = checkpoint.tz_localize("UTC")
        return self.venue.funding_history_since(min(smoothing_start, checkpoint))

    def _native_funding_interval(self) -> pd.Timedelta | None:
        """Return the venue-declared native interval without a row assumption."""

        explicit = getattr(self.venue, "native_funding_interval", None)
        if explicit is not None:
            return pd.Timedelta(explicit)
        payments_per_day = getattr(self.venue, "payments_per_day", None)
        if isinstance(payments_per_day, (int, float)) and payments_per_day > 0:
            return pd.Timedelta(seconds=86_400 / float(payments_per_day))
        return None

    def _mark_accounting_uncertain(self, reason: str, context: dict) -> None:
        if self.accounting_uncertain:
            return
        self.accounting_uncertain = True
        self.accounting_uncertainty_reason = reason
        self.store.record_incident(
            "accounting:carry:funding_uncertainty",
            engine="carry",
            severity="CRITICAL",
            kind="funding_accounting_uncertainty",
            message=reason,
            context=context,
        )
        self.store.save_engine_state(
            "carry",
            self._state_payload(),
            event_type="funding_accounting_uncertain",
            event_payload={"reason": reason, **context},
        )

    def _position_accounting_ready(self) -> bool:
        if not self.in_position:
            return True
        required = (
            self.entry_equity,
            self.entry_timestamp,
            self.position_generation,
            self.perp_notional,
            self.borrow_principal,
        )
        valid_timestamp = all(
            isinstance(value, pd.Timestamp) and value.tzinfo is not None
            for value in (self.entry_timestamp, self.last_funding_ts)
        )
        valid_notional = isinstance(self.perp_notional, (int, float)) and self.perp_notional > 0
        if not valid_timestamp or not valid_notional or any(value is None for value in required):
            self._mark_accounting_uncertain(
                "Position ouverte sans économie de position ni checkpoint fiable",
                {"classification": "ACCOUNTING_UNCERTAIN"},
            )
            return False
        return True

    def _apply_funding_atomic(self, funding: pd.Series) -> None:
        """Applique les événements et leur checkpoint dans une transaction unique."""
        if funding.empty or self.accounting_uncertain:
            return
        funding = normalize_funding_events(funding, context="funding runner")
        funding_interval = self._native_funding_interval()
        if not self._position_accounting_ready():
            return
        if len(funding) > 1:
            gap_report = funding_event_gaps(
                funding,
                funding_interval=funding_interval,
            )
            if gap_report["missing_events"]:
                self._mark_accounting_uncertain(
                    "Funding manquant : P&L historique non reconstructible",
                    {
                        "classification": "MISSING_SOURCE_DATA",
                        "gap_groups": gap_report["gap_groups"],
                        "missing_events": gap_report["missing_events"],
                    },
                )
                return
        if self.last_funding_ts is None:
            if self.in_position:
                self._mark_accounting_uncertain(
                    "Position ouverte sans checkpoint funding d'entrée",
                    {"classification": "ACCOUNTING_UNCERTAIN"},
                )
                return
            self.last_funding_ts = funding.index[-1]
            self.store.save_engine_state(
                "carry",
                self._state_payload(),
                event_type="funding_checkpoint_initialized",
                event_payload={"last_funding_ts": self.last_funding_ts.isoformat()},
            )
            return
        checkpoint = pd.Timestamp(self.last_funding_ts)
        checkpoint = (
            checkpoint.tz_localize("UTC")
            if checkpoint.tzinfo is None
            else checkpoint.tz_convert("UTC")
        )
        payments = funding[funding.index > checkpoint]
        if len(payments) and funding_interval is not None:
            interval_seconds = pd.Timedelta(funding_interval).total_seconds()
            first_elapsed = (payments.index[0] - checkpoint).total_seconds()
            missing_before_first = max(
                0,
                int(round(first_elapsed / interval_seconds)) - 1,
            )
            if missing_before_first and first_elapsed > interval_seconds + 1.0:
                self._mark_accounting_uncertain(
                    "Funding manquant avant le premier événement récupéré",
                    {
                        "classification": "MISSING_SOURCE_DATA",
                        "checkpoint": checkpoint.isoformat(),
                        "first_event": payments.index[0].isoformat(),
                        "missing_events": missing_before_first,
                    },
                )
                return
        for payment_ts, raw_rate in zip(
            pd.DatetimeIndex(payments.index), payments.to_numpy(), strict=True
        ):
            rate = float(raw_rate)
            event_id = funding_event_id(
                getattr(self.venue, "exchange_id", "unknown"),
                self.symbol,
                payment_ts,
            )
            previous_equity = self.equity
            previous_checkpoint = checkpoint
            elapsed_seconds = max(0.0, (payment_ts - checkpoint).total_seconds())
            elapsed_years = elapsed_years_between(checkpoint, payment_ts)
            funding_notional = self.perp_notional if self.in_position else 0.0
            borrow_principal = self.borrow_principal if self.in_position else 0.0
            funding_gain = funding_notional * rate
            borrow_cost = borrow_principal * self.borrow_rate_ann * elapsed_years
            gain = funding_gain - borrow_cost
            self.equity += gain
            self.last_funding_ts = payment_ts
            ledger = {
                "event_key": event_id,
                "venue": getattr(self.venue, "exchange_id", "unknown"),
                "instrument": self.symbol,
                "funding_timestamp": payment_ts.isoformat(),
                "native_funding_rate": rate,
                "position_generation": self.position_generation or "FLAT",
                "funding_notional": funding_notional,
                "funding_pnl": funding_gain,
                "borrow_principal": borrow_principal,
                "borrow_rate_ann": self.borrow_rate_ann,
                "borrow_dt_seconds": elapsed_seconds,
                "borrow_cost": borrow_cost,
                "applied_at": pd.Timestamp.now(tz="UTC").isoformat(),
            }
            event_payload = {
                "reason": "funding_payment",
                "event_id": event_id,
                "payment_ts": payment_ts.isoformat(),
                "native_funding_rate": rate,
                "funding_event_pnl": funding_gain,
                "borrow_cost": borrow_cost,
                "gain": gain,
                "elapsed_seconds": elapsed_seconds,
                "elapsed_years": elapsed_years,
                "funding_notional_price": self.funding_notional_price,
                "position_generation": self.position_generation or "FLAT",
            }
            try:
                result = self.store.apply_carry_accounting_event_and_checkpoint(
                    ledger,
                    self._state_payload(),
                    event_payload=event_payload,
                )
            except AccountingIdentityCollision as error:
                self.equity = previous_equity
                self.last_funding_ts = previous_checkpoint
                self._mark_accounting_uncertain(
                    "Collision d'identité funding : comptabilité ambiguë",
                    {
                        "classification": "ACCOUNTING_IDENTITY_COLLISION",
                        "event_id": event_id,
                        "detail": error.detail,
                    },
                )
                return
            except Exception:
                self.equity = previous_equity
                self.last_funding_ts = previous_checkpoint
                raise
            if result == "replayed":
                self.equity = previous_equity
            checkpoint = payment_ts
            if self.in_position:
                log.info(
                    "[CARRY] Funding %s : %+.4f%% -> %+.2f USDT (portage -%.2f USDT, equity %.2f)",
                    payment_ts,
                    rate * 100,
                    gain,
                    borrow_cost,
                    self.equity,
                )

    def _apply_funding(self, funding: pd.Series) -> None:
        """Comptabilise et persiste chaque paiement exactement une fois.

        Equity et curseur sont checkpointés ensemble après chaque paiement.
        Si l'écriture échoue, l'état mémoire est restauré : le tick suivant
        peut rejouer ce paiement sans doubler ceux déjà validés.
        """

        return self._apply_funding_atomic(funding)

    def _reset_position_accounting(self) -> None:
        self.entry_equity = None
        self.entry_timestamp = None
        self.spot_notional = 0.0
        self.perp_notional = 0.0
        self.borrow_principal = 0.0
        self.position_generation = None
        self.funding_notional_price = None

    def _open_position(self, smooth_ann: float) -> None:
        previous_checkpoint = self.last_funding_ts
        entry_equity = self.equity
        entry_timestamp = pd.Timestamp.now(tz="UTC")
        entry_notional = entry_equity * self.leverage
        self.entry_equity = entry_equity
        self.entry_timestamp = entry_timestamp
        self.spot_notional = entry_notional
        self.perp_notional = entry_notional
        self.borrow_principal = entry_equity * max(0.0, self.leverage - 1.0)
        self.position_generation = (
            funding_event_id(
                getattr(self.venue, "exchange_id", "unknown"),
                self.symbol,
                entry_timestamp,
            )
            + "|position"
        )
        self.funding_notional_price = None
        self.last_funding_ts = entry_timestamp
        order_id = None
        fill_price = None
        if self.live_broker is not None:
            intent_id = f"carry-open-{uuid.uuid4().hex}"
            self.execution_state = "OPENING"
            order_id = self.store.begin_order_and_checkpoint(
                "carry",
                "carry",
                intent_id,
                "CARRY_PAIR",
                "OPEN",
                0.0,
                f"notional={entry_notional:.2f}",
                self._state_payload(),
            )
            # Un timeout peut cacher un fill : l'intention PENDING et le
            # checkpoint OPENING restent persistés pour bloquer la reprise.
            result = self.live_broker.open_position(
                entry_notional,
                intent_id=intent_id,
            )
            self.spot_qty = result.spot_qty
            self.perp_qty = result.perp_qty
            self.qty = result.neutral_qty
            if result.status == CarrySagaStatus.UNBALANCED:
                self.in_position = result.spot_qty > 0 or result.perp_qty > 0
                self.execution_state = "UNBALANCED"
                self.store.complete_order_and_checkpoint(
                    order_id,
                    engine="carry",
                    state=self._state_payload(),
                    status="UNBALANCED",
                    filled_qty=self.qty,
                    error=result.error or "Ouverture double-jambe déséquilibrée",
                )
                self.notifier(
                    "⛔ CARRY CRITIQUE : ouverture déséquilibrée. Intervention manuelle requise."
                )
                return
            if result.status == CarrySagaStatus.REJECTED:
                self.in_position = False
                self.execution_state = "FLAT"
                self._reset_position_accounting()
                self.last_funding_ts = previous_checkpoint
                self.store.complete_order_and_checkpoint(
                    order_id,
                    engine="carry",
                    state=self._state_payload(),
                    status="REJECTED",
                    error=result.error,
                )
                return
            if not result.is_balanced or self.qty <= 0:
                raise RuntimeError("Résultat carry incohérent malgré un statut exécutable")
            fill_price = result.spot_fill.average_price if result.spot_fill is not None else None
            perp_fill = result.perp_fill
            if perp_fill is not None and perp_fill.average_price is not None:
                self.funding_notional_price = float(perp_fill.average_price)
                self.perp_notional = abs(float(perp_fill.filled_qty) * self.funding_notional_price)
            elif fill_price is not None and self.perp_qty > 0:
                self.funding_notional_price = float(fill_price)
                self.perp_notional = abs(float(self.perp_qty) * self.funding_notional_price)
            if result.spot_fill is not None and result.spot_fill.average_price is not None:
                self.spot_notional = abs(
                    float(result.spot_fill.filled_qty) * float(result.spot_fill.average_price)
                )
        else:
            self.equity *= 1.0 - self.switch_cost

        self.in_position = True
        self.execution_state = "OPEN"
        if order_id is not None:
            self.store.complete_order_and_checkpoint(
                order_id,
                engine="carry",
                state=self._state_payload(),
                status=result.status.value,
                filled_qty=self.qty,
                price=fill_price,
            )
        log.info(
            "[CARRY] ENTRÉE (funding lissé %.1f%%/an) — coût %.2f%%, équity %.2f",
            smooth_ann * 100,
            self.switch_cost * 100,
            self.equity,
        )
        self.notifier(
            f"🔵 Carry — position OUVERTE (funding lissé {smooth_ann:.1%}/an), "
            f"équity {self.equity:,.2f} $"
        )

    def _close_position(self, smooth_ann: float, reason: str = "funding_exit") -> None:
        if self.live_broker is not None:
            intent_id = f"carry-close-{uuid.uuid4().hex}"
            previous_qty = self.qty
            self.execution_state = "CLOSING"
            order_id = self.store.begin_order_and_checkpoint(
                "carry",
                "carry",
                intent_id,
                "CARRY_PAIR",
                "CLOSE",
                self.qty,
                reason,
                self._state_payload(),
            )
            result = self.live_broker.close_position(self.qty, intent_id=intent_id)
            self.spot_qty = result.spot_qty
            self.perp_qty = result.perp_qty
            self.qty = result.neutral_qty
            if result.status == CarrySagaStatus.UNBALANCED:
                self.execution_state = "UNBALANCED"
                self.store.complete_order_and_checkpoint(
                    order_id,
                    engine="carry",
                    state=self._state_payload(),
                    status="UNBALANCED",
                    filled_qty=max(0.0, previous_qty - self.qty),
                    error=result.error or "Une ou plusieurs jambes ne sont pas confirmées fermées",
                )
                log.critical("[CARRY] SORTIE INCOMPLÈTE : intervention requise")
                self.notifier(
                    "⛔ CARRY CRITIQUE : fermeture incomplète, état UNBALANCED. "
                    "La position locale est conservée."
                )
                return
            if result.status in (CarrySagaStatus.REJECTED, CarrySagaStatus.PARTIAL):
                self.execution_state = "OPEN"
                self.in_position = self.qty > 0
                if result.status == CarrySagaStatus.PARTIAL and previous_qty > 0:
                    remaining_ratio = max(0.0, min(1.0, self.qty / previous_qty))
                    self.spot_notional *= remaining_ratio
                    self.perp_notional *= remaining_ratio
                    self.borrow_principal *= remaining_ratio
                self.store.complete_order_and_checkpoint(
                    order_id,
                    engine="carry",
                    state=self._state_payload(),
                    status=result.status.value,
                    filled_qty=max(0.0, previous_qty - self.qty),
                    error=result.error,
                )
                return
            closed_qty = previous_qty
            self.qty = self.spot_qty = self.perp_qty = 0.0
        else:
            self.equity *= 1.0 - self.switch_cost
        self._reset_position_accounting()

        self.in_position = False
        self.execution_state = "FLAT"
        if self.live_broker is not None:
            self.store.complete_order_and_checkpoint(
                order_id,
                engine="carry",
                state=self._state_payload(),
                status="FILLED",
                filled_qty=closed_qty,
            )
        log.info(
            "[CARRY] SORTIE (%s, funding lissé %.1f%%/an) — équity %.2f",
            reason,
            smooth_ann * 100,
            self.equity,
        )
        motive = (
            f"funding lissé {smooth_ann:.1%}/an devenu défavorable"
            if reason == "funding_exit"
            else reason
        )
        self.notifier(f"⚪ Carry — position FERMÉE ({motive}), équity {self.equity:,.2f} $")

    def _update_kill_switches(self) -> None:
        """Coupe-circuits portefeuille du carry, mêmes règles que le trend.

        Le carry a longtemps tourné sans aucun filet : 40 % du portefeuille
        dépendaient du seul signal de funding. La politique de risque est
        désormais celle du moteur trend (`PortfolioRiskService`), appliquée à
        l'équity carry.
        """

        today = str(pd.Timestamp.now(tz="UTC").date())
        transition = self.risk_service.evaluate(
            PortfolioRiskState(
                peak_equity=self.peak_equity,
                day=self.day,
                day_start_equity=self.day_start_equity,
                halted=self.halted,
                daily_lockout=self.daily_lockout,
            ),
            equity=self.equity,
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
                "KILL SWITCH carry : équity %.2f < %.2f",
                self.equity,
                self.peak_equity * (1.0 - self.risk.max_drawdown_halt),
            )
            self.store.record_incident(
                "execution:carry:kill_switch",
                engine="carry",
                severity="CRITICAL",
                kind="kill_switch",
                message=f"Kill-switch carry : drawdown maximal atteint (équity {self.equity:,.0f})",
                context={"equity": self.equity, "peak_equity": self.peak_equity},
            )
            self.notifier(
                f"⛔ KILL-SWITCH carry : drawdown maximal atteint "
                f"(équity {self.equity:,.0f} $). Fermeture et arrêt du moteur carry."
            )
        if transition.lockout_triggered:
            log.warning("Limite de perte journalière carry atteinte : plus d'entrées aujourd'hui")
            self.notifier(
                f"🔒 Lockout journalier carry : perte du jour > "
                f"{self.risk.daily_loss_limit:.0%} (équity {self.equity:,.0f} $)."
            )

    def _tick(self) -> None:
        funding = self._recent_funding()
        if self.accounting_uncertain or funding.empty:
            return

        self._apply_funding(funding)
        if self.accounting_uncertain:
            return
        # Le risque est évalué AVANT toute décision de position, comme dans le
        # runner trend : un kill-switch ferme au tick courant.
        self._update_kill_switches()
        smoothing = smooth_funding_events(
            funding,
            smooth_days=self.smooth_days,
            funding_interval=self._native_funding_interval(),
        )
        latest = smoothing.coverage.iloc[-1]
        if int(latest["missing_events"]) > 0:
            self._mark_accounting_uncertain(
                "Fenêtre de smoothing funding incomplète",
                {
                    "classification": "MISSING_SOURCE_DATA",
                    "window_start": str(latest["window_start"]),
                    "window_end": str(latest["window_end"]),
                    "missing_events": int(latest["missing_events"]),
                    "coverage_ratio": float(latest["coverage_ratio"]),
                },
            )
            return
        smooth_ann = float(smoothing.annualized.iloc[-1])
        decision = decide_carry_payment(
            in_position=self.in_position,
            smooth_ann=smooth_ann,
            enter_ann=self.enter_ann,
            exit_ann=self.exit_ann,
            halted=self.halted,
            entry_blocked=self.daily_lockout,
        )
        if decision.action is CarryAction.OPEN:
            self._open_position(smooth_ann)
        elif decision.action is CarryAction.CLOSE:
            self._close_position(smooth_ann, reason=decision.reason or "funding_exit")
        elif self.halted and not self._halt_notified:
            log.error("Kill-switch carry actif et position fermée : moteur en veille")
            self._halt_notified = True

        self._save_state()
        self._append_equity()

    def _append_equity(self) -> None:
        """Historique d'équity (une ligne par tick, ~5 min)."""
        self.store.append_equity("carry", self.equity)

    def run_forever(self, stop_event: threading.Event | None = None) -> None:
        stop_event = stop_event or threading.Event()
        mode = "LIVE" if self.live_broker is not None else "PAPER"
        log.info(
            "Carry runner (%s) démarré : %s, levier %.1fx, entrée >%.0f%%/an, sortie <%.0f%%/an",
            mode,
            self.symbol,
            self.leverage,
            self.enter_ann * 100,
            self.exit_ann * 100,
        )
        try:
            while not stop_event.is_set():
                try:
                    self._tick()
                except Exception:
                    log.exception("Erreur carry (on continue)")
                stop_event.wait(TICK_SECONDS)
        finally:
            self._save_state()
            self._append_equity()
            log.info("Carry arrêté proprement ; checkpoint final enregistré")
