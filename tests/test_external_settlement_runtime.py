from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from btcquant.execution.broker import Broker
from btcquant.execution.external_settlement_coordinator import (
    ExternalSettlementReconciliationStatus,
)
from btcquant.execution.external_settlement_finalization import (
    ExternalSettlementFinalizationStatus,
)
from btcquant.execution.external_settlement_runtime import ExternalSettlementRuntime

from test_external_settlement_coordinator import OBSERVED, _prepared


class _Clock:
    def utc_now(self) -> datetime:
        return datetime(2026, 9, 5, 12, 5, tzinfo=UTC)


class _QualifiedBroker(Broker):
    external_execution = True
    supports_stop_orders = False
    supports_order_lookup = False
    supports_position_reconciliation = False
    exchange_id = "hyperliquid"
    environment = "testnet"
    market_kind = "perp"
    account_scope = "main"
    symbol = "BTC/USDC:USDC"

    def market_buy(self, qty: float, ref_price: float):
        raise AssertionError("runtime test broker must never submit")

    def market_sell(self, qty: float, ref_price: float):
        raise AssertionError("runtime test broker must never submit")


def test_runtime_reconciles_applies_and_finalizes_with_injected_read_only_acquirer(
    tmp_path: Path,
) -> None:
    store, _context, acquirer, persisted = _prepared(tmp_path, persist_commitment=True)
    runtime = ExternalSettlementRuntime(
        store,
        _QualifiedBroker(),
        _Clock(),
        acquirer=acquirer,
    )

    result = runtime.reconcile_order(persisted.local_order_id, observed_at=OBSERVED)

    assert result.reconciliation.status == ExternalSettlementReconciliationStatus.APPLIED
    assert result.reconciliation.finalized is False
    assert result.finalization.status == ExternalSettlementFinalizationStatus.FINALIZED
    assert result.finalization.local_order_id == persisted.local_order_id
    order = store.read_order_by_intent(persisted.intent_id)
    assert order is not None
    assert order["local_state"] == "TERMINAL"


def test_runtime_replay_is_idempotent_after_external_finalization(tmp_path: Path) -> None:
    store, _context, acquirer, persisted = _prepared(tmp_path, persist_commitment=True)
    runtime = ExternalSettlementRuntime(
        store,
        _QualifiedBroker(),
        _Clock(),
        acquirer=acquirer,
    )

    first = runtime.reconcile_order(persisted.local_order_id, observed_at=OBSERVED)
    second = runtime.reconcile_order(persisted.local_order_id, observed_at=OBSERVED)

    assert first.finalization.status == ExternalSettlementFinalizationStatus.FINALIZED
    assert second.reconciliation.status == ExternalSettlementReconciliationStatus.ALREADY_APPLIED
    assert second.finalization.status == ExternalSettlementFinalizationStatus.ALREADY_FINALIZED
    assert acquirer.calls == 2
    assert len(store.read_financial_settlement_application_chain(persisted.local_order_id)) == 1
