"""Check the two baseline contracts without network access.

The legacy Binance manifest is checked against immutable historical metadata;
it is not compared with the current Lot 3A source tree.  The Hyperliquid V1
manifest is stricter: its tracked gzip files, canonical CSV bytes, temporal
contract, cutoff, configuration and scoped quantitative source closure must all
match locally.
"""

from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btcquant.hyperliquid_baseline import (  # noqa: E402
    FUNDING_SLOT_TOLERANCE_SECONDS,
    HyperliquidBaselineError,
    deterministic_gzip_bytes,
    parse_utc_timestamp,
    scoped_source_hashes,
    sha256_bytes,
    validate_funding_bytes,
    validate_ohlcv_bytes,
)


class ProvenanceFailure(RuntimeError):
    """A committed baseline no longer satisfies its declared contract."""


LEGACY_EXPECTED = {
    "baseline_id": "LEGACY_RESEARCH_BASELINE_BINANCE",
    "status": "LEGACY_RESEARCH_BASELINE",
    "execution_parity": "NOT_EXECUTION_PARITY",
    "reproducibility": "ORIGINAL_DATA_FILES_NOT_BIT_FOR_BIT_AVAILABLE",
    "historical_code_commit": "f473573895010ba87e61873e5fe84d494c72a31b",
    "ohlcv": {
        "path": "data/binance_BTC-USDT_1h.csv",
        "bytes": 4816785,
        "sha256": "5dcf7a26d1034ca420526370bf5485ffb438aba2e7732854d80752edbdaa8a99",
        "rows": 66355,
        "span": [
            "2019-01-01T00:00:00+00:00",
            "2026-07-30T06:00:00+00:00",
        ],
    },
    "funding": {
        "path": "data/binanceusdm_BTCUSDT_USDT_funding.csv",
        "bytes": 295173,
        "sha256": "d288b930976ab66294b15dec55df221308e24c643c2953adffdea051bdea14ae",
        "rows": 7545,
        "span": [
            "2019-09-10T08:00:00+00:00",
            "2026-07-30T00:00:00+00:00",
        ],
    },
    "historical_results": {
        "trades": 470,
        "final_equity": 101221.49020494235,
        "cagr": 0.471615852998831,
        "sharpe": 1.1384579592223296,
        "max_drawdown": -0.5088459640981553,
        "trades_per_year": 64.29895748798302,
    },
}


def _read_json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProvenanceFailure(f"JSON illisible : {path}") from exc
    if not isinstance(payload, dict):
        raise ProvenanceFailure(f"Manifest JSON non objet : {path}")
    return payload


def _at(payload: dict, path: str) -> object:
    current: object = payload
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        if isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
            continue
        raise ProvenanceFailure(f"Champ absent : {path}")
    return current


def _equal(payload: dict, path: str, expected: object) -> None:
    actual = _at(payload, path)
    if actual != expected:
        raise ProvenanceFailure(f"{path}: {actual!r} != {expected!r}")


def check_legacy_manifest(root: str | Path = ROOT) -> None:
    repository = Path(root)
    manifest = _read_json(repository / "audit/baselines/legacy_binance.json")
    for key in ("baseline_id", "status", "execution_parity", "reproducibility"):
        _equal(manifest, key, LEGACY_EXPECTED[key])
    _equal(
        manifest,
        "source_artifacts.historical_code_commit",
        LEGACY_EXPECTED["historical_code_commit"],
    )
    for section in ("ohlcv", "funding"):
        for key, expected in LEGACY_EXPECTED[section].items():
            _equal(manifest, f"{section}.{key}", expected)
    for key, expected in LEGACY_EXPECTED["historical_results"].items():
        _equal(manifest, f"historical_results.{key}", expected)

    # The old files remain compatibility artifacts.  Check their recorded
    # metadata, but deliberately do not compare their old source-tree hash to
    # the current code.
    baseline = _read_json(repository / "audit/baseline_reference.json")
    _equal(baseline, "provenance.base_git_commit", LEGACY_EXPECTED["historical_code_commit"])
    _equal(baseline, "provenance.data.0.path", LEGACY_EXPECTED["ohlcv"]["path"])
    _equal(baseline, "provenance.data.0.bytes", LEGACY_EXPECTED["ohlcv"]["bytes"])
    _equal(baseline, "provenance.data.0.sha256", LEGACY_EXPECTED["ohlcv"]["sha256"])
    _equal(baseline, "provenance.data.1.path", LEGACY_EXPECTED["funding"]["path"])
    _equal(baseline, "provenance.data.1.bytes", LEGACY_EXPECTED["funding"]["bytes"])
    _equal(baseline, "provenance.data.1.sha256", LEGACY_EXPECTED["funding"]["sha256"])
    _equal(baseline, "provenance.base_rows", LEGACY_EXPECTED["ohlcv"]["rows"])
    _equal(baseline, "provenance.funding_rows", LEGACY_EXPECTED["funding"]["rows"])
    _equal(baseline, "provenance.base_span", LEGACY_EXPECTED["ohlcv"]["span"])
    _equal(baseline, "provenance.funding_span", LEGACY_EXPECTED["funding"]["span"])
    _equal(baseline, "results.combined.trades", LEGACY_EXPECTED["historical_results"]["trades"])
    _equal(
        baseline,
        "results.combined.final_equity",
        LEGACY_EXPECTED["historical_results"]["final_equity"],
    )
    _equal(baseline, "results.combined.cagr", LEGACY_EXPECTED["historical_results"]["cagr"])
    _equal(baseline, "results.combined.sharpe", LEGACY_EXPECTED["historical_results"]["sharpe"])
    _equal(
        baseline,
        "results.combined.max_drawdown",
        LEGACY_EXPECTED["historical_results"]["max_drawdown"],
    )
    _equal(
        baseline,
        "results.conformity.trades_per_year",
        LEGACY_EXPECTED["historical_results"]["trades_per_year"],
    )

    yearly = _read_json(repository / "dashboard/yearly_reference.json")
    _equal(yearly, "provenance.base_git_commit", LEGACY_EXPECTED["historical_code_commit"])
    _equal(yearly, "provenance.base_data_sha256", LEGACY_EXPECTED["ohlcv"]["sha256"])
    _equal(yearly, "provenance.base_rows", LEGACY_EXPECTED["ohlcv"]["rows"])


def _gzip_payload(path: Path, expected_compressed_sha256: str) -> tuple[bytes, bytes]:
    compressed = path.read_bytes()
    if sha256_bytes(compressed) != expected_compressed_sha256:
        raise ProvenanceFailure(f"Hash gzip incorrect : {path}")
    if compressed[:10] != b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x02\xff":
        raise ProvenanceFailure(f"En-tête gzip non déterministe : {path}")
    try:
        payload = gzip.decompress(compressed)
    except OSError as exc:
        raise ProvenanceFailure(f"Gzip illisible : {path}") from exc
    if deterministic_gzip_bytes(payload) != compressed:
        raise ProvenanceFailure(f"Gzip non reproductible : {path}")
    return compressed, payload


