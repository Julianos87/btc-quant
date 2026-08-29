"""Lecture read-only et typée des preuves externes d'ordre.

Ce module est séparé de ``Broker`` et de ``recovery``. Le lecteur acquiert
une réponse CCXT sans la résoudre et sans écrire SQLite; la persistance
passive est un pont distinct.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol
from collections.abc import Mapping

import ccxt

from .errors import InvalidExternalObservation
from .external_evidence import ExternalEvidenceSource, ExternalOrderObservation
from .order_state import ExternalOrderState

if TYPE_CHECKING:
    from .state_store import StateStore


_CLOID_PATTERN = re.compile(r"0x[0-9a-f]{32}")
_HASH_FIELDS = (
    "id",
    "clientOrderId",
    "cloid",
    "status",
    "amount",
    "filled",
    "remaining",
    "average",
    "price",
    "timestamp",
    "lastTradeTimestamp",
    "datetime",
    "fee",
    "fees",
)
_ACTIVE_STATUSES = {"open", "new", "pending", "triggered", "untriggered"}


class EvidenceLookupOutcome(StrEnum):
    """Résultat explicite d'une lecture, sans confondre absence et erreur."""

    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    TRANSPORT_FAILURE = "TRANSPORT_FAILURE"
    UNSUPPORTED = "UNSUPPORTED"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    CONFLICTING_RESPONSE = "CONFLICTING_RESPONSE"
    INCOMPLETE_RESPONSE = "INCOMPLETE_RESPONSE"


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} doit être une chaîne non vide")
    return value.strip()


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    return _required_text(value, "valeur optionnelle")


def _canonical_timestamp(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} doit être une date ISO UTC non vide")
    candidate = value.strip()
    candidate = candidate[:-1] + "+00:00" if candidate[-1:] in {"Z", "z"} else candidate
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as error:
        raise ValueError(f"{field} doit être une date ISO UTC valide") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{field} doit inclure un fuseau UTC")
    return parsed.astimezone(UTC).isoformat()


def _finite_optional(value: object, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} doit être un nombre fini")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise ValueError(f"{field} doit être un nombre fini")
    return normalized


def _safe_hash_value(value: object) -> object:
    """Retourne une forme JSON canonique sans conserver le dictionnaire brut."""

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


def _payload_hash(
    order: Mapping[str, Any],
    returned_client_order_id: str | None,
    *,
    ccxt_status: str | None,
    venue_status: str | None,
) -> str:
    """Hash déterministe des champs CCXT utiles et non sensibles."""

    safe: dict[str, object] = {
        "returned_client_order_id": returned_client_order_id,
        "ccxt_status": ccxt_status,
        "venue_status": venue_status,
    }
    for field in _HASH_FIELDS:
        if field in order:
            safe[field] = _safe_hash_value(order[field])
    encoded = json.dumps(safe, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _venue_timestamp(order: Mapping[str, Any]) -> str | None:
    value = order.get("timestamp")
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("timestamp doit être une date ISO ou un timestamp milliseconde")
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)):
            raise ValueError("timestamp doit être fini")
        return datetime.fromtimestamp(float(value) / 1000.0, UTC).isoformat()
    if isinstance(value, str):
        return _canonical_timestamp(value, "venue_event_at")
    raise ValueError("timestamp doit être une date ISO ou un timestamp milliseconde")


def _returned_client_order_id(order: Mapping[str, Any]) -> str | None:
    value: object = order.get("clientOrderId")
    if value is None:
        value = order.get("cloid")
    if value is None:
        info = order.get("info")
        if isinstance(info, Mapping):
            value = info.get("cloid")
            if value is None:
                value = info.get("clientOrderId")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("clientOrderId retourné doit être une chaîne non vide")
    return value.strip()


def _ccxt_status(order: Mapping[str, Any]) -> str | None:
    if "status" not in order or order.get("status") is None:
        return None
    value = order["status"]
    if not isinstance(value, str) or not value.strip():
        raise ValueError("ccxt_status retourné doit être une chaîne non vide")
    return value


