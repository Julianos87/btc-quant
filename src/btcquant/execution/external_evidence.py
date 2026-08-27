"""Contrats normalisés et immuables pour les preuves externes d'ordre.

Ces contrats décrivent des faits observés. Ils ne déclenchent aucune
réconciliation, ne modifient aucun ordre local et n'appliquent aucun fill.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from .errors import FillInvariantViolation, InvalidExternalObservation
from .order_state import ExternalOrderState

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_SIDE_VALUES = {"BUY", "SELL"}


class ExternalEvidenceSource(StrEnum):
    """Origine normalisée d'une preuve venue de l'exchange."""

    ORDER_LOOKUP = "ORDER_LOOKUP"
    OPEN_ORDERS = "OPEN_ORDERS"
    HISTORICAL_ORDERS = "HISTORICAL_ORDERS"
    FILL_LOOKUP = "FILL_LOOKUP"
    PRIVATE_EVENT = "PRIVATE_EVENT"
    SUBMISSION_RESPONSE = "SUBMISSION_RESPONSE"


def _canonical_timestamp(value: str | None, *, field: str, required: bool) -> str | None:
    if value is None:
        if required:
            raise InvalidExternalObservation(f"{field} est requis")
        return None
    if not isinstance(value, str) or not value.strip():
        raise InvalidExternalObservation(f"{field} doit être une date ISO UTC non vide")
    candidate = value.strip()
    candidate = candidate[:-1] + "+00:00" if candidate[-1:] in {"Z", "z"} else candidate
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as error:
        raise InvalidExternalObservation(f"{field} doit être une date ISO UTC valide") from error
    if parsed.tzinfo is None:
        raise InvalidExternalObservation(f"{field} doit inclure un fuseau UTC")
    return parsed.astimezone(UTC).isoformat()


def _required_text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidExternalObservation(f"{field} doit être une chaîne non vide")
    return value.strip()


def _optional_text(value: str | None, field: str) -> str | None:
    return None if value is None else _required_text(value, field)


def _finite(value: float, field: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidExternalObservation(f"{field} doit être un nombre")
    normalized = float(value)
    if (
        not math.isfinite(normalized)
        or (positive and normalized <= 0)
        or (not positive and normalized < 0)
    ):
        qualifier = "strictement positif" if positive else "positif ou nul"
        raise InvalidExternalObservation(f"{field} doit être fini et {qualifier}")
    return normalized


def _canonical_payload(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _sha256_key(prefix: str, payload: dict[str, Any]) -> str:
    digest = hashlib.sha256(_canonical_payload(payload).encode("utf-8")).hexdigest()
    return f"{prefix}-{digest}"


def _validate_key(value: str, prefix: str, field: str) -> str:
    if not isinstance(value, str) or not value.startswith(prefix):
        raise InvalidExternalObservation(f"{field} doit commencer par {prefix}")
    if not _SHA256_PATTERN.fullmatch(value.removeprefix(prefix)):
        raise InvalidExternalObservation(f"{field} doit contenir un SHA-256 hexadécimal")
    return value


def _validate_raw_payload_hash(value: str) -> str:
    if not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value):
        raise InvalidExternalObservation("raw_payload_hash doit être un SHA-256 hexadécimal")
    return value


@dataclass(frozen=True)
class ExternalOrderObservation:
    """Snapshot/event externe normalisé et attribué à un ordre local."""

    local_order_id: int
    intent_id: str
    venue: str
    account_scope: str
    instrument: str
    side: str
    source_kind: ExternalEvidenceSource | str
    normalized_external_status: ExternalOrderState | str
    requested_qty: float
    observed_at: str
    raw_payload_hash: str
    cumulative_filled_qty: float | None = None
    remaining_qty: float | None = None
    client_order_id: str | None = None
    external_order_id: str | None = None
    venue_event_at: str | None = None
    observation_key: str | None = None
    persisted_at: str | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.local_order_id, bool)
            or not isinstance(self.local_order_id, int)
            or self.local_order_id <= 0
        ):
            raise InvalidExternalObservation(
                "local_order_id doit être un entier strictement positif"
            )
        for field in ("intent_id", "venue", "account_scope", "instrument"):
            object.__setattr__(self, field, _required_text(getattr(self, field), field))
        side = _required_text(self.side, "side").upper()
        if side not in _SIDE_VALUES:
            raise InvalidExternalObservation("side doit être BUY ou SELL")
        object.__setattr__(self, "side", side)
        try:
            object.__setattr__(self, "source_kind", ExternalEvidenceSource(self.source_kind))
            object.__setattr__(
                self,
                "normalized_external_status",
                ExternalOrderState(self.normalized_external_status),
            )
        except ValueError as error:
            raise InvalidExternalObservation(
                "source_kind ou normalized_external_status invalide"
            ) from error
        object.__setattr__(
            self, "requested_qty", _finite(self.requested_qty, "requested_qty", positive=True)
        )
        if self.cumulative_filled_qty is not None:
            object.__setattr__(
                self,
                "cumulative_filled_qty",
                _finite(self.cumulative_filled_qty, "cumulative_filled_qty"),
            )
            if self.cumulative_filled_qty > self.requested_qty + self._tolerance:
                raise InvalidExternalObservation("cumulative_filled_qty dépasse requested_qty")
        if self.remaining_qty is not None:
            object.__setattr__(self, "remaining_qty", _finite(self.remaining_qty, "remaining_qty"))
        if (
            self.cumulative_filled_qty is not None
            and self.remaining_qty is not None
            and abs(self.cumulative_filled_qty + self.remaining_qty - self.requested_qty)
            > self._tolerance
        ):
            raise InvalidExternalObservation(
                "cumulative_filled_qty + remaining_qty doit égaler requested_qty"
            )
        object.__setattr__(
            self, "client_order_id", _optional_text(self.client_order_id, "client_order_id")
        )
        object.__setattr__(
            self, "external_order_id", _optional_text(self.external_order_id, "external_order_id")
        )
        object.__setattr__(
            self,
            "venue_event_at",
            _canonical_timestamp(self.venue_event_at, field="venue_event_at", required=False),
        )
        object.__setattr__(
            self,
            "observed_at",
            _canonical_timestamp(self.observed_at, field="observed_at", required=True),
        )
        object.__setattr__(
            self,
            "persisted_at",
            _canonical_timestamp(self.persisted_at, field="persisted_at", required=False),
        )
        if self.persisted_at is not None and self.observed_at > self.persisted_at:
            raise InvalidExternalObservation(
                "observed_at ne peut pas être postérieur à persisted_at"
            )
        object.__setattr__(
            self, "raw_payload_hash", _validate_raw_payload_hash(self.raw_payload_hash)
        )
        key = self.observation_key or self.derived_observation_key()
        object.__setattr__(self, "observation_key", _validate_key(key, "obs-", "observation_key"))

    @property
    def _tolerance(self) -> float:
        return max(1e-9, self.requested_qty * 1e-9)

    def derived_observation_key(self) -> str:
        """Identité d'un snapshot venue normalisé, hors métadonnées locales."""

        return _sha256_key(
            "obs",
            {
                "account_scope": self.account_scope,
                "client_order_id": self.client_order_id,
                "cumulative_filled_qty": self.cumulative_filled_qty,
                "external_order_id": self.external_order_id,
                "instrument": self.instrument,
                "normalized_external_status": ExternalOrderState(
                    self.normalized_external_status
                ).value,
                "remaining_qty": self.remaining_qty,
                "requested_qty": self.requested_qty,
                "side": self.side,
                "source_kind": ExternalEvidenceSource(self.source_kind).value,
                "venue": self.venue,
                "venue_event_at": self.venue_event_at,
            },
        )

    def with_persisted_at(self, persisted_at: str) -> ExternalOrderObservation:
        return replace(self, persisted_at=persisted_at)

    def semantic_content(self) -> dict[str, Any]:
        """Faits attribués et normalisés qui doivent coïncider pour une clé."""

        return {
            "local_order_id": self.local_order_id,
            "intent_id": self.intent_id,
            "venue": self.venue,
            "account_scope": self.account_scope,
            "instrument": self.instrument,
            "side": self.side,
            "source_kind": ExternalEvidenceSource(self.source_kind).value,
            "normalized_external_status": ExternalOrderState(self.normalized_external_status).value,
            "client_order_id": self.client_order_id,
            "external_order_id": self.external_order_id,
            "requested_qty": self.requested_qty,
            "cumulative_filled_qty": self.cumulative_filled_qty,
            "remaining_qty": self.remaining_qty,
            "venue_event_at": self.venue_event_at,
            "observation_key": self.observation_key,
        }


