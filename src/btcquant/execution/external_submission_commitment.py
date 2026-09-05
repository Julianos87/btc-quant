"""Durable, typed evidence for an external order submission response.

The contracts in this module are deliberately passive.  They do not submit
orders, call an exchange, or decide whether a retry is safe.  A filled IOC
response can commit the venue's order-level quantity and average price, but it
does not manufacture individual fills or prove fee completeness.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any


SUBMISSION_RESPONSE_CONTRACT_VERSION = 1
SUBMISSION_RESPONSE_EVENT_TYPE = "external_submission_response"
SUBMISSION_RESPONSE_AGGREGATE_TYPE = "external_submission_response"


class ExternalSubmissionOutcome(StrEnum):
    FILLED_COMMITMENT = "FILLED_COMMITMENT"
    RESTING_ACCEPTED = "RESTING_ACCEPTED"
    DETERMINISTIC_ORDER_ERROR = "DETERMINISTIC_ORDER_ERROR"
    AMBIGUOUS_TRANSPORT_FAILURE = "AMBIGUOUS_TRANSPORT_FAILURE"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    CONFLICTING_RESPONSE = "CONFLICTING_RESPONSE"
    EXTERNAL_IOC_RESTING_CONFLICT = "EXTERNAL_IOC_RESTING_CONFLICT"


class SubmissionCommitmentError(ValueError):
    """Invalid or contradictory external submission evidence."""


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SubmissionCommitmentError(f"{field} must be a non-empty string")
    return value.strip()


_SENSITIVE_KEY_MARKERS = (
    "api_key",
    "apikey",
    "password",
    "private_key",
    "privatekey",
    "secret",
    "signature",
    "token",
)


def _assert_no_sensitive_keys(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if any(marker in normalized for marker in _SENSITIVE_KEY_MARKERS):
                raise SubmissionCommitmentError("raw response contains a sensitive field")
            _assert_no_sensitive_keys(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_no_sensitive_keys(item)


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


def _decimal(value: object, field: str, *, positive: bool = False) -> str:
    # Decimal is intentionally represented canonically as text at this
    # boundary.  Floats are accepted only as an input convenience; no binary
    # float participates in the commitment identity or comparison.
    from decimal import Decimal, InvalidOperation

    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise SubmissionCommitmentError(f"{field} must be numeric")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise SubmissionCommitmentError(f"{field} must be numeric") from error
    if not number.is_finite() or (positive and number <= 0):
        qualifier = "strictly positive" if positive else "finite"
        raise SubmissionCommitmentError(f"{field} must be {qualifier}")
    if number == 0:
        return "0"
    normalized = format(number.normalize(), "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized or "0"


def _timestamp(value: object, field: str) -> str:
    candidate = _text(value, field)
    if candidate[-1:] in {"Z", "z"}:
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as error:
        raise SubmissionCommitmentError(f"{field} must be ISO 8601 with timezone") from error
    if parsed.tzinfo is None:
        raise SubmissionCommitmentError(f"{field} must contain an explicit timezone")
    return parsed.astimezone(UTC).isoformat()


def _hash_payload(value: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(
            _plain(value), ensure_ascii=True, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise SubmissionCommitmentError("raw response is not JSON serializable") from error
    return hashlib.sha256(encoded).hexdigest()


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _hash(value: Mapping[str, Any]) -> str:
    return _hash_payload(value)


@dataclass(frozen=True)
class AuthoritativeSubmissionFillCommitment:
    """Order-level quantity commitment from one exact venue response.

    ``total_filled_qty`` and ``average_price`` are Decimal-compatible strings
    after construction.  This is positive evidence for the aggregate size and
    VWAP of the accepted order only; it is not an individual-fill record.
    """

    local_order_id: int
    intent_id: str
    venue: str
    environment: str
    account_scope: str
    instrument: str
    side: str
    client_order_id: str
    external_order_id: str
    total_filled_qty: str
    average_price: str
    response_acquired_at: str
    raw_response_hash: str
    response_type: str = "filled"

    def __post_init__(self) -> None:
        if (
            isinstance(self.local_order_id, bool)
            or not isinstance(self.local_order_id, int)
            or self.local_order_id <= 0
        ):
            raise SubmissionCommitmentError("local_order_id must be positive")
        for field in (
            "intent_id",
            "venue",
            "environment",
            "account_scope",
            "instrument",
            "client_order_id",
            "external_order_id",
            "response_type",
        ):
            object.__setattr__(self, field, _text(getattr(self, field), field))
        side = _text(self.side, "side").upper()
        if side not in {"BUY", "SELL"}:
            raise SubmissionCommitmentError("side must be BUY or SELL")
        object.__setattr__(self, "side", side)
        object.__setattr__(
            self,
            "total_filled_qty",
            _decimal(self.total_filled_qty, "total_filled_qty", positive=True),
        )
        object.__setattr__(
            self, "average_price", _decimal(self.average_price, "average_price", positive=True)
        )
        object.__setattr__(
            self,
            "response_acquired_at",
            _timestamp(self.response_acquired_at, "response_acquired_at"),
        )
        raw_hash = _text(self.raw_response_hash, "raw_response_hash").lower()
        if len(raw_hash) != 64 or any(char not in "0123456789abcdef" for char in raw_hash):
            raise SubmissionCommitmentError("raw_response_hash must be a SHA-256 hex digest")
        object.__setattr__(self, "raw_response_hash", raw_hash)

    @property
    def submission_key(self) -> str:
        # One durable response slot per exact local intent.  A different
        # response for that slot is a conflict, never a second commitment.
        identity = {
            "account_scope": self.account_scope,
            "client_order_id": self.client_order_id,
            "environment": self.environment,
            "instrument": self.instrument,
            "intent_id": self.intent_id,
            "local_order_id": self.local_order_id,
            "side": self.side,
            "venue": self.venue,
            "version": SUBMISSION_RESPONSE_CONTRACT_VERSION,
        }
        return "submission-" + _hash(identity)

    def binding_content(self) -> dict[str, Any]:
        return {
            "account_scope": self.account_scope,
            "client_order_id": self.client_order_id,
            "environment": self.environment,
            "instrument": self.instrument,
            "intent_id": self.intent_id,
            "local_order_id": self.local_order_id,
            "side": self.side,
            "venue": self.venue,
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "contract_version": SUBMISSION_RESPONSE_CONTRACT_VERSION,
            "submission_key": self.submission_key,
            "response_type": self.response_type,
            **self.binding_content(),
            "external_order_id": self.external_order_id,
            "total_filled_qty": self.total_filled_qty,
            "average_price": self.average_price,
            "response_acquired_at": self.response_acquired_at,
            "raw_response_hash": self.raw_response_hash,
        }


@dataclass(frozen=True)
class ExternalSubmissionResponse:
    """Immutable durable response envelope, including non-filled outcomes."""

    local_order_id: int
    intent_id: str
    venue: str
    environment: str
    account_scope: str
    instrument: str
    side: str
    client_order_id: str
    response_acquired_at: str
    outcome: ExternalSubmissionOutcome | str
    raw_payload: Mapping[str, Any]
    raw_response_hash: str
    commitment: AuthoritativeSubmissionFillCommitment | None = None
    structured_error: str | None = None
    ambiguity_classification: str = "NONE"
    submission_key: str | None = None

    def __post_init__(self) -> None:
        _assert_no_sensitive_keys(self.raw_payload)
        if (
            isinstance(self.local_order_id, bool)
            or not isinstance(self.local_order_id, int)
            or self.local_order_id <= 0
        ):
            raise SubmissionCommitmentError("local_order_id must be positive")
        for field in (
            "intent_id",
            "venue",
            "environment",
            "account_scope",
            "instrument",
            "client_order_id",
            "ambiguity_classification",
        ):
            object.__setattr__(self, field, _text(getattr(self, field), field))
        side = _text(self.side, "side").upper()
        if side not in {"BUY", "SELL"}:
            raise SubmissionCommitmentError("side must be BUY or SELL")
        object.__setattr__(self, "side", side)
        try:
            outcome = ExternalSubmissionOutcome(self.outcome)
        except ValueError as error:
            raise SubmissionCommitmentError("unknown submission outcome") from error
        object.__setattr__(self, "outcome", outcome)
        if not isinstance(self.raw_payload, Mapping):
            raise SubmissionCommitmentError("raw_payload must be an object")
        frozen_payload = _freeze(dict(self.raw_payload))
        object.__setattr__(self, "raw_payload", frozen_payload)
        calculated_hash = _hash_payload(dict(self.raw_payload))
        supplied_hash = _text(self.raw_response_hash, "raw_response_hash").lower()
        if supplied_hash != calculated_hash:
            raise SubmissionCommitmentError("raw_response_hash does not match raw_payload")
        object.__setattr__(self, "raw_response_hash", supplied_hash)
        acquired = _timestamp(self.response_acquired_at, "response_acquired_at")
        object.__setattr__(self, "response_acquired_at", acquired)
        if self.commitment is not None:
            commitment = self.commitment
            if not isinstance(commitment, AuthoritativeSubmissionFillCommitment):
                raise SubmissionCommitmentError("invalid fill commitment")
            expected = {
                "local_order_id": self.local_order_id,
                "intent_id": self.intent_id,
                "venue": self.venue,
                "environment": self.environment,
                "account_scope": self.account_scope,
                "instrument": self.instrument,
                "side": self.side,
                "client_order_id": self.client_order_id,
                "response_acquired_at": self.response_acquired_at,
                "raw_response_hash": self.raw_response_hash,
            }
            if any(getattr(commitment, name) != value for name, value in expected.items()):
                raise SubmissionCommitmentError("submission commitment binding conflict")
            if outcome != ExternalSubmissionOutcome.FILLED_COMMITMENT:
                raise SubmissionCommitmentError("only a filled response may carry a commitment")
        elif outcome == ExternalSubmissionOutcome.FILLED_COMMITMENT:
            raise SubmissionCommitmentError("filled response requires a commitment")
        if self.structured_error is not None:
            object.__setattr__(
                self, "structured_error", _text(self.structured_error, "structured_error")
            )
        identity = {
            "account_scope": self.account_scope,
            "client_order_id": self.client_order_id,
            "environment": self.environment,
            "instrument": self.instrument,
            "intent_id": self.intent_id,
            "local_order_id": self.local_order_id,
            "side": self.side,
            "venue": self.venue,
            "version": SUBMISSION_RESPONSE_CONTRACT_VERSION,
        }
        derived_key = "submission-" + _hash(identity)
        if self.commitment is not None and self.commitment.submission_key != derived_key:
            raise SubmissionCommitmentError("submission commitment key is inconsistent")
        key = self.submission_key or derived_key
        if key != derived_key:
            raise SubmissionCommitmentError("submission_key is inconsistent with binding")
        object.__setattr__(self, "submission_key", _text(key, "submission_key"))

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> ExternalSubmissionResponse:
        """Reconstruct and validate one durable response envelope."""

        if not isinstance(payload, Mapping):
            raise SubmissionCommitmentError("submission response payload must be an object")
        if payload.get("contract_version") != SUBMISSION_RESPONSE_CONTRACT_VERSION:
            raise SubmissionCommitmentError("unsupported submission response contract version")
        commitment_payload = payload.get("commitment")
        commitment: AuthoritativeSubmissionFillCommitment | None = None
        if commitment_payload is not None:
            if not isinstance(commitment_payload, Mapping):
                raise SubmissionCommitmentError("submission commitment payload must be an object")
            try:
                commitment = AuthoritativeSubmissionFillCommitment(
                    local_order_id=commitment_payload["local_order_id"],
                    intent_id=commitment_payload["intent_id"],
                    venue=commitment_payload["venue"],
                    environment=commitment_payload["environment"],
                    account_scope=commitment_payload["account_scope"],
                    instrument=commitment_payload["instrument"],
                    side=commitment_payload["side"],
                    client_order_id=commitment_payload["client_order_id"],
                    external_order_id=commitment_payload["external_order_id"],
                    total_filled_qty=commitment_payload["total_filled_qty"],
                    average_price=commitment_payload["average_price"],
                    response_acquired_at=commitment_payload["response_acquired_at"],
                    raw_response_hash=commitment_payload["raw_response_hash"],
                    response_type=commitment_payload.get("response_type", "filled"),
                )
            except KeyError as error:
                raise SubmissionCommitmentError(
                    f"submission commitment field missing: {error.args[0]}"
                ) from error
        try:
            raw_payload = payload["raw_payload"]
            response = cls(
                local_order_id=payload["local_order_id"],
                intent_id=payload["intent_id"],
                venue=payload["venue"],
                environment=payload["environment"],
                account_scope=payload["account_scope"],
                instrument=payload["instrument"],
                side=payload["side"],
                client_order_id=payload["client_order_id"],
                response_acquired_at=payload["response_acquired_at"],
                outcome=payload["outcome"],
                raw_payload=raw_payload,
                raw_response_hash=payload["raw_response_hash"],
                commitment=commitment,
                structured_error=payload.get("structured_error"),
                ambiguity_classification=payload.get("ambiguity_classification", "NONE"),
                submission_key=payload.get("submission_key"),
            )
        except KeyError as error:
            raise SubmissionCommitmentError(
                f"submission response field missing: {error.args[0]}"
            ) from error
        return response

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "contract_version": SUBMISSION_RESPONSE_CONTRACT_VERSION,
            "submission_key": self.submission_key,
            "local_order_id": self.local_order_id,
            "intent_id": self.intent_id,
            "venue": self.venue,
            "environment": self.environment,
            "account_scope": self.account_scope,
            "instrument": self.instrument,
            "side": self.side,
            "client_order_id": self.client_order_id,
            "response_acquired_at": self.response_acquired_at,
            "outcome": ExternalSubmissionOutcome(self.outcome).value,
            "raw_response_hash": self.raw_response_hash,
            "raw_payload": _plain(self.raw_payload),
            "structured_error": self.structured_error,
            "ambiguity_classification": self.ambiguity_classification,
            "commitment": self.commitment.to_payload() if self.commitment else None,
        }
        return payload


def _filled_status_payload(raw_payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Extract the official Hyperliquid filled status without guessing.

    CCXT keeps the venue response under ``info.response.data.statuses``.  The
    strict path prevents a similarly named user field elsewhere in a response
    from becoming a financial commitment.
    """

    info = raw_payload.get("info")
    if not isinstance(info, Mapping):
        return None
    response = info.get("response")
    if not isinstance(response, Mapping):
        return None
    data = response.get("data")
    if not isinstance(data, Mapping):
        return None
    statuses = data.get("statuses")
    if not isinstance(statuses, list) or len(statuses) != 1:
        return None
    status = statuses[0]
    if not isinstance(status, Mapping):
        return None
    filled = status.get("filled")
    return filled if isinstance(filled, Mapping) else None


