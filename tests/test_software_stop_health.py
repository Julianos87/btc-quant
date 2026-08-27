"""SOFTWARE vs EXCHANGE stop-protection health contract."""

from __future__ import annotations

import threading
from pathlib import Path

import pandas as pd
import pytest

from btcquant.execution.broker import Broker, PaperBroker
from btcquant.execution.health import (
    evaluate_open_slot_protection,
    execution_health,
    execution_safety_health,
    software_stop_contract_valid,
    sync_execution_incidents,
)
from btcquant.execution.runner import LiveRunner, StrategySlot
from btcquant.execution.state_contract import (
    EXCHANGE_STOP_CONFIRMED,
    EXCHANGE_STOP_MISSING,
    EXCHANGE_STOP_REPLACEMENT_ACTIVE,
    PROTECTION_MODE_UNKNOWN,
    SOFTWARE_STOP_ACTIVE,
    SOFTWARE_STOP_INCONSISTENT_TRANSITION,
    SOFTWARE_STOP_INVALID,
    STOP_PROTECTION_EXCHANGE,
    STOP_PROTECTION_SOFTWARE,
    stop_protection_mode_from_broker,
    validate_trend_state,
)
from btcquant.execution.state_store import SCHEMA_VERSION, StateStore
from btcquant.observability import SafetyStatus
from btcquant.risk import RiskConfig
from btcquant.strategies.base import Position, Strategy


class _StubVenue:
    payments_per_day = 24
    exchange_id = "hyperliquid"

    def funding_history_since(self, since):
        del since
        return pd.Series(dtype=float)

    def last_price(self) -> float:
        return 72_000.0

    def fetch_ohlcv(self, *args, **kwargs):
        del args, kwargs
        return []

    def funding_rate_8h(self) -> float:
        return 0.0


class _FakeClock:
    def __init__(self, ts: float) -> None:
        self._ts = ts

    def utc_now(self) -> pd.Timestamp:
        return pd.Timestamp(self._ts, unit="s", tz="UTC")

    def time(self) -> float:
        return self._ts

    def monotonic(self) -> float:
        return self._ts


class _KernelStrategy(Strategy):
    name = "trend_ls_20"
    timeframe = "4h"

    @staticmethod
    def default_params() -> dict:
        return {}

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        return df.copy()

    def entry_signal(self, row: pd.Series) -> int:
        del row
        return 0

    def initial_stop(self, row: pd.Series, entry_price: float, direction: int = 1) -> float:
        del row
        return entry_price - direction * 1_000.0

    def trailing_stop(self, row: pd.Series, position: Position) -> float:
        del row
        return position.stop_price

    def exit_signal(self, row: pd.Series, position: Position) -> bool:
        del row, position
        return False

    def warmup_bars(self) -> int:
        return 1


class _ExchangeBroker(Broker):
    supports_stop_orders = True
    external_execution = True

    def market_buy(self, qty: float, ref_price: float):
        raise AssertionError("no external orders in health tests")

    def market_sell(self, qty: float, ref_price: float):
        raise AssertionError("no external orders in health tests")


def _open_position(**overrides: object) -> dict:
    payload = {
        "entry_time": "2026-08-20 12:00:23.176115+00:00",
        "entry_price": 72096.75876036278,
        "qty": 0.032482014269786356,
        "stop_price": 71055.93232831602,
        "direction": 1,
        "bars_held": 4,
        "best_close": 74516.0,
        "initial_qty": 0.027100868637916053,
        "last_add_price": 72660.32088085434,
        "pyramid_adds": 1,
    }
    payload.update(overrides)
    return payload


def _slot(position: dict | None = None, **overrides: object) -> dict:
    payload: dict[str, object] = {
        "cash": 1948.15518412185,
        "position": position,
        "stop_order_id": None,
        "stop_order_local_id": None,
        "stop_intent_id": None,
        "stop_transition": None,
        "entry_fee": 1.25,
        "last_bar_ts": "2026-08-21 00:00:00+00:00",
        "financial_transition_seq": 2,
    }
    payload.update(overrides)
    return payload


def _trend_state(*slots: tuple[str, dict], **engine: object) -> dict:
    payload: dict[str, object] = {
        "slots": {name: slot for name, slot in slots},
        "peak_equity": 2_000.0,
        "halted": False,
        "day": "2026-08-21",
        "day_start_equity": 1_948.15518412185,
        "daily_lockout": False,
        "reconciliation_required": False,
        "last_funding_ts": None,
    }
    payload.update(engine)
    return payload


