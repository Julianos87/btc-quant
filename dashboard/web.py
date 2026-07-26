"""Routes du shell web/PWA, indépendantes des données de trading."""

from __future__ import annotations

from pathlib import Path

from flask import Blueprint, Response, jsonify, send_file

web = Blueprint("web", __name__)

ICON_SVG = """<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'>
<defs><linearGradient id='g' x1='0' y1='0' x2='1' y2='1'>
<stop offset='0' stop-color='#3d8bec'/><stop offset='1' stop-color='#6b4de0'/></linearGradient></defs>
<rect width='100' height='100' rx='22' fill='url(#g)'/>
<text x='50' y='68' font-size='52' text-anchor='middle' fill='white'
 font-family='system-ui' font-weight='700'>&#8383;</text></svg>"""

SERVICE_WORKER = """
const CACHE = 'btcq-v1';
self.addEventListener('install', () => { self.skipWaiting(); });
self.addEventListener('activate', e => { e.waitUntil(clients.claim()); });
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (url.pathname.startsWith('/api/')) return;
  e.respondWith(
    fetch(e.request).then(r => {
      const copy = r.clone();
      caches.open(CACHE).then(c => c.put(e.request, copy)).catch(()=>{});
      return r;
    }).catch(() => caches.match(e.request))
  );
});
self.addEventListener('message', e => {
  if (e.data && e.data.type === 'notify') {
    self.registration.showNotification(e.data.title, {
      body: e.data.body, icon: '/icon.svg', badge: '/icon.svg', tag: e.data.tag,
    });
  }
});
self.addEventListener('notificationclick', e => {
  e.notification.close();
  e.waitUntil(clients.matchAll({type:'window'}).then(cs => {
    for (const c of cs) if ('focus' in c) return c.focus();
    return clients.openWindow('/');
  }));
});
"""


@web.get("/")
def index():
    return send_file(Path(__file__).with_name("index.html"))


@web.get("/icon.svg")
def icon():
    return Response(ICON_SVG, mimetype="image/svg+xml")


@web.get("/manifest.json")
def manifest():
    return jsonify(
        {
            "name": "Tandem",
            "short_name": "Tandem",
            "description": "Portefeuille systématique 60/40 — suivi paper trading",
            "start_url": "/",
            "display": "standalone",
            "background_color": "#0a0b0d",
            "theme_color": "#0a0b0d",
            "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}],
        }
    )


@web.get("/sw.js")
def service_worker():
    return Response(
        SERVICE_WORKER,
        mimetype="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )
