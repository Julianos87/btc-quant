from __future__ import annotations

import json
import re
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
    script = 'const PREFS = {lang:"fr"}; const t = () => "Indisponible"; const LOCALE = () => "fr-FR";\n' + f"{helper}\nprocess.stdout.write(trendDirectionSummary({json.dumps(trend)}));"
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
        ({"open_slots": 3, "slots": [{"state": "LONG"}] * 3}, "3 Long"),
        (
            {"open_slots": 3, "slots": [{"state": "LONG"}] * 2 + [{"state": "SHORT"}]},
            "2 Long · 1 Short",
        ),
        ({"open_slots": 1, "slots": [{"state": "UNKNOWN"}]}, "1 indisponible"),
        (
            {"open_slots": 3, "slots": [{"state": "LONG"}] * 2 + [{"state": "UNKNOWN"}]},
            "2 Long · 1 indisponible",
        ),
        ({"open_slots": 0, "slots": [{"state": "FLAT"}] * 3}, "Aucune position ouverte"),
        ({"open_slots": 1}, "Indisponible"),
        ({"open_slots": 0}, "Indisponible"),
        ({"open_slots": 1, "slots": [{}]}, "Indisponible"),
        ({"open_slots": None, "slots": [{"state": "FLAT"}]}, "Indisponible"),
    ],
)
def test_trend_summary_preserves_explicit_unknown_states(trend: dict, expected: str) -> None:
    result = _trend_summary(trend)
    assert result == expected
    if "indisponible" in expected.lower():
        assert "Aucune position ouverte" not in result


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for JS regression cases")
def test_monitor_masonry_span_tracks_rendered_card_height() -> None:
    javascript = (dashboard.ROOT / "dashboard" / "static" / "dashboard.js").read_text(
        encoding="utf-8"
    )
    start = javascript.index("function monitorGridSpan")
    end = javascript.index("\n\nfunction layoutMonitorBoard", start)
    helper = javascript[start:end]
    script = (
        f"{helper}\n"
        "process.stdout.write(JSON.stringify(["
        "monitorGridSpan(0, 1, 14),"
        "monitorGridSpan(1, 1, 14),"
        "monitorGridSpan(239, 1, 14),"
        "monitorGridSpan(616, 1, 14)"
        "]));"
    )
    result = subprocess.run(
        ["node", "--input-type=commonjs", "--eval", script],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout) == [1, 1, 17, 42]


def test_wide_monitor_layout_keeps_sidebar_and_columns_independent() -> None:
    css = (dashboard.ROOT / "dashboard" / "static" / "dashboard.css").read_text(encoding="utf-8")
    javascript = (dashboard.ROOT / "dashboard" / "static" / "dashboard.js").read_text(
        encoding="utf-8"
    )

    assert ".system-rail" not in css
    assert ".decision-board.monitor-masonry" in css
    for card, column in (("trend", "1"), ("carry", "2"), ("events", "1"), ("journal", "2")):
        selector = f'body[data-view="monitor"] .decision-board.monitor-masonry > [data-card="{card}"]'
        rule = re.search(re.escape(selector) + r"\s*\{([^{}]*)\}", css)
        assert rule and f"grid-column:{column};" in rule.group(1)
    assert "card.style.gridRowEnd = rowEnd" in javascript
    assert "ResizeObserver(scheduleMonitorLayout)" in javascript
