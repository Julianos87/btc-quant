from __future__ import annotations

import gzip
import json
import shutil
from pathlib import Path

import pytest

from btcquant.hyperliquid_baseline import (
    FUNDING_SLOT_TOLERANCE_SECONDS,
    canonical_csv_bytes,
    deterministic_gzip_bytes,
    funding_equivalent_8h,
    scoped_source_hashes,
    validate_funding_bytes,
    validate_ohlcv_bytes,
)
from scripts.check_baseline_provenance import (
    ProvenanceFailure,
    check_hyperliquid_manifest,
)


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "audit/baselines/data/hyperliquid_v1"
OHLCV = DATA / "hyperliquid_btc_perp_1h_v1.csv.gz"
FUNDING = DATA / "hyperliquid_btc_funding_v1_window.csv.gz"


def _fixture_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    for relative in (
        "audit/baselines/hyperliquid_execution_v1.json",
        "audit/baselines/legacy_binance.json",
        "audit/baseline_reference.json",
        "dashboard/yearly_reference.json",
        "pyproject.toml",
        "uv.lock",
        "environments/testnet/config.yaml",
    ):
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, destination)
    for relative in (
        "hyperliquid_btc_perp_1h_v1.csv.gz",
        "hyperliquid_btc_funding_v1_window.csv.gz",
    ):
        destination = root / "audit/baselines/data/hyperliquid_v1" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(DATA / relative, destination)
    for relative in (
        "src/btcquant/data.py",
        "src/btcquant/data_integrity.py",
        "src/btcquant/hyperliquid_baseline.py",
        "src/btcquant/indicators.py",
        "src/btcquant/backtest/engine.py",
        "src/btcquant/backtest/metrics.py",
        "src/btcquant/strategies/base.py",
        "src/btcquant/strategies/trend_ls.py",
        "src/btcquant/execution/venue.py",
    ):
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, destination)
    return root


def _rewrite_gzip(path: Path, payload: bytes) -> None:
    path.write_bytes(deterministic_gzip_bytes(payload))


def test_frozen_hyperliquid_manifest_passes_without_network() -> None:
    check_hyperliquid_manifest(ROOT)


def test_funding_window_contract_is_exact() -> None:
    payload = gzip.decompress(FUNDING.read_bytes())
    report = validate_funding_bytes(
        payload,
        start=__import__("datetime").datetime.fromisoformat("2026-01-14T12:00:00+00:00"),
        end_exclusive=__import__("datetime").datetime.fromisoformat("2026-08-10T20:00:00+00:00"),
        expected_rows=5000,
        tolerance_seconds=FUNDING_SLOT_TOLERANCE_SECONDS,
    )
    assert report["observed"] == 5000
    assert report["missing_slots"] == []
    assert report["max_timestamp_jitter_seconds"] == 0.261


def test_ohlcv_has_exactly_1250_complete_four_hour_windows() -> None:
    payload = gzip.decompress(OHLCV.read_bytes())
    report = validate_ohlcv_bytes(
        payload,
        start=__import__("datetime").datetime.fromisoformat("2026-01-14T12:00:00+00:00"),
        end=__import__("datetime").datetime.fromisoformat("2026-08-10T19:00:00+00:00"),
        expected_rows=5000,
    )
    assert report["complete_4h_windows"] == 1250


def test_native_funding_unit_is_not_an_implicit_eight_hour_payment() -> None:
    assert funding_equivalent_8h(0.0000125) == pytest.approx(0.0001)


@pytest.mark.parametrize("mutation", ["gzip_byte", "candle", "delete_candle", "reorder_candle"])
def test_candle_mutations_fail_provenance(tmp_path: Path, mutation: str) -> None:
    root = _fixture_root(tmp_path)
    path = root / "audit/baselines/data/hyperliquid_v1/hyperliquid_btc_perp_1h_v1.csv.gz"
    payload = gzip.decompress(path.read_bytes())
    if mutation == "gzip_byte":
        compressed = bytearray(path.read_bytes())
        compressed[-1] ^= 1
        path.write_bytes(compressed)
    elif mutation == "candle":
        path.write_bytes(deterministic_gzip_bytes(payload.replace(b",94773,", b",94774,", 1)))
    else:
        lines = payload.splitlines(keepends=True)
        if mutation == "delete_candle":
            lines.pop(2)
        else:
            lines[1], lines[2] = lines[2], lines[1]
        _rewrite_gzip(path, b"".join(lines))
    with pytest.raises(ProvenanceFailure):
        check_hyperliquid_manifest(root)


@pytest.mark.parametrize("mutation", ["rate", "delete"])
def test_funding_mutations_fail_provenance(tmp_path: Path, mutation: str) -> None:
    root = _fixture_root(tmp_path)
    path = root / "audit/baselines/data/hyperliquid_v1/hyperliquid_btc_funding_v1_window.csv.gz"
    payload = gzip.decompress(path.read_bytes())
    lines = payload.splitlines(keepends=True)
    if mutation == "rate":
        lines[1] = lines[1].replace(b",0.0000125", b",0.0000126", 1)
    else:
        lines.pop(2)
    _rewrite_gzip(path, b"".join(lines))
    with pytest.raises(ProvenanceFailure):
        check_hyperliquid_manifest(root)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("cutoff", "2026-08-10T19:00:00Z"),
        ("fee_model", "OTHER"),
        ("normalization_version", "other"),
    ],
)
def test_manifest_contract_mutations_fail(tmp_path: Path, field: str, value: str) -> None:
    root = _fixture_root(tmp_path)
    path = root / "audit/baselines/hyperliquid_execution_v1.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest[field] = value
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ProvenanceFailure):
        check_hyperliquid_manifest(root)


def test_scoped_quantitative_source_mutation_fails(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    path = root / "src/btcquant/data.py"
    path.write_bytes(path.read_bytes() + b"\n# quantitative mutation\n")
    with pytest.raises(ProvenanceFailure):
        check_hyperliquid_manifest(root)


def test_dashboard_is_outside_hyperliquid_quantitative_scope(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    before = scoped_source_hashes(root)
    dashboard = root / "dashboard/static/dashboard.js"
    dashboard.parent.mkdir(parents=True)
    dashboard.write_text("changed", encoding="utf-8")
    assert scoped_source_hashes(root) == before
    check_hyperliquid_manifest(root)


def test_canonical_csv_and_gzip_are_path_independent(tmp_path: Path) -> None:
    payload = canonical_csv_bytes(
        ("timestamp", "funding_rate"),
        (("2026-01-14T12:00:00.043Z", "0.0000125"),),
    )
    first = tmp_path / "a" / "data.gz"
    second = tmp_path / "b" / "data.gz"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_bytes(deterministic_gzip_bytes(payload))
    second.write_bytes(deterministic_gzip_bytes(payload))
    assert first.read_bytes() == second.read_bytes()
