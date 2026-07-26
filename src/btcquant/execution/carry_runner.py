"""Runner du cash-and-carry (paper trading).

Boucle :
- toutes les 5 minutes, récupère l'historique récent des funding réels
  (venue publique, Hyperliquid depuis le 17/07/2026) et calcule le signal :
  funding lissé `smooth_days` jours annualisé > enter_ann → position ON ;
  < exit_ann → position OFF ;
- en position, chaque paiement de funding réellement versé (toutes les
  HEURES sur Hyperliquid, toutes les 8 h sur Binance) est crédité :
  équity × taux × levier — l'annualisation s'adapte à la périodicité ;
- chaque bascule ON/OFF coûte 2 jambes × (frais + slippage) × levier ;
- état, ordres et événements persistés dans SQLite (reprise après redémarrage).

Mode live : non implémenté volontairement — l'exécution double-jambe
(spot + perp simultanés, gestion de marge) sera un jalon séparé, à valider
sur testnet. Ce runner paper utilise les VRAIS taux de funding, donc ses
résultats sont directement comparables au backtest.
"""

from __future__ import annotations

import logging
import threading
import uuid
from pathlib import Path

import pandas as pd

from ..carry import DEFAULT_BORROW_RATE_ANN
from ..notify import notify
from .carry_broker import CarrySagaStatus
from .ports import MarketDataPort, Notifier
from .state_store import StateStore, database_path
from .venue import Venue

log = logging.getLogger(__name__)

TICK_SECONDS = 300


class CarryRunner:
    def __init__(
        self,
        exchange_id: str = "hyperliquid",
        symbol_perp: str = "BTC/USDC:USDC",
        initial_capital: float = 4000.0,
        leverage: float = 3.0,
        enter_ann: float = 0.03,
        exit_ann: float = 0.0,
        smooth_days: int = 14,
        fee_rate: float = 0.0005,
        slippage_bps: float = 5.0,
        state_file: str | Path = "state/btcquant.db",
        legacy_state_file: str | Path | None = None,
        live_broker=None,
        borrow_rate_ann: float = DEFAULT_BORROW_RATE_ANN,
        venue: MarketDataPort | None = None,
        notifier: Notifier = notify,
    ) -> None:
        self.symbol = symbol_perp
        self.leverage = leverage
        self.enter_ann = enter_ann
        self.exit_ann = exit_ann
        self.smooth_days = smooth_days
        #: coût annuel des (levier−1)×capital empruntés pour financer la jambe
        #: spot. Débité à chaque paiement de funding, au prorata, tant que la
        #: position est ouverte — même convention que `carry.backtest_carry`.
        self.borrow_rate_ann = borrow_rate_ann
        self.switch_cost = 2 * (fee_rate + slippage_bps / 10_000.0) * leverage
        self.state_path = Path(state_file)
        self.legacy_state_path = (
            Path(legacy_state_file) if legacy_state_file is not None else self.state_path
        )
        self.store = StateStore(database_path(self.state_path))
        if self.store.path.name == "btcquant.db":
            self.store.migrate_legacy_journals(self.state_path.parent)
        self.venue: MarketDataPort = venue or Venue(exchange_id, symbol_perp)
        self.notifier = notifier
        self.live_broker = live_broker  # CarryBroker en mode réel, None en paper
        self.equity = initial_capital
        self.in_position = False
        self.execution_state = "FLAT"
        self.qty = 0.0  # BTC détenu (live)
        self.spot_qty = 0.0
        self.perp_qty = 0.0
        self.last_funding_ts: pd.Timestamp | None = None
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
        if self.live_broker is not None and not self.live_broker.reconcile():
            raise RuntimeError("Réconciliation carry échouée : runner arrêté (fail-closed)")

    def _load_state(self) -> None:
        self.store.migrate_legacy_json("carry", self.legacy_state_path)
        raw = self.store.load_engine_state("carry")
        if raw is None:
            return
        self.equity = raw["equity"]
        self.in_position = raw["in_position"]
        self.execution_state = raw.get("execution_state", "OPEN" if self.in_position else "FLAT")
        self.qty = raw.get("qty", 0.0)
        self.spot_qty = raw.get("spot_qty", self.qty)
        self.perp_qty = raw.get("perp_qty", self.qty)
        self.last_funding_ts = (
            pd.Timestamp(raw["last_funding_ts"]) if raw.get("last_funding_ts") else None
        )
        log.info("État carry rechargé : équity %.2f, position %s", self.equity, self.in_position)

    def _state_payload(self) -> dict:
        return {
            "equity": self.equity,
            "in_position": self.in_position,
            "execution_state": self.execution_state,
            "qty": self.qty,
            "spot_qty": self.spot_qty,
            "perp_qty": self.perp_qty,
            "last_funding_ts": (
                str(self.last_funding_ts) if self.last_funding_ts is not None else None
            ),
        }

    def _save_state(self) -> None:
        self.store.save_engine_state("carry", self._state_payload())

    def _recent_funding(self) -> pd.Series:
        # +1 jour de marge : le lissage a besoin de smooth_days complets même
        # si le premier paiement de la fenêtre tombe juste avant la borne
        return self.venue.funding_history(self.smooth_days + 1)

    def _apply_funding(self, funding: pd.Series) -> None:
        """Comptabilise exactement une fois les paiements depuis le checkpoint."""

        if self.in_position:
            payments = (
                funding
                if self.last_funding_ts is None
                else funding[funding.index > self.last_funding_ts]
            )
            borrow = (self.leverage - 1.0) * self.borrow_rate_ann / self.venue.payments_per_year
            for ts, rate in payments.items():
                carry_cost = self.equity * borrow
                gain = self.equity * (rate * self.leverage - borrow)
                self.equity += gain
                log.info(
                    "[CARRY] Funding %s : %+.4f%% -> %+.2f USDT "
                    "(dont portage -%.2f USDT, équity %.2f)",
                    ts,
                    rate * 100,
                    gain,
                    carry_cost,
                    self.equity,
                )
        self.last_funding_ts = funding.index[-1]

    def _open_position(self, smooth_ann: float) -> None:
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
                f"notional={self.equity * self.leverage:.2f}",
                self._state_payload(),
            )
            # Un timeout peut cacher un fill : l'intention PENDING et le
            # checkpoint OPENING restent persistés pour bloquer la reprise.
            result = self.live_broker.open_position(
                self.equity * self.leverage,
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

    def _close_position(self, smooth_ann: float) -> None:
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
                "funding_exit",
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
            "[CARRY] SORTIE (funding lissé %.1f%%/an) — équity %.2f",
            smooth_ann * 100,
            self.equity,
        )
        self.notifier(
            f"⚪ Carry — position FERMÉE (funding lissé {smooth_ann:.1%}/an devenu "
            f"défavorable), équity {self.equity:,.2f} $"
        )

    def _tick(self) -> None:
        funding = self._recent_funding()
        if funding.empty:
            return

        self._apply_funding(funding)
        window = self.smooth_days * self.venue.payments_per_day
        smooth_ann = float(funding.tail(window).mean() * self.venue.payments_per_year)
        if not self.in_position and smooth_ann > self.enter_ann:
            self._open_position(smooth_ann)
        elif self.in_position and smooth_ann < self.exit_ann:
            self._close_position(smooth_ann)

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
