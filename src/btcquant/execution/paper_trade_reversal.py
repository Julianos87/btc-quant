"""Auditable reversal of an incorrectly simulated PAPER stop trade.

The original trade, stop-fill evidence, and funding corrections stay in the
database. This operation appends a reversal event, restores the open position
and its cash/funding projections, and removes equity samples written after the
incorrect flat-state correction so the next runner samples rebuild the curve.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
import json
import math
from pathlib import Path
import sqlite3
from typing import Any

from .financial_application_plan import sha256_json
from .state_store import StateStore, utc_now

REVERSAL_EVENT = "paper_trade_reversed"
REVERSAL_PREFIX = "paper-trade-reversal-v1|"
FUNDING_CORRECTION_REASON = "historical_stop_funding_reconciliation"


def _connect_readonly(database: Path) -> sqlite3.Connection:
    path = database.resolve(strict=True)
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("BEGIN")
    return connection


def _loads(value: str | bytes) -> dict[str, Any]:
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise ValueError("Checkpoint PAPER invalide")
    return decoded


def _same_number(left: Any, right: Any, *, tolerance: float = 1e-12) -> bool:
    try:
        return math.isclose(float(left), float(right), rel_tol=0.0, abs_tol=tolerance)
    except (TypeError, ValueError):
        return False


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Horodatage sans fuseau dans la preuve PAPER")
    return parsed.astimezone(UTC)


def _event_state(payload: dict[str, Any]) -> dict[str, Any] | None:
    state = payload.get("state")
    if not isinstance(state, dict):
        return None
    digest = payload.get("state_sha256")
    if digest != sha256_json(state):
        raise ValueError("Hash de checkpoint invalide dans la preuve source")
    return state


def _trade_matches(row: sqlite3.Row, payload: dict[str, Any]) -> bool:
    for name in (
        "entry_ts",
        "exit_ts",
        "strategy",
        "direction",
        "qty",
        "entry_price",
        "exit_price",
        "pnl",
        "reason",
    ):
        if name in {"qty", "entry_price", "exit_price", "pnl"}:
            if not _same_number(row[name], payload.get(name)):
                return False
        elif str(row[name]) != str(payload.get(name)):
            return False
    return True


def _base_ledger(
    *,
    event_key: str,
    venue: str,
    instrument: str,
    funding_timestamp: str,
    applied_at: str,
    funding_pnl: float,
    position_generation: str,
    native_funding_rate: float = 0.0,
    funding_notional: float = 0.0,
    funding_notional_price: float | None = None,
    funding_notional_price_source: str | None = None,
    funding_notional_price_timestamp: str | None = None,
) -> dict[str, Any]:
    return {
        "event_key": event_key,
        "venue": venue,
        "instrument": instrument,
        "funding_timestamp": funding_timestamp,
        "native_funding_rate": native_funding_rate,
        "position_generation": position_generation,
        "funding_notional": funding_notional,
        "funding_notional_price": funding_notional_price,
        "funding_notional_price_source": funding_notional_price_source,
        "funding_notional_price_timestamp": funding_notional_price_timestamp,
        "funding_pnl": funding_pnl,
        "borrow_principal": 0.0,
        "borrow_rate_ann": 0.0,
        "borrow_dt_seconds": 0.0,
        "borrow_cost": 0.0,
        "applied_at": applied_at,
    }


def build_plan(database: Path, trade_id: int) -> dict[str, Any]:
    """Build a strict, read-only reversal plan for the selected latest stop trade."""

    if isinstance(trade_id, bool) or trade_id <= 0:
        raise ValueError("trade_id doit être strictement positif")
    with _connect_readonly(database) as connection:
        existing = connection.execute(
            """SELECT id FROM events WHERE engine='trend' AND event_type=?
               AND aggregate_type='trade' AND aggregate_id=? ORDER BY id LIMIT 1""",
            (REVERSAL_EVENT, str(trade_id)),
        ).fetchone()
        if existing is not None:
            return {"already_applied": True, "reversal_event_id": int(existing["id"])}

        trade = connection.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
        if trade is None:
            raise ValueError(f"Trade {trade_id} introuvable")
        latest = connection.execute(
            "SELECT id FROM trades ORDER BY exit_ts DESC, id DESC LIMIT 1"
        ).fetchone()
        if latest is None or int(latest["id"]) != trade_id:
            raise ValueError("Seul le dernier trade peut être annulé par cette procédure")
        if trade["reason"] != "stop_exchange" or trade["strategy"] not in {
            "trend_ls_20",
            "trend_ls_55",
            "trend_ls_100",
        }:
            raise ValueError("La procédure n'accepte qu'une clôture de stop Trend")

        trade_events = connection.execute(
            "SELECT * FROM events WHERE engine='trend' AND event_type='trade_closed' ORDER BY id"
        ).fetchall()
        matched_trade_events = []
        for event in trade_events:
            payload = _loads(event["payload"])
            if _trade_matches(trade, payload):
                matched_trade_events.append((event, payload))
        if len(matched_trade_events) != 1:
            raise ValueError("Preuve trade_closed absente ou ambiguë")
        trade_event, trade_payload = matched_trade_events[0]
        if trade_payload.get("execution_provenance") != "PAPER_SIMULATED_STOP":
            raise ValueError("La provenance n'est pas un stop Paper simulé")
        stop_intent_id = trade_event["correlation_id"]
        if not stop_intent_id:
            raise ValueError("Intention source du stop absente")

        stop_events = connection.execute(
            """SELECT * FROM events WHERE engine='trend'
               AND event_type='external_stop_fill_observed' AND correlation_id=?""",
            (stop_intent_id,),
        ).fetchall()
        if len(stop_events) != 1:
            raise ValueError("Preuve du fill stop Paper absente ou ambiguë")
        stop_event = stop_events[0]
        stop_payload = _loads(stop_event["payload"])
        closed_state = _event_state(stop_payload)
        if closed_state is None:
            raise ValueError("Checkpoint après stop absent")
        after_slot = closed_state.get("slots", {}).get(trade["strategy"], {})
        if after_slot.get("position") is not None:
            raise ValueError("Le checkpoint source du stop n'est pas à plat")

        before_events = connection.execute(
            """SELECT * FROM events WHERE engine='trend' AND id < ? ORDER BY id DESC""",
            (int(stop_event["id"]),),
        ).fetchall()
        before_state = None
        before_event_id = None
        slot_name = str(trade["strategy"])
        for event in before_events:
            payload = _loads(event["payload"])
            candidate = _event_state(payload)
            if candidate is None:
                continue
            slot = candidate.get("slots", {}).get(slot_name, {})
            position = slot.get("position")
            if (
                isinstance(position, dict)
                and position.get("entry_time") == trade["entry_ts"]
                and _same_number(position.get("qty"), trade["qty"])
                and int(position.get("direction", 0))
                == (-1 if trade["direction"] == "SHORT" else 1)
            ):
                before_state = candidate
                before_event_id = int(event["id"])
                break
        if before_state is None:
            raise ValueError("Checkpoint d'avant-stop sans position correspondante")

        engine_row = connection.execute(
            "SELECT payload, updated_at FROM engine_state WHERE engine='trend'"
        ).fetchone()
        if engine_row is None:
            raise ValueError("État Trend absent")
        current_state = _loads(engine_row["payload"])
        if current_state.get("reconciliation_required") is True:
            raise ValueError("Trend est marqué en réconciliation requise")
        current_slot = current_state.get("slots", {}).get(slot_name, {})
        if current_slot.get("position") is not None:
            raise ValueError("La position Trend n'est pas flat après le faux trade")
        for name, slot in current_state.get("slots", {}).items():
            if slot.get("position") is not None:
                raise ValueError(f"Le slot {name} est encore ouvert; état non isolé")

        timeline = current_slot.get("position_timeline")
        if not isinstance(timeline, list):
            raise ValueError("Timeline de position courante absente")
        restored_timeline = [
            item for item in timeline if str(item.get("intent_id")) != str(stop_intent_id)
        ]
        if not restored_timeline or float(restored_timeline[-1].get("qty", 0.0)) <= 0:
            raise ValueError("Suppression de la clôture ne restitue pas une exposition ouverte")

        pre_slot = before_state["slots"][slot_name]
        position = deepcopy(pre_slot["position"])
        position["bars_held"] = int(trade["bars_held"])
        if position.get("direction") not in (-1, 1):
            raise ValueError("Direction de la position source invalide")

        last_funding_ts = current_state.get("last_funding_ts")
        before_funding_ts = before_state.get("last_funding_ts")
        if not last_funding_ts or not before_funding_ts:
            raise ValueError("Checkpoint funding Trend absent")
        before_funding = _timestamp(str(before_funding_ts))
        current_funding = _timestamp(str(last_funding_ts))

        correction_events = connection.execute(
            """SELECT id, ts, payload FROM events WHERE engine='trend'
               AND event_type='funding_payment' ORDER BY id"""
        ).fetchall()
        corrections: list[dict[str, Any]] = []
        ledger_rows_to_reverse: list[dict[str, Any]] = []
        correction_events_to_reverse: list[int] = []
        correction_cutoff = str(stop_event["ts"])
        for event in correction_events:
            payload = _loads(event["payload"])
            if payload.get("reason") != FUNDING_CORRECTION_REASON:
                continue
            correction = payload.get("correction")
            if not isinstance(correction, dict):
                continue
            funding_ts = str(correction.get("funding_timestamp", ""))
            amounts = correction.get("amounts")
            if (
                not funding_ts
                or _timestamp(funding_ts) < _timestamp(str(trade["exit_ts"]))
                or not isinstance(amounts, dict)
                or slot_name not in amounts
            ):
                continue
            nonzero_other_slots = [
                name
                for name, amount in amounts.items()
                if name != slot_name and abs(float(amount)) > 1e-12
            ]
            if nonzero_other_slots:
                raise ValueError("Correction de funding mêlée à une autre position")
            key = str(correction.get("event_key", ""))
            ledger_row = connection.execute(
                "SELECT * FROM funding_ledger WHERE event_key=?", (key,)
            ).fetchone()
            if ledger_row is None or not _same_number(
                ledger_row["funding_pnl"], correction.get("funding_pnl")
            ):
                raise ValueError("Correction et ledger funding ne concordent pas")
            row = dict(ledger_row)
            reverse_key = REVERSAL_PREFIX + "correction|" + key
            if connection.execute(
                "SELECT 1 FROM funding_ledger WHERE event_key=?", (reverse_key,)
            ).fetchone():
                raise ValueError("Ledger de renversement existant sans événement de reprise")
            row["event_key"] = reverse_key
            row["funding_pnl"] = -float(row["funding_pnl"])
            row["position_generation"] = "PAPER_TRADE_REVERSAL_V1:" + str(
                ledger_row["position_generation"]
            )
            row["applied_at"] = utc_now()
            ledger_rows_to_reverse.append(row)
            correction_events_to_reverse.append(int(event["id"]))
            corrections.append(
                {
                    "event_id": int(event["id"]),
                    "event_key": key,
                    "funding_timestamp": funding_ts,
                    "cash_pnl_restored": -float(ledger_row["funding_pnl"]),
                }
            )
            correction_cutoff = max(correction_cutoff, str(event["ts"]))
        if not corrections:
            raise ValueError("Aucune correction funding liée à cette clôture à annuler")

        post_close_funding: list[dict[str, Any]] = []
        for event in correction_events:
            payload = _loads(event["payload"])
            if payload.get("reason") != "funding_payment":
                continue
            payment_ts = payload.get("payment_ts")
            if not payment_ts:
                continue
            payment_time = _timestamp(str(payment_ts))
            if not (before_funding < payment_time <= current_funding):
                continue
            source_key = str(payload.get("event_key", ""))
            original = connection.execute(
                "SELECT * FROM funding_ledger WHERE event_key=?", (source_key,)
            ).fetchone()
            if original is None or not _same_number(
                original["funding_pnl"], payload.get("funding_pnl")
            ):
                raise ValueError("Paiement funding post-stop sans ledger source exact")
            reference = payload.get("reference_price")
            source = payload.get("reference_price_source")
            reference_ts = payload.get("reference_price_timestamp")
            rate = float(payload["native_funding_rate"])
            qty = float(position["qty"])
            direction = int(position["direction"])
            if reference is None or float(reference) <= 0 or not source or not reference_ts:
                raise ValueError("Preuve de prix funding post-stop absente")
            cash_amount = direction * qty * float(reference) * rate
            restored_pnl = -cash_amount
            reverse_key = REVERSAL_PREFIX + "funding|" + source_key
            if connection.execute(
                "SELECT 1 FROM funding_ledger WHERE event_key=?", (reverse_key,)
            ).fetchone():
                raise ValueError("Funding post-stop déjà recrédité sans événement de reprise")
            ledger_rows_to_reverse.append(
                _base_ledger(
                    event_key=reverse_key,
                    venue=str(original["venue"]),
                    instrument=str(original["instrument"]),
                    funding_timestamp=str(payload["payment_ts"]),
                    applied_at=utc_now(),
                    funding_pnl=restored_pnl,
                    position_generation="PAPER_TRADE_REVERSAL_V1:"
                    + f"entry={trade['entry_ts']}|initial_qty={position.get('initial_qty', qty):.17g}",
                    native_funding_rate=rate,
                    funding_notional=qty * float(reference),
                    funding_notional_price=float(reference),
                    funding_notional_price_source=str(source),
                    funding_notional_price_timestamp=str(reference_ts),
                )
            )
            post_close_funding.append(
                {
                    "source_event_id": int(event["id"]),
                    "source_event_key": source_key,
                    "event_key": reverse_key,
                    "payment_ts": str(payload["payment_ts"]),
                    "native_funding_rate": rate,
                    "reference_price": float(reference),
                    "reference_price_source": str(source),
                    "reference_price_timestamp": str(reference_ts),
                    "cash_amount": cash_amount,
                    "funding_pnl": restored_pnl,
                    "qty": qty,
                    "direction": direction,
                }
            )

        state_after = deepcopy(current_state)
        target_slot = state_after["slots"][slot_name]
        target_slot["position"] = position
        target_slot["position_timeline"] = restored_timeline
        target_slot["cash"] = float(pre_slot["cash"]) - math.fsum(
            item["cash_amount"] for item in post_close_funding
        )
        target_slot["entry_fee"] = float(pre_slot.get("entry_fee", 0.0))
        target_slot["stop_order_id"] = None
        target_slot["stop_order_local_id"] = None
        target_slot["stop_intent_id"] = None
        target_slot["stop_transition"] = None
        equity_rows = connection.execute(
            """SELECT COUNT(*) AS n, MIN(ts) AS first_ts, MAX(ts) AS last_ts
               FROM equity_samples WHERE engine='trend' AND ts > ?""",
            (correction_cutoff,),
        ).fetchone()

        return {
            "already_applied": False,
            "trade_id": trade_id,
            "trade": dict(trade),
            "trade_event_id": int(trade_event["id"]),
            "stop_event_id": int(stop_event["id"]),
            "stop_intent_id": str(stop_intent_id),
            "before_event_id": before_event_id,
            "current_state_sha256": sha256_json(current_state),
            "state_after": state_after,
            "funding_corrections": corrections,
            "funding_ledger_reversals": ledger_rows_to_reverse,
            "post_close_funding": post_close_funding,
            "correction_cutoff": correction_cutoff,
            "equity_rows_to_remove": int(equity_rows["n"]),
            "equity_range_to_remove": [equity_rows["first_ts"], equity_rows["last_ts"]],
            "cash_before": float(current_slot["cash"]),
            "cash_restored": float(target_slot["cash"]),
            "cash_delta": float(target_slot["cash"]) - float(current_slot["cash"]),
            "funding_pnl_restored": math.fsum(
                float(row["funding_pnl"]) for row in ledger_rows_to_reverse
            ),
            "pre_close_state_sha256": sha256_json(before_state),
            "current_state_updated_at": str(engine_row["updated_at"]),
        }


def apply_plan(database: Path, plan: dict[str, Any]) -> dict[str, Any]:
    """Atomically reverse the Paper stop projections and append audit events."""

    if plan.get("already_applied"):
        return {"already_applied": True, "reversal_event_id": plan["reversal_event_id"]}
    store = StateStore(database, initialize=False)
    now = utc_now()
    reversal_id = f"false-paper-stop-v1-trade-{int(plan['trade_id'])}"
    state_after = plan["state_after"]
    with store._transaction() as connection:
        existing = connection.execute(
            """SELECT id FROM events WHERE engine='trend' AND event_type=?
               AND aggregate_type='trade' AND aggregate_id=? ORDER BY id LIMIT 1""",
            (REVERSAL_EVENT, str(plan["trade_id"])),
        ).fetchone()
        if existing is not None:
            return {"already_applied": True, "reversal_event_id": int(existing["id"])}
        current = connection.execute(
            "SELECT payload, updated_at FROM engine_state WHERE engine='trend'"
        ).fetchone()
        if (
            current is None
            or sha256_json(_loads(current["payload"])) != plan["current_state_sha256"]
            or str(current["updated_at"]) != plan["current_state_updated_at"]
        ):
            raise ValueError("État Trend changé depuis la préparation; aucune écriture appliquée")
        source_trade = connection.execute(
            "SELECT 1 FROM trades WHERE id=?", (int(plan["trade_id"]),)
        ).fetchone()
        if source_trade is None:
            raise ValueError("Trade source disparu avant la correction")
        for ledger in plan["funding_ledger_reversals"]:
            store._insert_funding_ledger(connection, ledger)

        checkpoint = store._checkpoint_payload(connection, "trend", state_after)
        connection.execute(
            """UPDATE engine_state SET payload=?, updated_at=? WHERE engine='trend'""",
            (store._json(checkpoint), now),
        )
        store._sync_positions(connection, "trend", checkpoint, now)
        deleted = connection.execute(
            "DELETE FROM equity_samples WHERE engine='trend' AND ts > ?",
            (plan["correction_cutoff"],),
        ).rowcount
        if int(deleted) != int(plan["equity_rows_to_remove"]):
            raise ValueError("L'historique d'equity a changé depuis la préparation")

        for payment in plan["post_close_funding"]:
            payload = store._state_event(
                checkpoint,
                {
                    "reason": "paper_trade_reversal_funding",
                    "reversal_id": reversal_id,
                    "source_event_id": payment["source_event_id"],
                    "source_event_key": payment["source_event_key"],
                    "event_key": payment["event_key"],
                    "payment_ts": payment["payment_ts"],
                    "native_funding_rate": payment["native_funding_rate"],
                    "reference_price": payment["reference_price"],
                    "reference_price_source": payment["reference_price_source"],
                    "reference_price_timestamp": payment["reference_price_timestamp"],
                    "amounts": {
                        plan["trade"]["strategy"]: {
                            "amount": payment["cash_amount"],
                            "qty": payment["qty"],
                            "direction": payment["direction"],
                        }
                    },
                    "funding_pnl": payment["funding_pnl"],
                },
            )
            store._insert_event(
                connection,
                "trend",
                "funding_payment",
                payload,
                "funding_event",
                payment["event_key"],
                reversal_id,
                ts=now,
            )

        reversal_payload = store._state_event(
            checkpoint,
            {
                "reason": "Faux stop Paper annulé; position, cash et historique restaurés",
                "reversal_id": reversal_id,
                "trade_id": int(plan["trade_id"]),
                "source_trade_event_id": int(plan["trade_event_id"]),
                "source_stop_event_id": int(plan["stop_event_id"]),
                "source_stop_intent_id": plan["stop_intent_id"],
                "pre_stop_checkpoint_event_id": plan["before_event_id"],
                "realized_pnl_excluded": float(plan["trade"]["pnl"]),
                "cash_before": float(plan["cash_before"]),
                "cash_restored": float(plan["cash_restored"]),
                "cash_delta": float(plan["cash_delta"]),
                "funding_pnl_restored": float(plan["funding_pnl_restored"]),
                "funding_corrections_reversed": plan["funding_corrections"],
                "equity_samples_pruned": {
                    "cutoff_after": plan["correction_cutoff"],
                    "count": int(plan["equity_rows_to_remove"]),
                    "range": plan["equity_range_to_remove"],
                    "reason": "Recalculated from the restored open position on runner restart",
                },
                "restored_position": {
                    "strategy": plan["trade"]["strategy"],
                    "direction": plan["trade"]["direction"],
                    "qty": float(plan["trade"]["qty"]),
                    "entry_ts": plan["trade"]["entry_ts"],
                    "entry_price": float(plan["trade"]["entry_price"]),
                    "stop_price": float(
                        checkpoint["slots"][plan["trade"]["strategy"]]["position"]["stop_price"]
                    ),
                    "last_bar_ts": checkpoint["slots"][plan["trade"]["strategy"]].get(
                        "last_bar_ts"
                    ),
                },
                "source_checkpoint_sha256": plan["current_state_sha256"],
                "pre_stop_checkpoint_sha256": plan["pre_close_state_sha256"],
            },
        )
        reversal_event_id = store._insert_event(
            connection,
            "trend",
            REVERSAL_EVENT,
            reversal_payload,
            "trade",
            str(plan["trade_id"]),
            reversal_id,
            ts=now,
        )
    return {"already_applied": False, "reversal_event_id": reversal_event_id}
