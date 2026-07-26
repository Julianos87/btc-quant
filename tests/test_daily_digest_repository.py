from __future__ import annotations

import sys
from pathlib import Path

from btcquant.entrypoints import digest
from btcquant.execution.state_store import StateStore

ROOT = Path(__file__).resolve().parents[1]


def test_digest_uses_shared_sqlite_first_repository(tmp_path, monkeypatch):
    (tmp_path / "live_state_4x.json").write_text('{"source": "legacy"}', encoding="utf-8")
    (tmp_path / "equity_trend.csv").write_text(
        "ts,equity\n2030-01-01T00:00:00Z,1\n",
        encoding="utf-8",
    )
    store = StateStore(tmp_path / "btcquant.db")
    store.save_engine_state("trend", {"source": "sqlite", "slots": {}})
    store.save_engine_state("carry", {"equity": 4000.0, "in_position": False})
    store.append_equity("trend", 6000.0, "2030-01-01T00:00:00Z")
    store.append_equity("carry", 4000.0, "2030-01-01T00:00:00Z")
    store.save_states_and_flow(
        {},
        kind="deposit",
        trend_flow=60.0,
        carry_flow=40.0,
    )
    store.record_trade(
        {
            "exit_ts": "2030-01-02T00:00:00Z",
            "entry_ts": "2030-01-01T00:00:00Z",
            "strategy": "trend_ls_1",
            "direction": 1,
            "qty": 1.0,
            "entry_price": 100.0,
            "exit_price": 110.0,
            "pnl": 10.0,
            "bars_held": 1,
            "reason": "signal",
        }
    )

    monkeypatch.setattr(digest, "STATE", tmp_path)

    assert digest._read_json(tmp_path / "live_state_4x.json")["source"] == "sqlite"
    assert digest._equity("equity_trend.csv").tolist() == [6000.0]
    assert digest._flows().iloc[0]["trend_flow"] == 60.0
    assert digest._repository().read_trades().iloc[0]["pnl"] == 10.0

    messages: list[str] = []
    monkeypatch.setattr(digest, "notify", messages.append)
    monkeypatch.setattr(sys, "argv", ["btcquant-digest", "--weekly"])
    digest.main()

    assert len(messages) == 1
    assert "Équity totale : 10,000 $" in messages[0]
    assert "Incidents ouverts" not in messages[0]
