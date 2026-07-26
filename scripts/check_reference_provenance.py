"""Refuse les références dashboard dont les hashes ne correspondent plus."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_tree_sha256() -> str:
    digest = hashlib.sha256()
    for path in sorted((ROOT / "src").rglob("*.py")):
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _check_file(path: Path, expected: str, label: str) -> None:
    if not path.exists() or _sha256(path) != expected:
        raise SystemExit(f"Référence périmée : hash {label} incorrect pour {path}")


def main() -> None:
    source_hash = _source_tree_sha256()
    baseline = json.loads((ROOT / "audit" / "baseline_reference.json").read_text(encoding="utf-8"))
    provenance = baseline["provenance"]
    if provenance["source_tree_sha256"] != source_hash:
        raise SystemExit("Baseline périmée : relancer scripts/make_baseline_snapshot.py")
    config = provenance["config"]
    _check_file(ROOT / config["path"], config["sha256"], "config baseline")
    for item in provenance["data"]:
        _check_file(ROOT / item["path"], item["sha256"], "données baseline")
    if "conformity" not in baseline["results"]:
        raise SystemExit("Baseline incomplète : référence de conformité absente")

    yearly = json.loads((ROOT / "dashboard" / "yearly_reference.json").read_text(encoding="utf-8"))
    yearly_provenance = yearly["provenance"]
    if yearly_provenance["source_tree_sha256"] != source_hash:
        raise SystemExit("Référence annuelle périmée : relancer make_yearly_reference.py")
    config = yearly_provenance["config"]
    _check_file(ROOT / config["path"], config["sha256"], "config annuelle")
    base_data = ROOT / "data" / "binance_BTC-USDT_1h.csv"
    _check_file(base_data, yearly_provenance["base_data_sha256"], "données annuelles")


if __name__ == "__main__":
    main()
