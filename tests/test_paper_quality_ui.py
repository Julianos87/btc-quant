"""Paper quality labels must not promote heartbeats into reconciliation/protection."""

import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "dashboard/static/dashboard.js").read_text()


def node(source):
    result = subprocess.run(["node", "-e", source], check=True, text=True, capture_output=True)
    return json.loads(result.stdout)


def test_cockpit_rejects_fresh_heartbeat_with_stale_or_unknown_progression():
    functions = JS[
        JS.index("function trendQualityIssues") : JS.index("function updateDataFreshness")
    ]
    result = node(
        """const nodes={};const $=id=>nodes[id] ||= {dataset:{},textContent:""};const t=x=>x;
const BTCQuantOperationalState={componentAvailabilitySeverity:()=>null};
"""
        + functions
        + """
const s={trend:{alive:true,freshness:"FRESH",open_slots:0,slots:[],progression:{status:"STALE"}},carry:{},health:{safety_status:"PASS"}};
renderCockpitStatus(s);const stale={status:nodes["cockpit-status"].dataset.status,detail:nodes["status-detail"].textContent};
delete s.trend.progression;renderCockpitStatus(s);const unknown=nodes["cockpit-status"].dataset.status;
s.trend.progression={status:"FRESH"};renderCockpitStatus(s);const good=nodes["cockpit-status"].dataset.status;
s.trend.open_slots=1;s.trend.protection_status="UNSAFE";renderCockpitStatus(s);const unsafe=nodes["cockpit-status"].dataset.status;
console.log(JSON.stringify({stale,unknown,good,unsafe}));"""
    )
    assert result["stale"] == {"status": "crit", "detail": "progression_stale"}
    assert result["unknown"] == "warn"
    assert result["good"] == "good"
    assert result["unsafe"] == "crit"


def test_claimed_active_stop_requires_each_open_slot_to_be_protected():
    helper = JS[JS.index("function trendQualityIssues") : JS.index("function renderCockpitStatus")]
    result = node(
        "const t=x=>x;"
        + helper
        + """
const s={trend:{progression:{status:"FRESH"},protection_status:"ACTIVE",open_slots:1,slots:[{state:"LONG",qty:1,protection_protected:false}]}};
const unverified=trendQualityIssues(s);s.trend.slots[0].protection_protected=true;
console.log(JSON.stringify({unverified,verified:trendQualityIssues(s)}));"""
    )
    assert result["unverified"] == [["crit", "stop_unverified"]]
    assert result["verified"] == []


def test_accounting_title_and_freshness_never_claim_reconciliation():
    html = (ROOT / "dashboard/index.html").read_text()
    assert 'data-i18n="accounting_title">Situation comptable' in html
    assert "Comptabilité réconciliée" not in html
    function = JS[
        JS.index("function renderAccountingPanel") : JS.index("function renderTrendAccounting")
    ]
    result = node(
        """const nodes={"portfolio-accounting-grid":{},"portfolio-accounting-state":{},"accounting-alerts":{}};
const $=id=>nodes[id];const PREFS={lang:"fr"};const t=x=>({accounting_read:"Données lues",accounting_unverified:"Réconciliation non attestée"}[x]||x);
const dashboardValue=()=>"N/A",esc=String,statusClass=()=>"",fmtTimeUTC=String;
"""
        + function
        + """renderAccountingPanel({portfolio:{status:"FRESH",last_reconciliation:{ts:"2026-11-01",event_type:"RECONCILIATION_SUCCESS"}}});console.log(JSON.stringify(nodes));"""
    )
    assert result["portfolio-accounting-state"]["textContent"] == "Données lues · FRESH"
    assert "Réconciliation non attestée" in result["portfolio-accounting-grid"]["innerHTML"]
    assert "Réconciliation non attestée" in result["accounting-alerts"]["innerHTML"]


def test_active_carry_documentation_matches_service_and_cash_configuration():
    readme = (ROOT / "README.md").read_text()
    doc = (ROOT / "docs/CARRY_CASH_FUNDED_PAPER.md").read_text()
    service = (ROOT / "deploy/btcquant-carry.service").read_text()
    command = next(
        line.removeprefix("ExecStart=")
        for line in service.splitlines()
        if line.startswith("ExecStart=")
    )
    assert command in readme and command in doc
    assert "btcquant-carry --capital 4000 --leverage 3" not in readme
    assert "uncertainty_reserve_rate: 0.0" in readme and "uncertainty_reserve_rate: 0.0" in doc
    assert "cash_utilization: 0.98" in readme and "cash_utilization: 0.98" in doc
    assert "x3 (profil actif)" not in readme