def _venue_status(order: Mapping[str, Any]) -> str | None:
    """Extrait uniquement le statut venue conservé dans ``info.status``."""

    info = order.get("info")
    if not isinstance(info, Mapping) or info.get("status") is None:
        return None
    value = info["status"]
    if not isinstance(value, str) or not value.strip():
        raise ValueError("venue_status conservé doit être une chaîne non vide")
    return value


def _normalization_status(
    ccxt_status: str | None,
    requested_qty: float | None,
    filled_qty: float | None,
    remaining_qty: float | None,
    *,
    contradictory: bool,
) -> ExternalOrderState:
    if contradictory or ccxt_status is None:
        return ExternalOrderState.UNKNOWN
    status = ccxt_status.strip().lower()
    if requested_qty is None or filled_qty is None or remaining_qty is None:
        return ExternalOrderState.UNKNOWN
    tolerance = max(1e-9, abs(requested_qty) * 1e-9)
    if status in _ACTIVE_STATUSES:
        if remaining_qty <= tolerance:
            return ExternalOrderState.UNKNOWN
        return (
            ExternalOrderState.PARTIAL_OPEN if filled_qty > tolerance else ExternalOrderState.OPEN
        )
    if status == "closed":
        if remaining_qty > tolerance:
            return ExternalOrderState.UNKNOWN
        if filled_qty >= requested_qty - tolerance:
            return ExternalOrderState.FILLED
        if filled_qty > tolerance:
            return ExternalOrderState.PARTIAL_TERMINAL
        return ExternalOrderState.UNKNOWN
    if status in {"canceled", "cancelled"}:
        return ExternalOrderState.CANCELED
    if status == "rejected":
        return ExternalOrderState.REJECTED
    if status == "expired":
        return ExternalOrderState.EXPIRED
    return ExternalOrderState.UNKNOWN


def _contradictory_quantities(
    requested_qty: float | None,
    filled_qty: float | None,
    remaining_qty: float | None,
    *,
    ccxt_status: str | None,
    remaining_explicit: bool,
) -> bool:
    if any(value is not None and value < 0 for value in (requested_qty, filled_qty, remaining_qty)):
        return True
    if requested_qty is None:
        return False
    tolerance = max(1e-9, requested_qty * 1e-9)
    if requested_qty <= 0:
        return True
    if filled_qty is not None and filled_qty > requested_qty + tolerance:
        return True
    if (
        ccxt_status is not None
        and ccxt_status.strip().lower() in _ACTIVE_STATUSES
        and (
            filled_qty is not None
            and remaining_qty is not None
            and abs(filled_qty + remaining_qty - requested_qty) > tolerance
        )
    ):
        return True
    if ccxt_status is not None and ccxt_status.strip().lower() in _ACTIVE_STATUSES:
        if filled_qty is not None and remaining_qty is not None and remaining_qty <= tolerance:
            return True
        if (
            filled_qty is not None
            and remaining_qty is None
            and requested_qty - filled_qty <= tolerance
        ):
            return True
        if remaining_explicit and remaining_qty is not None and remaining_qty <= tolerance:
            return True
    return False


@dataclass(frozen=True)
class OrderLookupContext:
    """Contexte local, explicitement distinct du cloid externe."""

    local_order_id: int
    intent_id: str
    venue: str
    account_scope: str
    instrument: str
    side: str
    expected_client_order_id: str
    requested_qty: float | None = None
    engine: str = "execution"

    def __post_init__(self) -> None:
        if isinstance(self.local_order_id, bool) or not isinstance(self.local_order_id, int):
            raise ValueError("local_order_id doit être un entier")
        if self.local_order_id <= 0:
            raise ValueError("local_order_id doit être strictement positif")
        for field in ("intent_id", "venue", "account_scope", "instrument", "engine"):
            object.__setattr__(self, field, _required_text(getattr(self, field), field))
        side = _required_text(self.side, "side").upper()
        if side not in {"BUY", "SELL"}:
            raise ValueError("side doit être BUY ou SELL")
        object.__setattr__(self, "side", side)
        expected = _required_text(self.expected_client_order_id, "expected_client_order_id")
        if self.venue.strip().lower() == "hyperliquid" and not _CLOID_PATTERN.fullmatch(expected):
            raise ValueError("expected_client_order_id doit être un cloid Hyperliquid canonique")
        object.__setattr__(self, "expected_client_order_id", expected)
        if self.requested_qty is not None:
            requested = _finite_optional(self.requested_qty, "requested_qty")
            if requested is None or requested <= 0:
                raise ValueError("requested_qty doit être strictement positive")
            object.__setattr__(self, "requested_qty", requested)


