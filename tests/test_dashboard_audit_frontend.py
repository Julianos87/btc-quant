"""Behavioral regressions execute the shipped functions in a small Node DOM harness."""

import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "dashboard/static/dashboard.js").read_text()


def node(source):
    result = subprocess.run(["node", "-e", source], check=True, text=True, capture_output=True)
    return json.loads(result.stdout)


def test_preferences_recover_from_corruption_wrong_schema_and_denied_storage():
    source = JS[JS.index("const PREFS_DEFAULT") : JS.index("const LOCALE")]
    result = node(
        'let localStorage={getItem:()=>"{",setItem:()=>{throw Error("denied")}};'
        + source
        + """
      const malformed=PREFS; localStorage.getItem=()=>JSON.stringify({hidden:null,lang:3,refresh:-1,range:999,ddAlert:"oops"});
      const badSchema=loadPreferences(); localStorage.getItem=()=>{throw Error("denied")};
      const denied=loadPreferences(); savePrefs(); console.log(JSON.stringify({malformed,badSchema,denied}));"""
    )
    for prefs in result.values():
        assert prefs["hidden"] == {}
        assert prefs["lang"] == "fr"
        assert prefs["refresh"] == 30000
        assert prefs["ddAlert"] == 12


def test_campaign_not_started_is_explicit_and_includes_campaign_blocker():
    result = node(
        f"const ux=require({json.dumps(str(ROOT / 'dashboard/static/dashboard_ux.js'))});"
        + """
      const r={campaign_status:"NOT_STARTED",ready:false,checks:[{key:"campaign",value:"non démarrée",passed:false}]};
      console.log(JSON.stringify({line:ux.campaignLine(r),decision:ux.decision(r),blockers:ux.blockers(r)}));"""
    )
    assert result["line"] == "Campagne non démarrée"
    assert result["decision"]["verdict"] == "NON PRÊT"
    assert result["blockers"][0]["key"] == "campaign"


HARNESS = """
const nodes={}; const $=id=>nodes[id] ||= {value:"",textContent:"",innerHTML:"",hidden:false};
["tr-from","tr-to","trades","tr-stats","tr-scope","tr-more","tr-status","tr-retry"].forEach($);
const link={href:"/api/trades.csv"}; const document={querySelector:()=>link};
const PREFS={lang:"fr"}; let tradeLimit=12, tradePageOffset=0,tradeLoadedRows=[],tradeAppliedFilter=null,lastTradeRows=[];
let sequence=0;const tradeRequestGate={begin:()=>++sequence,isCurrent:s=>s===sequence};let tradeAbortController=null;
const scheduleRedraw=()=>{},fmt$=v=>String(v),esc=v=>String(v),t=v=>v;
let fail=false; const requests=[]; let total=30;
async function fetchDashboard(url) {requests.push(url);if(fail)throw Error("503");const p=new URL(url,"http://local").searchParams;
const offset=+p.get("offset"),limit=+p.get("limit");
return {ok:true,json:async()=>({stats:{n:total,wins:1,pnl:0},has_more:offset+limit<total,rows:Array.from({length:Math.max(0,Math.min(limit,total-offset))},(_,i)=>({exit_ts:"2026-10-22",strategy:"D20",direction:"LONG",pnl:1,reason:"",id:offset+i}))})};}
"""


def test_trade_poll_preserves_depth_and_failed_filter_retains_applied_csv_then_recovers():
    functions = JS[JS.index("function tradeFilter()") : JS.index("function dashboardValue")]
    result = node(
        HARNESS
        + functions
        + """
(async()=>{
 await refreshTrades();await refreshTrades({append:true});const expanded=tradeLoadedRows.length;
 await refreshTrades();const polled=tradeLoadedRows.length;
 $("tr-from").value="2026-10-30";fail=true;try{await refreshTrades()}catch(_){}
 const failure={rows:tradeLoadedRows.length,notice:$("tr-status").textContent,csv:link.href,retry:!$("tr-retry").hidden};
 fail=false;total=1;await refreshTrades();const recovered={rows:tradeLoadedRows.length,notice:$("tr-status").hidden,csv:link.href};
 console.log(JSON.stringify({expanded,polled,failure,recovered,requests}));})();"""
    )
    assert result["expanded"] == result["polled"] == 24
    assert result["failure"]["rows"] == 24
    assert "Filtre non appliqué" in result["failure"]["notice"]
    assert result["failure"]["csv"] == "/api/trades.csv"
    assert result["failure"]["retry"]
    assert result["recovered"] == {
        "rows": 1,
        "notice": True,
        "csv": "/api/trades.csv?from=2026-10-30",
    }