def _legacy_unprotected(slot: dict) -> bool:
    """Pre-fix algorithm: any OPEN slot without exchange stop_order_id."""

    if slot.get("position") is None:
        return False
    transition = slot.get("stop_transition")
    previous_stop = transition.get("previous_stop_id") if isinstance(transition, dict) else None
    return slot.get("stop_order_id") is None and previous_stop is None


def test_legacy_paper_false_positive_is_reproduced_by_old_algorithm() -> None:
    slot = _slot(_open_position())
    assert PaperBroker.supports_stop_orders is False
    assert slot["stop_order_id"] is None
    assert slot["stop_transition"] is None
    assert software_stop_contract_valid(slot["position"]) is True
    assert _legacy_unprotected(slot) is True


def test_unknown_mode_open_paper_position_is_fail_closed(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "btcquant.db")
    store.save_engine_state("trend", _trend_state(("trend_ls_20", _slot(_open_position()))))
    health = execution_health(store, "trend")
    notifications = sync_execution_incidents(store, health)
    safety = execution_safety_health(store, engines=("trend",))
    assert health.unprotected_slots == ("trend_ls_20",)
    assert health.slot_protection == (("trend_ls_20", PROTECTION_MODE_UNKNOWN),)
    assert health.protection_mode is None
    assert safety.status == SafetyStatus.FAIL
    assert "TREND_UNPROTECTED_POSITION" in safety.reasons
    assert any(
        item["fingerprint"] == "execution:trend:unprotected_position"
        and item["severity"] == "CRITICAL"
        for item in notifications
    )


def test_software_valid_stop_is_protected(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "btcquant.db")
    store.save_engine_state(
        "trend",
        _trend_state(
            ("trend_ls_20", _slot(_open_position())),
            stop_protection_mode=STOP_PROTECTION_SOFTWARE,
        ),
    )
    health = execution_health(store, "trend")
    notifications = sync_execution_incidents(store, health)
    assert health.unprotected_slots == ()
    assert health.slot_protection == (("trend_ls_20", SOFTWARE_STOP_ACTIVE),)
    assert execution_safety_health(store, engines=("trend",)).status == SafetyStatus.PASS
    assert notifications == []
    assert store.read_incidents(open_only=True) == []


@pytest.mark.parametrize(
    "position_overrides",
    [
        {"stop_price": None},
        {"stop_price": 0.0},
        {"stop_price": -1.0},
        {"qty": 0.0},
    ],
)
def test_software_invalid_stop_is_unprotected(
    tmp_path: Path, position_overrides: dict[str, object]
) -> None:
    position = _open_position(**position_overrides)
    store = StateStore(tmp_path / "btcquant.db")
    store.save_engine_state(
        "trend",
        _trend_state(
            ("trend_ls_20", _slot(position)),
            stop_protection_mode=STOP_PROTECTION_SOFTWARE,
        ),
    )
    health = execution_health(store, "trend")
    assert health.unprotected_slots == ("trend_ls_20",)
    assert health.slot_protection[0][1] == SOFTWARE_STOP_INVALID


@pytest.mark.parametrize("stop_price", [float("nan"), float("inf"), float("-inf")])
def test_software_non_finite_stop_is_unprotected(stop_price: float) -> None:
    reason, protected, _pending = evaluate_open_slot_protection(
        _slot(_open_position(stop_price=stop_price)),
        protection_mode=STOP_PROTECTION_SOFTWARE,
        reconciliation_required=False,
    )
    assert protected is False
    assert reason == SOFTWARE_STOP_INVALID


def test_software_invalid_direction_is_unprotected() -> None:
    reason, protected, _pending = evaluate_open_slot_protection(
        _slot(_open_position(direction=0)),
        protection_mode=STOP_PROTECTION_SOFTWARE,
        reconciliation_required=False,
    )
    assert protected is False
    assert reason == SOFTWARE_STOP_INVALID


