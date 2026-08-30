"""Lecture read-only et persistance passive des fills externes individuels.

Cette frontière ne résout jamais un ordre et ne déduit jamais l'absence d'un
fill à partir d'une réponse vide. Elle acquiert au plus une réponse CCXT,
normalise uniquement les fills corrélés par ``oid`` et délègue la persistance
à une frontière StateStore atomique.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

import ccxt

from .errors import FillInvariantViolation
from .external_evidence import ExternalEvidenceSource, ExternalFill

if TYPE_CHECKING:
    from .state_store import StateStore


FILL_RESPONSE_LIMIT = 2000
FILL_RETENTION_LIMIT = 10_000
_SIDE_VALUES = {"BUY", "SELL"}
_UNIFIED_HASH_FIELDS = (
    "id",
    "order",
    "clientOrderId",
    "symbol",
    "side",
    "amount",
    "price",
    "timestamp",
    "fee",
)
_RAW_HASH_FIELDS = (
    "coin",
    "px",
    "sz",
    "side",
    "time",
    "fee",
    "feeToken",
    "oid",
    "tid",
    "cloid",
    "clientOrderId",
)


class FillEvidenceLookupOutcome(StrEnum):
    """Résultat explicite d'une recherche de fills individuels."""

    FOUND = "FOUND"
    NO_MATCH = "NO_MATCH"
    TRANSPORT_FAILURE = "TRANSPORT_FAILURE"
    UNSUPPORTED = "UNSUPPORTED"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    CONFLICTING_RESPONSE = "CONFLICTING_RESPONSE"
    INCOMPLETE_RESPONSE = "INCOMPLETE_RESPONSE"


class _InvalidResponse(Exception):
    pass


class _ConflictingResponse(Exception):
    pass


class _IncompleteResponse(Exception):
    pass


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} doit être une chaîne non vide")
    return value.strip()


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field)


