from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from btcquant.execution.external_evidence import ExternalEvidenceSource, ExternalFill
from btcquant.execution.financial_application_plan import (
    FinancialApplicationPlan,
    PersistedFinancialApplicationPlan,
    canonical_json,
    sha256_json,
)
from btcquant.execution.financial_fill_application import (
    FINANCIAL_FILL_APPLICATION_VERSION,
    FinancialApplicationLedgerConflict,
    FinancialFillApplicationError,
    FinancialFillApplicationRequest,
    calculate_financial_fill_application,
)
from btcquant.execution.order_state import FinancialTransitionType, LogicalOrderIdentity
from btcquant.execution.resolution import (
    ResolutionAssessment,
    ResolutionOutcome,
)
from btcquant.execution.state_store import SCHEMA_VERSION, StateStore
from btcquant.execution.state_contract import validate_trend_state


def _state(*, position: dict | None = None, cash: float = 1_000.0) -> dict:
    return {
        "slots": {
            "slot": {
                "cash": cash,
                "position": position,
                "stop_order_id": "stop-1" if position else None,
                "stop_order_local_id": 4 if position else None,
                "stop_intent_id": "stop-intent" if position else None,
                "stop_transition": None,
                "entry_fee": 1.0 if position else 0.0,
                "last_bar_ts": None,
                "financial_transition_seq": 0,
            }
        },
        "peak_equity": 1_000.0,
        "halted": False,
        "day": None,
        "day_start_equity": 1_000.0,
        "daily_lockout": False,
        "reconciliation_required": False,
        "last_funding_ts": None,
        "stop_protection_mode": "SOFTWARE",
    }


def _plan(
    transition: FinancialTransitionType = FinancialTransitionType.ENTER_LONG,
    *,
    position: dict | None = None,
    requested_qty: float = 1.0,
    side: str = "BUY",
    reason: str = "entry",
    reduce_only: bool = False,
) -> FinancialApplicationPlan:
    identity = LogicalOrderIdentity(
        "trend",
        "slot",
        "2026-09-01T11:00:00Z",
        transition,
        ("entry=2026-08-31T12:00:00+00:00|initial_qty=1" if position is not None else None),
        0,
    )
    payload = _state(position=position)
    if position is not None:
        payload["slots"]["slot"]["financial_transition_seq"] = 0
    return FinancialApplicationPlan(
        identity=identity,
        side=side,
        requested_qty=requested_qty,
        reference_price=100.0,
        reason=reason,
        reduce_only=reduce_only,
        planned_effect_at="2026-09-01T12:00:00Z",
        pre_state_payload=payload,
        protection_mode="SOFTWARE",
        entry_direction=1 if transition == FinancialTransitionType.ENTER_LONG else None,
        entry_stop_price=90.0 if transition == FinancialTransitionType.ENTER_LONG else None,
    )


def _persisted(
    store: StateStore, plan: FinancialApplicationPlan
) -> PersistedFinancialApplicationPlan:
    reservation = store.reserve_market_order_with_application_plan(plan.identity, plan=plan)
    persisted = store.get_financial_application_plan(reservation.order_id)
    assert persisted is not None
    return persisted


def _fill(
    persisted: PersistedFinancialApplicationPlan,
    *,
    quantity: float = 0.25,
    price: float = 100.0,
    fee: float | None = -0.01,
    event_at: str | None = "2026-09-01T12:01:00Z",
    venue_fill_id: str | None = "venue-fill-1",
) -> ExternalFill:
    return ExternalFill(
        local_order_id=persisted.local_order_id,
        intent_id=persisted.intent_id,
        venue="hyperliquid",
        account_scope="main",
        instrument="BTC/USDC:USDC",
        side=persisted.plan.side,
        source_kind=ExternalEvidenceSource.FILL_LOOKUP,
        quantity=quantity,
        price=price,
        fee=fee,
        fee_asset="USDC" if fee is not None else None,
        venue_event_at=event_at,
        observed_at="2026-09-01T12:02:00Z",
        raw_payload_hash="a" * 64,
        venue_fill_id=venue_fill_id,
    )