def check_hyperliquid_manifest(root: str | Path = ROOT) -> None:
    repository = Path(root)
    manifest = _read_json(repository / "audit/baselines/hyperliquid_execution_v1.json")
    _equal(manifest, "baseline_id", "HYPERLIQUID_EXECUTION_PARITY_BASELINE_V1")
    _equal(manifest, "status", "FROZEN_EXECUTION_PARITY_BASELINE")
    _equal(manifest, "venue", "hyperliquid")
    _equal(manifest, "network", "mainnet")
    _equal(manifest, "native_coin", "BTC")
    _equal(manifest, "ccxt_symbol", "BTC/USDC:USDC")
    _equal(manifest, "cutoff", "2026-08-10T20:00:00Z")
    _equal(manifest, "normalization_version", "hyperliquid-canonical-v1")
    _equal(manifest, "fee_model", "HYPERLIQUID_BASE_PERP_TAKER_V1")
    _equal(manifest, "taker_fee", 0.00045)
    _equal(manifest, "slippage_bps", 5.0)
    _equal(manifest, "market_impact_bps", 15.0)
    _equal(manifest, "price_semantics.funding_notional_price", "OHLC approximation")

    ohlcv = manifest["ohlcv"]
    ohlcv_path = repository / ohlcv["canonical_file"]
    _, ohlcv_payload = _gzip_payload(ohlcv_path, ohlcv["compressed_sha256"])
    if len(ohlcv_payload) != ohlcv["normalized_bytes"]:
        raise ProvenanceFailure("Taille CSV OHLCV incorrecte")
    if sha256_bytes(ohlcv_payload) != ohlcv["normalized_sha256"]:
        raise ProvenanceFailure("Hash CSV OHLCV incorrect")
    try:
        ohlcv_report = validate_ohlcv_bytes(
            ohlcv_payload,
            start=parse_utc_timestamp(ohlcv["start"]),
            end=parse_utc_timestamp(ohlcv["end"]),
            expected_rows=ohlcv["rows"],
        )
    except HyperliquidBaselineError as exc:
        raise ProvenanceFailure(str(exc)) from exc
    if ohlcv_report["complete_4h_windows"] != 1250:
        raise ProvenanceFailure("Le nombre de fenêtres 4h V1 n'est pas 1250")

    funding = manifest["funding"]
    funding_path = repository / funding["canonical_file"]
    _, funding_payload = _gzip_payload(funding_path, funding["compressed_sha256"])
    if len(funding_payload) != funding["normalized_bytes"]:
        raise ProvenanceFailure("Taille CSV funding incorrecte")
    if sha256_bytes(funding_payload) != funding["normalized_sha256"]:
        raise ProvenanceFailure("Hash CSV funding incorrect")
    try:
        funding_report = validate_funding_bytes(
            funding_payload,
            start=parse_utc_timestamp(funding["start"]),
            end_exclusive=parse_utc_timestamp(manifest["cutoff"]),
            expected_rows=funding["expected_slots"],
            tolerance_seconds=FUNDING_SLOT_TOLERANCE_SECONDS,
        )
    except HyperliquidBaselineError as exc:
        raise ProvenanceFailure(str(exc)) from exc
    if funding_report["rows"] != funding["rows"]:
        raise ProvenanceFailure("Nombre de lignes funding incorrect")
    if funding_report["max_timestamp_jitter_seconds"] != funding["max_timestamp_jitter_seconds"]:
        raise ProvenanceFailure("Jitter funding différent du manifeste")
    if (
        ohlcv_report["complete_4h_windows"]
        != manifest["data_integrity"]["ohlcv_complete_4h_windows"]
    ):
        raise ProvenanceFailure("Rapport resampling divergent")
    if funding_report["missing_slots"] != funding["missing_slots"]:
        raise ProvenanceFailure("Rapport des slots funding divergent")

    actual_sources = scoped_source_hashes(repository)
    if actual_sources != manifest["quantitative_source_closure"]["files"]:
        raise ProvenanceFailure("Provenance du code quantitatif scoped divergente")

    for entry in manifest["config_provenance"].values():
        path = repository / entry["path"]
        if not path.is_file() or sha256_bytes(path.read_bytes()) != entry["sha256"]:
            raise ProvenanceFailure(f"Configuration quantitative divergente : {entry['path']}")


def main() -> None:
    try:
        check_legacy_manifest(ROOT)
        check_hyperliquid_manifest(ROOT)
    except (OSError, KeyError, TypeError, ProvenanceFailure) as exc:
        raise SystemExit(f"Provenance baseline invalide : {exc}") from exc
    print("Legacy baseline integrity: PASS")
    print("Hyperliquid V1 integrity: PASS")


if __name__ == "__main__":
    main()