def build_submission_response(
    *,
    local_order_id: int,
    intent_id: str,
    venue: str,
    environment: str,
    account_scope: str,
    instrument: str,
    side: str,
    client_order_id: str,
    raw_payload: Mapping[str, Any] | None,
    response_acquired_at: str,
    ioc_expected: bool = False,
    structured_error: str | None = None,
) -> ExternalSubmissionResponse:
    """Classify one already-returned broker response; never performs I/O."""

    payload = (
        dict(raw_payload)
        if raw_payload is not None
        else {
            "missing_raw_response": True,
            "error": structured_error or "BROKER_RESPONSE_RAW_PAYLOAD_MISSING",
        }
    )
    status = str(payload.get("status") or "").strip().lower()
    filled = _filled_status_payload(payload)
    commitment: AuthoritativeSubmissionFillCommitment | None = None
    outcome: ExternalSubmissionOutcome
    error_text = structured_error
    if status in {"closed", "filled"} and filled is not None:
        oid = filled.get("oid")
        total = filled.get("totalSz")
        average = filled.get("avgPx")
        try:
            raw_hash = _hash_payload(payload)
            commitment = AuthoritativeSubmissionFillCommitment(
                local_order_id=local_order_id,
                intent_id=intent_id,
                venue=venue,
                environment=environment,
                account_scope=account_scope,
                instrument=instrument,
                side=side,
                client_order_id=client_order_id,
                external_order_id=str(oid),
                total_filled_qty=_decimal(total, "total_filled_qty", positive=True),
                average_price=_decimal(average, "average_price", positive=True),
                response_acquired_at=response_acquired_at,
                raw_response_hash=raw_hash,
            )
        except (SubmissionCommitmentError, TypeError, ValueError) as error:
            outcome = ExternalSubmissionOutcome.INVALID_RESPONSE
            error_text = f"INVALID_FILLED_COMMITMENT: {error}"
        else:
            outcome = ExternalSubmissionOutcome.FILLED_COMMITMENT
    elif status in {"open", "new", "pending", "accepted"}:
        outcome = (
            ExternalSubmissionOutcome.EXTERNAL_IOC_RESTING_CONFLICT
            if ioc_expected
            else ExternalSubmissionOutcome.RESTING_ACCEPTED
        )
    elif status in {"rejected", "canceled", "cancelled", "expired"}:
        outcome = ExternalSubmissionOutcome.DETERMINISTIC_ORDER_ERROR
        error_text = error_text or status.upper()
    elif raw_payload is None:
        outcome = ExternalSubmissionOutcome.AMBIGUOUS_TRANSPORT_FAILURE
        error_text = error_text or "BROKER_RESPONSE_RAW_PAYLOAD_MISSING"
    else:
        outcome = ExternalSubmissionOutcome.INVALID_RESPONSE
        error_text = error_text or "UNRECOGNIZED_BROKER_RESPONSE"
    return ExternalSubmissionResponse(
        local_order_id=local_order_id,
        intent_id=intent_id,
        venue=venue,
        environment=environment,
        account_scope=account_scope,
        instrument=instrument,
        side=side,
        client_order_id=client_order_id,
        response_acquired_at=response_acquired_at,
        outcome=outcome,
        raw_payload=payload,
        raw_response_hash=_hash_payload(payload),
        commitment=commitment,
        structured_error=error_text,
        ambiguity_classification=(
            "NONE"
            if outcome
            in {
                ExternalSubmissionOutcome.FILLED_COMMITMENT,
                ExternalSubmissionOutcome.DETERMINISTIC_ORDER_ERROR,
            }
            else outcome.value
        ),
    )


__all__ = [
    "AuthoritativeSubmissionFillCommitment",
    "ExternalSubmissionOutcome",
    "ExternalSubmissionResponse",
    "SUBMISSION_RESPONSE_AGGREGATE_TYPE",
    "SUBMISSION_RESPONSE_CONTRACT_VERSION",
    "SUBMISSION_RESPONSE_EVENT_TYPE",
    "SubmissionCommitmentError",
    "build_submission_response",
]
