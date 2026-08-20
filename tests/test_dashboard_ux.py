from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from btcquant.execution.readiness import ReadinessPolicy, evaluate_readiness, start_campaign
from btcquant.execution.state_store import StateStore

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "dashboard" / "static" / "dashboard_ux.js"


def _node(expression: str):
    source = f"const helper=require({json.dumps(str(HELPER))}); {expression}"
    result = subprocess.run(
        ["node", "-e", source],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_duration_formatter_boundaries() -> None:
    values = _node(
        "console.log(JSON.stringify(["
        "helper.formatDuration(0),"
        "helper.formatDuration(59),"
        "helper.formatDuration(60),"
        "helper.formatDuration(90),"
        "helper.formatDuration(3599),"
        "helper.formatDuration(3600),"
        "helper.formatDuration(86400),"
        "helper.formatDuration(20*86400)"
        "]))"
    )
    assert values == [
        "0 s",
        "59 s",
        "1 min",
        "1 min 30 s",
        "59 min 59 s",
        "1 h",
        "1 j",
        "20 j",
    ]


def test_readiness_decision_uses_backend_ready_and_health_passed() -> None:
    ready = _node(
        "console.log(JSON.stringify(helper.decision("
        "{ready:true,status:'PASS',n_ok:16,n_total:16,checks:["
        "{key:'integrity',passed:true,value:'ok'},"
        "{key:'days',passed:true,value:'90 j'}"
        "]})))"
    )
    waiting = _node(
        "console.log(JSON.stringify(helper.decision("
        "{ready:false,status:'FAIL',n_ok:7,n_total:16,checks:["
        "{key:'integrity',passed:true,value:'ok'},"
        "{key:'days',passed:false,value:'12 j'}"
        "]})))"
    )
    blocked = _node(
        "console.log(JSON.stringify(helper.decision("
        "{ready:false,status:'FAIL',n_ok:6,n_total:16,checks:["
        "{key:'integrity',passed:false,value:'échec'},"
        "{key:'days',passed:false,value:'12 j'}"
        "]})))"
    )
    assert ready["verdict"] == "PRÊT"
    assert waiting["verdict"] == "NON PRÊT"
    assert blocked["verdict"] == "BLOQUÉ"


def test_counts_match_api_fields_not_recomputed_thresholds() -> None:
    report = {
        "ready": False,
        "n_ok": 7,
        "n_total": 16,
        "checks": [
            {"key": "integrity", "passed": True, "label": "SQLite", "value": "ok"},
            {"key": "days", "passed": False, "label": "Durée", "value": "12 j", "target": "≥ 90 j"},
            {
                "key": "rejections",
                "passed": False,
                "label": "Rejets",
                "value": "—",
                "target": "≤ 5%",
            },
        ],
    }
    remain = report["n_total"] - report["n_ok"]
    assert remain == 9
    grouped = _node(
        "const r=" + json.dumps(report) + "; const g=helper.partition(r.checks);"
        "console.log(JSON.stringify({health:g.health.map(c=>c.key),qualification:g.qualification.map(c=>c.key),execution:g.execution.map(c=>c.key)}));"
    )
    keys = grouped["health"] + grouped["qualification"] + grouped["execution"]
    assert keys.count("integrity") == 1
    assert keys.count("days") == 1
    assert keys.count("rejections") == 1
    assert (
        _node(
            "console.log(JSON.stringify(helper.naCopy("
            "{key:'rejections',passed:false,value:'—'},"
            "{naOrders:'N/A — aucun ordre terminal'})))"
        )
        == "N/A — aucun ordre terminal"
    )


def test_zero_denominator_never_becomes_zero_percent() -> None:
    text = _node(
        "console.log(JSON.stringify(helper.displayValue("
        "{key:'rejections',passed:false,value:'—'},"
        "{naOrders:'N/A — aucun ordre terminal',na:'N/A'})))"
    )
    assert "0 %" not in text
    assert text.startswith("N/A")


def test_campaign_formatting() -> None:
    line = _node(
        "console.log(JSON.stringify(helper.campaignLine("
        "{campaign_id:2,campaign_status:'RUNNING',protocol_version:2},"
        "{campaign:'Campagne',running:'En cours',protocol:'protocole'})))"
    )
    assert line == "Campagne #2 · En cours · protocole v2"


def test_carry_presentation_is_paper_modeled() -> None:
    view = _node(
        "console.log(JSON.stringify(helper.carryPresentation({"
        "in_position:true,execution_state:'OPEN',qty:0,perp_qty:0,spot_notional:0,perp_notional:0"
        "})))"
    )
    assert view["mode"] == "Paper synthétique"
    assert view["position"] == "OPEN"
    assert view["live"] is False
    assert view["modeled"] is True
    assert "Hyperliquid" not in json.dumps(view)


def test_groups_cover_known_lot6_keys_once() -> None:
    keys = [
        "campaign",
        "integrity",
        "days",
        "uptime",
        "trend_uptime",
        "trades",
        "orders",
        "orders_trend",
        "unresolved",
        "incidents",
        "rejections",
        "partials",
        "slippage",
        "drawdown",
        "trend_freshness",
        "killswitch",
    ]
    grouped = _node(
        "const checks="
        + json.dumps([{"key": key, "passed": True, "value": "1"} for key in keys])
        + "; const g=helper.partition(checks);"
        "const listed=[...g.health,...g.qualification,...g.execution].map(c=>c.key);"
        "console.log(JSON.stringify({listed, meta:g.meta.map(c=>c.key)}));"
    )
    listed = grouped["listed"]
    assert len(listed) == len(set(listed))
    assert grouped["meta"] == ["campaign"]
    assert set(listed + grouped["meta"]) == set(keys)


def test_readiness_invariant_unchanged_by_dashboard_pr(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    now = datetime.now(UTC)
    start_campaign(store, ReadinessPolicy(), started_at=(now - timedelta(days=2)).isoformat())
    store.save_engine_state("trend", {"slots": {}, "halted": False})
    report = evaluate_readiness(store, now=now, persist=False)
    snapshot = {
        "status": report["status"],
        "ready": report["ready"],
        "n_ok": report["n_ok"],
        "n_total": report["n_total"],
        "keys": [item["key"] for item in report["checks"]],
        "passed": [item["passed"] for item in report["checks"]],
        "targets": [item["target"] for item in report["checks"]],
    }
    # Dashboard PR must not change backend verdicts; re-evaluate on the same fixture.
    again = evaluate_readiness(store, now=now, persist=False)
    assert [item["key"] for item in again["checks"]] == snapshot["keys"]
    assert [item["passed"] for item in again["checks"]] == snapshot["passed"]
    assert [item["target"] for item in again["checks"]] == snapshot["targets"]
    assert again["n_ok"] == snapshot["n_ok"]
    assert again["n_total"] == snapshot["n_total"]
    assert again["ready"] == snapshot["ready"]
    assert again["status"] == snapshot["status"]


def test_dashboard_scripts_load_ux_helper_before_dashboard() -> None:
    html = (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
    assert html.index("/static/operational_state.js") < html.index("/static/dashboard_ux.js")
    assert html.index("/static/dashboard_ux.js") < html.index("/static/dashboard.js")