@dataclass(frozen=True)
class ExternalOrderEvidence:
    """Preuve immuable, avec statut brut et explicitation des quantités."""

    local_order_id: int
    intent_id: str
    venue: str
    account_scope: str
    instrument: str
    side: str
    engine: str
    expected_client_order_id: str
    returned_client_order_id: str | None
    external_order_id: str | None
    ccxt_status: str | None
    venue_status: str | None
    normalized_state: ExternalOrderState
    requested_qty: float | None
    filled_qty: float | None
    remaining_qty: float | None
    requested_qty_explicit: bool
    filled_qty_explicit: bool
    remaining_qty_explicit: bool
    source_kind: ExternalEvidenceSource
    venue_event_at: str | None
    observed_at: str
    raw_payload_hash: str
    correlation_complete: bool
    quantities_complete: bool
    contradictory: bool

    def __post_init__(self) -> None:
        if isinstance(self.local_order_id, bool) or not isinstance(self.local_order_id, int):
            raise ValueError("local_order_id doit être un entier")
        if self.local_order_id <= 0:
            raise ValueError("local_order_id doit être strictement positif")
        for field in (
            "intent_id",
            "venue",
            "account_scope",
            "instrument",
            "engine",
            "expected_client_order_id",
        ):
            object.__setattr__(self, field, _required_text(getattr(self, field), field))
        side = _required_text(self.side, "side").upper()
        if side not in {"BUY", "SELL"}:
            raise ValueError("side doit être BUY ou SELL")
        object.__setattr__(self, "side", side)
        object.__setattr__(
            self, "returned_client_order_id", _optional_text(self.returned_client_order_id)
        )
        object.__setattr__(self, "external_order_id", _optional_text(self.external_order_id))
        for field in ("ccxt_status", "venue_status"):
            value = getattr(self, field)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{field} doit rester une chaîne ou None")
        try:
            object.__setattr__(self, "normalized_state", ExternalOrderState(self.normalized_state))
            object.__setattr__(self, "source_kind", ExternalEvidenceSource(self.source_kind))
        except ValueError as error:
            raise ValueError("normalized_state ou source_kind invalide") from error
        for field in ("requested_qty", "filled_qty", "remaining_qty"):
            object.__setattr__(self, field, _finite_optional(getattr(self, field), field))
        for value, explicit, field in (
            (self.requested_qty, self.requested_qty_explicit, "requested_qty"),
            (self.filled_qty, self.filled_qty_explicit, "filled_qty"),
            (self.remaining_qty, self.remaining_qty_explicit, "remaining_qty"),
        ):
            if not isinstance(explicit, bool):
                raise ValueError(f"{field}_explicit doit être booléen")
            if value is None and explicit:
                raise ValueError(f"{field}_explicit ne peut pas être vrai sans valeur")
        if self.venue_event_at is not None:
            object.__setattr__(
                self, "venue_event_at", _canonical_timestamp(self.venue_event_at, "venue_event_at")
            )
        object.__setattr__(
            self, "observed_at", _canonical_timestamp(self.observed_at, "observed_at")
        )
        if not isinstance(self.raw_payload_hash, str) or not re.fullmatch(
            r"[0-9a-f]{64}", self.raw_payload_hash
        ):
            raise ValueError("raw_payload_hash doit être un SHA-256 hexadécimal")


