from __future__ import annotations

from pathlib import Path

import pytest

from btcquant.provenance import quantitative_source_files, quantitative_source_sha256
from scripts.check_reference_provenance import _check_file, _sha256


def _write_quant_fixture(root: Path) -> Path:
    (root / "src/btcquant/backtest").mkdir(parents=True)
    (root / "src/btcquant/execution").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "src/btcquant/__init__.py").write_text("\n", encoding="utf-8")
    (root / "src/btcquant/backtest/__init__.py").write_text(
        "from .engine import calculate\n", encoding="utf-8"
    )
    (root / "src/btcquant/backtest/engine.py").write_text(
        "from .new_component import VALUE\n\ndef calculate():\n    return VALUE\n",
        encoding="utf-8",
    )
    (root / "src/btcquant/backtest/new_component.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "src/btcquant/execution/irrelevant.py").write_text("VALUE = 1\n", encoding="utf-8")
    entrypoint = root / "scripts/producer.py"
    entrypoint.write_text("from btcquant.backtest import calculate\n", encoding="utf-8")
    return entrypoint


def test_relevant_component_changes_hash(tmp_path: Path) -> None:
    entrypoint = _write_quant_fixture(tmp_path)
    before = quantitative_source_sha256(entrypoint, root=tmp_path)

    component = tmp_path / "src/btcquant/backtest/engine.py"
    component.write_text(
        component.read_text(encoding="utf-8") + "\n# changed quant logic\n",
        encoding="utf-8",
    )

    assert quantitative_source_sha256(entrypoint, root=tmp_path) != before


def test_execution_only_change_does_not_change_hash(tmp_path: Path) -> None:
    entrypoint = _write_quant_fixture(tmp_path)
    before = quantitative_source_sha256(entrypoint, root=tmp_path)

    execution_file = tmp_path / "src/btcquant/execution/irrelevant.py"
    execution_file.write_text("VALUE = 2\n", encoding="utf-8")

    assert quantitative_source_sha256(entrypoint, root=tmp_path) == before


def test_new_module_in_used_package_is_included(tmp_path: Path) -> None:
    entrypoint = _write_quant_fixture(tmp_path)
    before = quantitative_source_sha256(entrypoint, root=tmp_path)

    new_module = tmp_path / "src/btcquant/backtest/new_relevant_module.py"
    new_module.write_text("VALUE = 3\n", encoding="utf-8")

    files = quantitative_source_files(entrypoint, root=tmp_path)
    assert new_module in files
    assert quantitative_source_sha256(entrypoint, root=tmp_path) != before


@pytest.mark.parametrize("kind", ["config", "data"])
def test_modified_config_or_data_expires_reference(tmp_path: Path, kind: str) -> None:
    reference_file = tmp_path / f"{kind}.json"
    reference_file.write_text("canonical\n", encoding="utf-8")
    expected = _sha256(reference_file)
    _check_file(reference_file, expected, kind)

    reference_file.write_text("changed\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="périmée"):
        _check_file(reference_file, expected, kind)
