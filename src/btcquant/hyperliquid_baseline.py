"""Venue-aware, deterministic primitives for the Hyperliquid V1 baseline.

This module deliberately has no network dependency.  It defines the byte-level
normalization used by the frozen files and the checks that make their temporal
contract explicit.  Hyperliquid's native funding rate remains hourly; the
8-hour equivalent is an explicitly named compatibility transform, not a
replacement for the native observations.
"""

from __future__ import annotations

import csv
import hashlib
import io
import math
import struct
import zlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from collections.abc import Iterable, Mapping, Sequence

NORMALIZATION_VERSION = "hyperliquid-canonical-v1"
FUNDING_SLOT_TOLERANCE_SECONDS = 300


OHLCV_SCHEMA = ("timestamp", "open", "high", "low", "close", "volume")
FUNDING_SCHEMA = ("timestamp", "funding_rate")

# This is an intentionally explicit closure.  The dashboard and exchange
# order adapters are not part of a historical backtest's quantitative hash.
HYPERLIQUID_SOURCE_FILES = (
    "src/btcquant/data.py",
    "src/btcquant/data_integrity.py",
    "src/btcquant/hyperliquid_baseline.py",
    "src/btcquant/indicators.py",
    "src/btcquant/backtest/engine.py",
    "src/btcquant/backtest/metrics.py",
    "src/btcquant/strategies/base.py",
    "src/btcquant/strategies/trend_ls.py",
    "src/btcquant/execution/venue.py",
    "environments/testnet/config.yaml",
    "pyproject.toml",
    "uv.lock",
)


class HyperliquidBaselineError(ValueError):
    """The frozen Hyperliquid baseline violates its local contract."""


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: str | Path) -> str:
    return sha256_bytes(Path(path).read_bytes())


def canonical_number(value: object) -> str:
    """Render an API number without locale, exponent, or insignificant zeros."""
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise HyperliquidBaselineError(f"Nombre invalide : {value!r}") from exc
    if not decimal.is_finite():
        raise HyperliquidBaselineError(f"Nombre non fini : {value!r}")
    if decimal == 0:
        return "0"
    rendered = format(decimal.normalize(), "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def canonical_csv_bytes(
    schema: Sequence[str], rows: Iterable[Mapping[str, object] | Sequence[object]]
) -> bytes:
    """Serialize canonical CSV with a stable schema and LF newline."""
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(schema)
    for row in rows:
        if isinstance(row, Mapping):
            values = [row[column] for column in schema]
        else:
            values = list(row)
        if len(values) != len(schema):
            raise HyperliquidBaselineError("Nombre de colonnes CSV inattendu")
        writer.writerow([str(value) for value in values])
    return output.getvalue().encode("utf-8")


def deterministic_gzip_bytes(payload: bytes) -> bytes:
    """Create a gzip stream independent of path, mtime, OS, and Python minor."""
    compressor = zlib.compressobj(level=9, method=zlib.DEFLATED, wbits=-15)
    body = compressor.compress(payload) + compressor.flush()
    # RFC 1952 header: no filename/comment, mtime=0, OS=255 (unknown).
    header = b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x02\xff"
    trailer = struct.pack("<II", zlib.crc32(payload) & 0xFFFFFFFF, len(payload) & 0xFFFFFFFF)
    return header + body + trailer


def parse_utc_timestamp(value: str) -> datetime:
    if not value.endswith("Z"):
        raise HyperliquidBaselineError(f"Timestamp non UTC canonique : {value!r}")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise HyperliquidBaselineError(f"Timestamp invalide : {value!r}") from exc
    if parsed.tzinfo != UTC:
        raise HyperliquidBaselineError(f"Timestamp non UTC : {value!r}")
    return parsed


def _validate_csv(payload: bytes, schema: Sequence[str]) -> list[dict[str, str]]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HyperliquidBaselineError("CSV non UTF-8") from exc
    if "\r" in text or not text.endswith("\n"):
        raise HyperliquidBaselineError("CSV : newline non canonique")
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if tuple(reader.fieldnames or ()) != tuple(schema):
        raise HyperliquidBaselineError(f"Schéma CSV attendu {tuple(schema)!r}")
    rows: list[dict[str, str]] = []
    for row in reader:
        if None in row or any(value is None for value in row.values()):
            raise HyperliquidBaselineError("CSV : ligne ou colonne incomplète")
        rows.append({key: str(value) for key, value in row.items() if key is not None})
    return rows


