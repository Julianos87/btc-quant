"""PAPER-only coordinator for one BTC spot / linear dated-future carry cycle.

This runner accepts only a read-only public market client and the pure frozen
entry policy. It deliberately has no order-routing port. All simulated fills
consume the supplied book depth and are committed through ``CarryDatedLedger``.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..domain.carry_dated import DatedBook, DatedInstrument
from .carry_dated_ledger import (
    CarryDatedLedger,
    BOOK_DEPTH_LEVELS,
    BOOK_EVIDENCE_PROTOCOL_VERSION,
    DEFAULT_MARKET_EVIDENCE_QUOTA_BYTES,
    DEFAULT_MARKET_EVIDENCE_MIN_FREE_BYTES,
    DatedLedgerError,
    common_quantity_step,
    decimal,
    decimal_string,
    fill_from_book,
    floor_to_step,
    state_decimal,
    utc_string,
)
from .instance_lock import EngineInstanceLock
from .state_store import StateStore

LOG = logging.getLogger("btcquant.carry_dated.runner")
DEFAULT_STATUS_PATH = Path("/opt/btcquant/state/carry-dated-experiment/carry_dated_status.json")
DEFAULT_POLL_SECONDS = 30
DEFAULT_MAX_BOOK_AGE_SECONDS = Decimal("15")
DEFAULT_MAX_BOOK_SKEW_SECONDS = Decimal("5")
DEFAULT_MAX_METADATA_AGE_SECONDS = Decimal("300")
MIN_EXPIRY_DAYS = 14
MAX_EXPIRY_DAYS = 90


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _seconds(left: datetime, right: datetime) -> Decimal:
    return abs(Decimal(str((left - right).total_seconds())))


class CarryDatedRunner:
    """Run a single isolated PAPER allocation using public market observations."""

    def __init__(
        self,
        *,
        store: StateStore,
        market: Any,
        fee_schedule: Any,
        margin_policy: Any,
        capital: Decimal,
        decision_evaluator: Callable[..., Any] | None = None,
        clock: Callable[[], datetime] = _utc_now,
        status_path: str | Path | None = None,
        poll_seconds: int = DEFAULT_POLL_SECONDS,
        max_book_age_seconds: Decimal = DEFAULT_MAX_BOOK_AGE_SECONDS,
        max_book_skew_seconds: Decimal = DEFAULT_MAX_BOOK_SKEW_SECONDS,
        max_metadata_age_seconds: Decimal = DEFAULT_MAX_METADATA_AGE_SECONDS,
        market_evidence_quota_bytes: int = DEFAULT_MARKET_EVIDENCE_QUOTA_BYTES,
        market_evidence_archive_dir: str | Path | None = None,
        market_evidence_min_free_bytes: int = DEFAULT_MARKET_EVIDENCE_MIN_FREE_BYTES,
    ) -> None:
        if poll_seconds < 1:
            raise ValueError("poll_seconds must be positive")
        self.store = store
        self.market = market
        self.fee_schedule = fee_schedule
        self.margin_policy = margin_policy
        self.capital = decimal(capital, field="capital")
        if decision_evaluator is None:
            from ..domain.carry_dated_policy import evaluate_entry

            decision_evaluator = evaluate_entry
        self.decision_evaluator = decision_evaluator
        self.clock = clock
        self.poll_seconds = poll_seconds
        self.max_book_age_seconds = decimal(max_book_age_seconds, field="max_book_age_seconds")
        self.max_book_skew_seconds = decimal(max_book_skew_seconds, field="max_book_skew_seconds")
        self.max_metadata_age_seconds = decimal(
            max_metadata_age_seconds, field="max_metadata_age_seconds"
        )
        configured_status = status_path or os.environ.get("BTCQUANT_CARRY_DATED_STATUS_PATH")
        self.status_path = Path(configured_status) if configured_status else DEFAULT_STATUS_PATH
        from ..research.carry_dated_collector import DatedMarketEvidenceRecorder

        evidence_archive_dir = (
            Path(market_evidence_archive_dir)
            if market_evidence_archive_dir is not None
            else store.path.parent / "archive"
        )
        evidence_recorder = DatedMarketEvidenceRecorder(
            database=store.path.parent / "market.sqlite3",
            archive_dir=evidence_archive_dir,
            max_total_bytes=market_evidence_quota_bytes,
            min_free_bytes=market_evidence_min_free_bytes,
        )
        self.ledger = CarryDatedLedger(
            store,
            self.capital,
            market_evidence_quota_bytes=market_evidence_quota_bytes,
            market_evidence_recorder=evidence_recorder,
        )
        self.market_snapshot_lock_path = store.path.parent / "market.sqlite3.snapshot.lock"
        self._lock = EngineInstanceLock(store.path, "carry_dated")
        self._stop = False

    def start(self) -> None:
        self._lock.acquire()

    def close(self) -> None:
        self._lock.release()

    def run_once(self) -> dict[str, Any]:
        """Advance one bounded cycle, then atomically publish its status view."""

        from ..research.carry_dated_collector import exclusive_lock

        # Coordinate the carry DB + market evidence writer with the collector
        # and clean-room backup snapshot. A fill event must never be backed up
        # without the exact book evidence it references.
        with exclusive_lock(self.market_snapshot_lock_path, blocking=True):
            return self._run_once_under_snapshot_lock()

    def _run_once_under_snapshot_lock(self) -> dict[str, Any]:
        """Run one cycle while holding the cross-database evidence snapshot lock."""

        now = self._now()
        try:
            if self.ledger.state.get("accounting_uncertain") is True:
                LOG.error("dated carry is latched ACCOUNTING_UNCERTAIN; no actions attempted")
            elif self.ledger.state.get("status") == "FLAT":
                self._evaluate_entry(now)
            elif self.ledger.state.get("status") in {"OPENING", "UNBALANCED"}:
                self._recover_opening(now)
            elif self.ledger.state.get("status") == "CLOSING":
                self._execute_close(now)
            elif self.ledger.state.get("status") == "SETTLEMENT_PENDING" and str(
                self.ledger.state.get("active_saga_id") or ""
            ).startswith("dated-close:"):
                self._execute_close(now)
            else:
                self._manage_position(now)
        except Exception as error:  # public API and data failures must not kill supervision
            LOG.exception("dated carry cycle failed (%s)", type(error).__name__)
            try:
                if (
                    self.ledger.state.get("accounting_uncertain") is True
                    or self.ledger.state.get("status") == "ACCOUNTING_UNCERTAIN"
                ):
                    return self.ledger.checkpoint
                failure_now = self._now()
                if state_decimal(self.ledger.state, "spot_qty") or state_decimal(
                    self.ledger.state, "future_qty"
                ):
                    quota_exceeded = "MARKET_EVIDENCE_QUOTA_EXCEEDED" in str(error)
                    self.ledger.mark_settlement_pending(
                        reason_code=(
                            "MARKET_EVIDENCE_QUOTA_EXCEEDED"
                            if quota_exceeded
                            else "RUNNER_CYCLE_ERROR"
                        ),
                        reason_message=f"{type(error).__name__}: {str(error)[:240]}",
                        now=failure_now,
                    )
                else:
                    self.ledger.record_wait(
                        reason_code=(
                            "MARKET_EVIDENCE_QUOTA_EXCEEDED"
                            if "MARKET_EVIDENCE_QUOTA_EXCEEDED" in str(error)
                            else "PUBLIC_DATA_OR_POLICY_ERROR"
                        ),
                        reason_message=f"{type(error).__name__}: {str(error)[:240]}",
                        now=failure_now,
                    )
            except Exception:
                LOG.exception("could not persist the dated carry failure status")
        finally:
            self._write_status(self._now())
        return self.ledger.checkpoint

    def run_forever(self) -> None:
        self.start()
        try:
            self._install_signal_handlers()
            while not self._stop:
                started = time.monotonic()
                self.run_once()
                remaining = max(0.0, self.poll_seconds - (time.monotonic() - started))
                time.sleep(min(remaining, self.poll_seconds))
        finally:
            self.close()

    def stop(self, *_args: Any) -> None:
        self._stop = True

    def _install_signal_handlers(self) -> None:
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, self.stop)

    def _now(self) -> datetime:
        now = self.clock()
        utc_string(now)
        return now.astimezone(UTC)

    def _evaluate_entry(self, now: datetime) -> None:
        instruments = tuple(self.market.list_active_instruments())
        # Production metadata carries the response receipt time. Evaluate it
        # against a clock sample taken after the HTTP requests, not at cycle start.
        now = self._now()
        spots = tuple(
            item
            for item in instruments
            if item.kind == "spot" and item.base == "BTC" and item.quote == "USDC" and item.active
        )
        if len(spots) != 1:
            self.ledger.record_wait(
                reason_code="SPOT_INSTRUMENT_AMBIGUOUS",
                reason_message=f"Expected one active BTC_USDC spot instrument, found {len(spots)}",
                now=now,
            )
            return
        spot = spots[0]
        spot_reasons = self._instrument_quality_reasons(spot, now)
        if spot_reasons:
            self.ledger.record_wait(
                reason_code=spot_reasons[0],
                reason_message="Spot metadata is not eligible: " + ", ".join(spot_reasons),
                now=now,
            )
            return
        spot_book = self._get_book(spot.instrument_id)
        now = self._now()
        spot_evidence_id = self.ledger.persist_market_evidence(
            spot_book,
            purpose="ENTRY_DECISION",
            requested_depth=BOOK_DEPTH_LEVELS,
            protocol_version=BOOK_EVIDENCE_PROTOCOL_VERSION,
            now=now,
            required=False,
        )
        reasons = self._book_quality_reasons(spot_book, now)
        if reasons:
            self.ledger.record_wait(
                reason_code=reasons[0],
                reason_message="Spot book is not eligible: " + ", ".join(reasons),
                now=now,
            )
            return
        if state_decimal(self.ledger.state, "spot_dust_qty") > 0:
            dust_evidence_id = self.ledger.persist_market_evidence(
                spot_book,
                purpose="SPOT_DUST_SWEEP",
                requested_depth=BOOK_DEPTH_LEVELS,
                protocol_version=BOOK_EVIDENCE_PROTOCOL_VERSION,
                now=now,
                required=False,
            )
            self.ledger.mark_spot_dust(
                spot_mark=spot_book.bids[0].price if spot_book.bids else None,
                now=now,
                market_timestamp=spot_book.market_timestamp,
            )
            exit_rate = self._effective_taker_rate(self.fee_schedule.spot_exit_taker_rate, spot)
            self._sweep_residual_spot_dust(
                spot,
                spot_book,
                exit_rate,
                now=now,
                market_evidence_id=dust_evidence_id,
            )
            dust_qty = state_decimal(self.ledger.state, "spot_dust_qty")
            if dust_qty >= decimal(spot.min_amount):
                self.ledger.record_wait(
                    reason_code="SPOT_DUST_SWEEP_PENDING",
                    reason_message=(
                        "Residual spot exceeds the venue minimum but could not be fully sold "
                        "from the current observed bid depth"
                    ),
                    now=now,
                )
                return
        if not spot_evidence_id:
            self.ledger.record_wait(
                reason_code="MARKET_EVIDENCE_QUOTA_EXCEEDED",
                reason_message=(
                    "Entry is refused because the exact observed spot book could not be durably archived"
                ),
                now=now,
            )
            return

        eligible_futures = [
            item
            for item in instruments
            if item.kind == "dated_future"
            and item.base == "BTC"
            and item.quote == "USDC"
            and item.settlement_currency == "USDC"
            and item.venue == spot.venue
            and item.active
            and item.expiration_utc is not None
            and now + timedelta(days=MIN_EXPIRY_DAYS)
            <= item.expiration_utc
            <= now + timedelta(days=MAX_EXPIRY_DAYS)
        ]
        if not eligible_futures:
            self.ledger.record_wait(
                reason_code="NO_ELIGIBLE_DATED_FUTURE",
                reason_message=(
                    "No active BTC_USDC linear USDC-settled future expires in the frozen "
                    f"{MIN_EXPIRY_DAYS}-{MAX_EXPIRY_DAYS} day window"
                ),
                now=now,
            )
            return
        # Select the nearest maturity before checking its book. A stale/empty
        # selected book is a WAIT; it must not fall through to a later contract.
        future = min(eligible_futures, key=lambda item: (item.expiration_utc, item.instrument_id))
        metadata_reasons = self._instrument_quality_reasons(future, now)
        if metadata_reasons:
            self.ledger.record_wait(
                reason_code=metadata_reasons[0],
                reason_message="Selected maturity metadata is not eligible: "
                + ", ".join(metadata_reasons),
                now=now,
            )
            return
        try:
            future_book = self._get_book(future.instrument_id)
            now = self._now()
        except Exception as error:
            now = self._now()
            self.ledger.record_wait(
                reason_code="SELECTED_FUTURE_BOOK_UNAVAILABLE",
                reason_message=f"Selected maturity book unavailable: {type(error).__name__}",
                now=now,
            )
            return
        future_evidence_id = self.ledger.persist_market_evidence(
            future_book,
            purpose="ENTRY_DECISION",
            requested_depth=BOOK_DEPTH_LEVELS,
            protocol_version=BOOK_EVIDENCE_PROTOCOL_VERSION,
            now=now,
        )
        book_reasons = self._book_quality_reasons(future_book, now)
        if book_reasons:
            self.ledger.record_wait(
                reason_code=book_reasons[0],
                reason_message="Selected maturity book is not eligible: " + ", ".join(book_reasons),
                now=now,
            )
            return
        if (
            _seconds(spot_book.market_timestamp, future_book.market_timestamp)
            > self.max_book_skew_seconds
        ):
            self.ledger.record_wait(
                reason_code="BOOKS_DESYNCHRONIZED",
                reason_message="Spot and selected future book market timestamps exceed the skew limit",
                now=now,
            )
            return
        evidence_ids = (spot_evidence_id, future_evidence_id)
        candidates = [(spot, future, spot_book, future_book)]

        decision = self.decision_evaluator(
            candidates,
            capital=self.capital,
            fee_schedule=self.fee_schedule,
            margin_policy=self.margin_policy,
            now=now,
        )
        self.ledger.persist_decision(decision, market_evidence_ids=evidence_ids)
        if decision.action != "OPEN":
            return
        selected = next(
            (
                candidate
                for candidate in candidates
                if candidate[1].instrument_id == decision.selected_instrument_id
            ),
            None,
        )
        if selected is None:
            self.ledger.record_wait(
                reason_code="POLICY_SELECTED_UNAVAILABLE_INSTRUMENT",
                reason_message="Policy selected an instrument outside the validated candidate set",
                now=now,
            )
            return
        self._open(decision, *selected, now=now)

    def _open(
        self,
        decision: Any,
        spot: DatedInstrument,
        future: DatedInstrument,
        spot_book: DatedBook,
        future_book: DatedBook,
        *,
        now: datetime,
    ) -> None:
        if decision.action != "OPEN":
            raise DatedLedgerError("runner cannot start an entry without an OPEN decision")
        now = self._now()
        current_spot_reasons = self._book_quality_reasons(spot_book, now)
        current_future_reasons = self._book_quality_reasons(future_book, now)
        if current_spot_reasons or current_future_reasons:
            self.ledger.record_wait(
                reason_code="ENTRY_BOOK_INVALIDATED",
                reason_message="Entry books invalidated before simulation: "
                + ", ".join((*current_spot_reasons, *current_future_reasons)),
                now=now,
            )
            return
        evidence_ids = (
            self.ledger.persist_market_evidence(
                spot_book,
                purpose="ENTRY_FILL_PREFLIGHT",
                requested_depth=BOOK_DEPTH_LEVELS,
                protocol_version=BOOK_EVIDENCE_PROTOCOL_VERSION,
                now=now,
            ),
            self.ledger.persist_market_evidence(
                future_book,
                purpose="ENTRY_FILL_PREFLIGHT",
                requested_depth=BOOK_DEPTH_LEVELS,
                protocol_version=BOOK_EVIDENCE_PROTOCOL_VERSION,
                now=now,
            ),
        )
        decision_evidence_ids = tuple(
            str(item)
            for item in (self.ledger.state.get("decision") or {}).get("market_evidence_ids", ())
        )
        market_evidence_ids = tuple(dict.fromkeys((*decision_evidence_ids, *evidence_ids)))
        if self.ledger.state.get("decision") is not None:
            checkpoint = dict(self.ledger.state)
            decision_payload = dict(checkpoint["decision"])
            decision_payload["fill_preflight_market_evidence_ids"] = list(evidence_ids)
            checkpoint["decision"] = decision_payload
            self.store.save_carry_dated_checkpoint(checkpoint)
            self.ledger.state = checkpoint

        fee_currency_assumption = decision.spot_fee_currency_assumption
        applied_scenario = (
            fee_currency_assumption if fee_currency_assumption in {"BASE", "QUOTE"} else "BASE"
        )
        spot_rate = self._effective_taker_rate(self.fee_schedule.spot_taker_rate, spot)
        future_rate = self._effective_taker_rate(self.fee_schedule.future_taker_rate, future)
        spot_exit_rate = self._effective_taker_rate(self.fee_schedule.spot_exit_taker_rate, spot)
        gross_spot_qty = decimal(decision.spot_gross_quantity, field="spot_gross_quantity")
        target_future_qty = decimal(decision.quantity, field="quantity")
        spot_check = fill_from_book(
            book=spot_book,
            leg="spot",
            side="BUY",
            quantity=gross_spot_qty,
            fee_rate=spot_rate,
            fee_currency=applied_scenario,
            fill_id="preflight:spot",
        )
        future_check = fill_from_book(
            book=future_book,
            leg="future",
            side="SELL",
            quantity=target_future_qty,
            fee_rate=future_rate,
            fee_currency="QUOTE",
            fill_id="preflight:future",
        )
        if not spot_check.complete or not future_check.complete:
            self.ledger.record_wait(
                reason_code="ENTRY_DEPTH_INSUFFICIENT",
                reason_message=(
                    f"Depth preflight incomplete: spot={decimal_string(spot_check.filled_qty)}"
                    f"/{decimal_string(gross_spot_qty)}, future="
                    f"{decimal_string(future_check.filled_qty)}/{decimal_string(target_future_qty)}"
                ),
                now=now,
            )
            return
        if spot_check.fee_quote_equivalent != decimal(
            decision.spot_fee, field="decision.spot_fee"
        ) or future_check.fee_quote_equivalent != decimal(
            decision.future_entry_fee, field="decision.future_entry_fee"
        ):
            self.ledger.record_wait(
                reason_code="ENTRY_FEE_DECISION_MISMATCH",
                reason_message=(
                    "Effective instrument fee rates changed or no longer match the frozen "
                    "decision budget"
                ),
                now=now,
            )
            return

        if future_book.mark_price is None:
            self.ledger.record_wait(
                reason_code="FUTURE_MARK_MISSING",
                reason_message="Selected future has no public mark price for margin admission",
                now=now,
            )
            return
        actual_im = decimal(
            self.margin_policy.initial_margin(target_future_qty, future_book.mark_price),
            field="initial_margin",
        )
        actual_stress = decimal(
            self.margin_policy.stress_cash_buffer(target_future_qty, future_book.mark_price),
            field="stress_cash_buffer",
        )
        cash_cost = spot_check.notional
        if applied_scenario == "QUOTE":
            cash_cost += spot_check.fee
        cash_cost += future_check.fee + actual_im + actual_stress
        if cash_cost > state_decimal(self.ledger.state, "free_cash"):
            self.ledger.record_wait(
                reason_code="ENTRY_CASH_RECHECK_FAILED",
                reason_message=(
                    f"Actual depth/margin cash {decimal_string(cash_cost)} exceeds free cash "
                    f"{self.ledger.state['free_cash']}"
                ),
                now=now,
            )
            return

        expected_cash = decimal(decision.total_cash_required, field="total_cash_required")
        if expected_cash > state_decimal(self.ledger.state, "free_cash"):
            self.ledger.record_wait(
                reason_code="ENTRY_POLICY_CASH_RECHECK_FAILED",
                reason_message="Frozen decision total cash requirement exceeds current free cash",
                now=now,
            )
            return
        step_tolerance = common_quantity_step(spot.amount_step, future.amount_step)
        tolerance = step_tolerance + abs(
            decimal(decision.expected_hedge_residual, field="expected_hedge_residual")
        )
        delivery_rate_raw = getattr(self.fee_schedule, "delivery_rate", None)
        delivery_rate = (
            decimal(delivery_rate_raw, field="delivery_rate")
            if delivery_rate_raw is not None
            else None
        )
        self.ledger.begin_entry(
            decision=decision,
            spot_instrument=spot,
            future_instrument=future,
            spot_book=spot_book,
            future_book=future_book,
            reserved_margin=actual_im,
            stress_cash_buffer=actual_stress,
            delivery_fee_estimate=decision.delivery_fee_estimate,
            delivery_fee_rate=delivery_rate,
            delivery_fee_model_version=self.fee_schedule.version,
            spot_fee_currency_scenario=applied_scenario,
            spot_entry_fee_rate=spot_rate,
            future_taker_fee_rate=future_rate,
            spot_exit_fee_rate=spot_exit_rate,
            hedge_tolerance=tolerance,
            market_evidence_ids=market_evidence_ids,
        )
        spot_fill = fill_from_book(
            book=spot_book,
            leg="spot",
            side="BUY",
            quantity=gross_spot_qty,
            fee_rate=spot_rate,
            fee_currency=applied_scenario,
            fill_id=self.ledger.fill_id(leg="spot", side="BUY"),
            market_evidence_id=evidence_ids[0],
        )
        future_fill = fill_from_book(
            book=future_book,
            leg="future",
            side="SELL",
            quantity=target_future_qty,
            fee_rate=future_rate,
            fee_currency="QUOTE",
            fill_id=self.ledger.fill_id(leg="future", side="SELL"),
            market_evidence_id=evidence_ids[1],
        )
        self.ledger.apply_fill(spot_fill, timestamp=now)
        self.ledger.apply_fill(future_fill, timestamp=now)
        if not spot_fill.complete or not future_fill.complete:
            self._recover_opening(now)
            return
        self.ledger.mark_open(decision_id=decision.decision_id, now=now)
        self._set_market_timestamps(spot_book, future_book)

    def _recover_opening(self, now: datetime) -> None:
        state = self.ledger.state
        status = state.get("status")
        if status == "UNBALANCED":
            incident = state.get("incident") or {}
            if incident.get("kind") != "DATED_CARRY_HEDGE_QUANTITY_MISMATCH":
                return
        if status not in {"OPENING", "UNBALANCED"}:
            return
        spot_qty = state_decimal(state, "spot_qty")
        future_qty = state_decimal(state, "future_qty")
        if spot_qty == 0 and future_qty == 0:
            decision = state.get("decision") or {}
            self.ledger.abort_unfilled_entry(
                decision_id=str(decision.get("decision_id", "unknown")),
                now=now,
                reason="restart or failed preflight before any durable PAPER fill",
            )
            return

        decision = state.get("decision") or {}
        spot_target_raw = decision.get("spot_expected_net_quantity")
        future_target_raw = decision.get("quantity")
        spot_target = decimal(
            spot_target_raw if spot_target_raw is not None else state_decimal(state, "spot_qty"),
            field="spot_expected_net_quantity",
        )
        future_target = decimal(
            future_target_raw
            if future_target_raw is not None
            else state_decimal(state, "future_qty"),
            field="quantity",
        )
        tolerance = state_decimal(state, "hedge_tolerance")
        fully_filled = (
            abs(spot_qty - spot_target) <= tolerance
            and abs(future_qty - future_target) <= tolerance
            and abs(spot_qty - future_qty) <= tolerance
            and spot_qty > 0
            and future_qty > 0
        )
        if status == "OPENING" and fully_filled:
            self.ledger.mark_open(decision_id=str(decision.get("decision_id", "unknown")), now=now)
            return

        self.ledger.begin_close(
            reason_code="ENTRY_PARTIAL_COMPENSATION",
            reason_message="Resuming the persisted opening saga by closing its partial exposure",
            now=now,
        )
        self._execute_close(now)

    def _manage_position(self, now: datetime) -> None:
        state = self.ledger.state
        instrument = state.get("instrument") or {}
        future_record = instrument.get("future") or {}
        spot_record = instrument.get("spot") or {}
        future_id = future_record.get("instrument_id")
        spot_id = spot_record.get("instrument_id")
        if not future_id or not spot_id:
            self.ledger.mark_uncertain(
                kind="POSITION_INSTRUMENT_MISSING",
                message="Open checkpoint does not identify both held instruments",
                now=now,
            )
            return

        settlement_audit_key = state.get("settlement_audit_start_key") or state.get(
            "last_settlement_key"
        )
        try:
            settlements = self.market.get_settlements(
                str(future_id), after_key=settlement_audit_key
            )
            now = self._now()
        except Exception as error:
            now = self._now()
            self.ledger.mark_settlement_pending(
                reason_code="SETTLEMENT_HISTORY_UNAVAILABLE",
                reason_message=f"Public settlement history unavailable: {type(error).__name__}",
                now=now,
            )
            return
        opened_at_raw = state.get("position_opened_at")
        opened_at = (
            datetime.fromisoformat(str(opened_at_raw).replace("Z", "+00:00"))
            if opened_at_raw
            else None
        )
        for settlement in settlements:
            if settlement.event_timestamp > now:
                continue
            if (
                state.get("last_settlement_key") is None
                and opened_at is not None
                and settlement.event_timestamp <= opened_at
            ):
                # An uninitialized cursor can return older public history. The
                # short starts at its own fill reference, never at old events.
                continue
            try:
                self.ledger.apply_settlement(settlement, now=now)
            except DatedLedgerError as error:
                LOG.error("settlement not applied (%s)", error)
                return
            if settlement.kind == "expiry":
                break

        state = self.ledger.state
        future_qty = state_decimal(state, "future_qty")
        spot_qty = state_decimal(state, "spot_qty")
        if future_qty == 0 and spot_qty > 0:
            self._liquidate_spot_after_expiry(str(spot_id), now)
            return
        expiry_raw = future_record.get("expiration_utc")
        if expiry_raw:
            expiry = datetime.fromisoformat(str(expiry_raw).replace("Z", "+00:00"))
            if now >= expiry and future_qty > 0:
                self.ledger.mark_settlement_pending(
                    reason_code="OFFICIAL_EXPIRY_SETTLEMENT_MISSING",
                    reason_message="Contract expiry passed but official delivery settlement is not available",
                    now=now,
                )
                return

        try:
            spot_book = self._get_book(str(spot_id))
            future_book = self._get_book(str(future_id))
            now = self._now()
        except Exception as error:
            now = self._now()
            self.ledger.mark_settlement_pending(
                reason_code="POSITION_MARKET_DATA_UNAVAILABLE",
                reason_message=f"Position market data unavailable: {type(error).__name__}",
                now=now,
            )
            return
        for book in (spot_book, future_book):
            self.ledger.persist_market_evidence(
                book,
                purpose="POSITION_RISK_CONTROL",
                requested_depth=BOOK_DEPTH_LEVELS,
                protocol_version=BOOK_EVIDENCE_PROTOCOL_VERSION,
                now=now,
                required=False,
            )
        spot_reasons = self._book_quality_reasons(spot_book, now)
        future_reasons = self._book_quality_reasons(future_book, now)
        if spot_reasons or future_reasons:
            self.ledger.mark_settlement_pending(
                reason_code="STALE_MARKET_RISK_REVIEW",
                reason_message="Position books require review: "
                + ", ".join((*spot_reasons, *future_reasons)),
                now=now,
            )
            return
        if (
            _seconds(spot_book.market_timestamp, future_book.market_timestamp)
            > self.max_book_skew_seconds
        ):
            self.ledger.mark_settlement_pending(
                reason_code="BOOKS_DESYNCHRONIZED_RISK_REVIEW",
                reason_message="Position books exceed the timestamp skew limit",
                now=now,
            )
            return
        self._set_market_timestamps(spot_book, future_book)
        self.ledger.resume_open_after_recovery(now=now)

        if future_qty > 0:
            if future_book.mark_price is None:
                self.ledger.mark_settlement_pending(
                    reason_code="FUTURE_MARK_MISSING_RISK_REVIEW",
                    reason_message="Fresh future book has no mark price for margin risk assessment",
                    now=now,
                )
                return
            assessment = self.margin_policy.assess(future_qty, future_book.mark_price)
            cash_after_stress = self.ledger.record_margin_assessment(
                assessment,
                future_mark=future_book.mark_price,
                now=now,
            )
            derivative_cash_after_loss = state_decimal(
                self.ledger.state, "derivative_cash_after_unrealized_short_loss"
            )
            maintenance_margin = decimal(
                assessment.maintenance_margin_usdc,
                field="maintenance_margin_usdc",
            )
            if cash_after_stress < 0 or derivative_cash_after_loss < maintenance_margin:
                self.ledger.begin_close(
                    reason_code="PROTECTIVE_MARGIN_STRESS_EXIT",
                    reason_message=(
                        "Derivative quote cash after adverse unrealized short loss is "
                        f"{decimal_string(derivative_cash_after_loss)}; cash after IM and "
                        f"stress is {decimal_string(cash_after_stress)}"
                    ),
                    now=now,
                )
                self._execute_close(now, spot_book=spot_book, future_book=future_book)
                return

        if spot_book.mark_price is not None and future_book.mark_price is not None:
            self.ledger.mark_to_market(
                spot_mark=spot_book.mark_price,
                future_mark=future_book.mark_price,
                now=now,
                spot_market_timestamp=spot_book.market_timestamp,
            )

    def _execute_close(
        self,
        now: datetime,
        *,
        spot_book: DatedBook | None = None,
        future_book: DatedBook | None = None,
    ) -> None:
        state = self.ledger.state
        instrument = state.get("instrument") or {}
        spot_record = instrument.get("spot") or {}
        future_record = instrument.get("future") or {}
        spot_id = str(spot_record.get("instrument_id") or "")
        future_id = str(future_record.get("instrument_id") or "")
        if not spot_id or not future_id:
            self.ledger.mark_uncertain(
                kind="CLOSE_INSTRUMENT_MISSING",
                message="Close saga cannot identify its held instruments",
                now=now,
            )
            return
        try:
            spot_book = spot_book or self._get_book(spot_id)
            if state_decimal(self.ledger.state, "future_qty") > 0:
                future_book = future_book or self._get_book(future_id)
            now = self._now()
        except Exception as error:
            now = self._now()
            self.ledger.mark_settlement_pending(
                reason_code="CLOSE_BOOK_UNAVAILABLE",
                reason_message=f"No fresh close book is available: {type(error).__name__}",
                now=now,
            )
            return
        spot_evidence_id = self.ledger.persist_market_evidence(
            spot_book,
            purpose="CLOSE_EXECUTION",
            requested_depth=BOOK_DEPTH_LEVELS,
            protocol_version=BOOK_EVIDENCE_PROTOCOL_VERSION,
            now=now,
            required=False,
        )
        if future_book is not None:
            future_evidence_id = self.ledger.persist_market_evidence(
                future_book,
                purpose="CLOSE_EXECUTION",
                requested_depth=BOOK_DEPTH_LEVELS,
                protocol_version=BOOK_EVIDENCE_PROTOCOL_VERSION,
                now=now,
                required=False,
            )
        spot_reasons = self._book_quality_reasons(spot_book, now)
        future_reasons = (
            self._book_quality_reasons(future_book, now) if future_book is not None else ()
        )
        if spot_reasons or future_reasons:
            self.ledger.mark_settlement_pending(
                reason_code="CLOSE_BOOK_STALE",
                reason_message="Close requires current observed books: "
                + ", ".join((*spot_reasons, *future_reasons)),
                now=now,
            )
            return
        future_qty = state_decimal(self.ledger.state, "future_qty")
        if future_qty > 0 and future_book is not None:
            amount = floor_to_step(future_qty, decimal(future_record["amount_step"]))
            if amount < decimal(future_record["min_amount"]):
                self.ledger.mark_settlement_pending(
                    reason_code="FUTURE_DUST_REQUIRES_REVIEW",
                    reason_message="Remaining future short is below the executable minimum amount",
                    now=now,
                )
                return
            if amount > 0:
                fill = fill_from_book(
                    book=future_book,
                    leg="future",
                    side="BUY",
                    quantity=amount,
                    fee_rate=self._position_fee_rate(
                        "future_taker_fee_rate",
                        self.fee_schedule.future_taker_rate,
                        future_record,
                    ),
                    fee_currency="QUOTE",
                    fill_id=self.ledger.fill_id(leg="future", side="BUY"),
                    market_evidence_id=future_evidence_id,
                )
                if fill.filled_qty > 0:
                    self.ledger.apply_fill(fill, timestamp=now)
            if state_decimal(self.ledger.state, "future_qty") > 0:
                return

        spot_qty = state_decimal(self.ledger.state, "spot_qty")
        if spot_qty > 0:
            rate = self._position_fee_rate(
                "spot_exit_fee_rate",
                self.fee_schedule.spot_exit_taker_rate,
                spot_record,
            )
            scenario = "BASE"  # routed spot fee currency remains publicly unknown
            order_amount = floor_to_step(
                spot_qty / (Decimal("1") + rate), decimal(spot_record["amount_step"])
            )
            if order_amount < decimal(spot_record["min_amount"]):
                self.ledger.classify_spot_dust(
                    minimum_amount=decimal(spot_record["min_amount"]),
                    orderable_quantity=order_amount,
                    market_book=spot_book,
                    now=now,
                )
                return
            fill = fill_from_book(
                book=spot_book,
                leg="spot",
                side="SELL",
                quantity=order_amount,
                fee_rate=rate,
                fee_currency=scenario,
                fill_id=self.ledger.fill_id(leg="spot", side="SELL"),
                market_evidence_id=spot_evidence_id,
            )
            if fill.filled_qty > 0:
                self.ledger.apply_fill(fill, timestamp=now)

        self._set_market_timestamps(spot_book, future_book)

    def _liquidate_spot_after_expiry(self, spot_id: str, now: datetime) -> None:
        try:
            spot_book = self._get_book(spot_id)
            now = self._now()
        except Exception as error:
            now = self._now()
            self.ledger.record_wait(
                reason_code="EXPIRY_SPOT_BOOK_UNAVAILABLE",
                reason_message=f"Official future settlement is booked; spot sale awaits a book ({type(error).__name__})",
                now=now,
            )
            return
        self.ledger.persist_market_evidence(
            spot_book,
            purpose="EXPIRY_SPOT_LIQUIDATION",
            requested_depth=BOOK_DEPTH_LEVELS,
            protocol_version=BOOK_EVIDENCE_PROTOCOL_VERSION,
            now=now,
            required=False,
        )
        reasons = self._book_quality_reasons(spot_book, now)
        if reasons:
            self.ledger.record_wait(
                reason_code="EXPIRY_SPOT_BOOK_STALE",
                reason_message="Spot liquidation awaits a fresh observed bid book: "
                + ", ".join(reasons),
                now=now,
            )
            return
        future_record = (self.ledger.state.get("instrument") or {}).get("future") or {}
        expiry_raw = future_record.get("expiration_utc")
        if not expiry_raw:
            self.ledger.mark_uncertain(
                kind="EXPIRY_TIME_MISSING",
                message="Settled future checkpoint lacks its configured expiry timestamp",
                now=now,
            )
            return
        expiry = datetime.fromisoformat(str(expiry_raw).replace("Z", "+00:00"))
        self.ledger.record_expiry_spot_liquidation_timing(
            expiry=expiry,
            observed_book_timestamp=spot_book.market_timestamp,
            now=now,
        )
        late = self.ledger.state.get("expiry_spot_liquidation_late") is True
        if self.ledger.state.get("status") != "CLOSING":
            self.ledger.begin_close(
                reason_code=("EXPIRY_SPOT_LIQUIDATION_LATE" if late else "EXPIRY_SPOT_LIQUIDATION"),
                reason_message=(
                    "Future has settled at official index; the first fresh spot book arrived late, "
                    "so this exit is reported outside the configured timing window"
                    if late
                    else "Future has settled at official index; sell spot separately from observed bids"
                ),
                now=now,
            )
        self._execute_close(now, spot_book=spot_book)

    def _book_quality_reasons(self, book: DatedBook, now: datetime) -> tuple[str, ...]:
        return book.quality_reasons(
            now=now,
            max_age_seconds=self.max_book_age_seconds,
        )

    @staticmethod
    def _effective_taker_rate(schedule_rate: Decimal, instrument: DatedInstrument) -> Decimal:
        configured = decimal(schedule_rate, field="fee_schedule_rate")
        metadata = (
            decimal(instrument.taker_commission, field="instrument.taker_commission")
            if instrument.taker_commission is not None
            else Decimal("0")
        )
        return max(configured, metadata)

    def _get_book(self, instrument_id: str) -> DatedBook:
        """Request the frozen 20-level evidence depth and reject wider responses."""

        book = self.market.get_book(instrument_id, depth=BOOK_DEPTH_LEVELS)
        if len(book.bids) > BOOK_DEPTH_LEVELS or len(book.asks) > BOOK_DEPTH_LEVELS:
            raise DatedLedgerError(
                f"{instrument_id} returned more than the frozen {BOOK_DEPTH_LEVELS}-level book"
            )
        return book

    def _position_fee_rate(
        self,
        state_key: str,
        schedule_rate: Decimal,
        instrument_record: dict[str, Any],
    ) -> Decimal:
        persisted = self.ledger.state.get(state_key)
        if persisted is not None:
            return decimal(persisted, field=state_key)
        # Compatibility for a checkpoint created before effective rates were
        # persisted: use the conservative maximum from stored instrument metadata.
        metadata_raw = instrument_record.get("taker_commission")
        metadata = (
            decimal(metadata_raw, field="instrument.taker_commission")
            if metadata_raw is not None
            else Decimal("0")
        )
        return max(decimal(schedule_rate, field="fee_schedule_rate"), metadata)

    def _sweep_residual_spot_dust(
        self,
        spot: DatedInstrument,
        spot_book: DatedBook,
        fee_rate: Decimal,
        *,
        now: datetime,
        market_evidence_id: str = "",
    ) -> None:
        dust_qty = state_decimal(self.ledger.state, "spot_dust_qty")
        if dust_qty <= 0:
            return
        amount = floor_to_step(dust_qty / (Decimal("1") + fee_rate), spot.amount_step)
        if amount < spot.min_amount:
            return
        fill = fill_from_book(
            book=spot_book,
            leg="spot",
            side="SELL",
            quantity=amount,
            fee_rate=fee_rate,
            fee_currency="BASE",
            fill_id=self.ledger.dust_fill_id(),
            market_evidence_id=market_evidence_id,
        )
        if fill.filled_qty > 0:
            self.ledger.apply_spot_dust_fill(fill, timestamp=now)

    def _instrument_quality_reasons(
        self, instrument: DatedInstrument, now: datetime
    ) -> tuple[str, ...]:
        reasons: list[str] = []
        if not instrument.active or instrument.instrument_state.lower() != "open":
            reasons.append("INSTRUMENT_NOT_OPEN")
        metadata_age = Decimal(str((now - instrument.metadata_timestamp).total_seconds()))
        if metadata_age > self.max_metadata_age_seconds:
            reasons.append("INSTRUMENT_METADATA_STALE")
        if metadata_age < Decimal("-1"):
            reasons.append("INSTRUMENT_METADATA_FROM_FUTURE")
        if instrument.kind == "dated_future":
            if instrument.expiration_utc is None or instrument.expiration_utc <= now:
                reasons.append("FUTURE_EXPIRED_OR_EXPIRY_UNKNOWN")
            if instrument.settlement_currency != "USDC":
                reasons.append("FUTURE_SETTLEMENT_NOT_USDC")
        return tuple(dict.fromkeys(reasons))

    def _set_market_timestamps(self, spot_book: DatedBook, future_book: DatedBook | None) -> None:
        candidate = dict(self.ledger.state)
        market = dict(candidate.get("market") or {})
        market["spot_timestamp"] = utc_string(spot_book.market_timestamp)
        if future_book is not None:
            market["future_timestamp"] = utc_string(future_book.market_timestamp)
        candidate["market"] = market
        candidate["spot_market_timestamp"] = market["spot_timestamp"]
        candidate["future_market_timestamp"] = market["future_timestamp"]
        candidate["market_timestamps"] = {
            "spot": market["spot_timestamp"],
            "future": market["future_timestamp"],
        }
        self.store.save_carry_dated_checkpoint(candidate)
        self.ledger.state = candidate

    def _write_status(self, now: datetime) -> None:
        try:
            state = self.ledger.checkpoint
            market = state.get("market") or {}
            state.update(
                {
                    "schema_version": 1,
                    "strategy": "carry_btc_dated",
                    "paper_only": True,
                    "status_updated_at": utc_string(now),
                    "spot_market_timestamp": state.get(
                        "spot_market_timestamp", market.get("spot_timestamp")
                    ),
                    "future_market_timestamp": state.get(
                        "future_market_timestamp", market.get("future_timestamp")
                    ),
                    "market_timestamps": {
                        "spot": state.get("spot_market_timestamp", market.get("spot_timestamp")),
                        "future": state.get(
                            "future_market_timestamp", market.get("future_timestamp")
                        ),
                    },
                }
            )
            encoded = (
                json.dumps(
                    state,
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                    allow_nan=False,
                )
                + "\n"
            )
            path = self.status_path
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
                directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except BaseException:
                try:
                    temporary.unlink(missing_ok=True)
                finally:
                    raise
        except FileExistsError:
            LOG.warning("status snapshot temporary file already exists; next tick will retry")
        except Exception:
            LOG.exception("could not atomically publish the dated-carry status JSON")