def _identifier(value: object, field: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise _InvalidResponse(f"{field} doit être textuel ou numérique")
    normalized = str(value).strip()
    if not normalized:
        raise _InvalidResponse(f"{field} ne peut pas être vide")
    return normalized


def _finite_number(
    value: object,
    field: str,
    *,
    positive: bool = False,
    signed: bool = False,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise _InvalidResponse(f"{field} doit être un nombre fini")
    try:
        normalized = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise _InvalidResponse(f"{field} doit être un nombre fini") from error
    if not math.isfinite(normalized):
        raise _InvalidResponse(f"{field} doit être un nombre fini")
    if positive and normalized <= 0:
        raise _InvalidResponse(f"{field} doit être strictement positif")
    if not positive and not signed and normalized < 0:
        raise _InvalidResponse(f"{field} doit être positif ou nul")
    return normalized


def _numeric_field(
    mapping: Mapping[str, Any],
    field: str,
    *,
    positive: bool = False,
    signed: bool = False,
) -> float | None:
    if field not in mapping or mapping.get(field) is None:
        return None
    return _finite_number(mapping[field], field, positive=positive, signed=signed)


def _same_numeric(left: float, right: float) -> bool:
    tolerance = max(1e-9, max(abs(left), abs(right)) * 1e-9)
    return abs(left - right) <= tolerance


def _merge_numeric(
    unified: float | None,
    raw: float | None,
    field: str,
) -> float | None:
    if unified is not None and raw is not None and not _same_numeric(unified, raw):
        raise _ConflictingResponse(f"{field} unified/raw contradictoire")
    return unified if unified is not None else raw


def _timestamp(value: object, field: str) -> tuple[float, str] | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise _InvalidResponse(f"{field} doit être un instant UTC exploitable")
    if isinstance(value, (int, float)):
        try:
            milliseconds = float(value)
        except (TypeError, ValueError, OverflowError) as error:
            raise _InvalidResponse(f"{field} doit être un instant UTC exploitable") from error
        if not math.isfinite(milliseconds) or milliseconds < 0:
            raise _InvalidResponse(f"{field} doit être un instant UTC exploitable")
        try:
            canonical = datetime.fromtimestamp(milliseconds / 1000.0, UTC).isoformat()
        except (OverflowError, OSError, ValueError) as error:
            raise _InvalidResponse(f"{field} doit être un instant UTC exploitable") from error
        return milliseconds, canonical
    if isinstance(value, str):
        candidate = value.strip()
        if not candidate:
            raise _InvalidResponse(f"{field} doit être un instant UTC exploitable")
        candidate = candidate[:-1] + "+00:00" if candidate[-1:] in {"Z", "z"} else candidate
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError as error:
            raise _InvalidResponse(f"{field} doit être un instant UTC exploitable") from error
        if parsed.tzinfo is None:
            raise _InvalidResponse(f"{field} doit inclure un fuseau UTC")
        parsed = parsed.astimezone(UTC)
        return parsed.timestamp() * 1000.0, parsed.isoformat()
    raise _InvalidResponse(f"{field} doit être un instant UTC exploitable")


def _merge_timestamp(
    unified: tuple[float, str] | None,
    raw: tuple[float, str] | None,
) -> tuple[float, str] | None:
    if unified is not None and raw is not None and unified[0] != raw[0]:
        raise _ConflictingResponse("timestamp/time unified/raw contradictoire")
    return unified if unified is not None else raw


def _safe_hash_value(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {
            str(key): _safe_hash_value(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_safe_hash_value(item) for item in value]
    return f"<{type(value).__module__}.{type(value).__qualname__}>"


def _fill_payload_hash(trade: Mapping[str, Any], info: Mapping[str, Any]) -> str:
    safe: dict[str, object] = {
        "unified": {
            field: _safe_hash_value(trade[field])
            for field in _UNIFIED_HASH_FIELDS
            if field in trade
        },
        "raw": {
            field: _safe_hash_value(info[field]) for field in _RAW_HASH_FIELDS if field in info
        },
    }
    payload = json.dumps(safe, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _raw_side(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _InvalidResponse("info.side doit être A ou B")
    if value == "A":
        return "SELL"
    if value == "B":
        return "BUY"
    raise _InvalidResponse("info.side doit être A ou B")


def _unified_side(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value.strip().upper() not in {"BUY", "SELL"}:
        raise _InvalidResponse("trade.side doit être buy ou sell")
    return value.strip().upper()


def _instrument_coin(instrument: str) -> str:
    return instrument.split("/", 1)[0].split(":", 1)[0].upper()


def _merge_text(
    unified: str | None,
    raw: str | None,
    field: str,
) -> str | None:
    if unified is not None and raw is not None and unified != raw:
        raise _ConflictingResponse(f"{field} unified/raw contradictoire")
    return unified if unified is not None else raw


@dataclass(frozen=True)
class FillLookupContext:
    """Contexte local nécessaire à une recherche de fills bornée."""

    local_order_id: int
    intent_id: str
    venue: str
    account_scope: str
    instrument: str
    side: str
    expected_external_order_id: str
    start_time_ms: int
    end_time_ms: int
    expected_client_order_id: str | None = None
    engine: str = "execution"

    def __post_init__(self) -> None:
        if isinstance(self.local_order_id, bool) or not isinstance(self.local_order_id, int):
            raise ValueError("local_order_id doit être un entier")
        if self.local_order_id <= 0:
            raise ValueError("local_order_id doit être strictement positif")
        for field in ("intent_id", "venue", "account_scope", "instrument", "engine"):
            object.__setattr__(self, field, _required_text(getattr(self, field), field))
        side = _required_text(self.side, "side").upper()
        if side not in _SIDE_VALUES:
            raise ValueError("side doit être BUY ou SELL")
        object.__setattr__(self, "side", side)
        try:
            external_id = _identifier(self.expected_external_order_id, "expected_external_order_id")
        except _InvalidResponse as error:
            raise ValueError(str(error)) from error
        if external_id is None:
            raise ValueError("expected_external_order_id est requis")
        object.__setattr__(self, "expected_external_order_id", external_id)
        object.__setattr__(
            self,
            "expected_client_order_id",
            _optional_text(self.expected_client_order_id, "expected_client_order_id"),
        )
        for field in ("start_time_ms", "end_time_ms"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field} doit être un entier supérieur ou égal à zéro")
        if self.end_time_ms < self.start_time_ms:
            raise ValueError("end_time_ms doit être supérieur ou égal à start_time_ms")


@dataclass(frozen=True)
class FillEvidenceLookup:
    """Réponse de lookup, sans conclusion d'exhaustivité ni de zero-fill."""

    context: FillLookupContext
    outcome: FillEvidenceLookupOutcome
    fills: tuple[ExternalFill, ...] = ()
    venue_fill_id_candidates: tuple[str | None, ...] = ()
    response_count: int = 0
    response_limit: int = FILL_RESPONSE_LIMIT
    response_limit_reached: bool = False
    retention_limit: int = FILL_RETENTION_LIMIT
    absence_authoritative: bool = False
    reason: str | None = None
    retryable: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.context, FillLookupContext):
            raise ValueError("context doit être un FillLookupContext")
        try:
            object.__setattr__(self, "outcome", FillEvidenceLookupOutcome(self.outcome))
        except ValueError as error:
            raise ValueError("outcome de fill invalide") from error
        fills = tuple(self.fills)
        candidates = tuple(self.venue_fill_id_candidates)
        if len(fills) != len(candidates):
            raise ValueError("un candidat tid doit correspondre à chaque fill")
        object.__setattr__(self, "fills", fills)
        object.__setattr__(self, "venue_fill_id_candidates", candidates)
        if isinstance(self.response_count, bool) or not isinstance(self.response_count, int):
            raise ValueError("response_count doit être un entier")
        if self.response_count < 0:
            raise ValueError("response_count ne peut pas être négatif")
        if self.response_limit != FILL_RESPONSE_LIMIT:
            raise ValueError("response_limit doit rester fixé à 2000")
        if self.retention_limit != FILL_RETENTION_LIMIT:
            raise ValueError("retention_limit doit rester fixé à 10000")
        if not isinstance(self.response_limit_reached, bool):
            raise ValueError("response_limit_reached doit être booléen")
        if self.response_limit_reached != (self.response_count >= self.response_limit):
            raise ValueError("response_limit_reached doit refléter response_count")
        if self.absence_authoritative:
            raise ValueError("l'absence d'un fill ne peut pas être autoritative")
        if self.reason is not None:
            object.__setattr__(self, "reason", _required_text(self.reason, "reason"))
        if not isinstance(self.retryable, bool):
            raise ValueError("retryable doit être booléen")

    @property
    def matched_count(self) -> int:
        return len(self.fills)


class ExternalFillEvidenceReader(Protocol):
    """Interface read-only pour une recherche de fills individuels."""

    def lookup_fills(
        self,
        context: FillLookupContext,
        *,
        observed_at: str | None = None,
    ) -> FillEvidenceLookup: ...


def _row_oids(
    trade: Mapping[str, Any],
) -> tuple[str | None, Mapping[str, Any]]:
    unified_oid = _identifier(trade.get("order"), "trade.order")
    raw_value = trade.get("info")
    if raw_value is None:
        info: Mapping[str, Any] = {}
    elif isinstance(raw_value, Mapping):
        info = raw_value
    else:
        if unified_oid is not None:
            raise _InvalidResponse("trade.info doit être un mapping")
        raise _IncompleteResponse("aucun oid exploitable dans trade/info")
    raw_oid = _identifier(info.get("oid"), "info.oid")
    if unified_oid is not None and raw_oid is not None and unified_oid != raw_oid:
        raise _ConflictingResponse("trade.order et info.oid contradictoires")
    return unified_oid if unified_oid is not None else raw_oid, info


def _normalize_target_trade(
    trade: Mapping[str, Any],
    info: Mapping[str, Any],
    context: FillLookupContext,
    external_order_id: str,
    *,
    observed_at: str,
) -> tuple[ExternalFill, str | None]:
    symbol = _optional_text(trade.get("symbol"), "trade.symbol")
    raw_coin = _optional_text(info.get("coin"), "info.coin")
    expected_coin = _instrument_coin(context.instrument)
    if symbol is not None and symbol != context.instrument:
        raise _ConflictingResponse("trade.symbol ne correspond pas à l'instrument attendu")
    if raw_coin is not None and raw_coin.upper() != expected_coin:
        raise _ConflictingResponse("info.coin ne correspond pas à l'instrument attendu")
    if symbol is None and raw_coin is None:
        raise _IncompleteResponse("instrument absent du fill")

    side = _unified_side(trade.get("side"))
    raw_side = _raw_side(info.get("side"))
    side = _merge_text(side, raw_side, "side")
    if side is None:
        raise _IncompleteResponse("side absent du fill")
    if side != context.side:
        raise _ConflictingResponse("side du fill différent du contexte")

    amount = _merge_numeric(
        _numeric_field(trade, "amount", positive=True),
        _numeric_field(info, "sz", positive=True),
        "amount/sz",
    )
    price = _merge_numeric(
        _numeric_field(trade, "price", positive=True),
        _numeric_field(info, "px", positive=True),
        "price/px",
    )
    if amount is None or price is None:
        raise _IncompleteResponse("amount/sz ou price/px absent du fill")

    timestamp = _merge_timestamp(
        _timestamp(trade.get("timestamp"), "trade.timestamp"),
        _timestamp(info.get("time"), "info.time"),
    )
    if timestamp is None:
        raise _IncompleteResponse("timestamp/time absent du fill")
    timestamp_ms, venue_event_at = timestamp
    if not context.start_time_ms <= timestamp_ms <= context.end_time_ms:
        raise _InvalidResponse("timestamp du fill hors de la fenêtre demandée")

    unified_fee: float | None = None
    unified_fee_asset: str | None = None
    if "fee" in trade and trade.get("fee") is not None:
        fee_value = trade["fee"]
        if not isinstance(fee_value, Mapping):
            raise _InvalidResponse("trade.fee doit être un mapping")
        unified_fee = _numeric_field(fee_value, "cost", signed=True)
        unified_fee_asset = _optional_text(fee_value.get("currency"), "fee.currency")
    raw_fee = _numeric_field(info, "fee", signed=True)
    raw_fee_asset = _optional_text(info.get("feeToken"), "feeToken")
    fee = _merge_numeric(unified_fee, raw_fee, "fee/cost")
    fee_asset = _merge_text(unified_fee_asset, raw_fee_asset, "fee asset")
    if fee is None and fee_asset is not None:
        raise _IncompleteResponse("feeToken observé sans fee exploitable")

    unified_tid = _identifier(trade.get("id"), "trade.id")
    raw_tid = _identifier(info.get("tid"), "info.tid")
    tid_candidate = unified_tid if unified_tid is not None else raw_tid
    if unified_tid is not None and raw_tid is not None and unified_tid != raw_tid:
        raise _ConflictingResponse("trade.id et info.tid contradictoires")

    unified_cloid = _identifier(trade.get("clientOrderId"), "trade.clientOrderId")
    raw_cloid = _identifier(
        info.get("cloid") if info.get("cloid") is not None else info.get("clientOrderId"),
        "info.cloid",
    )
    client_order_id = unified_cloid if unified_cloid is not None else raw_cloid
    if unified_cloid is not None and raw_cloid is not None and unified_cloid != raw_cloid:
        raise _ConflictingResponse("client order id unified/raw contradictoire")

    try:
        fill = ExternalFill(
            local_order_id=context.local_order_id,
            intent_id=context.intent_id,
            venue=context.venue,
            account_scope=context.account_scope,
            instrument=context.instrument,
            side=side,
            source_kind=ExternalEvidenceSource.FILL_LOOKUP,
            client_order_id=client_order_id,
            external_order_id=external_order_id,
            venue_fill_id=None,
            quantity=amount,
            price=price,
            fee=fee,
            fee_asset=fee_asset,
            venue_event_at=venue_event_at,
            observed_at=observed_at,
            raw_payload_hash=_fill_payload_hash(trade, info),
        )
    except (FillInvariantViolation, ValueError) as error:
        raise _InvalidResponse(f"fill normalisé invalide: {error}") from error
    return fill, tid_candidate


@dataclass(frozen=True)
class FillEvidencePersistenceResult:
    """Résultat de la persistance passive d'une tentative de fills."""

    fills: tuple[ExternalFill, ...]
    fill_created_flags: tuple[bool, ...]
    attempt_recorded: bool


class CcxtExternalFillEvidenceReader:
    """Lecteur CCXT sans retry, pagination, fallback ni effet métier."""

    def __init__(self, exchange: Any) -> None:
        self.exchange = exchange

    def lookup_fills(
        self,
        context: FillLookupContext,
        *,
        observed_at: str | None = None,
    ) -> FillEvidenceLookup:
        fetch_my_trades = getattr(self.exchange, "fetch_my_trades", None)
        if not callable(fetch_my_trades):
            return _lookup_result(
                context,
                FillEvidenceLookupOutcome.UNSUPPORTED,
                reason="L'exchange CCXT ne fournit pas fetch_my_trades",
            )
        observed = observed_at or datetime.now(UTC).isoformat()
        try:
            response = fetch_my_trades(
                None,
                context.start_time_ms,
                FILL_RESPONSE_LIMIT,
                {"until": context.end_time_ms, "aggregateByTime": False},
            )
        except (ccxt.NetworkError, ccxt.ExchangeNotAvailable, ccxt.RequestTimeout) as error:
            return _lookup_result(
                context,
                FillEvidenceLookupOutcome.TRANSPORT_FAILURE,
                reason=f"{type(error).__name__}: {error}",
                retryable=True,
            )
        except (ccxt.NotSupported, NotImplementedError) as error:
            return _lookup_result(
                context,
                FillEvidenceLookupOutcome.UNSUPPORTED,
                reason=f"{type(error).__name__}: {error}",
            )
        if not isinstance(response, list):
            return _lookup_result(
                context,
                FillEvidenceLookupOutcome.INVALID_RESPONSE,
                reason="fetch_my_trades n'a pas retourné une liste CCXT",
            )

        response_count = len(response)
        limit_reached = response_count >= FILL_RESPONSE_LIMIT
        fills: list[ExternalFill] = []
        candidates: list[str | None] = []
        try:
            expected_oid = context.expected_external_order_id
            for index, raw_trade in enumerate(response):
                if not isinstance(raw_trade, Mapping):
                    raise _InvalidResponse(f"trade[{index}] doit être un mapping")
                oid, info = _row_oids(raw_trade)
                if oid is None:
                    raise _IncompleteResponse(f"trade[{index}] ne contient aucun oid exploitable")
                if oid != expected_oid:
                    continue
                fill, tid_candidate = _normalize_target_trade(
                    raw_trade,
                    info,
                    context,
                    expected_oid,
                    observed_at=observed,
                )
                fills.append(fill)
                candidates.append(tid_candidate)
        except _ConflictingResponse as error:
            return _lookup_result(
                context,
                FillEvidenceLookupOutcome.CONFLICTING_RESPONSE,
                response_count=response_count,
                response_limit_reached=limit_reached,
                reason=str(error),
            )
        except _IncompleteResponse as error:
            return _lookup_result(
                context,
                FillEvidenceLookupOutcome.INCOMPLETE_RESPONSE,
                response_count=response_count,
                response_limit_reached=limit_reached,
                reason=str(error),
            )
        except _InvalidResponse as error:
            return _lookup_result(
                context,
                FillEvidenceLookupOutcome.INVALID_RESPONSE,
                response_count=response_count,
                response_limit_reached=limit_reached,
                reason=str(error),
            )

        if not fills:
            outcome = (
                FillEvidenceLookupOutcome.INCOMPLETE_RESPONSE
                if limit_reached
                else FillEvidenceLookupOutcome.NO_MATCH
            )
            reason = (
                "La réponse atteint la limite; l'absence de l'oid cible est non concluante"
                if limit_reached
                else "Aucun fill de la réponse ne correspond à l'oid cible"
            )
            return _lookup_result(
                context,
                outcome,
                response_count=response_count,
                response_limit_reached=limit_reached,
                reason=reason,
            )
        return _lookup_result(
            context,
            FillEvidenceLookupOutcome.FOUND,
            fills=tuple(fills),
            candidates=tuple(candidates),
            response_count=response_count,
            response_limit_reached=limit_reached,
        )


def _lookup_result(
    context: FillLookupContext,
    outcome: FillEvidenceLookupOutcome,
    *,
    fills: tuple[ExternalFill, ...] = (),
    candidates: tuple[str | None, ...] = (),
    response_count: int = 0,
    response_limit_reached: bool = False,
    reason: str | None = None,
    retryable: bool = False,
) -> FillEvidenceLookup:
    return FillEvidenceLookup(
        context=context,
        outcome=outcome,
        fills=fills,
        venue_fill_id_candidates=candidates,
        response_count=response_count,
        response_limit_reached=response_limit_reached,
        reason=reason,
        retryable=retryable,
    )


class ExternalFillEvidencePersistence:
    """Pont passif vers ``external_fills`` et le journal de lookup."""

    _EVENT_TYPES = {
        FillEvidenceLookupOutcome.FOUND: "external_fill_lookup_found",
        FillEvidenceLookupOutcome.NO_MATCH: "external_fill_lookup_no_match",
        FillEvidenceLookupOutcome.TRANSPORT_FAILURE: "external_fill_lookup_transport_failure",
        FillEvidenceLookupOutcome.UNSUPPORTED: "external_fill_lookup_unsupported",
        FillEvidenceLookupOutcome.INVALID_RESPONSE: "external_fill_lookup_invalid",
        FillEvidenceLookupOutcome.CONFLICTING_RESPONSE: "external_fill_lookup_conflict",
        FillEvidenceLookupOutcome.INCOMPLETE_RESPONSE: "external_fill_lookup_incomplete",
    }

    @classmethod
    def persist(
        cls,
        store: StateStore,
        lookup: FillEvidenceLookup,
    ) -> FillEvidencePersistenceResult:
        fills = lookup.fills if lookup.outcome == FillEvidenceLookupOutcome.FOUND else ()
        matched_fills = [
            {
                "raw_payload_hash": fill.raw_payload_hash,
                "reported_trade_id_candidate": candidate,
            }
            for fill, candidate in zip(fills, lookup.venue_fill_id_candidates, strict=True)
        ]
        context = lookup.context
        payload: dict[str, Any] = {
            "outcome": lookup.outcome.value,
            "reason": lookup.reason,
            "retryable": lookup.retryable,
            "local_order_id": context.local_order_id,
            "intent_id": context.intent_id,
            "venue": context.venue,
            "account_scope": context.account_scope,
            "instrument": context.instrument,
            "side": context.side,
            "engine": context.engine,
            "expected_client_order_id": context.expected_client_order_id,
            "expected_external_order_id": context.expected_external_order_id,
            "start_time_ms": context.start_time_ms,
            "end_time_ms": context.end_time_ms,
            "response_count": lookup.response_count,
            "matched_count": lookup.matched_count,
            "response_limit": lookup.response_limit,
            "response_limit_reached": lookup.response_limit_reached,
            "retention_limit": lookup.retention_limit,
            "absence_authoritative": lookup.absence_authoritative,
            "matched_fills": matched_fills,
        }
        persisted, created = store.persist_external_fill_lookup_evidence(
            fills=fills,
            engine=context.engine,
            aggregate_id=context.intent_id,
            payload=payload,
            event_type=cls._EVENT_TYPES[lookup.outcome],
        )
        return FillEvidencePersistenceResult(
            fills=tuple(persisted),
            fill_created_flags=tuple(created),
            attempt_recorded=True,
        )