def _finite_numbers(rows: Iterable[Mapping[str, str]], columns: Sequence[str]) -> None:
    for row in rows:
        for column in columns:
            try:
                value = float(row[column])
            except (KeyError, ValueError) as exc:
                raise HyperliquidBaselineError(f"Valeur numérique invalide : {column}") from exc
            if not math.isfinite(value):
                raise HyperliquidBaselineError(f"NaN/inf dans {column}")


def _nearest_hour(timestamp: datetime) -> datetime:
    base = timestamp.replace(minute=0, second=0, microsecond=0)
    if timestamp - base >= timedelta(minutes=30):
        return base + timedelta(hours=1)
    return base


def validate_ohlcv_bytes(
    payload: bytes,
    *,
    start: datetime,
    end: datetime,
    expected_rows: int,
) -> dict[str, object]:
    rows = _validate_csv(payload, OHLCV_SCHEMA)
    _finite_numbers(rows, OHLCV_SCHEMA[1:])
    timestamps = [parse_utc_timestamp(row["timestamp"]) for row in rows]
    if any(timestamp.microsecond for timestamp in timestamps):
        raise HyperliquidBaselineError("OHLCV : timestamp sub-second inattendu")
    if timestamps != sorted(timestamps):
        raise HyperliquidBaselineError("OHLCV : ordre non croissant")
    if len(timestamps) != len(set(timestamps)):
        raise HyperliquidBaselineError("OHLCV : doublon")
    expected = [start + timedelta(hours=index) for index in range(expected_rows)]
    if timestamps != expected or end != expected[-1]:
        raise HyperliquidBaselineError("OHLCV : fenêtre ou cadence différente de V1")
    return {
        "rows": len(rows),
        "start": timestamps[0].isoformat().replace("+00:00", "Z"),
        "end": timestamps[-1].isoformat().replace("+00:00", "Z"),
        "missing_rows": 0,
        "duplicates": 0,
        "out_of_order": False,
        "partial_windows_removed": 0,
        "complete_4h_windows": len(rows) // 4,
    }


def validate_funding_bytes(
    payload: bytes,
    *,
    start: datetime,
    end_exclusive: datetime,
    expected_rows: int,
    tolerance_seconds: int = FUNDING_SLOT_TOLERANCE_SECONDS,
) -> dict[str, object]:
    rows = _validate_csv(payload, FUNDING_SCHEMA)
    _finite_numbers(rows, ("funding_rate",))
    timestamps = [parse_utc_timestamp(row["timestamp"]) for row in rows]
    if timestamps != sorted(timestamps):
        raise HyperliquidBaselineError("Funding : ordre non croissant")
    if len(timestamps) != len(set(timestamps)):
        raise HyperliquidBaselineError("Funding : doublon")
    expected = [start + timedelta(hours=index) for index in range(expected_rows)]
    assigned: dict[datetime, datetime] = {}
    for timestamp in timestamps:
        target = _nearest_hour(timestamp)
        jitter = abs((timestamp - target).total_seconds())
        if jitter > tolerance_seconds:
            raise HyperliquidBaselineError("Funding : timestamp hors tolérance de slot")
        if target < start or target >= end_exclusive:
            raise HyperliquidBaselineError("Funding : observation hors fenêtre V1")
        if target in assigned:
            raise HyperliquidBaselineError("Funding : deux observations pour un slot")
        assigned[target] = timestamp
    missing = [slot for slot in expected if slot not in assigned]
    if missing:
        raise HyperliquidBaselineError(
            "Funding : slots manquants " + ", ".join(value.isoformat() for value in missing)
        )
    max_jitter = max(
        abs((timestamp - target).total_seconds()) for target, timestamp in assigned.items()
    )
    return {
        "rows": len(rows),
        "start": timestamps[0].isoformat().replace("+00:00", "Z"),
        "end": timestamps[-1].isoformat().replace("+00:00", "Z"),
        "expected_slots": len(expected),
        "observed": len(timestamps),
        "missing_slots": [],
        "duplicates": 0,
        "out_of_order": False,
        "max_timestamp_jitter_seconds": max_jitter,
        "slot_tolerance_seconds": tolerance_seconds,
    }


def funding_equivalent_8h(native_funding_rate: float) -> float:
    """Return an explicit compatibility equivalent, never a native payment."""
    return native_funding_rate * 8.0


def scoped_source_hashes(root: str | Path) -> dict[str, str]:
    repository = Path(root).resolve()
    result: dict[str, str] = {}
    for relative in HYPERLIQUID_SOURCE_FILES:
        path = repository / relative
        if not path.is_file():
            raise HyperliquidBaselineError(f"Source quantitative absente : {relative}")
        result[relative] = sha256_bytes(path.read_bytes().replace(b"\r\n", b"\n"))
    return result