def test_software_missing_stop_price_is_unprotected(tmp_path: Path) -> None:
    position = _open_position()
    del position["stop_price"]
    store = StateStore(tmp_path / "btcquant.db")
    store.save_engine_state(
        "trend",
        _trend_state(
            ("trend_ls_20", _slot(position)),
            stop_protection_mode=STOP_PROTECTION_SOFTWARE,
        ),
    )
    health = execution_health(store, "trend")
    assert health.unprotected_slots == ("trend_ls_20",)
    assert health.slot_protection[0][1] == SOFTWARE_STOP_INVALID


def test_software_unexpected_exchange_transition_is_fail_closed(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "btcquant.db")
    store.save_engine_state(
        "trend",
        _trend_state(
            (
                "trend_ls_20",
                _slot(
                    _open_position(),
                    stop_transition={"phase": "PLACING", "previous_stop_id": "x"},
                ),
            ),
            stop_protection_mode=STOP_PROTECTION_SOFTWARE,
        ),
    )
    health = execution_health(store, "trend")
    assert health.unprotected_slots == ("trend_ls_20",)
    assert health.stop_transition_slots == ("trend_ls_20",)
    assert health.slot_protection[0][1] == SOFTWARE_STOP_INCONSISTENT_TRANSITION


def test_exchange_numeric_stop_without_order_is_unprotected(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "btcquant.db")
    store.save_engine_state(
        "trend",
        _trend_state(
            ("trend_ls_20", _slot(_open_position())),
            stop_protection_mode=STOP_PROTECTION_EXCHANGE,
        ),
    )
    health = execution_health(store, "trend")
    notifications = sync_execution_incidents(store, health)
    assert health.unprotected_slots == ("trend_ls_20",)
    assert health.slot_protection[0][1] == EXCHANGE_STOP_MISSING
    assert any(item["kind"] == "unprotected_position" for item in notifications)
    assert any(
        item["severity"] == "CRITICAL" and item["kind"] == "unprotected_position"
        for item in store.read_incidents(open_only=True)
    )


def test_exchange_confirmed_stop_is_protected(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "btcquant.db")
    store.save_engine_state(
        "trend",
        _trend_state(
            ("trend_ls_20", _slot(_open_position(), stop_order_id="exch-stop-1")),
            stop_protection_mode=STOP_PROTECTION_EXCHANGE,
        ),
    )
    health = execution_health(store, "trend")
    assert health.unprotected_slots == ()
    assert health.slot_protection[0][1] == EXCHANGE_STOP_CONFIRMED


def test_exchange_replacement_previous_stop_id_preserves_protection(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "btcquant.db")
    store.save_engine_state(
        "trend",
        _trend_state(
            (
                "trend_ls_20",
                _slot(
                    _open_position(),
                    stop_order_id=None,
                    stop_transition={
                        "phase": "PLACING",
                        "previous_stop_id": "old-stop",
                    },
                ),
            ),
            stop_protection_mode=STOP_PROTECTION_EXCHANGE,
        ),
    )
    health = execution_health(store, "trend")
    assert health.unprotected_slots == ()
    assert health.stop_transition_slots == ("trend_ls_20",)
    assert health.slot_protection[0][1] == EXCHANGE_STOP_REPLACEMENT_ACTIVE


def test_flat_legacy_missing_mode_is_not_unprotected(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "btcquant.db")
    store.save_engine_state(
        "trend",
        _trend_state(("trend_ls_20", _slot(None))),
    )
    health = execution_health(store, "trend")
    assert health.unprotected_slots == ()
    assert health.slot_protection == ()
    assert execution_safety_health(store, engines=("trend",)).status == SafetyStatus.PASS


def test_paper_broker_payload_is_software(tmp_path: Path) -> None:
    runner = LiveRunner(
        [StrategySlot(_KernelStrategy(), 1.0, 2_000.0)],
        PaperBroker(),
        RiskConfig(initial_capital=2_000.0),
        "hyperliquid",
        "BTC/USDC:USDC",
        tmp_path / "btcquant.db",
        venue=_StubVenue(),
        notifier=lambda *_args, **_kwargs: None,
        clock=_FakeClock(1_777_000_000.0),
    )
    payload = runner._state_payload()
    assert payload["stop_protection_mode"] == STOP_PROTECTION_SOFTWARE
    assert stop_protection_mode_from_broker(supports_stop_orders=False) == STOP_PROTECTION_SOFTWARE


