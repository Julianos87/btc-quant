"""Cutover explicite Carry synthétique legacy → v6 FLAT."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from btcquant.backup import write_recovery_marker
from btcquant.carry import PAPER_CARRY_POLICY
from btcquant.entrypoints.carry_cutover import main as cutover_main
from btcquant.execution.carry_cutover import (
    CUTOVER_APPLIED,
    CUTOVER_EVENT_TYPE,
    NO_OP_ALREADY_CUT_OVER,
    CutoverRefused,
    apply_legacy_synthetic_carry_cutover,
    canonical_carry_state_sha256,
    diagnose_legacy_synthetic_pattern,
)
from btcquant.execution.carry_runner import CarryRunner
from btcquant.execution.instance_lock import EngineInstanceLock
from btcquant.execution.state_store import SCHEMA_VERSION, StateStore

ROOT = Path(__file__).resolve().parents[1]
PAPER_CONFIG = ROOT / "environments" / "paper" / "config.yaml"
TESTNET_CONFIG = ROOT / "environments" / "testnet" / "config.yaml"
GIT_SHA = "15fd6a3149f72a1add692869d34e53d4abf10b5f"
EXACT_EQUITY = 3995.5154037536913
SOURCE_IDENTITY = "github.com/example/btcquant.git"


def _legacy_payload(**overrides: object) -> dict:
    payload: dict = {
        "equity": EXACT_EQUITY,
        "in_position": True,
        "execution_state": "OPEN",
        "qty": 0.0,
        "spot_qty": 0.0,
        "perp_qty": 0.0,
        "last_funding_ts": "2026-08-18 19:00:00.048000+00:00",
        "peak_equity": 4000.5206531069093,
        "day": "2026-08-18",
        "day_start_equity": 3997.9162744563228,
        "halted": False,
        "daily_lockout": False,
    }
    payload.update(overrides)
    return payload


def _seed(tmp_path: Path, payload: dict | None = None) -> Path:
    database = tmp_path / "btcquant.db"
    store = StateStore(database)
    store.save_engine_state(
        "trend",
        {
            "slots": {
                "trend_ls_20": {
                    "cash": 1949.8689579887307,
                    "position": None,
                    "stop_order_id": None,
                    "entry_fee": 0.0,
                    "last_bar_ts": "2026-08-18 12:00:00+00:00",
                }
            },
            "peak_equity": 6155.554453935577,
            "halted": False,
            "day": "2026-08-18",
            "day_start_equity": 5774.22371126673,
            "daily_lockout": False,
            "reconciliation_required": False,
            "last_funding_ts": "2026-08-18T19:00:00.048000+00:00",
        },
    )
    store.save_engine_state("carry", payload or _legacy_payload())
    return database


def _sha(database: Path) -> str:
    payload = StateStore(database, read_only=True).load_engine_state("carry")
    assert payload is not None
    return canonical_carry_state_sha256(payload)


def _apply(database: Path, digest: str | None = None) -> object:
    return apply_legacy_synthetic_carry_cutover(
        database,
        expected_state_sha256=digest or _sha(database),
        git_sha=GIT_SHA,
        operator="pytest",
    )


def _counts(database: Path) -> dict[str, int]:
    connection = sqlite3.connect(database)
    tables = [
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    ]
    counts = {
        table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables
    }
    connection.close()
    return counts


def test_canonical_hash_is_deterministic() -> None:
    payload = _legacy_payload()
    first = canonical_carry_state_sha256(payload)
    second = canonical_carry_state_sha256(dict(reversed(list(payload.items()))))
    assert first == second
    assert len(first) == 64
    assert diagnose_legacy_synthetic_pattern(payload) == []


def test_exact_legacy_pattern_cuts_over_to_flat(tmp_path: Path) -> None:
    database = _seed(tmp_path)
    before = StateStore(database, read_only=True).load_engine_state("carry")
    assert before is not None
    result = _apply(database)
    assert result.status == CUTOVER_APPLIED
    after = StateStore(database, read_only=True).load_engine_state("carry")
    assert after is not None
    assert after["in_position"] is False
    assert after["execution_state"] == "FLAT"
    assert after["qty"] == after["spot_qty"] == after["perp_qty"] == 0.0
    connection = sqlite3.connect(database)
    position = connection.execute(
        "SELECT status, qty, cash FROM positions WHERE engine='carry'"
    ).fetchone()
    connection.close()
    assert position is not None
    assert position[0] == "FLAT"
    assert position[1] == 0.0


def test_equity_and_risk_baselines_are_preserved_exactly(tmp_path: Path) -> None:
    database = _seed(tmp_path)
    result = _apply(database)
    after = StateStore(database, read_only=True).load_engine_state("carry")
    assert after is not None
    assert after["equity"] == EXACT_EQUITY
    assert result.equity == EXACT_EQUITY
    assert after["peak_equity"] == 4000.5206531069093
    assert after["day"] == "2026-08-18"
    assert after["day_start_equity"] == 3997.9162744563228
    assert after["halted"] is False
    assert after["daily_lockout"] is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("qty", 0.01),
        ("perp_qty", 0.02),
        ("spot_qty", 0.03),
        ("entry_price", 100_000.0),
        ("perp_notional", 12.0),
        ("execution_state", "OPENING"),
        ("execution_state", "CLOSING"),
        ("execution_state", "UNBALANCED"),
    ],
)
def test_unexpected_economic_evidence_is_refused(tmp_path: Path, field: str, value: object) -> None:
    database = _seed(tmp_path, _legacy_payload(**{field: value}))
    with pytest.raises(CutoverRefused, match="CUTOVER_BLOCKED"):
        _apply(database)
    still = StateStore(database, read_only=True).load_engine_state("carry")
    assert still is not None
    assert still["in_position"] is True
    assert still["execution_state"] == value if field == "execution_state" else "OPEN"


def test_funding_ledger_nonempty_is_refused(tmp_path: Path) -> None:
    database = _seed(tmp_path)
    store = StateStore(database)
    payload = store.load_engine_state("carry")
    assert payload is not None
    store.apply_carry_accounting_event_and_checkpoint(
        {
            "event_key": "test-funding-1",
            "venue": "hyperliquid",
            "instrument": "BTC/USDC:USDC",
            "funding_timestamp": "2026-08-18T19:00:00+00:00",
            "native_funding_rate": 0.0001,
            "position_generation": "FLAT",
            "funding_notional": 0.0,
            "funding_notional_price": None,
            "funding_notional_price_source": None,
            "funding_notional_price_timestamp": None,
            "funding_pnl": 0.0,
            "borrow_principal": 0.0,
            "borrow_rate_ann": 0.05,
            "borrow_dt_seconds": 3600.0,
            "borrow_cost": 0.0,
            "applied_at": "2026-08-18T19:01:00+00:00",
        },
        payload,
    )
    with pytest.raises(CutoverRefused, match="funding_ledger"):
        _apply(database)


def test_carry_order_is_refused(tmp_path: Path) -> None:
    database = _seed(tmp_path)
    store = StateStore(database)
    store.begin_order("carry", "carry", "intent-1", "MARKET", "OPEN", 0.0, "test")
    with pytest.raises(CutoverRefused, match="ordre"):
        _apply(database)


def test_unresolved_carry_order_is_refused(tmp_path: Path) -> None:
    database = _seed(tmp_path)
    store = StateStore(database)
    store.begin_order("carry", "carry", "intent-pending", "MARKET", "OPEN", 1.0, "pending")
    with pytest.raises(CutoverRefused, match="ordre"):
        _apply(database)


def test_wrong_expected_hash_is_refused(tmp_path: Path) -> None:
    database = _seed(tmp_path)
    with pytest.raises(CutoverRefused, match="hash d'état différent"):
        apply_legacy_synthetic_carry_cutover(
            database,
            expected_state_sha256="0" * 64,
            git_sha=GIT_SHA,
            operator="pytest",
        )


def test_schema_4_is_refused_without_auto_migration(tmp_path: Path) -> None:
    database = _seed(tmp_path)
    connection = sqlite3.connect(database)
    connection.execute("UPDATE metadata SET value = '4' WHERE key = 'schema_version'")
    connection.commit()
    connection.close()
    with pytest.raises(CutoverRefused, match="schéma 4"):
        _apply(database)
    meta = dict(sqlite3.connect(database).execute("SELECT key, value FROM metadata"))
    assert meta["schema_version"] == "4"


def test_live_or_testnet_config_is_refused(tmp_path: Path) -> None:
    database = _seed(tmp_path)
    digest = _sha(database)
    assert (
        cutover_main(
            [
                "--database",
                str(database),
                "--config",
                str(TESTNET_CONFIG),
                "--expected-state-sha256",
                digest,
                "--git-sha",
                GIT_SHA,
                "--confirm-legacy-synthetic-cutover",
            ]
        )
        == 2
    )
    payload = StateStore(database, read_only=True).load_engine_state("carry")
    assert payload is not None
    assert payload["in_position"] is True


def test_successful_cutover_creates_no_trade_order_flow_or_ledger(tmp_path: Path) -> None:
    database = _seed(tmp_path)
    before = _counts(database)
    _apply(database)
    after = _counts(database)
    assert after["trades"] == before["trades"] == 0
    assert after["orders"] == before["orders"] == 0
    assert after.get("flows", 0) == before.get("flows", 0)
    assert after["funding_ledger"] == before["funding_ledger"] == 0
    assert after["events"] == before["events"] + 1
    events = StateStore(database, read_only=True).read_events("carry")
    cutovers = [event for event in events if event["event_type"] == CUTOVER_EVENT_TYPE]
    assert len(cutovers) == 1
    payload = json.loads(cutovers[0]["payload"])
    assert payload["reason"] == "LEGACY_SYNTHETIC_OPEN_QTY0"
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["equity_before"] == EXACT_EQUITY
    assert payload["equity_after"] == EXACT_EQUITY
    assert payload["git_sha"] == GIT_SHA
    assert "token" not in json.dumps(payload)


def test_second_invocation_is_idempotent_no_op(tmp_path: Path) -> None:
    database = _seed(tmp_path)
    first = _apply(database)
    assert first.status == CUTOVER_APPLIED
    second = _apply(database)
    assert second.status == NO_OP_ALREADY_CUT_OVER
    events = [
        event
        for event in StateStore(database, read_only=True).read_events("carry")
        if event["event_type"] == CUTOVER_EVENT_TYPE
    ]
    assert len(events) == 1


def test_random_flat_is_not_reported_as_already_cut_over(tmp_path: Path) -> None:
    database = _seed(
        tmp_path,
        {
            "equity": EXACT_EQUITY,
            "in_position": False,
            "execution_state": "FLAT",
            "qty": 0.0,
            "spot_qty": 0.0,
            "perp_qty": 0.0,
            "last_funding_ts": "2026-08-18T19:00:00+00:00",
            "peak_equity": EXACT_EQUITY,
            "day": "2026-08-18",
            "day_start_equity": EXACT_EQUITY,
            "halted": False,
            "daily_lockout": False,
            "accounting_uncertain": False,
            "accounting_uncertainty_reason": None,
            "entry_equity": None,
            "entry_timestamp": None,
            "entry_price": None,
            "spot_notional": 0.0,
            "perp_notional": 0.0,
            "borrow_principal": 0.0,
            "position_generation": None,
            "funding_notional_price": None,
        },
    )
    with pytest.raises(CutoverRefused, match="CUTOVER_BLOCKED"):
        _apply(database)


def test_injected_mid_transaction_failure_rolls_back(tmp_path: Path, monkeypatch) -> None:
    database = _seed(tmp_path)
    original = StateStore(database, read_only=True).load_engine_state("carry")
    events_before = len(StateStore(database, read_only=True).read_events("carry"))
    store = StateStore(database)

    def fail_event(*_args, **_kwargs):
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(store, "_insert_event", fail_event)
    from btcquant.execution import carry_cutover as module

    with pytest.raises(RuntimeError, match="simulated crash"):
        module._apply_in_transaction(
            store,
            expected_state_sha256=_sha(database),
            git_sha=GIT_SHA,
            operator="pytest",
        )
    after = StateStore(database, read_only=True).load_engine_state("carry")
    assert after == original
    assert len(StateStore(database, read_only=True).read_events("carry")) == events_before


def test_open_critical_carry_incident_is_refused(tmp_path: Path) -> None:
    database = _seed(tmp_path)
    StateStore(database).record_incident(
        "execution:carry:unrelated",
        engine="carry",
        severity="CRITICAL",
        kind="loop_failure",
        message="incident unrelated au motif synthétique",
    )
    with pytest.raises(CutoverRefused, match="incident CRITICAL"):
        _apply(database)


def test_recovery_marker_is_refused(tmp_path: Path) -> None:
    database = _seed(tmp_path)
    write_recovery_marker(
        tmp_path,
        backup_id="backup-1",
        restored_app_schema_version=6,
        source_identity=SOURCE_IDENTITY,
    )
    with pytest.raises(CutoverRefused, match="recovery"):
        _apply(database)


def test_active_instance_lock_is_refused(tmp_path: Path) -> None:
    database = _seed(tmp_path)
    with EngineInstanceLock(database, "carry"):
        with pytest.raises(CutoverRefused, match="writer carry actif"):
            _apply(database)


def test_cli_requires_confirmation(tmp_path: Path) -> None:
    database = _seed(tmp_path)
    assert (
        cutover_main(
            [
                "--database",
                str(database),
                "--config",
                str(PAPER_CONFIG),
                "--expected-state-sha256",
                _sha(database),
                "--git-sha",
                GIT_SHA,
            ]
        )
        == 3
    )


def test_cli_print_hash_and_apply(tmp_path: Path, capsys) -> None:
    database = _seed(tmp_path)
    assert (
        cutover_main(
            [
                "--database",
                str(database),
                "--config",
                str(PAPER_CONFIG),
                "--print-expected-state-sha256",
            ]
        )
        == 0
    )
    printed = capsys.readouterr().out
    assert "carry_state_sha256=" in printed
    digest = printed.split("carry_state_sha256=", 1)[1].splitlines()[0]
    assert (
        cutover_main(
            [
                "--database",
                str(database),
                "--config",
                str(PAPER_CONFIG),
                "--expected-state-sha256",
                digest,
                "--git-sha",
                GIT_SHA,
                "--confirm-legacy-synthetic-cutover",
            ]
        )
        == 0
    )
    assert CUTOVER_APPLIED in capsys.readouterr().out


def test_post_cutover_runner_starts_flat_without_uncertainty(tmp_path: Path) -> None:
    database = _seed(tmp_path)
    _apply(database)
    runner = CarryRunner(
        policy=PAPER_CARRY_POLICY,
        state_file=database,
        venue=_StubVenue(),
        live_broker=None,
        notifier=lambda *_args, **_kwargs: None,
    )
    assert runner.accounting_uncertain is False
    assert runner.in_position is False
    assert runner.execution_state == "FLAT"
    assert runner.equity == EXACT_EQUITY
    incidents = [
        incident
        for incident in runner.store.read_incidents()
        if incident["engine"] == "carry" and incident["status"] == "OPEN"
    ]
    assert incidents == []
    assert runner.store.read_orders("carry") == []


def test_future_genuine_v6_open_initializes_accounting(tmp_path: Path) -> None:
    database = _seed(tmp_path)
    _apply(database)
    runner = CarryRunner(
        policy=PAPER_CARRY_POLICY,
        state_file=database,
        venue=_StubVenue(),
        live_broker=None,
        notifier=lambda *_args, **_kwargs: None,
    )
    entry_ts = pd.Timestamp("2026-08-18T20:00:00+00:00")
    runner._open_position(0.12, 100_000.0, "TEST_MARK", entry_ts, entry_ts)
    assert runner.in_position is True
    assert runner.entry_equity == EXACT_EQUITY
    assert runner.entry_timestamp == entry_ts
    assert runner.entry_price == 100_000.0
    assert runner.perp_qty > 0
    assert runner.spot_notional > 0
    assert runner.perp_notional > 0
    assert runner.borrow_principal > 0
    assert runner.position_generation
    assert runner.last_funding_ts == entry_ts
    assert runner.funding_notional_price == 100_000.0


class _StubVenue:
    payments_per_day = 24
    native_funding_interval = pd.Timedelta(hours=1)
    exchange_id = "hyperliquid"

    def funding_history_since(self, since):
        start = pd.Timestamp(since)
        start = start.tz_localize("UTC") if start.tzinfo is None else start.tz_convert("UTC")
        index = pd.date_range(start=start.ceil("h"), periods=4, freq="h", tz="UTC")
        return pd.Series([0.0001] * len(index), index=index, dtype=float)