def test_session_redirect_is_classified_and_stops_repeated_requests():
    functions = JS[JS.index("let sessionExpired") : JS.index("// ── i18n")]
    result = node(
        """let calls=0;const notice={hidden:true,innerHTML:""};const $=()=>notice;const PREFS={lang:"fr"};
const window={setTimeout,clearTimeout};const FETCH_TIMEOUT_MS=12;const fetch=()=>{};
const DASHBOARD_STATE={fetchResponseWithTimeout:async()=>{calls++;return {ok:true,redirected:true,url:"https://local/login",status:200,json:async()=>{throw Error("HTML")}}}};
"""
        + functions
        + """(async()=>{let errors=[];for(let i=0;i<2;i++)try{await fetchDashboard("/api/summary")}catch(e){errors.push(e.name)};
console.log(JSON.stringify({calls,errors,notice}));})();"""
    )
    assert result["calls"] == 1
    assert result["errors"] == ["SessionExpiredError"] * 2
    assert 'href="/login"' in result["notice"]["innerHTML"]
    assert not result["notice"]["hidden"]


def test_incomparable_reference_never_renders_normality_or_exact_bucket_probability():
    functions = JS[
        JS.index("async function refreshConformity()") : JS.index("// ── critères go/no-go")
    ]
    result = node(
        """const root={};const $=()=>root;const esc=String;
async function fetchDashboard(){return {ok:true,json:async()=>({reference:{profile:"Trend 4x",win_rate:.4,monthly_return_p10:-.1,monthly_return_p90:.2,worst_month:-.3},drawdown:{current:-.073,backtest_time_at_least_as_deep:.904},realized:null})}}
"""
        + functions
        + """(async()=>{await refreshConformity();console.log(JSON.stringify(root.innerHTML))})();"""
    )
    assert "comparabilité non établie" in result
    assert "90,4" not in result
    assert "→ normal" not in result
    assert "var(--good)" not in result


def test_poll_preserves_window_above_api_cap_and_empty_period():
    functions = JS[JS.index("function tradeFilter()") : JS.index("function dashboardValue")]
    result = node(
        HARNESS
        + functions
        + """
(async()=>{
 total=650;tradeLimit=500;await refreshTrades();await refreshTrades({append:true});
 const expanded=tradeLoadedRows.length;await refreshTrades();const polled=tradeLoadedRows.length;
 total=0;$("tr-from").value="2030-01-01";await refreshTrades();
 console.log(JSON.stringify({expanded,polled,empty:tradeLoadedRows.length,moreHidden:$("tr-more").hidden,requests}));})();"""
    )
    assert result["expanded"] == result["polled"] == 650
    assert result["empty"] == 0
    assert result["moreHidden"]
    assert any("offset=500" in request for request in result["requests"])


def test_initial_theme_is_applied_before_first_toggle():
    start = JS.index("(function initTheme()")
    end = JS.index("})();", start) + len("})();")
    result = node(
        """const attrs={}; const button={setAttribute:()=>{}};
const document={documentElement:{setAttribute:(k,v)=>attrs[k]=v,getAttribute:k=>attrs[k]}};
const readStorage=()=>"invalid";const writeStorage=()=>{};
const matchMedia=()=>({matches:false});const $=()=>button;const scheduleRedraw=()=>{};
"""
        + JS[start:end]
        + """const initial=attrs["data-theme"];button.onclick();
console.log(JSON.stringify({initial,afterToggle:attrs["data-theme"]}));"""
    )
    assert result == {"initial": "light", "afterToggle": "dark"}
