from dashboard import app as dashboard


def test_dashboard_loads_external_versioned_assets(monkeypatch):
    monkeypatch.setattr(dashboard, "AUTH_TOKEN", None)
    client = dashboard.app.test_client()

    page = client.get("/")
    assert page.status_code == 200
    assert b'<link rel="stylesheet" href="/static/dashboard.css">' in page.data
    assert b'<script src="/static/dashboard.js" defer></script>' in page.data
    assert b'<script src="/static/effects.js" defer></script>' in page.data
    assert b"<style>" not in page.data
    assert b"<script>" not in page.data

    assert client.get("/static/dashboard.css").status_code == 200
    assert client.get("/static/dashboard.js").status_code == 200
    assert client.get("/static/effects.js").status_code == 200


def test_content_security_policy_rejects_inline_scripts(monkeypatch):
    monkeypatch.setattr(dashboard, "AUTH_TOKEN", None)
    response = dashboard.app.test_client().get("/")

    policy = response.headers["Content-Security-Policy"]
    assert "script-src 'self'" in policy
    assert "script-src 'self' 'unsafe-inline'" not in policy


def test_frontend_escapes_remote_text_before_html_injection():
    javascript = (dashboard.ROOT / "dashboard" / "static" / "dashboard.js").read_text(
        encoding="utf-8"
    )

    assert "const esc =" in javascript
    assert "${esc(e.msg)}" in javascript
    assert "${esc(r.reason)}" in javascript
    assert "${esc(sl.name)}" in javascript