def test_exchange_broker_payload_is_exchange(tmp_path: Path) -> None:
    runner = LiveRunner(
        [StrategySlot(_KernelStrategy(), 1.0, 2_000.0)],
        _ExchangeBroker(),
        RiskConfig(initial_capital=2_000.0),
        "hyperliquid",
        "BTC/USDC:USDC",
        tmp_path / "btcquant.db",
        venue=_StubVenue(),
        notifier=lambda *_args, **_kwargs: None,
        clock=_FakeClock(1_777_000_000.0),
    )
    payload = runner._state_payload()
    assert payload["stop_protection_mode"] == STOP_PROTECTION_EXCHANGE
    assert stop_protection_mode_from_broker(supports_stop_orders=True) == STOP_PROTECTION_EXCHANGE


def test_legacy_checkpoint_without_protection_mode_still_validates() -> None:
    payload = _trend_state(("trend_ls_20", _slot(_open_position())))
    loaded = validate_trend_state(payload)
    assert "stop_protection_mode" not in loaded


def test_schema_version_is_unchanged() -> None:
    assert SCHEMA_VERSION == 7


def _seed_open_runner(tmp_path: Path, *, last_bar: str | None = "2026-08-21 00:00:00+00:00"):
    store = StateStore(tmp_path / "btcquant.db")
    position = _open_position()
    store.save_engine_state(
        "trend",
        _trend_state(("trend_ls_20", _slot(position, last_bar_ts=last_bar))),
    )
    store.record_incident(
        "execution:trend:unprotected_position",
        engine="trend",
        severity="CRITICAL",
        kind="unprotected_position",
        message="3 position(s) sans stop confirmé",
        context={"slots": ["trend_ls_20"]},
    )
    terminal = store.begin_order(
        "trend",
        "trend_ls_20",
        "historical-entry",
        "MARKET",
        "BUY",
        0.0271,
        "entry",
        reference_price=72096.75,
    )
    store.complete_order(terminal, status="FILLED", filled_qty=0.0271, price=72096.75)
    runner = LiveRunner(
        [StrategySlot(_KernelStrategy(), 1.0, 1948.15518412185)],
        PaperBroker(),
        RiskConfig(initial_capital=1948.15518412185),
        "hyperliquid",
        "BTC/USDC:USDC",
        tmp_path / "btcquant.db",
        venue=_StubVenue(),
        notifier=lambda *_args, **_kwargs: None,
        clock=_FakeClock(1_777_334_400.0 + 30.0),
    )
    runner._apply_funding_payments = lambda _price: None  # type: ignore[method-assign]
    return runner, position, store


def test_migrated_open_position_restores_exactly(tmp_path: Path) -> None:
    runner, source, store = _seed_open_runner(tmp_path)
    slot = runner.slots[0]
    assert slot.position is not None
    assert slot.position.entry_time == pd.Timestamp(source["entry_time"])
    assert slot.position.entry_price == source["entry_price"]
    assert slot.position.qty == source["qty"]
    assert slot.position.stop_price == source["stop_price"]
    assert int(slot.position.direction) == source["direction"]
    assert slot.position.bars_held == source["bars_held"]
    assert slot.position.best_close == source["best_close"]
    assert slot.position.initial_qty == source["initial_qty"]
    assert slot.position.last_add_price == source["last_add_price"]
    assert slot.position.pyramid_adds == source["pyramid_adds"]
    assert slot.cash == 1948.15518412185
    assert slot.entry_fee == 1.25
    assert str(slot.last_bar_ts) == "2026-08-21 00:00:00+00:00"
    assert store.unresolved_orders("trend") == []


def test_first_cycle_above_stop_preserves_position_and_persists_software(
    tmp_path: Path,
) -> None:
    runner, source, store = _seed_open_runner(tmp_path)
    order: list[str] = []
    original_soft = runner._check_soft_stops
    original_bars = runner._process_due_bars

    def soft(price: float) -> None:
        order.append("soft")
        original_soft(price)

    def bars(price: float) -> None:
        order.append("bars")
        original_bars(price)

    runner._check_soft_stops = soft  # type: ignore[method-assign]
    runner._process_due_bars = bars  # type: ignore[method-assign]
    before_orders = len(store.read_orders("trend"))
    price = source["entry_price"] + 500.0
    runner._run_cycle(price, threading.Event())
    slot = runner.slots[0]
    assert order == ["soft", "bars"]
    assert slot.position is not None
    assert slot.position.qty == source["qty"]
    assert slot.position.stop_price == source["stop_price"]
    persisted = store.load_engine_state("trend") or {}
    assert persisted["stop_protection_mode"] == STOP_PROTECTION_SOFTWARE
    health = execution_health(store, "trend")
    sync_execution_incidents(store, health)
    assert health.unprotected_slots == ()
    assert health.slot_protection[0][1] == SOFTWARE_STOP_ACTIVE
    assert len(store.read_orders("trend")) == before_orders
    open_unprotected = [
        item
        for item in store.read_incidents(open_only=True)
        if item["fingerprint"] == "execution:trend:unprotected_position"
    ]
    assert open_unprotected == []


