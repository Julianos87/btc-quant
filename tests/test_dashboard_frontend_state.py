from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pandas as pd

from dashboard import app as dashboard


ROOT = Path(__file__).resolve().parents[1]
STATE_HELPER = ROOT / "dashboard" / "static" / "dashboard_state.js"


def _node(expression: str):
    source = f"const state=require({json.dumps(str(STATE_HELPER))}); {expression}"
    result = subprocess.run(["node", "-e", source], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def test_quantity_format_preserves_small_positive_btc_and_unknown_values() -> None:
    values = _node(
        "console.log(JSON.stringify(["
        "0, 0.00001, 0.0002, 0.001, 0.123456, 1, null, NaN, Infinity].map(value => state.formatQuantity(value))))"
    )
    assert values == [
        "0 BTC",
        "0,00001 BTC",
        "0,0002 BTC",
        "0,001 BTC",
        "0,123456 BTC",
        "1 BTC",
        "N/A BTC",
        "N/A BTC",
        "N/A BTC",
    ]
    assert all(value != "0 BTC" for value in values[1:6])


def test_summary_error_stays_unavailable_until_a_real_success() -> None:
    result = _node(
        "const source=new state.SourceRequestState();"
        "const first=source.begin(); source.succeed(first, 100);"
        "const failed=source.begin(); source.fail(failed, new Error('network'));"
        "const afterError=source.status;"
        "const recovered=source.begin(); source.succeed(recovered, 200);"
        "console.log(JSON.stringify({afterError, status:source.status, at:source.lastSuccessAt}));"
    )
    assert result == {"afterError": "unavailable", "status": "available", "at": 200}


def test_unavailable_source_stays_visible_during_retry() -> None:
    result = _node(
        "const source=new state.SourceRequestState();"
        "const first=source.begin(); source.succeed(first, 100);"
        "const failed=source.begin(); source.fail(failed, new Error('network'));"
        "source.begin();"
        "console.log(JSON.stringify({status:source.status, inFlight:source.inFlight}));"
    )
    assert result == {"status": "unavailable", "inFlight": True}


def test_timeout_covers_stalled_json_and_next_request_can_succeed() -> None:
    result = _node(
        "let calls=0, aborted=0;"
        "const fakeFetch=(url, options)=>{ calls++;"
        "if(calls===1) return Promise.resolve({ok:true,json:()=>new Promise((resolve,reject)=>{"
        "const abort=()=>{aborted++; const error=new Error('aborted'); error.name='AbortError'; reject(error);};"
        "options.signal.addEventListener('abort', abort, {once:true});"
        "})});"
        "return Promise.resolve({ok:true,json:async()=>({ok:true})});};"
        "(async()=>{const first=await state.fetchResponseWithTimeout(fakeFetch,'/stalled',{}, {timeoutMs:15});"
        "let errorName=''; try{await first.json();}catch(error){errorName=error.name;}"
        "const second=await state.fetchResponseWithTimeout(fakeFetch,'/next',{}, {timeoutMs:15});"
        "console.log(JSON.stringify({errorName, calls, aborted, payload:await second.json()}));})();"
    )
    assert result == {
        "errorName": "TimeoutError",
        "calls": 2,
        "aborted": 1,
        "payload": {"ok": True},
    }


def test_old_trade_response_cannot_become_current_after_filter_change() -> None:
    result = _node(
        "const gate=new state.LatestRequestGate();"
        "const first=gate.begin(); const second=gate.begin();"
        "console.log(JSON.stringify({old:gate.isCurrent(first), current:gate.isCurrent(second)}));"
    )
    assert result == {"old": False, "current": True}


def test_trades_endpoint_exposes_filtered_total_and_returned_rows(monkeypatch) -> None:
    frame = pd.DataFrame(
        [
            {"exit_ts": "2026-01-02T00:00:00Z", "pnl": 1.0},
            {"exit_ts": "2026-01-03T00:00:00Z", "pnl": -2.0},
            {"exit_ts": "2026-01-04T00:00:00Z", "pnl": 3.0},
        ]
    )
    monkeypatch.setattr(dashboard, "AUTH_TOKEN", None)
    monkeypatch.setattr(dashboard, "_read_trades", lambda: frame)

    response = dashboard.app.test_client().get(
        "/api/trades?from=2026-01-02&to=2026-01-04&limit=2",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["stats"] == {"n": 3, "wins": 2, "pnl": 2.0}
    assert payload["limit"] == 2
    assert payload["returned"] == 2
    assert payload["offset"] == 0
    assert payload["has_more"] is True

    older = dashboard.app.test_client().get(
        "/api/trades?from=2026-01-02&to=2026-01-04&limit=2&offset=2",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )
    older_payload = older.get_json()
    assert older_payload["returned"] == 1
    assert older_payload["offset"] == 2
    assert older_payload["has_more"] is False
    assert older_payload["rows"][0]["exit_ts"] == "2026-01-02T00:00:00Z"
