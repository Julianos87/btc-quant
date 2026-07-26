from __future__ import annotations

import sys

import pytest

from btcquant.entrypoints import carry, digest, readiness, rebalance, trend, watchdog


@pytest.mark.parametrize(
    "entrypoint",
    [trend.main, carry.main, readiness.main, digest.main, rebalance.main, watchdog.main],
)
def test_installed_entrypoint_help_is_usable(entrypoint, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["btcquant", "--help"])

    with pytest.raises(SystemExit) as stopped:
        entrypoint()

    assert stopped.value.code == 0
    assert "usage:" in capsys.readouterr().out


def test_readiness_console_output_is_cp1252_safe(capsys):
    readiness._print_report(
        {
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
    )
    output = capsys.readouterr().out
    assert ">= 90 j" in output
    output.encode("cp1252")