def _assessment(
    persisted: PersistedFinancialApplicationPlan, fill: ExternalFill
) -> ResolutionAssessment:
    assert fill.fill_key is not None
    return ResolutionAssessment(
        outcome=ResolutionOutcome.EFFECT_PROVEN_INCOMPLETE,
        proven_filled_lower_bound=fill.quantity,
        requested_qty=persisted.plan.requested_qty,
        external_order_active=False,
        terminal_state_observed=False,
        terminal_states_observed=(),
        fill_completeness_proven=False,
        zero_effect_proven=False,
        binding_complete=True,
        deduplicated_fill_keys=(fill.fill_key,),
        financially_applicable_fill_keys=(fill.fill_key,),
        financially_ambiguous_fill_keys=(),
    )


def _request(
    persisted: PersistedFinancialApplicationPlan,
    fill: ExternalFill,
    *,
    current_state: dict | None = None,
    previous: tuple[ExternalFill, ...] = (),
) -> FinancialFillApplicationRequest:
    state = current_state or json.loads(canonical_json(persisted.plan.pre_state_payload))
    return FinancialFillApplicationRequest(
        persisted_plan=persisted,
        fill=fill,
        assessment=_assessment(persisted, fill),
        current_state_payload=state,
        current_state_sha256=sha256_json(state),
        previously_applied_fills=previous,
    )


