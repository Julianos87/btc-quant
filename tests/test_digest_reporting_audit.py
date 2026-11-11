"""Missing canonical observations must not generate a fictitious loss digest."""

from pathlib import Path

import pytest

from btcquant.entrypoints import digest
from btcquant.execution.state_store import StateStore


@pytest.mark.parametrize("engine", [None, "trend", "carry"])
def test_missing_engine_reports_unavailable_instead_of_total_loss(
    tmp_path: Path, monkeypatch, engine
):
    store = StateStore(tmp_path / "btcquant.db")
    if engine == "trend":
        store.save_engine_state("trend", {"slots": {"test": {"cash": 5000}}})
    elif engine == "carry":
        store.save_engine_state("carry", {"equity": 5000})
    # Historical cold backups must not turn an incomplete canonical source into a balance.
    (tmp_path / "live_state_4x.json").write_text('{"slots":{"test":{"cash":5000}}}')
    (tmp_path / "carry_state.json").write_text('{"equity":5000}')
    messages = []
    monkeypatch.setattr(digest, "STATE", tmp_path)
    monkeypatch.setattr(digest, "notify", messages.append)
    monkeypatch.setattr("sys.argv", ["digest"])
    digest.main()
    assert len(messages) == 1
    assert "bilan indisponible" in messages[0]
    assert "-100" not in messages[0]
    assert "0 $" not in messages[0]


def test_real_zero_balance_remains_a_valid_observation(tmp_path: Path, monkeypatch):
    store = StateStore(tmp_path / "btcquant.db")
    store.save_engine_state("trend", {"slots": {"test": {"cash": 0}}})
    store.save_engine_state("carry", {"equity": 0})
    messages = []
    monkeypatch.setattr(digest, "STATE", tmp_path)
    monkeypatch.setattr(digest, "notify", messages.append)
    monkeypatch.setattr("sys.argv", ["digest"])
    digest.main()
    assert len(messages) == 1
    assert "Équity totale : 0 $" in messages[0]
    assert "-100.0%" in messages[0]
    assert "bilan indisponible" not in messages[0]
