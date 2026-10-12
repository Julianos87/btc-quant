"""Read-only public Deribit market adapter for the experimental dated Carry.

The client has no authentication or trading methods. Settlement lookup uses
Deribit's public settlement endpoints and fails closed when history cannot be
reconciled to the supplied cursor.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import UTC, datetime, time as datetime_time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from btcquant.domain.carry_dated import (
    BookLevel,
    DatedBook,
    DatedInstrument,
    DatedSettlement,
    InstrumentKind,
    SettlementKind,
)

API_ROOT = "https://www.deribit.com/api/v2/public"
API_DOCS = "https://docs.deribit.com/api-reference/market-data"
VENUE = "deribit"
BTC_SPOT = "BTC_USDC"
BTC_USDC_INDEX = "btc_usdc"
DEFAULT_BOOK_DEPTH = 100
SETTLEMENT_PAGE_SIZE = 1000
MAX_SETTLEMENT_PAGES = 25
DELIVERY_PRICE_PAGE_SIZE = 1000
MAX_DELIVERY_PRICE_PAGES = 10
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 8.0
MAX_ATTEMPTS = 3
LOG = logging.getLogger("btcquant.carry_dated.market")


class DeribitPublicAPIError(RuntimeError):
    """A bounded public API error that never includes request secrets."""

    def __init__(
        self,
        method: str,
        message: str,
        *,
        reason: str | None = None,
        http_status: int | None = None,
        rpc_code: int | None = None,
    ) -> None:
        self.method = method
        self.reason = reason
        self.http_status = http_status
        self.rpc_code = rpc_code
        super().__init__(f"Deribit public {method} failed: {message[:240]}")


class SettlementCursorMissing(DeribitPublicAPIError):
    """The venue history could not prove continuity from the persisted cursor."""


class SettlementDataMismatch(DeribitPublicAPIError):
    """Official settlement sources disagree or the response identity is invalid."""


def _now() -> datetime:
    return datetime.now(UTC)


def _decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{name} must be a finite positive decimal")
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a finite positive decimal") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"{name} must be a finite positive decimal")
    return parsed


def _optional_decimal(value: Any, name: str) -> Decimal | None:
    return None if value is None else _decimal(value, name)


def _timestamp_ms(value: Any, name: str) -> datetime:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be milliseconds since the epoch")
    try:
        milliseconds = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be milliseconds since the epoch") from exc
    if milliseconds <= 0:
        raise ValueError(f"{name} must be positive")
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=milliseconds)


def _timestamp_to_ms(value: datetime) -> int:
    delta = value.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86_400 + delta.seconds) * 1000 + delta.microseconds // 1000


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _cursor_parts(settlement_key: str, instrument_id: str) -> tuple[str, int]:
    parts = settlement_key.split(":")
    if (
        len(parts) != 4
        or parts[0] != VENUE
        or parts[1] != instrument_id
        or parts[2] not in {"daily", "expiry"}
    ):
        raise SettlementCursorMissing(
            "get_last_settlements_by_instrument",
            "settlement cursor does not match the venue/instrument contract",
        )
    try:
        timestamp_ms = int(parts[3])
    except ValueError as exc:
        raise SettlementCursorMissing(
            "get_last_settlements_by_instrument", "settlement cursor has invalid timestamp"
        ) from exc
    if timestamp_ms <= 0:
        raise SettlementCursorMissing(
            "get_last_settlements_by_instrument", "settlement cursor has invalid timestamp"
        )
    return parts[2], timestamp_ms


class DeribitPublicClient:
    """Bounded, unauthenticated reads of BTC spot, linear dated futures and settlements."""

    def __init__(
        self,
        *,
        timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
        max_attempts: int = MAX_ATTEMPTS,
        max_settlement_pages: int = MAX_SETTLEMENT_PAGES,
        sleeper: Any = time.sleep,
        clock: Any = _now,
    ) -> None:
        if not 0.1 <= timeout_seconds <= 30:
            raise ValueError("timeout_seconds must be between 0.1 and 30")
        if not 1 <= max_attempts <= 5:
            raise ValueError("max_attempts must be between 1 and 5")
        if not 1 <= max_settlement_pages <= 25:
            raise ValueError("max_settlement_pages must be between 1 and 25")
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self.max_settlement_pages = max_settlement_pages
        self._sleep = sleeper
        self._clock = clock

    def _get(self, method: str, params: dict[str, Any]) -> tuple[Any, datetime]:
        if not method.startswith("get_"):
            raise ValueError("only public market-data methods are allowed")
        query = urlencode({key: str(value) for key, value in params.items() if value is not None})
        request = Request(
            f"{API_ROOT}/{method}?{query}",
            headers={"User-Agent": "btcquant-carry-paper/1", "Accept": "application/json"},
            method="GET",
        )
        for attempt in range(self.max_attempts):
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    body = response.read(MAX_RESPONSE_BYTES + 1)
                    received = self._clock().astimezone(UTC)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise DeribitPublicAPIError(method, "response exceeds size limit")
                    payload = json.loads(body, parse_float=Decimal, parse_int=int)
                    if not isinstance(payload, dict):
                        raise DeribitPublicAPIError(method, "malformed JSON-RPC response")
                    if payload.get("error"):
                        error = payload["error"]
                        data = error.get("data") if isinstance(error, dict) else None
                        rpc_reason = data.get("reason") if isinstance(data, dict) else None
                        message = (
                            error.get("message", "JSON-RPC error")
                            if isinstance(error, dict)
                            else "JSON-RPC error"
                        )
                        code = error.get("code") if isinstance(error, dict) else None
                        try:
                            rpc_code = int(code) if code is not None else None
                        except (TypeError, ValueError, OverflowError):
                            rpc_code = None
                        error_message = str(message)
                        # Deribit documents JSON-RPC 10028 / `too_many_requests`
                        # as rate-limit exhaustion, distinct from HTTP 429.
                        # Retry only these transient forms, with the same strict cap
                        # and backoff used for HTTP 429/5xx and transport failures.
                        retryable = (
                            rpc_code == 10028
                            or str(rpc_reason or "").casefold()
                            in {"too_many_requests", "rate_limit"}
                            or "too_many_requests" in error_message.casefold()
                            or "too many requests" in error_message.casefold()
                        )
                        if retryable and attempt + 1 < self.max_attempts:
                            self._sleep(min(0.25 * (2**attempt), 1.0))
                            continue
                        raise DeribitPublicAPIError(
                            method,
                            error_message,
                            reason=str(rpc_reason) if rpc_reason is not None else None,
                            rpc_code=rpc_code,
                        )
                    if "result" not in payload:
                        raise DeribitPublicAPIError(method, "result is missing")
                    return payload["result"], received
            except HTTPError as exc:
                body = exc.read(MAX_RESPONSE_BYTES)
                message = f"HTTP {exc.code}"
                http_reason: str | None = None
                try:
                    error_payload = json.loads(body, parse_float=Decimal, parse_int=int)
                    error = error_payload.get("error", {})
                    data = error.get("data", {}) if isinstance(error, dict) else {}
                    message = (
                        str(error.get("message", message)) if isinstance(error, dict) else message
                    )
                    reason_value = data.get("reason") if isinstance(data, dict) else None
                    http_reason = str(reason_value) if reason_value is not None else None
                except (ValueError, TypeError, AttributeError):
                    pass
                if exc.code == 429 or 500 <= exc.code <= 599:
                    if attempt + 1 < self.max_attempts:
                        self._sleep(min(0.25 * (2**attempt), 1.0))
                        continue
                raise DeribitPublicAPIError(
                    method, message, reason=http_reason, http_status=exc.code
                ) from None
            except (URLError, TimeoutError, ConnectionError) as exc:
                if attempt + 1 < self.max_attempts:
                    self._sleep(min(0.25 * (2**attempt), 1.0))
                    continue
                raise DeribitPublicAPIError(method, type(exc).__name__) from None
            except (json.JSONDecodeError, UnicodeDecodeError, InvalidOperation) as exc:
                raise DeribitPublicAPIError(
                    method, f"malformed response: {type(exc).__name__}"
                ) from None
        raise DeribitPublicAPIError(method, "retry limit exceeded")

    def _instrument(self, raw: Any, received_at: datetime) -> DatedInstrument:
        if not isinstance(raw, dict):
            raise DeribitPublicAPIError("get_instruments", "instrument row is not an object")
        name = raw.get("instrument_name")
        base = raw.get("base_currency")
        quote = raw.get("quote_currency")
        raw_kind = raw.get("kind")
        state = raw.get("state")
        if not isinstance(name, str) or not isinstance(base, str) or not isinstance(quote, str):
            raise DeribitPublicAPIError("get_instruments", "instrument identity fields are missing")
        kind: InstrumentKind
        if raw_kind == "spot":
            kind = "spot"
            if name != BTC_SPOT or base != "BTC" or quote != "USDC":
                raise DeribitPublicAPIError("get_instruments", "unexpected BTC spot identity")
            expiry = None
        elif raw_kind == "future":
            if (
                base != "BTC"
                or quote != "USDC"
                or raw.get("settlement_currency") != "USDC"
                or raw.get("instrument_type", raw.get("future_type")) != "linear"
                or not name.startswith("BTC_USDC-")
                or name.endswith("-PERPETUAL")
                or raw.get("settlement_period") == "perpetual"
            ):
                raise DeribitPublicAPIError(
                    "get_instruments", "future is not linear BTC-USDC dated"
                )
            kind = "dated_future"
            expiry = _timestamp_ms(raw.get("expiration_timestamp"), "expiration_timestamp")
        else:
            raise DeribitPublicAPIError("get_instruments", "unsupported instrument kind")
        amount_step = _decimal(raw.get("min_trade_amount"), "min_trade_amount")
        contract_size = _decimal(raw.get("contract_size"), "contract_size")
        tick_size = _decimal(raw.get("tick_size"), "tick_size")
        maker = _optional_decimal(raw.get("maker_commission"), "maker_commission")
        taker = _optional_decimal(raw.get("taker_commission"), "taker_commission")
        # BTC_USDC spot is routed to Coinbase Exchange. Public metadata does not
        # identify the user's fee debit currency or personal VIP tier.
        fee_provenance = (
            "Deribit public instrument metadata; BTC_USDC spot is Coinbase Exchange-routed; "
            "account fee tier and fee debit currency are not public"
        )
        return DatedInstrument(
            venue=VENUE,
            instrument_id=name,
            kind=kind,
            base=base,
            quote=quote,
            settlement_currency=raw.get("settlement_currency"),
            contract_size=contract_size,
            amount_step=amount_step,
            min_amount=amount_step,
            tick_size=tick_size,
            expiration_utc=expiry,
            active=bool(raw.get("is_active")),
            maker_commission=maker,
            taker_commission=taker,
            fee_currency=None,
            fee_provenance=fee_provenance,
            metadata_timestamp=received_at,
            metadata_hash=_canonical_hash(raw),
            instrument_state=str(state or "unknown"),
            source=f"{API_ROOT}/get_instruments",
        )

    def list_active_instruments(self) -> tuple[DatedInstrument, ...]:
        """Discover the BTC_USDC spot book and currently open dated linear futures."""

        collected: list[DatedInstrument] = []
        for kind in ("spot", "future"):
            result, received = self._get(
                "get_instruments",
                {"currency": "USDC", "kind": kind, "expired": "false"},
            )
            if not isinstance(result, list):
                raise DeribitPublicAPIError("get_instruments", "result is not an instrument list")
            for raw in result:
                if not isinstance(raw, dict):
                    continue
                if kind == "spot" and raw.get("instrument_name") != BTC_SPOT:
                    continue
                if kind == "future" and raw.get("base_currency") != "BTC":
                    continue
                if kind == "future" and (
                    str(raw.get("instrument_name", "")).endswith("-PERPETUAL")
                    or raw.get("settlement_period") == "perpetual"
                    or raw.get("instrument_type", raw.get("future_type")) != "linear"
                ):
                    continue
                instrument = self._instrument(raw, received)
                if not instrument.active or instrument.instrument_state != "open":
                    continue
                if instrument.kind == "dated_future" and (
                    instrument.expiration_utc is None or instrument.expiration_utc <= received
                ):
                    continue
                collected.append(instrument)
        collected.sort(
            key=lambda item: (
                0 if item.kind == "spot" else 1,
                item.expiration_utc or datetime.max.replace(tzinfo=UTC),
                item.instrument_id,
            )
        )
        return tuple(collected)

    def get_book(self, instrument_id: str, *, depth: int = DEFAULT_BOOK_DEPTH) -> DatedBook:
        if not instrument_id or depth not in {1, 5, 10, 20, 50, 100, 1000, 10000}:
            raise ValueError(
                "instrument_id is required and depth must be an official supported value"
            )
        result, received = self._get(
            "get_order_book", {"instrument_name": instrument_id, "depth": depth}
        )
        if not isinstance(result, dict) or result.get("instrument_name") != instrument_id:
            raise DeribitPublicAPIError("get_order_book", "response instrument identity mismatch")
        bids = self._levels(result.get("bids"), "bids")
        asks = self._levels(result.get("asks"), "asks")
        self._check_ordering(bids, asks)
        timestamp = _timestamp_ms(result.get("timestamp"), "order-book timestamp")
        sequence_value = result.get("change_id")
        sequence = int(sequence_value) if sequence_value is not None else None
        return DatedBook(
            instrument_id=instrument_id,
            bids=bids,
            asks=asks,
            market_timestamp=timestamp,
            received_timestamp=received,
            source=f"{API_ROOT}/get_order_book",
            instrument_state=str(result.get("state", "unknown")),
            mark_price=_optional_decimal(result.get("mark_price"), "mark_price"),
            index_price=_optional_decimal(result.get("index_price"), "index_price"),
            sequence=sequence,
            content_hash=_canonical_hash(result),
        )

    @staticmethod
    def _levels(raw: Any, side: str) -> tuple[BookLevel, ...]:
        """Parse Deribit order-book amounts as base-asset quantity.

        Deribit's public instrument metadata/docs define linear future amount
        in underlying currency (BTC for BTC_USDC); `contract_size` describes
        a contract's BTC value and must not be multiplied into this quantity.
        See https://docs.deribit.com/api-reference/market-data/public-get_order_book
        and https://support.deribit.com/hc/en-us/articles/31424954805405-Linear-Futures.
        """
        if not isinstance(raw, list):
            raise DeribitPublicAPIError("get_order_book", f"{side} is not a level list")
        levels: list[BookLevel] = []
        for row in raw:
            if not isinstance(row, list) or len(row) != 2:
                raise DeribitPublicAPIError("get_order_book", f"malformed {side} level")
            try:
                levels.append(
                    BookLevel(_decimal(row[0], f"{side} price"), _decimal(row[1], f"{side} amount"))
                )
            except ValueError as exc:
                raise DeribitPublicAPIError("get_order_book", str(exc)) from exc
        return tuple(levels)

    @staticmethod
    def _check_ordering(bids: tuple[BookLevel, ...], asks: tuple[BookLevel, ...]) -> None:
        if any(left.price < right.price for left, right in zip(bids, bids[1:], strict=False)):
            raise DeribitPublicAPIError("get_order_book", "bid levels are not descending")
        if any(left.price > right.price for left, right in zip(asks, asks[1:], strict=False)):
            raise DeribitPublicAPIError("get_order_book", "ask levels are not ascending")

    def get_settlements(
        self,
        instrument_id: str,
        after_key: str | None = None,
    ) -> tuple[DatedSettlement, ...]:
        """Fetch daily/final official settlements, verifying any persisted cursor.

        Active instruments use the by-instrument endpoint. Deribit rejects that
        endpoint after a contract becomes inactive; the public by-currency
        endpoint is then paged and filtered by exact instrument name.
        """

        if not instrument_id.startswith("BTC_USDC-") or instrument_id.endswith("-PERPETUAL"):
            raise ValueError("settlement history requires a dated BTC_USDC instrument name")
        cursor_kind: str | None = None
        cursor_ms: int | None = None
        if after_key is not None:
            cursor_kind, cursor_ms = _cursor_parts(after_key, instrument_id)
        by_instrument = True
        all_rows: list[tuple[dict[str, Any], datetime]] = []
        for api_type in ("settlement", "delivery"):
            try:
                rows = self._collect_by_instrument_type(
                    instrument_id,
                    api_type,
                    cursor_kind=cursor_kind,
                    cursor_ms=cursor_ms,
                )
            except DeribitPublicAPIError as exc:
                if exc.reason != "instrument is not active" or exc.http_status != 400:
                    raise
                by_instrument = False
                break
            all_rows.extend(rows)
        if not by_instrument:
            if after_key is None:
                raise SettlementCursorMissing(
                    "get_last_settlements_by_currency",
                    "expired contract history requires the last persisted settlement cursor",
                )
            assert cursor_kind is not None and cursor_ms is not None
            all_rows = []
            for api_type in ("settlement", "delivery"):
                rows = self._collect_by_currency_type(
                    instrument_id,
                    api_type,
                    cursor_kind=cursor_kind,
                    cursor_ms=cursor_ms,
                )
                all_rows.extend(rows)

        normalized: list[DatedSettlement] = []
        cursor_found = False
        for row, received in all_rows:
            settlement = self._normalize_settlement(instrument_id, row, received)
            if settlement.settlement_key == after_key:
                cursor_found = True
            if cursor_ms is None or _timestamp_to_ms(settlement.event_timestamp) >= cursor_ms:
                normalized.append(settlement)
        if after_key is not None and not cursor_found:
            raise SettlementCursorMissing(
                "get_last_settlements_by_currency"
                if not by_instrument
                else "get_last_settlements_by_instrument",
                "persisted settlement cursor was not returned within the bounded history scan",
            )
        normalized = [
            self._verify_expiry_delivery(item) if item.kind == "expiry" else item
            for item in normalized
        ]
        normalized.sort(key=lambda item: (item.event_timestamp, item.settlement_key))
        return tuple(normalized)

    def _collect_by_instrument_type(
        self,
        instrument_id: str,
        api_type: str,
        *,
        cursor_kind: str | None,
        cursor_ms: int | None,
    ) -> list[tuple[dict[str, Any], datetime]]:
        rows: list[tuple[dict[str, Any], datetime]] = []
        continuation: str | None = None
        cursor_event_found = False
        for page in range(self.max_settlement_pages):
            params = {
                "instrument_name": instrument_id,
                "type": api_type,
                "count": SETTLEMENT_PAGE_SIZE,
                "continuation": continuation,
            }
            result, received = self._get("get_last_settlements_by_instrument", params)
            page_rows, next_cursor, page_min_timestamp = self._settlement_page(
                result, instrument_id, api_type
            )
            for row in page_rows:
                timestamp = int(row["timestamp"])
                if (
                    cursor_ms is not None
                    and timestamp == cursor_ms
                    and api_type == self._api_type_for_kind(cursor_kind)
                ):
                    cursor_event_found = True
                if cursor_ms is None or timestamp >= cursor_ms:
                    rows.append((row, received))
            if (
                cursor_ms is not None
                and page_min_timestamp is not None
                and page_min_timestamp <= cursor_ms
            ):
                break
            if not next_cursor:
                break
            if page + 1 == self.max_settlement_pages:
                raise SettlementCursorMissing(
                    "get_last_settlements_by_instrument", "history page limit reached before cursor"
                )
            continuation = next_cursor
        if (
            cursor_ms is not None
            and api_type == self._api_type_for_kind(cursor_kind)
            and not cursor_event_found
        ):
            raise SettlementCursorMissing(
                "get_last_settlements_by_instrument", "persisted settlement cursor is absent"
            )
        return rows

    def _collect_by_currency_type(
        self,
        instrument_id: str,
        api_type: str,
        *,
        cursor_kind: str,
        cursor_ms: int,
    ) -> list[tuple[dict[str, Any], datetime]]:
        rows: list[tuple[dict[str, Any], datetime]] = []
        continuation: str | None = None
        cursor_event_found = False
        for page in range(self.max_settlement_pages):
            result, received = self._get(
                "get_last_settlements_by_currency",
                {
                    "currency": "USDC",
                    "type": api_type,
                    "count": SETTLEMENT_PAGE_SIZE,
                    "continuation": continuation,
                },
            )
            page_rows, next_cursor, page_min_timestamp = self._settlement_page(
                result, instrument_id, api_type
            )
            for row in page_rows:
                timestamp = int(row["timestamp"])
                if timestamp == cursor_ms and api_type == self._api_type_for_kind(cursor_kind):
                    cursor_event_found = True
                if timestamp >= cursor_ms:
                    rows.append((row, received))
            if page_min_timestamp is not None and page_min_timestamp <= cursor_ms:
                break
            if not next_cursor:
                break
            if page + 1 == self.max_settlement_pages:
                raise SettlementCursorMissing(
                    "get_last_settlements_by_currency", "history page limit reached before cursor"
                )
            continuation = next_cursor
        if api_type == self._api_type_for_kind(cursor_kind) and not cursor_event_found:
            raise SettlementCursorMissing(
                "get_last_settlements_by_currency", "persisted settlement cursor is absent"
            )
        return rows

    @staticmethod
    def _api_type_for_kind(kind: str | None) -> str | None:
        return {"daily": "settlement", "expiry": "delivery"}.get(kind or "")

    @staticmethod
    def _settlement_page(
        result: Any, instrument_id: str, expected_type: str
    ) -> tuple[list[dict[str, Any]], str | None, int | None]:
        if not isinstance(result, dict) or not isinstance(result.get("settlements"), list):
            raise DeribitPublicAPIError("get_last_settlements_by_*", "settlement list is malformed")
        rows: list[dict[str, Any]] = []
        all_timestamps: list[int] = []
        for row in result["settlements"]:
            if not isinstance(row, dict):
                raise DeribitPublicAPIError(
                    "get_last_settlements_by_*", "settlement row is malformed"
                )
            timestamp = row.get("timestamp")
            if not isinstance(timestamp, int):
                raise DeribitPublicAPIError(
                    "get_last_settlements_by_*", "settlement timestamp is malformed"
                )
            all_timestamps.append(timestamp)
            if row.get("instrument_name") != instrument_id:
                continue
            if row.get("type") != expected_type:
                continue
            rows.append(row)
        rows.sort(key=lambda row: int(row["timestamp"]), reverse=True)
        continuation = result.get("continuation")
        token = str(continuation) if continuation not in (None, "none", "") else None
        return rows, token, min(all_timestamps) if all_timestamps else None

    def _normalize_settlement(
        self,
        instrument_id: str,
        row: dict[str, Any],
        received_at: datetime,
    ) -> DatedSettlement:
        api_type = row.get("type")
        if (
            api_type not in {"settlement", "delivery"}
            or row.get("instrument_name") != instrument_id
        ):
            raise SettlementDataMismatch(
                "get_last_settlements_by_*", "settlement response identity mismatch"
            )
        kind: SettlementKind = "daily" if api_type == "settlement" else "expiry"
        event_at = _timestamp_ms(row.get("timestamp"), "settlement timestamp")
        official_price = _decimal(row.get("index_price"), "official settlement index price")
        if kind == "daily":
            period_start = datetime.combine(
                event_at.date() - timedelta(days=1), datetime_time(8, 0), tzinfo=UTC
            )
        else:
            period_start = datetime.combine(event_at.date(), datetime_time(7, 30), tzinfo=UTC)
        core = {
            "venue": VENUE,
            "instrument_id": instrument_id,
            "kind": kind,
            "timestamp_ms": int(row["timestamp"]),
            "official_index_price": format(official_price, "f"),
            "mark_price": str(row.get("mark_price")) if row.get("mark_price") is not None else None,
            "source": "Deribit official public settlement history",
        }
        digest = _canonical_hash(core)
        return DatedSettlement(
            venue=VENUE,
            instrument_id=instrument_id,
            settlement_key=f"{VENUE}:{instrument_id}:{kind}:{int(row['timestamp'])}",
            kind=kind,
            period_start=period_start,
            period_end=event_at,
            official_index_price=official_price,
            source="Deribit official public settlement history",
            received_at=received_at,
            content_hash=digest,
            correction_version=f"sha256:{digest}",
            event_timestamp=event_at,
        )

    def _verify_expiry_delivery(self, settlement: DatedSettlement) -> DatedSettlement:
        target_date = settlement.event_timestamp.date().isoformat()
        target_price: Decimal | None = None
        found = False
        for page in range(MAX_DELIVERY_PRICE_PAGES):
            result, _ = self._get(
                "get_delivery_prices",
                {
                    "index_name": BTC_USDC_INDEX,
                    "offset": page * DELIVERY_PRICE_PAGE_SIZE,
                    "count": DELIVERY_PRICE_PAGE_SIZE,
                },
            )
            if not isinstance(result, dict) or not isinstance(result.get("data"), list):
                raise SettlementDataMismatch(
                    "get_delivery_prices", "delivery price result is malformed"
                )
            for row in result["data"]:
                if not isinstance(row, dict):
                    raise SettlementDataMismatch(
                        "get_delivery_prices", "delivery price row is malformed"
                    )
                if row.get("date") == target_date:
                    price = _decimal(row.get("delivery_price"), "official delivery price")
                    if found and price != target_price:
                        raise SettlementDataMismatch(
                            "get_delivery_prices",
                            "multiple official prices conflict for expiry date",
                        )
                    found = True
                    target_price = price
            records_total = int(result.get("records_total", 0))
            if found or (page + 1) * DELIVERY_PRICE_PAGE_SIZE >= records_total:
                break
        if not found or target_price is None:
            raise SettlementCursorMissing(
                "get_delivery_prices", "official BTC-USDC delivery price is not yet available"
            )
        if target_price != settlement.official_index_price:
            raise SettlementDataMismatch(
                "get_delivery_prices",
                "currency settlement index and delivery price history disagree",
            )
        digest = _canonical_hash(
            {
                "settlement_hash": settlement.content_hash,
                "delivery_price_date": target_date,
                "delivery_price": format(target_price, "f"),
                "delivery_price_source": f"{API_ROOT}/get_delivery_prices",
            }
        )
        return DatedSettlement(
            venue=settlement.venue,
            instrument_id=settlement.instrument_id,
            settlement_key=settlement.settlement_key,
            kind=settlement.kind,
            period_start=settlement.period_start,
            period_end=settlement.period_end,
            official_index_price=settlement.official_index_price,
            source="Deribit official public settlement history + verified delivery-price history",
            received_at=settlement.received_at,
            content_hash=digest,
            correction_version=f"sha256:{digest}",
            event_timestamp=settlement.event_timestamp,
        )


__all__ = [
    "DeribitPublicAPIError",
    "DeribitPublicClient",
    "SettlementCursorMissing",
    "SettlementDataMismatch",
]