def _insert_ledger_record(
    store: StateStore,
    result,
    *,
    application_index: int = 0,
    previous_application_key: str | None = None,
) -> None:
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """
            INSERT INTO financial_fill_applications(
                application_key, application_version, local_order_id, intent_id,
                plan_key, fill_key, application_index, previous_application_key,
                transition_type, economic_effect_at, state_before_sha256,
                state_after_sha256, result_payload, result_sha256, applied_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result.application_key,
                result.application_version,
                result.local_order_id,
                result.intent_id,
                result.plan_key,
                result.fill_key,
                application_index,
                previous_application_key,
                result.transition_type.value,
                result.economic_effect_at,
                result.state_before_sha256,
                result.state_after_sha256,
                json.dumps(result.as_payload(), sort_keys=True, separators=(",", ":")),
                result.result_sha256,
                "2026-09-01T12:03:00+00:00",
            ),
        )


def test_schema_v10_is_fresh_and_additive(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    assert SCHEMA_VERSION == 11
    with sqlite3.connect(store.path) as connection:
        assert (
            connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[
                0
            ]
            == "11"
        )
        columns = [
            row[1] for row in connection.execute("PRAGMA table_info(financial_fill_applications)")
        ]
        assert columns == [
            "application_key",
            "application_version",
            "local_order_id",
            "intent_id",
            "plan_key",
            "fill_key",
            "application_index",
            "previous_application_key",
            "transition_type",
            "economic_effect_at",
            "state_before_sha256",
            "state_after_sha256",
            "result_payload",
            "result_sha256",
            "applied_at",
        ]
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='idx_financial_fill_applications_previous'"
        ).fetchone()
        index_sql = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='index' AND name='idx_financial_fill_applications_previous'"
        ).fetchone()[0]
        assert "WHERE previous_application_key IS NOT NULL" in index_sql


def test_v9_to_v10_migration_requires_opt_in_and_is_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    StateStore(database)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE financial_fill_applications")
        connection.execute("UPDATE metadata SET value='9' WHERE key='schema_version'")
        connection.commit()
    with pytest.raises(Exception) as error:
        StateStore(database)
    assert error.value.__class__.__name__ == "MigrationRequiredError"
    migrated = StateStore(database, allow_migration=True)
    assert migrated.path == database
    StateStore(database, allow_migration=True)
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[
                0
            ]
            == "11"
        )
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='financial_fill_applications'"
        ).fetchone()


def test_v10_migration_baseexception_rolls_back_table_index_and_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class InjectedPowerLoss(BaseException):
        pass

    database = tmp_path / "state.db"
    StateStore(database)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE financial_fill_applications")
        connection.execute("UPDATE metadata SET value='9' WHERE key='schema_version'")
        connection.commit()
    original = StateStore._ensure_financial_fill_application_schema

    def fail(connection):
        original(connection)
        raise InjectedPowerLoss("injected v10 power loss")

    monkeypatch.setattr(
        StateStore,
        "_ensure_financial_fill_application_schema",
        staticmethod(fail),
    )
    with pytest.raises(InjectedPowerLoss, match="injected v10 power loss"):
        StateStore(database, allow_migration=True)
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[
                0
            ]
            == "9"
        )
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name='financial_fill_applications'"
            ).fetchone()
            is None
        )
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND name='idx_financial_fill_applications_previous'"
            ).fetchone()
            is None
        )


def test_v9_to_v10_preserves_existing_order_plan_and_fill(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    store = StateStore(database)
    persisted = _persisted(store, _plan())
    fill = _fill(persisted)
    stored_fill, created = store.append_external_fill(fill)
    assert created is True
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE financial_fill_applications")
        connection.execute("UPDATE metadata SET value='9' WHERE key='schema_version'")
        connection.commit()

    migrated = StateStore(database, allow_migration=True)
    restored_plan = migrated.get_financial_application_plan(persisted.local_order_id)
    restored_fills = migrated.get_external_fills(persisted.local_order_id)
    assert restored_plan is not None
    assert restored_plan.plan.plan_key == persisted.plan.plan_key
    assert len(restored_fills) == 1
    assert restored_fills[0].fill_key == stored_fill.fill_key
    assert restored_fills[0].venue_fill_id == stored_fill.venue_fill_id
    assert restored_fills[0].fee == stored_fill.fee
    assert migrated.read_financial_fill_application_chain(persisted.local_order_id) == ()
    readonly = StateStore(database, read_only=True)
    assert readonly.read_financial_fill_application_chain(persisted.local_order_id) == ()


def test_ledger_reader_rejects_hashed_result_diverging_from_fill(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    persisted = _persisted(store, _plan())
    fill = _fill(persisted)
    store.append_external_fill(fill)
    result = calculate_financial_fill_application(_request(persisted, fill))
    payload = result.as_payload()
    payload["quantity"] = 0.5
    payload["result_sha256"] = sha256_json(
        {key: value for key, value in payload.items() if key != "result_sha256"}
    )
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """
            INSERT INTO financial_fill_applications(
                application_key, application_version, local_order_id, intent_id,
                plan_key, fill_key, application_index, previous_application_key,
                transition_type, economic_effect_at, state_before_sha256,
                state_after_sha256, result_payload, result_sha256, applied_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result.application_key,
                result.application_version,
                result.local_order_id,
                result.intent_id,
                result.plan_key,
                result.fill_key,
                0,
                None,
                result.transition_type.value,
                result.economic_effect_at,
                result.state_before_sha256,
                result.state_after_sha256,
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                payload["result_sha256"],
                "2026-09-01T12:03:00+00:00",
            ),
        )
    with pytest.raises(FinancialApplicationLedgerConflict):
        store.read_financial_fill_application_chain(persisted.local_order_id)


