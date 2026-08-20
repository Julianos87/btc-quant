from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from btcquant.entrypoints import (
    carry,
    carry_cutover,
    digest,
    readiness,
    rebalance,
    shadow,
    trend,
    watchdog,
)


def test_digest_import_follows_btcquant_root_so_validation_must_unset_it(tmp_path, monkeypatch):
    """Runtime root is not a source tree; digest loads paper config at import.

    Release validation therefore unsets BTCQUANT_ROOT rather than planting a
    fake environments/ directory under the deploy root.
    """

    runtime_root = tmp_path / "runtime-root"
    runtime_root.mkdir()
    monkeypatch.setenv("BTCQUANT_ROOT", str(runtime_root))
    with pytest.raises(FileNotFoundError, match="environments/paper/config.yaml"):
        importlib.reload(digest)
    monkeypatch.delenv("BTCQUANT_ROOT", raising=False)
    monkeypatch.chdir(Path(__file__).resolve().parents[1])
    importlib.reload(digest)
    importlib.reload(rebalance)


@pytest.mark.parametrize(
    "entrypoint",
    [
        trend.main,
        carry.main,
        carry_cutover.main,
        readiness.main,
        digest.main,
        rebalance.main,
        watchdog.main,
        shadow.main,
    ],
)
def test_installed_entrypoint_help_is_usable(entrypoint, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["btcquant", "--help"])

    with pytest.raises(SystemExit) as stopped:
        entrypoint()

    assert stopped.value.code == 0
    assert "usage:" in capsys.readouterr().out


REPORT_WITH_NON_LATIN1_SYMBOLS = {
    "campaign_id": 1,
    "protocol_version": 1,
    "status": "FAIL",
    "n_ok": 0,
    "n_total": 1,
    "checks": [
        {
            "passed": False,
            "label": "Durée observée",
            "value": "0 j",
            "target": "≥ 90 j",
        }
    ],
}


def test_readiness_report_keeps_its_symbols(capsys):
    """Les symboles ne sont plus translittérés : la console est passée en UTF-8.

    L'ancienne approche remplaçait « ≥ » par « >= » dans trois cas connus, ce
    qui laissait tomber tout caractère non anticipé et affichait des accents
    illisibles sur le poste Windows.
    """
    readiness._print_report(REPORT_WITH_NON_LATIN1_SYMBOLS)
    output = capsys.readouterr().out
    assert "≥ 90 j" in output
    assert "Durée observée" in output


def test_readiness_output_survives_a_cp1252_console(monkeypatch):
    """Sur une console Windows héritée, l'affichage doit se dégrader, pas planter.

    `enable_utf8_output` reconfigure les flux ; quand c'est impossible (flux
    déjà détaché, redirection exotique), la sortie doit rester tolérante.
    """
    import io

    from btcquant.console import enable_utf8_output

    buffer = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stdout", buffer)
    enable_utf8_output()

    readiness._print_report(REPORT_WITH_NON_LATIN1_SYMBOLS)  # ne doit pas lever

    buffer.flush()
