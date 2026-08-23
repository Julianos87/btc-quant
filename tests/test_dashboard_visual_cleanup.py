from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from dashboard import app as dashboard


def _trend_summary(trend: dict) -> str:
    javascript = (dashboard.ROOT / "dashboard" / "static" / "dashboard.js").read_text(
        encoding="utf-8"
    )
    start = javascript.index("  function trendDirectionSummary")
    end = javascript.index("\n\n  function renderTrendOverview", start)
    helper = javascript[start:end]
    script = f"{helper}\nprocess.stdout.write(trendDirectionSummary({json.dumps(trend)}));"
    result = subprocess.run(
        ["node", "--input-type=commonjs", "--eval", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for JS regression cases")
@pytest.mark.parametrize(
    ("trend", "expected"),
    [
        ({"open_slots": 3, "slots": [{"state": "LONG"}] * 3}, "3 LONG"),
        (
            {"open_slots": 3, "slots": [{"state": "LONG"}] * 2 + [{"state": "SHORT"}]},
            "2 LONG · 1 SHORT",
        ),
        ({"open_slots": 1, "slots": [{"state": "UNKNOWN"}]}, "1 UNKNOWN"),
        (
            {"open_slots": 3, "slots": [{"state": "LONG"}] * 2 + [{"state": "UNKNOWN"}]},
            "2 LONG · 1 UNKNOWN",
        ),
        ({"open_slots": 0, "slots": [{"state": "FLAT"}] * 3}, "FLAT"),
        ({"open_slots": 1}, "N/A"),
        ({"open_slots": 0}, "N/A"),
        ({"open_slots": 1, "slots": [{}]}, "N/A"),
        ({"open_slots": None, "slots": [{"state": "FLAT"}]}, "état indisponible"),
    ],
)
def test_trend_summary_preserves_explicit_unknown_states(trend: dict, expected: str) -> None:
    result = _trend_summary(trend)
    assert result == expected
    if "UNKNOWN" in expected:
        assert "FLAT" not in result
