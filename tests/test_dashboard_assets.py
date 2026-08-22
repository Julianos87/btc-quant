from dashboard import app as dashboard


def test_dashboard_loads_external_versioned_assets(monkeypatch):
    monkeypatch.setattr(dashboard, "AUTH_TOKEN", None)
    client = dashboard.app.test_client()

    page = client.get("/")
    assert page.status_code == 200
    assert b'<link rel="stylesheet" href="/static/dashboard.css">' in page.data
    assert b'<script src="/static/operational_state.js" defer></script>' in page.data
    assert b'<script src="/static/dashboard_ux.js" defer></script>' in page.data
    assert b'<script src="/static/dashboard.js" defer></script>' in page.data
    assert b'<script src="/static/effects.js" defer></script>' in page.data
    assert b'data-view="monitor"' in page.data
    assert b'id="board"' in page.data
    assert b"Paper synth" in page.data
    assert b"<style>" not in page.data
    assert b"<script>" not in page.data
    assert b"fonts.googleapis.com" not in page.data
    assert b"fonts.gstatic.com" not in page.data
    assert b"BTC-PERP / USDC" in page.data
    assert b"Hyperliquid" in page.data
    assert b'id="signal-guide"' in page.data
    assert b"cl\xc3\xb4ture d\xe2\x80\x99une bougie 4 h" in page.data
    assert b'data-i18n="threshold_active"' in page.data
    assert b'data-i18n="threshold_inactive"' in page.data
    assert b'data-i18n="waiting_zone"' in page.data

    assert client.get("/static/dashboard.css").status_code == 200
    assert client.get("/static/dashboard.js").status_code == 200
    assert client.get("/static/effects.js").status_code == 200


def test_content_security_policy_rejects_inline_scripts(monkeypatch):
    monkeypatch.setattr(dashboard, "AUTH_TOKEN", None)
    response = dashboard.app.test_client().get("/")

    policy = response.headers["Content-Security-Policy"]
    assert "script-src 'self'" in policy
    assert "script-src 'self' 'unsafe-inline'" not in policy
    assert "fonts.googleapis.com" not in policy
    assert "fonts.gstatic.com" not in policy


def test_frontend_escapes_remote_text_before_html_injection():
    javascript = (dashboard.ROOT / "dashboard" / "static" / "dashboard.js").read_text(
        encoding="utf-8"
    )

    assert "const esc =" in javascript
    assert "${esc(e.msg)}" in javascript
    assert "${esc(r.reason)}" in javascript
    assert "${esc(sl.name)}" in javascript
    assert 'btcusdt:"BTC-PERP / USDC"' in javascript
    assert 'signal_wait:"ATTENTE · AUCUN SIGNAL"' in javascript
    assert (
        'label: `${ch.name} ${ch.direction}${ch.active ? "" : ` · ${t("inactive_suffix")}`}`'
        in javascript
    )
    assert 'direction:"LONG", active:!isShort' in javascript
    assert 'direction:"SHORT", active:isShort' in javascript
    assert "const waiting = candles.map" in javascript
    assert "if (all.length < 10 || !sideKnown)" in javascript


def test_position_cards_expose_operational_detail_contract(monkeypatch):
    monkeypatch.setattr(dashboard, "AUTH_TOKEN", None)
    page = dashboard.app.test_client().get("/").data

    for element_id in (
        b"trend-open-slots",
        b"trend-total-notional",
        b"trend-total-upnl",
        b"trend-protection",
        b"trend-protection-mode",
        b"carry-gross-net",
        b"carry-pnl-net",
        b"carry-accounting",
        b"exp-trend",
        b"exp-carry",
        b"exp-protection",
        b"exp-freshness",
        b"trend-next-boundary",
        b"carry-entry-price",
        b"carry-funding-gross",
        b"exp-carry-spot",
        b"exp-net",
        b"exp-ratio",
    ):
        assert b'id="' + element_id + b'"' in page
    assert b'class="position-table"' in page
    assert b"Brut total = somme des notionnels absolus" in page
    javascript = (dashboard.ROOT / "dashboard" / "static" / "dashboard.js").read_bytes()
    assert b"prix observ\xc3\xa9" in javascript.lower()
    assert b"PROTECTION INCONNUE" in javascript
    assert b"PROTECTION NON CONFIRM\xc3\x89E" in javascript
    assert b"aucune position venue" in page