def test_first_cycle_stop_hit_exits_before_due_bar(tmp_path: Path) -> None:
    runner, source, store = _seed_open_runner(tmp_path, last_bar="2026-01-01 00:00:00+00:00")
    bar_saw_open: list[bool] = []
    original_soft = runner._check_soft_stops
    original_bars = runner._process_due_bars
    original_process_bar = runner._process_bar
    order: list[str] = []

    def soft(price: float) -> None:
        order.append("soft")
        original_soft(price)

    def bars(price: float) -> None:
        order.append("bars")
        original_bars(price)

    def process_bar(slot, price):
        bar_saw_open.append(slot.position is not None)
        return original_process_bar(slot, price)

    runner._check_soft_stops = soft  # type: ignore[method-assign]
    runner._process_due_bars = bars  # type: ignore[method-assign]
    runner._process_bar = process_bar  # type: ignore[method-assign]
    runner._fetch_frame = lambda _strategy: pd.DataFrame()  # type: ignore[method-assign]
    stop_price = float(source["stop_price"])
    runner._run_cycle(stop_price, threading.Event())
    assert order[0] == "soft"
    assert "bars" in order
    assert runner.slots[0].position is None
    assert all(open_during is False for open_during in bar_saw_open)
    trades = store.read_trades() if hasattr(store, "read_trades") else []
    reasons = [item.get("reason") for item in trades] if trades else []
    if reasons:
        assert "stop" in reasons


def test_historical_terminal_orders_are_not_replayed(tmp_path: Path) -> None:
    runner, _source, store = _seed_open_runner(tmp_path)
    before = store.read_orders("trend")
    assert store.unresolved_orders("trend") == []
    runner._apply_funding_payments = lambda _price: None  # type: ignore[method-assign]
    runner._run_cycle(80_000.0, threading.Event())
    after = store.read_orders("trend")
    assert [item["intent_id"] for item in before] == [item["intent_id"] for item in after]


def test_false_positive_incident_resolves_after_software_checkpoint(tmp_path: Path) -> None:
    runner, source, store = _seed_open_runner(tmp_path)
    before = next(
        item
        for item in store.read_incidents(open_only=True)
        if item["fingerprint"] == "execution:trend:unprotected_position"
    )
    assert before["status"] != "RESOLVED"
    runner._run_cycle(float(source["entry_price"]) + 100.0, threading.Event())
    health = execution_health(store, "trend")
    sync_execution_incidents(store, health)
    remaining = [
        item
        for item in store.read_incidents(open_only=True)
        if item["fingerprint"] == "execution:trend:unprotected_position"
    ]
    assert remaining == []
    resolved = [
        item
        for item in store.read_incidents()
        if item["fingerprint"] == "execution:trend:unprotected_position"
    ]
    assert resolved and resolved[0]["status"] == "RESOLVED"


def test_invalid_software_stop_keeps_incident_open(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "btcquant.db")
    store.save_engine_state(
        "trend",
        _trend_state(
            ("trend_ls_20", _slot(_open_position(stop_price=0.0))),
            stop_protection_mode=STOP_PROTECTION_SOFTWARE,
        ),
    )
    store.record_incident(
        "execution:trend:unprotected_position",
        engine="trend",
        severity="CRITICAL",
        kind="unprotected_position",
        message="1 position(s) sans stop confirmé",
        context={"slots": ["trend_ls_20"]},
    )
    health = execution_health(store, "trend")
    sync_execution_incidents(store, health)
    open_items = [
        item
        for item in store.read_incidents(open_only=True)
        if item["fingerprint"] == "execution:trend:unprotected_position"
    ]
    assert open_items
    assert health.unprotected_slots == ("trend_ls_20",)
