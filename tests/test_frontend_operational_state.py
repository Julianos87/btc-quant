from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "dashboard" / "static" / "operational_state.js"


def _node(expression: str):
    source = f"const helper=require({json.dumps(str(HELPER))}); {expression}"
    result = subprocess.run(
        ["node", "-e", source],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_optional_carry_unknown_not_frontend_critical() -> None:
    severities = _node(
        "console.log(JSON.stringify(["
        "helper.componentAvailabilitySeverity('carry', {alive:false, freshness:'UNKNOWN'}, {required_components:['trend']}),"
        "helper.componentAvailabilitySeverity('carry', {alive:false, freshness:'UNKNOWN'}, {required_components:['trend','carry']})"
        "]))"
    )
    assert severities == ["warn", "crit"]


def test_required_carry_unknown_frontend_critical() -> None:
    severity = _node(
        "console.log(JSON.stringify(helper.componentAvailabilitySeverity("
        "'carry', {alive:false, freshness:'UNAVAILABLE'}, {required_components:['trend','carry']})))"
    )
    assert severity == "crit"


def test_optional_carry_freshness_does_not_send_critical_browser_notification() -> None:
    values = _node(
        "console.log(JSON.stringify(["
        "helper.shouldNotifyFreshnessTransition('carry', true, false, {required_components:['trend']}),"
        "helper.shouldNotifyFreshnessTransition('carry', true, false, {required_components:['trend','carry']})"
        "]))"
    )
    assert values == [False, True]


def test_required_recovery_rearms_notification() -> None:
    values = _node(
        "console.log(JSON.stringify(["
        "helper.shouldNotifyFreshnessTransition('carry', true, false, {required_components:['carry']}),"
        "helper.shouldNotifyFreshnessTransition('carry', false, true, {required_components:['carry']}),"
        "helper.shouldNotifyFreshnessTransition('carry', true, false, {required_components:['carry']})"
        "]))"
    )
    assert values == [True, False, True]


def test_execution_safety_failure_is_critical_independent_of_component_profile() -> None:
    values = _node(
        "console.log(JSON.stringify(["
        "helper.shouldNotifySafetyFailureTransition('PASS', 'FAIL'),"
        "helper.shouldNotifySafetyFailureTransition('FAIL', 'FAIL'),"
        "helper.shouldNotifySafetyFailureTransition('UNKNOWN', 'FAIL')"
        "]))"
    )
    assert values == [True, False, True]


def test_dashboard_loads_operational_state_before_dashboard_script() -> None:
    html = (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
    assert html.index("/static/operational_state.js") < html.index("/static/dashboard.js")
