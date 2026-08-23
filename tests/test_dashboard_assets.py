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
    assert b"Brut = somme des notionnels absolus" in page
    javascript = (dashboard.ROOT / "dashboard" / "static" / "dashboard.js").read_bytes()
    assert b"prix observ\xc3\xa9" in javascript.lower()
    assert b"? Inconnu" in javascript
    assert b"! Non prot\xc3\xa9g\xc3\xa9" in javascript
    assert b"carry.position_status" in javascript
    assert b"badge unknown" in javascript
    assert "ÉTAT INCONNU".encode() in javascript
    assert b"aucune position venue" in page


def test_dashboard_visual_hierarchy_uses_progressive_disclosure(monkeypatch):
    monkeypatch.setattr(dashboard, "AUTH_TOKEN", None)
    html = dashboard.app.test_client().get("/").data.decode("utf-8")
    css = (dashboard.ROOT / "dashboard" / "static" / "dashboard.css").read_text(encoding="utf-8")
    javascript = (dashboard.ROOT / "dashboard" / "static" / "dashboard.js").read_text(
        encoding="utf-8"
    )

    trend = html.split('id="trend-overview"', 1)[1].split('class="table-wrap', 1)[0]
    assert trend.count('class="summary-item"') == 4
    assert 'class="summary-item trend-next"' not in trend
    assert 'class="trend-next-line"' in trend
    assert '<details class="technical-details carry-tech-details">' in html
    assert '<details class="technical-details carry-tech-details" open' not in html
    assert "Détails techniques" in html

    assert "@media (max-width:899px)" in css
    assert ".position-table thead { display:none; }" in css
    assert ".position-table .protection-cell { grid-column:1/-1;" in css
    assert "overflow-x:clip" in css
    assert 'role="button" tabindex="0"' in javascript
    assert "slot.protection_reason" in javascript
    assert "raison ${protectionReason}" not in javascript
    for field in (
        "carry-funding-price-source",
        "carry-ledger-status",
        "carry-entry-price",
        "carry-state-age",
    ):
        assert f'id="{field}"' in html