@dataclass(frozen=True)
class OrderEvidenceLookup:
    context: OrderLookupContext
    outcome: EvidenceLookupOutcome
    evidence: ExternalOrderEvidence | None = None
    reason: str | None = None
    retryable: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.context, OrderLookupContext):
            raise ValueError("context doit être un OrderLookupContext")
        try:
            object.__setattr__(self, "outcome", EvidenceLookupOutcome(self.outcome))
        except ValueError as error:
            raise ValueError("outcome de lecture invalide") from error
        if self.reason is not None:
            object.__setattr__(self, "reason", _required_text(self.reason, "reason"))
        if not isinstance(self.retryable, bool):
            raise ValueError("retryable doit être booléen")


class ExternalEvidenceReader(Protocol):
    """Interface strictement read-only: une seule lookup par invocation."""

    def lookup_order(
        self,
        context: OrderLookupContext,
        *,
        observed_at: str | None = None,
    ) -> OrderEvidenceLookup: ...


class CcxtExternalEvidenceReader:
    """Lecteur CCXT sans retry, sans écriture et sans effet de trading."""

    def __init__(self, exchange: Any, *, exchange_id: str = "hyperliquid") -> None:
        self.exchange = exchange
        self.exchange_id = exchange_id

    def lookup_order(
        self,
        context: OrderLookupContext,
        *,
        observed_at: str | None = None,
    ) -> OrderEvidenceLookup:
        fetch_order = getattr(self.exchange, "fetch_order", None)
        if not callable(fetch_order):
            return OrderEvidenceLookup(
                context=context,
                outcome=EvidenceLookupOutcome.UNSUPPORTED,
                reason="L'exchange CCXT ne fournit pas fetch_order",
            )
        params = (
            {"clientOrderId": context.expected_client_order_id}
            if self.exchange_id == "hyperliquid"
            else {"origClientOrderId": context.expected_client_order_id}
        )
        try:
            order = fetch_order(context.expected_client_order_id, context.instrument, params)
        except ccxt.OrderNotFound:
            return OrderEvidenceLookup(context, EvidenceLookupOutcome.NOT_FOUND)
        except (ccxt.NetworkError, ccxt.ExchangeNotAvailable, ccxt.RequestTimeout) as error:
            return OrderEvidenceLookup(
                context,
                EvidenceLookupOutcome.TRANSPORT_FAILURE,
                reason=f"{type(error).__name__}: {error}",
                retryable=True,
            )
        except (ccxt.NotSupported, NotImplementedError) as error:
            return OrderEvidenceLookup(
                context,
                EvidenceLookupOutcome.UNSUPPORTED,
                reason=f"{type(error).__name__}: {error}",
            )
        if not isinstance(order, Mapping):
            return OrderEvidenceLookup(
                context,
                EvidenceLookupOutcome.INVALID_RESPONSE,
                reason="fetch_order n'a pas retourné un mapping CCXT",
            )
        observed = observed_at or datetime.now(UTC).isoformat()
        try:
            returned_client_order_id = _returned_client_order_id(order)
            ccxt_status = _ccxt_status(order)
            venue_status = _venue_status(order)
            requested_qty, requested_explicit = _numeric_field(order, "amount")
            filled_qty, filled_explicit = _numeric_field(order, "filled")
            remaining_qty, remaining_explicit = _numeric_field(order, "remaining")
            if (
                remaining_qty is None
                and ccxt_status is not None
                and ccxt_status.strip().lower() in _ACTIVE_STATUSES
            ):
                if requested_qty is not None and filled_qty is not None:
                    remaining_qty = requested_qty - filled_qty
                    remaining_explicit = False
            contradictory = _contradictory_quantities(
                requested_qty,
                filled_qty,
                remaining_qty,
                ccxt_status=ccxt_status,
                remaining_explicit=remaining_explicit,
            )
            normalized_state = _normalization_status(
                ccxt_status,
                requested_qty,
                filled_qty,
                remaining_qty,
                contradictory=contradictory,
            )
            external_order_id = order.get("id")
            if external_order_id is not None and (
                isinstance(external_order_id, bool) or not isinstance(external_order_id, (str, int))
            ):
                raise ValueError("id retourné doit être textuel ou numérique")
            evidence = ExternalOrderEvidence(
                local_order_id=context.local_order_id,
                intent_id=context.intent_id,
                venue=context.venue,
                account_scope=context.account_scope,
                instrument=context.instrument,
                side=context.side,
                engine=context.engine,
                expected_client_order_id=context.expected_client_order_id,
                returned_client_order_id=returned_client_order_id,
                external_order_id=str(external_order_id) if external_order_id is not None else None,
                ccxt_status=ccxt_status,
                venue_status=venue_status,
                normalized_state=normalized_state,
                requested_qty=requested_qty,
                filled_qty=filled_qty,
                remaining_qty=remaining_qty,
                requested_qty_explicit=requested_explicit,
                filled_qty_explicit=filled_explicit,
                remaining_qty_explicit=remaining_explicit,
                source_kind=ExternalEvidenceSource.ORDER_LOOKUP,
                venue_event_at=_venue_timestamp(order),
                observed_at=observed,
                raw_payload_hash=_payload_hash(
                    order,
                    returned_client_order_id,
                    ccxt_status=ccxt_status,
                    venue_status=venue_status,
                ),
                correlation_complete=returned_client_order_id == context.expected_client_order_id,
                quantities_complete=(
                    requested_qty is not None
                    and filled_qty is not None
                    and remaining_qty is not None
                ),
                contradictory=contradictory,
            )
        except (ValueError, TypeError, OverflowError) as error:
            return OrderEvidenceLookup(
                context,
                EvidenceLookupOutcome.INVALID_RESPONSE,
                reason=f"Réponse CCXT invalide: {error}",
            )
        if evidence.returned_client_order_id is None:
            return OrderEvidenceLookup(
                context,
                EvidenceLookupOutcome.INCOMPLETE_RESPONSE,
                evidence=evidence,
                reason="Le cloid retourné est absent; il ne peut pas être fabriqué",
            )
        if evidence.returned_client_order_id != context.expected_client_order_id:
            return OrderEvidenceLookup(
                context,
                EvidenceLookupOutcome.CONFLICTING_RESPONSE,
                evidence=evidence,
                reason="Le cloid retourné diffère du cloid attendu",
            )
        if (
            evidence.ccxt_status is None
            or not evidence.quantities_complete
            or evidence.contradictory
        ):
            return OrderEvidenceLookup(
                context,
                EvidenceLookupOutcome.INCOMPLETE_RESPONSE
                if not evidence.contradictory
                else EvidenceLookupOutcome.INVALID_RESPONSE,
                evidence=evidence,
                reason=(
                    "La réponse ne contient pas toutes les quantités/statuts nécessaires"
                    if not evidence.contradictory
                    else "La réponse contient des quantités contradictoires"
                ),
            )
        return OrderEvidenceLookup(context, EvidenceLookupOutcome.FOUND, evidence=evidence)