@dataclass(frozen=True)
class ExternalFill:
    """Exécution externe immuable, distincte des snapshots d'ordre."""

    local_order_id: int
    intent_id: str
    venue: str
    account_scope: str
    instrument: str
    side: str
    source_kind: ExternalEvidenceSource | str
    quantity: float
    price: float
    observed_at: str
    raw_payload_hash: str
    client_order_id: str | None = None
    external_order_id: str | None = None
    venue_fill_id: str | None = None
    fee: float | None = None
    fee_asset: str | None = None
    venue_event_at: str | None = None
    fill_key: str | None = None
    persisted_at: str | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.local_order_id, bool)
            or not isinstance(self.local_order_id, int)
            or self.local_order_id <= 0
        ):
            raise FillInvariantViolation("local_order_id doit être un entier strictement positif")
        for field in ("intent_id", "venue", "account_scope", "instrument"):
            try:
                object.__setattr__(self, field, _required_text(getattr(self, field), field))
            except InvalidExternalObservation as error:
                raise FillInvariantViolation(str(error)) from error
        side = _required_text(self.side, "side").upper()
        if side not in _SIDE_VALUES:
            raise FillInvariantViolation("side doit être BUY ou SELL")
        object.__setattr__(self, "side", side)
        try:
            object.__setattr__(self, "source_kind", ExternalEvidenceSource(self.source_kind))
            object.__setattr__(self, "quantity", _finite(self.quantity, "quantity", positive=True))
            object.__setattr__(self, "price", _finite(self.price, "price", positive=True))
        except (InvalidExternalObservation, ValueError) as error:
            raise FillInvariantViolation(str(error)) from error
        for field in ("client_order_id", "external_order_id", "venue_fill_id", "fee_asset"):
            object.__setattr__(self, field, _optional_text(getattr(self, field), field))
        if self.fee is not None:
            try:
                object.__setattr__(self, "fee", _finite(self.fee, "fee"))
            except InvalidExternalObservation as error:
                raise FillInvariantViolation(str(error)) from error
        elif self.fee_asset is not None:
            raise FillInvariantViolation("fee_asset exige une fee explicitement observée")
        try:
            object.__setattr__(
                self,
                "venue_event_at",
                _canonical_timestamp(self.venue_event_at, field="venue_event_at", required=False),
            )
            object.__setattr__(
                self,
                "observed_at",
                _canonical_timestamp(self.observed_at, field="observed_at", required=True),
            )
            object.__setattr__(
                self,
                "persisted_at",
                _canonical_timestamp(self.persisted_at, field="persisted_at", required=False),
            )
            object.__setattr__(
                self, "raw_payload_hash", _validate_raw_payload_hash(self.raw_payload_hash)
            )
        except InvalidExternalObservation as error:
            raise FillInvariantViolation(str(error)) from error
        if self.persisted_at is not None and self.observed_at > self.persisted_at:
            raise FillInvariantViolation("observed_at ne peut pas être postérieur à persisted_at")
        key = self.fill_key or self.derived_fill_key()
        try:
            object.__setattr__(self, "fill_key", _validate_key(key, "fill-", "fill_key"))
        except InvalidExternalObservation as error:
            raise FillInvariantViolation(str(error)) from error

    def derived_fill_key(self) -> str:
        identity: dict[str, Any] = {"account_scope": self.account_scope, "venue": self.venue}
        if self.venue_fill_id is not None:
            identity["venue_fill_id"] = self.venue_fill_id
        else:
            identity.update(
                {
                    "client_order_id": self.client_order_id,
                    "external_order_id": self.external_order_id,
                    "fee": self.fee,
                    "fee_asset": self.fee_asset,
                    "instrument": self.instrument,
                    "price": self.price,
                    "quantity": self.quantity,
                    "raw_payload_hash": self.raw_payload_hash,
                    "side": self.side,
                    "venue_event_at": self.venue_event_at,
                }
            )
        return _sha256_key("fill", identity)

    def with_persisted_at(self, persisted_at: str) -> ExternalFill:
        return replace(self, persisted_at=persisted_at)

    def is_semantically_compatible_with(self, other: ExternalFill) -> bool:
        """Compare une redelivery avec la preuve immuable déjà persistée.

        Les faits économiques et l'attribution locale sont stricts. Les champs
        de corrélation venue sont optionnels : l'absence d'une valeur n'est pas
        contradictoire avec une valeur connue, mais deux valeurs connues
        différentes le sont. Les métadonnées de livraison sont exclues.
        """

        required_fields = (
            "local_order_id",
            "intent_id",
            "venue",
            "account_scope",
            "instrument",
            "side",
            "venue_fill_id",
            "quantity",
            "price",
            "fee",
            "fee_asset",
        )
        optional_correlation_fields = (
            "client_order_id",
            "external_order_id",
            "venue_event_at",
        )
        if any(getattr(self, field) != getattr(other, field) for field in required_fields):
            return False
        return all(
            left == right or left is None or right is None
            for left, right in (
                (getattr(self, field), getattr(other, field))
                for field in optional_correlation_fields
            )
        )
