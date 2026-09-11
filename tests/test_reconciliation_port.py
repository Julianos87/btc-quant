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


def test_fine_venue_quantum_rejects_multiple_tradable_deltas():
    class PortBroker(PaperBroker):
        supports_position_reconciliation = True

        def net_position(self, _symbol: str) -> float:
            return -8e-6

        def position_quantity_quantum(self, _symbol: str) -> float:
            return 1e-6

    slot = SimpleNamespace(position=SimpleNamespace(direction=1, qty=0.0))
    report = inspect_position_reconciliation(PortBroker(), [slot], "BTC/USDT")

    assert report.ok is False
    assert report.reason == "position_mismatch"
    assert report.context is not None
    assert report.context["quantity_quantum"] == pytest.approx(1e-6)


@pytest.mark.parametrize("delta", [0.5e-6, 0.999999e-6])
def test_fine_venue_accepts_only_sub_quantum_noise(delta):
    class PortBroker(PaperBroker):
        supports_position_reconciliation = True

        def net_position(self, _symbol: str) -> float:
            return -delta

        def position_quantity_quantum(self, _symbol: str) -> float:
            return 1e-6

    slot = SimpleNamespace(position=SimpleNamespace(direction=1, qty=0.0))
    report = inspect_position_reconciliation(PortBroker(), [slot], "BTC/USDT")

    assert report.ok is True
    assert report.reason == "position_equal"


def test_large_quantum_cannot_expand_legacy_acceptance_region():
    class PortBroker(PaperBroker):
        supports_position_reconciliation = True

        def net_position(self, _symbol: str) -> float:
            return -5e-5

        def position_quantity_quantum(self, _symbol: str) -> float:
            return 1e-4

    slot = SimpleNamespace(position=SimpleNamespace(direction=1, qty=0.0))
    report = inspect_position_reconciliation(PortBroker(), [slot], "BTC/USDT")

    assert report.ok is False
    assert report.context is not None
    assert report.context["effective_tolerance"] == pytest.approx(1e-5)


@pytest.mark.parametrize("quantum", [None, 0.0, -1e-6, float("nan"), float("inf"), True])
def test_missing_or_malformed_quantum_fails_closed(quantum):
    class PortBroker(PaperBroker):
        supports_position_reconciliation = True

        def net_position(self, _symbol: str) -> float:
            return -1e-12

        def position_quantity_quantum(self, _symbol: str):
            return quantum

    slot = SimpleNamespace(position=SimpleNamespace(direction=1, qty=0.0))
    report = inspect_position_reconciliation(PortBroker(), [slot], "BTC/USDC")

    assert report.ok is False
    assert report.reason == "position_mismatch"


def test_exact_match_does_not_require_metadata():
    class PortBroker(PaperBroker):
        supports_position_reconciliation = True

        def net_position(self, _symbol: str) -> float:
            return 1.25

    slot = SimpleNamespace(position=SimpleNamespace(direction=1, qty=1.25))

    assert inspect_position_reconciliation(PortBroker(), [slot], "ETH/USDC").ok is True


@pytest.mark.parametrize("delta", [1e-6, 1e-5])
def test_one_tradable_quantum_is_not_economic_zero(delta):
    class PortBroker(PaperBroker):
        supports_position_reconciliation = True

        def net_position(self, _symbol: str) -> float:
            return -delta

        def position_quantity_quantum(self, _symbol: str) -> float:
            return 1e-6

    slot = SimpleNamespace(position=SimpleNamespace(direction=1, qty=0.0))
    report = inspect_position_reconciliation(PortBroker(), [slot], "BTC/USDC")

    assert report.ok is False
    assert report.reason == "position_mismatch"


def test_reconciliation_policy_is_symbol_specific():
    class PortBroker(PaperBroker):
        supports_position_reconciliation = True

        def net_position(self, _symbol: str) -> float:
            return -5e-8

        def position_quantity_quantum(self, symbol: str) -> float:
            return 1e-6 if symbol == "BTC/USDC" else 1e-8

    slot = SimpleNamespace(position=SimpleNamespace(direction=1, qty=0.0))
    broker = PortBroker()

    assert inspect_position_reconciliation(broker, [slot], "BTC/USDC").ok is True
    assert inspect_position_reconciliation(broker, [slot], "ETH/USDC").ok is False
