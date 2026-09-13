"""Architecture boundary tests for the extracted engine-state repository."""

from __future__ import annotations

import ast
from pathlib import Path


def test_engine_state_repository_has_no_venue_or_financial_imports():
    path = Path(__file__).parents[1] / "src/btcquant/execution/engine_state_repository.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    imported.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    forbidden = {
        "ccxt",
        "btcquant.broker",
        "btcquant.venue",
        "btcquant.execution.financial_settlement",
    }
    assert imported.isdisjoint(forbidden)
