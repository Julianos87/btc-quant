"""Le watchdog alerte une fois par incident, puis suit sa résolution."""

from __future__ import annotations

from btcquant.execution.state_store import StateStore
from btcquant.entrypoints import watchdog


def test_watchdog_deduplicates_and_resolves_execution_alerts(tmp_path, monkeypatch):
    monkeypatch.setattr(watchdog, "STATE", tmp_path)
    monkeypatch.setattr(watchdog, "CHECKS", [("trend", 600, "btcquant-trend")])
    messages: list[str] = []
    monkeypatch.setattr(watchdog, "notify", messages.append)

    store = StateStore(tmp_path / "btcquant.db")
    store.save_engine_state("trend", {"slots": {}})
    order_id = store.begin_order(
        "trend",
        "strategy",
        "watchdog-unbalanced",
        "MARKET",
        "BUY",
        1.0,
        "entry",
        reference_price=100.0,
    )
    store.complete_order(
        order_id,
        status="UNBALANCED",
        filled_qty=0.5,
        price=100.0,
    )

    watchdog.main([])
    watchdog.main([])

    assert len(messages) == 1
    assert "UNBALANCED" in messages[0]
    incident = next(
        item for item in store.read_incidents(open_only=True) if item["kind"] == "unbalanced_orders"
    )
    assert incident["occurrences"] == 2

    store.complete_order(order_id, status="RECOVERED_ABORTED")
    watchdog.main([])

    assert messages == [messages[0]]
    assert not any(
        item["kind"] == "unbalanced_orders" for item in store.read_incidents(open_only=True)
    )


def test_watchdog_can_monitor_isolated_testnet_database(tmp_path, monkeypatch):
    messages: list[str] = []
    monkeypatch.setattr(watchdog, "notify", messages.append)
    database = tmp_path / "btcquant-testnet.db"
    StateStore(database).save_engine_state(
        "trend",
        {
            "slots": {
                "trend_ls_55": {
                    "position": {"qty": 0.01},
                    "stop_order_id": None,
                    "stop_transition": None,
                }
            }
        },
    )

    watchdog.main(
        [
            "--database",
            str(database),
            "--service",
            "btcquant-hyperliquid-testnet",
        ]
    )

    assert any("sans stop" in message for message in messages)
