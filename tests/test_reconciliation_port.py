from __future__ import annotations

from types import SimpleNamespace

import pytest

from btcquant.execution.broker import PaperBroker
from btcquant.execution.ccxt_broker import CcxtBroker
from btcquant.execution.reconcile import inspect_position_reconciliation, reconcile


def test_paper_broker_needs_no_remote_position_port():
    assert reconcile(PaperBroker(), [], "BTC/USDT") is True


def test_ccxt_adapter_normalizes_long_and_short_positions():
    broker = CcxtBroker.__new__(CcxtBroker)
    broker.exchange = SimpleNamespace(
        fetch_positions=lambda _symbols: [
            {"contracts": 2.0, "side": "long"},
            {"contracts": 0.75, "side": "short"},
            {"contracts": None, "side": "long"},
        ]
    )

    assert broker.net_position("BTC/USDT") == pytest.approx(1.25)


def test_reconcile_consumes_only_the_explicit_broker_port():
    class PortBroker(PaperBroker):
        supports_position_reconciliation = True

        def net_position(self, symbol: str) -> float:
            assert symbol == "BTC/USDT"
            return 1.5

    slot = SimpleNamespace(position=SimpleNamespace(direction=1, qty=1.5))
    broker = PortBroker()
    # Aucun attribut exchange : la réconciliation ne dépend plus de CCXT.
    assert not hasattr(broker, "exchange")

    assert reconcile(broker, [slot], "BTC/USDT") is True


def test_multi_slot_net_zero_is_not_claimed_as_slot_reconciled():
    class PortBroker(PaperBroker):
        supports_position_reconciliation = True

        def net_position(self, _symbol: str) -> float:
            return 0.0

    slots = [
        SimpleNamespace(position=SimpleNamespace(direction=1, qty=1.0)),
        SimpleNamespace(position=SimpleNamespace(direction=-1, qty=1.0)),
    ]

    report = inspect_position_reconciliation(PortBroker(), slots, "BTC/USDT")

    assert report.ok is False
    assert report.reason == "multi_slot_net_attribution_unavailable"
    assert reconcile(PortBroker(), slots, "BTC/USDT") is False