def _numeric_field(order: Mapping[str, Any], field: str) -> tuple[float | None, bool]:
    raw = order.get(field)
    if raw is None:
        return None, False
    return _finite_optional(raw, field), True


@dataclass(frozen=True)
class EvidencePersistenceResult:
    observation: ExternalOrderObservation | None
    observation_created: bool
    attempt_recorded: bool


class ExternalEvidencePersistence:
    """Pont passif vers observations et journal; aucun état métier n'est touché."""

    @staticmethod
    def persist(store: StateStore, lookup: OrderEvidenceLookup) -> EvidencePersistenceResult:
        evidence = lookup.evidence
        observation: ExternalOrderObservation | None = None
        observation_created = False
        persistence_note: str | None = None
        if lookup.outcome == EvidenceLookupOutcome.FOUND and evidence is not None:
            try:
                if (
                    evidence.correlation_complete
                    and evidence.quantities_complete
                    and not evidence.contradictory
                    and evidence.requested_qty is not None
                    and evidence.filled_qty is not None
                    and evidence.remaining_qty is not None
                ):
                    observation = ExternalOrderObservation(
                        local_order_id=evidence.local_order_id,
                        intent_id=evidence.intent_id,
                        venue=evidence.venue,
                        account_scope=evidence.account_scope,
                        instrument=evidence.instrument,
                        side=evidence.side,
                        source_kind=evidence.source_kind,
                        normalized_external_status=evidence.normalized_state,
                        requested_qty=evidence.requested_qty,
                        cumulative_filled_qty=evidence.filled_qty,
                        remaining_qty=evidence.remaining_qty,
                        client_order_id=evidence.returned_client_order_id,
                        external_order_id=evidence.external_order_id,
                        venue_event_at=evidence.venue_event_at,
                        observed_at=evidence.observed_at,
                        raw_payload_hash=evidence.raw_payload_hash,
                    )
                else:
                    persistence_note = "FOUND non représentable comme observation de confiance"
            except (InvalidExternalObservation, ValueError) as error:
                persistence_note = f"Observation normalisée refusée: {error}"
        event_type = {
            EvidenceLookupOutcome.FOUND: "external_order_lookup_found",
            EvidenceLookupOutcome.NOT_FOUND: "external_order_lookup_not_found",
            EvidenceLookupOutcome.TRANSPORT_FAILURE: "external_order_lookup_transport_failure",
            EvidenceLookupOutcome.UNSUPPORTED: "external_order_lookup_unsupported",
            EvidenceLookupOutcome.INVALID_RESPONSE: "external_order_lookup_invalid",
            EvidenceLookupOutcome.CONFLICTING_RESPONSE: "external_order_lookup_conflict",
            EvidenceLookupOutcome.INCOMPLETE_RESPONSE: "external_order_lookup_incomplete",
        }[lookup.outcome]
        payload: dict[str, Any] = {
            "outcome": lookup.outcome.value,
            "reason": lookup.reason,
            "retryable": lookup.retryable,
            "local_order_id": lookup.context.local_order_id,
            "intent_id": lookup.context.intent_id,
            "venue": lookup.context.venue,
            "account_scope": lookup.context.account_scope,
            "instrument": lookup.context.instrument,
            "side": lookup.context.side,
            "engine": lookup.context.engine,
            "expected_client_order_id": lookup.context.expected_client_order_id,
        }
        if evidence is not None:
            payload.update(
                {
                    "returned_client_order_id": evidence.returned_client_order_id,
                    "external_order_id": evidence.external_order_id,
                    "ccxt_status": evidence.ccxt_status,
                    "venue_status": evidence.venue_status,
                    "normalized_state": evidence.normalized_state.value,
                    "requested_qty": evidence.requested_qty,
                    "filled_qty": evidence.filled_qty,
                    "remaining_qty": evidence.remaining_qty,
                    "requested_qty_explicit": evidence.requested_qty_explicit,
                    "filled_qty_explicit": evidence.filled_qty_explicit,
                    "remaining_qty_explicit": evidence.remaining_qty_explicit,
                    "source_kind": evidence.source_kind.value,
                    "venue_event_at": evidence.venue_event_at,
                    "observed_at": evidence.observed_at,
                    "raw_payload_hash": evidence.raw_payload_hash,
                    "correlation_complete": evidence.correlation_complete,
                    "quantities_complete": evidence.quantities_complete,
                    "contradictory": evidence.contradictory,
                }
            )
        if persistence_note is not None:
            payload["persistence_note"] = persistence_note
        if observation is not None:
            observation, observation_created = store.append_external_order_observation(observation)
        store.append_external_order_lookup_attempt(
            engine=lookup.context.engine,
            aggregate_id=lookup.context.intent_id,
            payload=payload,
            event_type=event_type,
        )
        return EvidencePersistenceResult(observation, observation_created, True)