def test_signed_fee_entry_is_applied_without_sign_loss(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    persisted = _persisted(store, _plan())
    fill = _fill(persisted, quantity=0.5, price=101.0, fee=-0.01)
    result = calculate_financial_fill_application(_request(persisted, fill))
    state = result.state_after_payload
    slot = state["slots"]["slot"]
    assert result.fee == -0.01
    assert result.cash_delta == 0.01
    assert slot["cash"] == 1_000.01
    assert slot["entry_fee"] == -0.01
    assert slot["position"]["qty"] == 0.5
    assert slot["position"]["entry_price"] == 101.0
    assert slot["financial_transition_seq"] == 0
    assert state["peak_equity"] == 1_000.0
    assert state["stop_protection_mode"] == "SOFTWARE"


def test_entry_multiple_fills_use_vwap_and_one_position(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    persisted = _persisted(store, _plan())
    first = _fill(persisted, quantity=0.25, price=100.0, fee=0.01, event_at="2026-09-01T12:01:00Z")
    second = _fill(
        persisted,
        quantity=0.25,
        price=102.0,
        fee=-0.02,
        event_at="2026-09-01T12:02:00Z",
        venue_fill_id="venue-fill-2",
    )
    first_result = calculate_financial_fill_application(_request(persisted, first))
    second_result = calculate_financial_fill_application(
        _request(
            persisted,
            second,
            current_state=json.loads(canonical_json(first_result.state_after_payload)),
            previous=(first,),
        )
    )
    pos = second_result.state_after_payload["slots"]["slot"]["position"]
    assert pos["qty"] == 0.5
    assert pos["entry_price"] == 101.0
    assert second_result.state_after_payload["slots"]["slot"]["entry_fee"] == -0.01
    assert second_result.economic_effect_at == "2026-09-01T12:02:00+00:00"
    assert first_result.state_after_payload["slots"]["slot"]["position"]["pyramid_adds"] == 0


def test_add_recomputes_weighted_price_and_increments_once(tmp_path: Path) -> None:
    position = {
        "entry_time": "2026-08-31T12:00:00+00:00",
        "entry_price": 100.0,
        "qty": 1.0,
        "stop_price": 90.0,
        "direction": 1,
        "bars_held": 3,
        "best_close": 110.0,
        "initial_qty": 1.0,
        "last_add_price": 100.0,
        "pyramid_adds": 2,
    }
    store = StateStore(tmp_path / "state.db")
    persisted = _persisted(
        store,
        _plan(
            FinancialTransitionType.ADD,
            position=position,
            requested_qty=0.5,
            side="BUY",
            reason="pyramid",
        ),
    )
    fill = _fill(persisted, quantity=0.5, price=104.0, fee=-0.03)
    result = calculate_financial_fill_application(_request(persisted, fill))
    pos = result.state_after_payload["slots"]["slot"]["position"]
    assert pos["qty"] == 1.5
    assert pos["entry_price"] == (100.0 + 0.5 * 104.0) / 1.5
    assert pos["pyramid_adds"] == 3
    assert pos["last_add_price"] == 104.0
    assert result.state_after_payload["slots"]["slot"]["cash"] == 1_000.03


@pytest.mark.parametrize(
    ("transition", "position", "side", "requested_qty"),
    [
        (FinancialTransitionType.ENTER_LONG, None, "BUY", 1.0),
        (
            FinancialTransitionType.ADD,
            {
                "entry_time": "2026-08-31T12:00:00+00:00",
                "entry_price": 100.0,
                "qty": 1.0,
                "stop_price": 90.0,
                "direction": 1,
                "bars_held": 3,
                "best_close": 110.0,
                "initial_qty": 1.0,
                "last_add_price": 100.0,
                "pyramid_adds": 0,
            },
            "BUY",
            0.5,
        ),
        (
            FinancialTransitionType.EXIT,
            {
                "entry_time": "2026-08-31T12:00:00+00:00",
                "entry_price": 100.0,
                "qty": 1.0,
                "stop_price": 90.0,
                "direction": 1,
                "bars_held": 3,
                "best_close": 110.0,
                "initial_qty": 1.0,
                "last_add_price": 100.0,
                "pyramid_adds": 0,
            },
            "SELL",
            0.5,
        ),
    ],
)
def test_late_fill_recomposition_is_arrival_order_invariant(
    tmp_path: Path,
    transition: FinancialTransitionType,
    position: dict | None,
    side: str,
    requested_qty: float,
) -> None:
    store = StateStore(tmp_path / "state.db")
    persisted = _persisted(
        store,
        _plan(
            transition,
            position=position,
            requested_qty=requested_qty,
            side=side,
            reason="pyramid" if transition == FinancialTransitionType.ADD else "entry",
            reduce_only=transition == FinancialTransitionType.EXIT,
        ),
    )
    first_discovered = _fill(
        persisted,
        quantity=requested_qty / 2,
        price=110.0 if transition == FinancialTransitionType.EXIT else 100.0,
        fee=0.01,
        event_at="2026-09-01T12:02:00Z",
        venue_fill_id="late-a",
    )
    second_discovered = _fill(
        persisted,
        quantity=requested_qty / 2,
        price=90.0 if transition == FinancialTransitionType.EXIT else 102.0,
        fee=-0.02,
        event_at="2026-09-01T12:01:00Z",
        venue_fill_id="late-b",
    )

    first_a = calculate_financial_fill_application(_request(persisted, first_discovered))
    final_ab = calculate_financial_fill_application(
        _request(
            persisted,
            second_discovered,
            current_state=json.loads(canonical_json(first_a.state_after_payload)),
            previous=(first_discovered,),
        )
    )
    first_b = calculate_financial_fill_application(_request(persisted, second_discovered))
    final_ba = calculate_financial_fill_application(
        _request(
            persisted,
            first_discovered,
            current_state=json.loads(canonical_json(first_b.state_after_payload)),
            previous=(second_discovered,),
        )
    )

    assert final_ab.state_after_sha256 == final_ba.state_after_sha256
    assert final_ab.state_after_payload == final_ba.state_after_payload
    assert final_ab.cash_delta == first_b.cash_delta
    assert final_ba.cash_delta == first_a.cash_delta
    if transition == FinancialTransitionType.EXIT:
        assert final_ab.trade_payload == first_b.trade_payload
        assert final_ba.trade_payload == first_a.trade_payload


def test_exit_applies_signed_fee_and_pro_rata_entry_fee(tmp_path: Path) -> None:
    position = {
        "entry_time": "2026-08-31T12:00:00+00:00",
        "entry_price": 100.0,
        "qty": 1.0,
        "stop_price": 90.0,
        "direction": 1,
        "bars_held": 3,
        "best_close": 110.0,
        "initial_qty": 1.0,
        "last_add_price": 100.0,
        "pyramid_adds": 0,
    }
    store = StateStore(tmp_path / "state.db")
    persisted = _persisted(
        store,
        _plan(
            FinancialTransitionType.EXIT,
            position=position,
            requested_qty=0.5,
            side="SELL",
            reason="exit",
            reduce_only=True,
        ),
    )
    fill = _fill(persisted, quantity=0.5, price=110.0, fee=-0.02)
    result = calculate_financial_fill_application(_request(persisted, fill))
    pos = result.state_after_payload["slots"]["slot"]["position"]
    assert result.cash_delta == 5.02
    assert result.trade_payload is not None
    assert result.trade_payload["pnl"] == 4.52
    assert pos["qty"] == 0.5
    assert result.state_after_payload["slots"]["slot"]["entry_fee"] == 0.5


def test_current_state_is_replayed_and_arbitrary_hash_cannot_bypass(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    persisted = _persisted(store, _plan())
    fill = _fill(persisted)
    state = json.loads(canonical_json(persisted.plan.pre_state_payload))
    with pytest.raises(FinancialFillApplicationError, match="STATE_CONFLICT"):
        calculate_financial_fill_application(
            FinancialFillApplicationRequest(
                persisted_plan=persisted,
                fill=fill,
                assessment=_assessment(persisted, fill),
                current_state_payload={**state, "halted": True},
                current_state_sha256=sha256_json({**state, "halted": True}),
            )
        )
    with pytest.raises(FinancialFillApplicationError, match="STATE_CONFLICT"):
        calculate_financial_fill_application(
            FinancialFillApplicationRequest(
                persisted_plan=persisted,
                fill=fill,
                assessment=_assessment(persisted, fill),
                current_state_payload=state,
                current_state_sha256="0" * 64,
            )
        )


@pytest.mark.parametrize(
    "change",
    [
        lambda fill: ExternalFill(
            **{
                **fill.__dict__,
                "venue_fill_id": None,
            }
        ),
        lambda fill: ExternalFill(
            **{
                **fill.__dict__,
                "fee": None,
                "fee_asset": None,
            }
        ),
    ],
)
def test_irreversible_identity_and_fee_are_required(tmp_path: Path, change) -> None:
    store = StateStore(tmp_path / "state.db")
    persisted = _persisted(store, _plan())
    fill = change(_fill(persisted))
    with pytest.raises(FinancialFillApplicationError):
        calculate_financial_fill_application(_request(persisted, fill))


def test_result_and_persisted_record_are_immutable_and_hashed(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    persisted = _persisted(store, _plan())
    fill = _fill(persisted)
    result = calculate_financial_fill_application(_request(persisted, fill))
    assert result.result_sha256 == sha256_json(result.result_content())
    with pytest.raises(TypeError):
        result.state_after_payload["slots"]["slot"]["cash"] = 1.0
    payload = result.as_payload()
    payload["state_after_payload"]["slots"]["slot"]["cash"] = 999.0
    assert result.state_after_payload["slots"]["slot"]["cash"] != 999.0


@pytest.mark.parametrize("tamper", ["cash", "position_qty", "cash_delta"])
def test_ledger_reader_replays_economics_after_coherent_state_tamper(
    tmp_path: Path, tamper: str
) -> None:
    store = StateStore(tmp_path / "state.db")
    persisted = _persisted(store, _plan())
    fill = _fill(persisted, quantity=0.5)
    store.append_external_fill(fill)
    result = calculate_financial_fill_application(_request(persisted, fill))
    _insert_ledger_record(store, result)

    payload = result.as_payload()
    if tamper == "cash":
        state_after = payload["state_after_payload"]
        state_after["slots"]["slot"]["cash"] += 1.0
        payload["state_after_sha256"] = sha256_json(state_after)
    elif tamper == "position_qty":
        payload["state_after_payload"]["slots"]["slot"]["position"]["qty"] += 0.1
        payload["state_after_sha256"] = sha256_json(payload["state_after_payload"])
    else:
        payload["cash_delta"] += 1.0
    payload["result_sha256"] = sha256_json(
        {key: value for key, value in payload.items() if key != "result_sha256"}
    )
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE financial_fill_applications SET state_after_sha256=?, "
            "result_payload=?, result_sha256=?",
            (
                payload["state_after_sha256"],
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                payload["result_sha256"],
            ),
        )
    with pytest.raises(FinancialApplicationLedgerConflict, match="economic replay"):
        store.read_financial_fill_application_chain(persisted.local_order_id)


def test_ledger_reader_replays_exit_trade_economics(
    tmp_path: Path,
) -> None:
    position = {
        "entry_time": "2026-08-31T12:00:00+00:00",
        "entry_price": 100.0,
        "qty": 1.0,
        "stop_price": 90.0,
        "direction": 1,
        "bars_held": 3,
        "best_close": 110.0,
        "initial_qty": 1.0,
        "last_add_price": 100.0,
        "pyramid_adds": 0,
    }
    store = StateStore(tmp_path / "state.db")
    persisted = _persisted(
        store,
        _plan(
            FinancialTransitionType.EXIT,
            position=position,
            requested_qty=0.5,
            side="SELL",
            reason="exit",
            reduce_only=True,
        ),
    )
    fill = _fill(persisted, quantity=0.5, price=110.0, fee=-0.02)
    store.append_external_fill(fill)
    result = calculate_financial_fill_application(_request(persisted, fill))
    assert result.trade_payload is not None
    _insert_ledger_record(store, result)
    payload = result.as_payload()
    payload["trade_payload"]["pnl"] += 1.0
    payload["result_sha256"] = sha256_json(
        {key: value for key, value in payload.items() if key != "result_sha256"}
    )
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE financial_fill_applications SET result_payload=?, result_sha256=?",
            (
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                payload["result_sha256"],
            ),
        )
    with pytest.raises(FinancialApplicationLedgerConflict, match="economic replay"):
        store.read_financial_fill_application_chain(persisted.local_order_id)


def test_v10_schema_with_columns_but_without_constraints_is_rejected(tmp_path: Path) -> None:
    database = tmp_path / "malformed.db"
    StateStore(database)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE financial_fill_applications")
        connection.execute(
            """
            CREATE TABLE financial_fill_applications(
                application_key TEXT,
                application_version INTEGER,
                local_order_id INTEGER,
                intent_id TEXT,
                plan_key TEXT,
                fill_key TEXT,
                application_index INTEGER,
                previous_application_key TEXT,
                transition_type TEXT,
                economic_effect_at TEXT,
                state_before_sha256 TEXT,
                state_after_sha256 TEXT,
                result_payload TEXT,
                result_sha256 TEXT,
                applied_at TEXT
            )
            """
        )
        connection.commit()
    with pytest.raises(RuntimeError, match="Invalid v10"):
        StateStore(database)


def test_v10_schema_with_wrong_previous_index_predicate_is_rejected(tmp_path: Path) -> None:
    database = tmp_path / "wrong-predicate.db"
    StateStore(database)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP INDEX idx_financial_fill_applications_previous")
        connection.execute(
            "CREATE UNIQUE INDEX idx_financial_fill_applications_previous "
            "ON financial_fill_applications(previous_application_key) "
            "WHERE previous_application_key = 'fake-parent'"
        )
        connection.commit()
    with pytest.raises(RuntimeError, match="previous index predicate"):
        StateStore(database)


def test_ledger_reader_validates_and_returns_immutable_chain(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    persisted = _persisted(store, _plan())
    fill = _fill(persisted)
    store.append_external_fill(fill)
    result = calculate_financial_fill_application(_request(persisted, fill))
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """
            INSERT INTO financial_fill_applications(
                application_key, application_version, local_order_id, intent_id,
                plan_key, fill_key, application_index, previous_application_key,
                transition_type, economic_effect_at, state_before_sha256,
                state_after_sha256, result_payload, result_sha256, applied_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result.application_key,
                result.application_version,
                result.local_order_id,
                result.intent_id,
                result.plan_key,
                result.fill_key,
                0,
                None,
                result.transition_type.value,
                result.economic_effect_at,
                result.state_before_sha256,
                result.state_after_sha256,
                json.dumps(result.as_payload(), sort_keys=True, separators=(",", ":")),
                result.result_sha256,
                "2026-09-01T12:03:00+00:00",
            ),
        )
    chain = store.read_financial_fill_application_chain(persisted.local_order_id)
    assert len(chain) == 1
    assert chain[0].application_key == result.application_key
    assert chain[0].result == result
    with pytest.raises(FinancialApplicationLedgerConflict):
        with sqlite3.connect(store.path) as connection:
            connection.execute(
                "UPDATE financial_fill_applications SET result_sha256=?",
                ("0" * 64,),
            )
        store.read_financial_fill_application_chain(persisted.local_order_id)


def test_ledger_has_no_public_financial_insert_writer() -> None:
    assert not hasattr(StateStore, "apply_financial_fill")
    assert not hasattr(StateStore, "record_financial_application")


def test_schema_validation_accepts_post_state() -> None:
    validate_trend_state(_state())
    assert FINANCIAL_FILL_APPLICATION_VERSION == 1
