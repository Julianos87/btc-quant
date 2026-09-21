"""Moteur PAPER carry à deux jambes, fail-closed et rejouable.

Ce module ne connaît aucun secret et n'envoie jamais d'ordre.  Il fournit le
contrat commun que l'adaptateur PAPER et un futur adaptateur externe devront
respecter : financement explicite, exécution causale, fills idempotents,
comptabilité spot/perp et contrôle de marge.

Le profil Hyperliquid courant est volontairement *non qualifié* pour le
financement du spot : la capacité d'emprunt et le partage de collatéral entre
wallet spot et perp ne sont pas des hypothèses acceptables.  Un test peut
construire un :class:`CarryVenueSpec` qualifié avec des capacités explicites.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

import pandas as pd

from ..domain.execution import ExecutionConfig, ExecutionSimulator, MarketOrder, OrderSide


class CarryPaperError(ValueError):
    """Donnée économique, marché ou compte non qualifié."""


class CarryExecutionState(StrEnum):
    FLAT = "FLAT"
    ENTRY_PENDING = "ENTRY_PENDING"
    ONE_LEG_FILLED = "ONE_LEG_FILLED"
    PARTIALLY_HEDGED = "PARTIALLY_HEDGED"
    HEDGED = "HEDGED"
    EXIT_PENDING = "EXIT_PENDING"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


class CarryLeg(StrEnum):
    SPOT = "SPOT"
    PERP = "PERP"


def _utc(value: pd.Timestamp | str) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        raise CarryPaperError("un horodatage UTC explicite est obligatoire")
    return timestamp.tz_convert("UTC")


def _finite(value: float, label: str, *, non_negative: bool = False) -> float:
    number = float(value)
    if not math.isfinite(number) or (non_negative and number < 0):
        raise CarryPaperError(f"{label} doit être fini et positif ou nul")
    return number


@dataclass(frozen=True)
class CarryVenueSpec:
    """Capacités économiques déclarées du compte, jamais déduites du levier."""

    venue: str
    spot_symbol: str
    perp_symbol: str
    base_asset: str = "BTC"
    quote_asset: str = "USDC"
    spot_account: str = "spot"
    perp_account: str = "perp"
    collateral_asset: str = "USDC"
    borrow_asset: str = "USDC"
    borrow_enabled: bool = False
    max_borrow: float | None = None
    max_leverage: float | None = None
    initial_margin_rate: float | None = None
    maintenance_margin_rate: float | None = None
    fee_source: str = "UNSPECIFIED"
    financing_source: str = "UNSPECIFIED"
    qualified: bool = False
    qualification_reason: str = "account funding and collateral reuse are not qualified"

    def __post_init__(self) -> None:
        for name in ("venue", "spot_symbol", "perp_symbol", "base_asset", "quote_asset"):
            if not getattr(self, name).strip():
                raise CarryPaperError(f"{name} ne peut pas être vide")
        if self.max_borrow is not None:
            _finite(self.max_borrow, "max_borrow", non_negative=True)
        if self.max_leverage is not None and self.max_leverage < 1:
            raise CarryPaperError("max_leverage doit être supérieur ou égal à 1")
        for name in ("initial_margin_rate", "maintenance_margin_rate"):
            value = getattr(self, name)
            if value is not None and not 0 < float(value) < 1:
                raise CarryPaperError(f"{name} doit être dans ]0, 1[")
        if self.qualified:
            missing = [
                name
                for name in (
                    "max_borrow",
                    "max_leverage",
                    "initial_margin_rate",
                    "maintenance_margin_rate",
                )
                if getattr(self, name) is None
            ]
            if missing or not self.borrow_enabled:
                raise CarryPaperError(
                    "un compte qualifié doit déclarer l'emprunt et les règles de marge : "
                    + ", ".join(missing or ["borrow_enabled"])
                )

    @classmethod
    def hyperliquid_btc_usdc(cls) -> CarryVenueSpec:
        """Profil public connu ; le financement du spot reste non qualifié."""

        return cls(
            venue="hyperliquid",
            spot_symbol="BTC/USDC",
            perp_symbol="BTC/USDC:USDC",
            collateral_asset="USDC",
            borrow_asset="USDC",
            financing_source="HYPERLIQUID_ACCOUNT_CAPACITY_NOT_PROVIDED",
            fee_source="HYPERLIQUID_FEE_TIER_REQUIRED",
            qualified=False,
        )


@dataclass(frozen=True)
class CarryCostProvenance:
    spot_fee_rate: float
    perp_fee_rate: float
    spot_slippage_bps: float
    perp_slippage_bps: float
    borrow_rate_ann: float
    source: str
    assumptions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "spot_fee_rate",
            "perp_fee_rate",
            "spot_slippage_bps",
            "perp_slippage_bps",
            "borrow_rate_ann",
        ):
            _finite(getattr(self, name), name, non_negative=True)
        if not self.source.strip():
            raise CarryPaperError("la provenance des coûts est obligatoire")


@dataclass(frozen=True)
class CarryFundingEvent:
    event_id: str
    venue: str
    instrument: str
    timestamp: pd.Timestamp | str
    native_rate: float
    reference_price: float | None
    reference_source: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", _utc(self.timestamp))
        _finite(self.native_rate, "native_rate")
        if not self.event_id or not self.venue or not self.instrument:
            raise CarryPaperError("un événement funding doit être identifié")
        if self.reference_price is not None:
            _finite(self.reference_price, "reference_price")
            if self.reference_price <= 0:
                raise CarryPaperError("reference_price doit être positif")
        if self.reference_price is None or not self.reference_source.strip():
            raise CarryPaperError("un prix de funding manquant reste incertain")


@dataclass(frozen=True)
class CarryMarketState:
    """Observation synchrone et causale des deux marchés."""

    timestamp: pd.Timestamp | str
    spot_bid: float
    spot_ask: float
    perp_bid: float
    perp_ask: float
    spot_mark: float
    perp_mark: float
    source: str
    freshness_seconds: float = 0.0
    spot_order_book: Mapping[str, object] | None = None
    perp_order_book: Mapping[str, object] | None = None
    market_timestamp: pd.Timestamp | str | None = None
    received_timestamp: pd.Timestamp | str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", _utc(self.timestamp))
        object.__setattr__(
            self,
            "market_timestamp",
            self.timestamp if self.market_timestamp is None else _utc(self.market_timestamp),
        )
        object.__setattr__(
            self,
            "received_timestamp",
            self.timestamp if self.received_timestamp is None else _utc(self.received_timestamp),
        )
        for name in (
            "spot_bid",
            "spot_ask",
            "perp_bid",
            "perp_ask",
            "spot_mark",
            "perp_mark",
        ):
            _finite(getattr(self, name), name)
            if getattr(self, name) <= 0:
                raise CarryPaperError(f"{name} doit être positif")
        if self.spot_bid > self.spot_ask or self.perp_bid > self.perp_ask:
            raise CarryPaperError("bid supérieur à ask")
        _finite(self.freshness_seconds, "freshness_seconds", non_negative=True)
        if not self.source.strip():
            raise CarryPaperError("la source du marché est obligatoire")


@dataclass(frozen=True)
class FinancingPlan:
    """Plan d'ouverture vérifiable avant toute émission des deux jambes."""

    capital_available: float
    target_notional: float
    spot_price: float
    perp_mark_price: float
    leverage: float
    max_borrow: float
    initial_margin_rate: float
    spot_qty: float
    perp_qty: float
    borrow_principal: float
    spot_cash_required: float
    perp_initial_margin: float
    estimated_entry_cost: float
    total_cash_required: float | str
    qualified: bool
    reason: str

    @classmethod
    def build(
        cls,
        *,
        capital_available: float,
        target_notional: float,
        spot_price: float,
        perp_mark_price: float,
        leverage: float,
        max_borrow: float | None,
        initial_margin_rate: float | None,
        spot_fee_rate: float,
        perp_fee_rate: float,
        spot_slippage_bps: float,
        perp_slippage_bps: float,
    ) -> FinancingPlan:
        values = {
            "capital_available": capital_available,
            "target_notional": target_notional,
            "spot_price": spot_price,
            "perp_mark_price": perp_mark_price,
            "leverage": leverage,
        }
        for name, value in values.items():
            _finite(value, name)
        if target_notional <= 0 or spot_price <= 0 or perp_mark_price <= 0 or leverage < 1:
            raise CarryPaperError("plan de financement invalide")
        spot_qty = target_notional / spot_price
        perp_qty = target_notional / spot_price
        if max_borrow is None or initial_margin_rate is None:
            return cls(
                capital_available=capital_available,
                target_notional=target_notional,
                spot_price=spot_price,
                perp_mark_price=perp_mark_price,
                leverage=leverage,
                max_borrow=0.0 if max_borrow is None else max_borrow,
                initial_margin_rate=0.0 if initial_margin_rate is None else initial_margin_rate,
                spot_qty=spot_qty,
                perp_qty=perp_qty,
                borrow_principal=0.0,
                spot_cash_required=target_notional,
                perp_initial_margin=0.0,
                estimated_entry_cost=0.0,
                total_cash_required="UNKNOWN",
                qualified=False,
                reason="capacités d'emprunt ou marge inconnues",
            )
        max_borrow = _finite(max_borrow, "max_borrow", non_negative=True)
        initial_margin_rate = _finite(initial_margin_rate, "initial_margin_rate", non_negative=True)
        perp_notional = perp_qty * perp_mark_price
        perp_margin = perp_notional * initial_margin_rate
        entry_cost = target_notional * (
            spot_fee_rate + spot_slippage_bps / 10_000.0
        ) + perp_notional * (perp_fee_rate + perp_slippage_bps / 10_000.0)
        cash_after_margin_and_cost = capital_available - perp_margin - entry_cost
        required_borrow = max(0.0, target_notional - max(0.0, cash_after_margin_and_cost))
        borrow = min(
            target_notional, required_borrow if required_borrow <= max_borrow else max_borrow
        )
        spot_cash = max(0.0, target_notional - borrow)
        total = spot_cash + perp_margin + entry_cost
        qualified = required_borrow <= max_borrow + 1e-12 and capital_available >= total - 1e-12
        reason = (
            "financement équilibré" if qualified else "capital simultané spot + marge insuffisant"
        )
        return cls(
            capital_available=capital_available,
            target_notional=target_notional,
            spot_price=spot_price,
            perp_mark_price=perp_mark_price,
            leverage=leverage,
            max_borrow=max_borrow,
            initial_margin_rate=initial_margin_rate,
            spot_qty=spot_qty,
            perp_qty=perp_qty,
            borrow_principal=borrow,
            spot_cash_required=spot_cash,
            perp_initial_margin=perp_margin,
            estimated_entry_cost=entry_cost,
            total_cash_required=total,
            qualified=qualified,
            reason=reason,
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CarryAccountingState:
    """Bilan mutable dont chaque variation vient d'un événement identifié."""

    initial_cash: float
    cash_available: float | None = None
    cash_locked: float = 0.0
    spot_qty: float = 0.0
    spot_cost_basis: float = 0.0
    perp_qty: float = 0.0
    perp_entry_price: float = 0.0
    debt_principal: float = 0.0
    accrued_interest: float = 0.0
    funding_received: float = 0.0
    funding_paid: float = 0.0
    spot_fees: float = 0.0
    perp_fees: float = 0.0
    transfers: list[dict[str, Any]] = field(default_factory=list)
    applied_event_ids: list[str] = field(default_factory=list)
    spot_mark: float | None = None
    perp_mark: float | None = None
    last_timestamp: pd.Timestamp | None = None
    _reserved_margin: float = field(default=0.0, repr=False)

    def __post_init__(self) -> None:
        self.initial_cash = _finite(self.initial_cash, "initial_cash")
        if self.initial_cash <= 0:
            raise CarryPaperError("initial_cash doit être positif")
        if self.cash_available is None:
            self.cash_available = self.initial_cash

    def _cash(self) -> float:
        if self.cash_available is None:
            raise CarryPaperError("cash_available absent du bilan")
        return self.cash_available

    @property
    def funding_net(self) -> float:
        return self.funding_received - self.funding_paid

    @property
    def fees_total(self) -> float:
        return self.spot_fees + self.perp_fees

    @property
    def perp_unrealized_pnl(self) -> float:
        if not self.perp_qty or self.perp_mark is None:
            return 0.0
        return self.perp_qty * (self.perp_entry_price - self.perp_mark)

    @property
    def spot_unrealized_pnl(self) -> float:
        if not self.spot_qty or self.spot_mark is None:
            return 0.0
        return self.spot_qty * self.spot_mark - self.spot_cost_basis

    @property
    def equity(self) -> float:
        if self.spot_mark is None:
            spot_value = self.spot_cost_basis
        else:
            spot_value = self.spot_qty * self.spot_mark
        return (
            self._cash()
            + self.cash_locked
            + spot_value
            + self.perp_unrealized_pnl
            - self.debt_principal
            - self.accrued_interest
        )

    def _debit_cash(self, amount: float) -> None:
        amount = _finite(amount, "cash debit", non_negative=True)
        available = self._cash()
        from_available = min(available, amount)
        self.cash_available = available - from_available
        remainder = amount - from_available
        if remainder > self.cash_locked + 1e-12:
            raise CarryPaperError("liquidité de règlement insuffisante")
        self.cash_locked -= remainder

    def reserve_margin(self, amount: float) -> None:
        amount = _finite(amount, "margin", non_negative=True)
        if self._cash() < amount - 1e-12:
            raise CarryPaperError("collatéral disponible insuffisant")
        self.cash_available = self._cash() - amount
        self.cash_locked += amount
        self._reserved_margin = amount

    def release_margin(self, amount: float | None = None) -> None:
        amount = (
            self._reserved_margin
            if amount is None
            else _finite(amount, "margin", non_negative=True)
        )
        amount = min(amount, self.cash_locked)
        self.cash_locked -= amount
        self.cash_available = self._cash() + amount
        self._reserved_margin = max(0.0, self._reserved_margin - amount)

    def apply_spot_fill(
        self, *, side: str, qty: float, price: float, fee: float, event_id: str
    ) -> None:
        self._once(event_id)
        qty, price, fee = (
            _finite(qty, "spot qty"),
            _finite(price, "spot price"),
            _finite(fee, "spot fee", non_negative=True),
        )
        if qty <= 0 or price <= 0:
            raise CarryPaperError("fill spot invalide")
        notional = qty * price
        if side == "BUY":
            available = self._cash()
            own_cash = min(available, notional + fee)
            borrowed = notional + fee - own_cash
            self.cash_available = available - own_cash
            self.debt_principal += borrowed
            self.spot_cost_basis += notional
            self.spot_qty += qty
        elif side == "SELL":
            if qty > self.spot_qty + 1e-12:
                raise CarryPaperError("vente spot supérieure à la quantité détenue")
            average = self.spot_cost_basis / self.spot_qty if self.spot_qty else 0.0
            self.spot_cost_basis -= average * qty
            self.spot_qty -= qty
            self.cash_available = self._cash() + notional - fee
            repayment = min(self.debt_principal, max(0.0, self._cash()))
            self.cash_available = self._cash() - repayment
            self.debt_principal -= repayment
        else:
            raise CarryPaperError("side spot invalide")
        self.spot_fees += fee
        self._touch(event_id)

    def apply_perp_fill(
        self, *, side: str, qty: float, price: float, fee: float, event_id: str
    ) -> None:
        self._once(event_id)
        qty, price, fee = (
            _finite(qty, "perp qty"),
            _finite(price, "perp price"),
            _finite(fee, "perp fee", non_negative=True),
        )
        if qty <= 0 or price <= 0:
            raise CarryPaperError("fill perp invalide")
        if side == "SELL":
            current = self.perp_qty
            self.perp_entry_price = ((current * self.perp_entry_price) + qty * price) / (
                current + qty
            )
            self.perp_qty += qty
            self._debit_cash(fee)
        elif side == "BUY":
            if qty > self.perp_qty + 1e-12:
                raise CarryPaperError("rachat perp supérieur à la position short")
            self.cash_available = self._cash() + qty * (self.perp_entry_price - price)
            self._debit_cash(fee)
            self.perp_qty -= qty
            if self.perp_qty <= 1e-12:
                self.perp_qty = 0.0
                self.perp_entry_price = 0.0
        else:
            raise CarryPaperError("side perp invalide")
        self.perp_fees += fee
        self._touch(event_id)

    def apply_funding(self, event: CarryFundingEvent) -> None:
        if event.event_id in self.applied_event_ids:
            return
        if self.perp_qty <= 0:
            return
        if event.reference_price is None:
            raise CarryPaperError("funding sans prix de référence")
        amount = self.perp_qty * event.reference_price * event.native_rate
        if amount >= 0:
            self.funding_received += amount
        else:
            self.funding_paid += -amount
        if amount >= 0:
            self.cash_available = self._cash() + amount
        else:
            self._debit_cash(-amount)
        self._touch(event.event_id, _utc(event.timestamp))

    def accrue_interest(
        self, *, annual_rate: float, until: pd.Timestamp | str, event_id: str
    ) -> float:
        until_ts = _utc(until)
        if event_id in self.applied_event_ids:
            return 0.0
        previous = self.last_timestamp or until_ts
        seconds = max(0.0, (until_ts - previous).total_seconds())
        cost = (
            self.debt_principal
            * _finite(annual_rate, "annual borrow rate", non_negative=True)
            * seconds
            / (365.25 * 24 * 3600)
        )
        self.accrued_interest += cost
        self._debit_cash(cost)
        self._touch(event_id, until_ts)
        return cost

    def mark(self, market: CarryMarketState) -> None:
        self.spot_mark = market.spot_mark
        self.perp_mark = market.perp_mark
        self.last_timestamp = _utc(market.timestamp)

    def balance_sheet(self, market: CarryMarketState | None = None) -> dict[str, Any]:
        if market is not None:
            self.mark(market)
        assets = (
            self._cash()
            + self.cash_locked
            + (
                self.spot_qty * self.spot_mark
                if self.spot_mark is not None
                else self.spot_cost_basis
            )
            + self.perp_unrealized_pnl
        )
        liabilities = self.debt_principal + self.accrued_interest
        return {
            "assets": assets,
            "liabilities": liabilities,
            "equity": assets - liabilities,
            "cash_available": self._cash(),
            "cash_locked": self.cash_locked,
            "spot_qty": self.spot_qty,
            "spot_cost_basis": self.spot_cost_basis,
            "perp_qty": self.perp_qty,
            "perp_entry_price": self.perp_entry_price,
            "perp_unrealized_pnl": self.perp_unrealized_pnl,
            "debt_principal": self.debt_principal,
            "accrued_interest": self.accrued_interest,
            "funding_received": self.funding_received,
            "funding_paid": self.funding_paid,
            "spot_fees": self.spot_fees,
            "perp_fees": self.perp_fees,
            "identity_residual": (assets - liabilities) - self.equity,
        }

    def snapshot(self) -> dict[str, Any]:
        data = self.balance_sheet()
        data.update(
            {
                "initial_cash": self.initial_cash,
                "applied_event_ids": list(self.applied_event_ids),
                "transfers": list(self.transfers),
            }
        )
        return data

    def _once(self, event_id: str) -> None:
        if not event_id.strip():
            raise CarryPaperError("event_id obligatoire")
        if event_id in self.applied_event_ids:
            raise CarryPaperError(f"événement déjà appliqué : {event_id}")

    def _touch(self, event_id: str, timestamp: pd.Timestamp | None = None) -> None:
        self.applied_event_ids.append(event_id)
        if timestamp is not None:
            self.last_timestamp = timestamp


@dataclass(frozen=True)
class MarginCheck:
    account_value: float
    initial_margin: float
    maintenance_margin: float
    liquidatable: bool
    qualified: bool
    reason: str


def check_margin(
    balance: CarryAccountingState,
    *,
    perp_mark: float,
    initial_margin_rate: float | None,
    maintenance_margin_rate: float | None,
) -> MarginCheck:
    if initial_margin_rate is None or maintenance_margin_rate is None:
        return MarginCheck(0.0, 0.0, 0.0, False, False, "règles de marge inconnues")
    notional = balance.perp_qty * perp_mark
    initial = notional * initial_margin_rate
    maintenance = notional * maintenance_margin_rate
    value = balance.cash_locked + balance.perp_qty * (balance.perp_entry_price - perp_mark)
    return MarginCheck(value, initial, maintenance, value < maintenance, True, "marge évaluée")


@dataclass(frozen=True)
class CarryExecutionFill:
    event_id: str
    leg: CarryLeg
    side: str
    status: str
    requested_qty: float
    filled_qty: float
    price: float
    fee: float
    timestamp: pd.Timestamp
    liquidity_source: str


class CausalMarketTape:
    """Choisit la première observation non antérieure à la deadline."""

    def __init__(self, observations: Iterable[CarryMarketState]) -> None:
        self.observations = tuple(observations)
        received = [_utc(item.received_timestamp or item.timestamp) for item in self.observations]
        if any(right <= left for left, right in zip(received, received[1:], strict=False)):
            raise CarryPaperError("messages reçus dans le désordre ou dupliqués")

    def after(self, decision_timestamp: pd.Timestamp | str, latency_ms: int) -> CarryMarketState:
        deadline = _utc(decision_timestamp) + pd.Timedelta(milliseconds=latency_ms)
        for item in self.observations:
            if _utc(item.received_timestamp or item.timestamp) >= deadline:
                return item
        raise CarryPaperError("aucune donnée de marché disponible après la soumission")


class PaperTwoLegExecutor:
    """Exécuteur PAPER commun aux deux jambes, fondé sur le simulateur de fills."""

    def __init__(self, config: ExecutionConfig, tape: CausalMarketTape) -> None:
        self.config = config
        self.tape = tape
        self.simulator = ExecutionSimulator(config)

    def execute(
        self,
        *,
        leg: CarryLeg,
        side: str,
        qty: float,
        decision_timestamp: pd.Timestamp | str,
        event_id: str,
    ) -> CarryExecutionFill:
        market = self.tape.after(decision_timestamp, self.config.latency_ms)
        if leg == CarryLeg.SPOT:
            bid, ask, book = market.spot_bid, market.spot_ask, market.spot_order_book
        else:
            bid, ask, book = market.perp_bid, market.perp_ask, market.perp_order_book
        order_side = OrderSide(side)
        reference = ask if order_side == OrderSide.BUY else bid
        result = self.simulator.execute_market(
            MarketOrder(
                order_id=event_id,
                side=order_side,
                qty=qty,
                reference_price=reference,
                delayed_price=reference,
                order_book=book,
            )
        )
        return CarryExecutionFill(
            event_id=event_id,
            leg=leg,
            side=side,
            status=result.status.value,
            requested_qty=result.requested_qty,
            filled_qty=result.qty,
            price=result.price,
            fee=result.fee,
            timestamp=_utc(market.market_timestamp or market.timestamp),
            liquidity_source=result.liquidity_source or "unknown",
        )


def expected_net_carry(
    *,
    funding_ann: float,
    notional: float,
    borrow_rate_ann: float,
    debt: float,
    holding_days: float,
    entry_cost: float,
    exit_cost: float,
    uncertainty_reserve: float,
) -> dict[str, float | bool]:
    """Décide sur le rendement net attendu, jamais sur le funding brut seul."""

    if holding_days <= 0:
        raise CarryPaperError("holding_days doit être positif")
    years = holding_days / 365.25
    funding = notional * funding_ann * years
    borrow = debt * borrow_rate_ann * years
    net = funding - borrow - entry_cost - exit_cost - uncertainty_reserve
    return {
        "funding_expected": funding,
        "borrow_expected": borrow,
        "entry_cost": entry_cost,
        "exit_cost": exit_cost,
        "uncertainty_reserve": uncertainty_reserve,
        "net_expected": net,
        "qualified": net > 0,
    }


def serialize_two_leg_state(
    *,
    state: CarryExecutionState,
    spec: CarryVenueSpec,
    balance: CarryAccountingState,
    active_intent: Mapping[str, Any] | None = None,
    journal: Sequence[Mapping[str, Any]] = (),
    cost_provenance: Mapping[str, Any] | None = None,
    model_version: str = "carry_two_leg_execution_v1",
) -> dict[str, Any]:
    return {
        "model_version": model_version,
        "state": state.value,
        "venue_spec": asdict(spec),
        "balance": balance.snapshot(),
        "active_intent": dict(active_intent) if active_intent else None,
        "journal": [dict(item) for item in journal],
        "cost_provenance": dict(cost_provenance) if cost_provenance else None,
        "qualification": "QUALIFIED" if spec.qualified else "NON_QUALIFIED",
    }
