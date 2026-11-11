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
    script = (
        'const PREFS = {lang:"fr"}; const t = () => "Indisponible"; const LOCALE = () => "fr-FR";\n'
        + f"{helper}\nprocess.stdout.write(trendDirectionSummary({json.dumps(trend)}));"
    )
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
@pytest.mark.parametrize(
    "positions",
    [[], [50], [80] * 8, [-200, -20, 0, 1], [199, 200, 201, 700], [80, 81, 82, 83]],
)
def test_chart_annotations_do_not_collide_or_leave_plot(positions: list[int]) -> None:
    javascript = (dashboard.ROOT / "dashboard" / "static" / "dashboard.js").read_text(
        encoding="utf-8"
    )
    start = javascript.index("function placeChartLabels")
    helper = javascript[start : javascript.index("\nfunction chartValueList", start)]
    items = [{"id": index, "y": y} for index, y in enumerate(positions)]
    script = (
        helper
        + "\nprocess.stdout.write(JSON.stringify(placeChartLabels("
        + json.dumps(items)
        + ", 10, 210, 18)));"
    )
    result = subprocess.run(
        ["node", "--input-type=commonjs", "--eval", script],
        check=True,
        capture_output=True,
        text=True,
    )
    placed = json.loads(result.stdout)
    assert sorted(item["id"] for item in placed) == list(range(len(items)))
    assert all(10 <= item["y"] <= 210 for item in placed)
    assert all(
        right["y"] - left["y"] >= 18 for left, right in zip(placed, placed[1:], strict=False)
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for JS regression cases")
def test_view_switches_preserve_card_identity_and_reading_priority() -> None:
    javascript = (dashboard.ROOT / "dashboard" / "static" / "dashboard.js").read_text(
        encoding="utf-8"
    )
    start = javascript.index("const VIEW_CARDS")
    helper = javascript[start : javascript.index("\nfunction setChartUnit", start)]
    page = (dashboard.ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
    card_names = re.findall(r'data-card="([^"]+)"', page)
    # Exercise the real view routine against stable DOM node objects. Moving cards
    # must preserve open disclosures and match the reading priority after any tab history.
    script = (
        """
const names = CARD_NAMES;
const cards = names.map(name => ({dataset:{card:name}, details:{open:true}}));
const original = new Map(cards.map(card => [card.dataset.card, card]));
const board = {
  querySelector(selector) { return cards.find(card => selector.includes('"' + card.dataset.card + '"')); },
  append(card) { cards.splice(cards.indexOf(card), 1); cards.push(card); }
};
const element = {classList:{toggle(){}}, setAttribute(){}};
const buttons = ['monitor','performance','risk'].map(view => ({...element,dataset:{view}}));
const document = {
  body:{dataset:{}},
  querySelector() { return board; },
  querySelectorAll(selector) {
    if(selector === '[data-card]') return cards;
    if(selector === '[data-view-tile]') return [];
    return buttons;
  }
};
const $ = () => element;
const setPressed = () => {};
const scheduleRedraw = () => {};
const requestAnimationFrame = fn => fn();
const PREFS = {view:'monitor'};
let unit = 'pct';
""".replace("CARD_NAMES", json.dumps(card_names))
        + helper
    )
    script += """
const results = [];
for (const view of ['monitor','risk','performance','monitor','performance','risk']) {
  PREFS.view = view;
  applyDashboardView();
  const visible = cards.filter(card => card.dataset.viewHidden === '0');
  results.push({view, names:visible.map(card => card.dataset.card), unit,
    preserved:visible.every(card => original.get(card.dataset.card) === card && card.details.open)});
}
process.stdout.write(JSON.stringify(results));
"""
    result = subprocess.run(
        ["node", "--input-type=commonjs", "--eval", script],
        check=True,
        capture_output=True,
        text=True,
    )
    for state in json.loads(result.stdout):
        names = state["names"]
        assert state["preserved"]
        assert len(names) == len(set(names))
        if state["view"] == "monitor":
            assert names[:2] == ["trend", "carry"]
            assert "dated_carry" in names
        elif state["view"] == "performance":
            assert names[:2] == ["performance_brief", "chart"]
            assert state["unit"] == "pct"
            assert "dated_carry" not in names
        else:
            assert names[:2] == ["risk_radar", "exposure"]
            assert state["unit"] == "dd"
