# Audit de sécurité applicative — TANDEM (btc-quant)

**Date** : 2026-07-23
**Périmètre** : intégralité du dépôt (`src/btcquant`, `dashboard/`, `scripts/`, `deploy/`, `tests/`, config, CI, dépendances)
**Méthode** : revue manuelle fichier par fichier (OWASP Top 10, secrets exposés, erreurs d'autorisation, dépendances vulnérables, bugs à risque de perte financière) + `pip-audit` sur l'environnement figé (`uv.lock` / `requirements.txt`) + `ruff` + suite de tests (`pytest`).
**Verdict global** : dépôt dans un état déjà mature — plusieurs classes de bugs à risque financier ont visiblement déjà été corrigées et couvertes par des tests de non-régression (`tests/test_audit_fixes.py`, `tests/test_funding_parity.py`, `tests/test_carry_financing.py`). Aucun secret, aucune injection, aucune désérialisation dangereuse trouvés. Les problèmes identifiés ici concernent le durcissement du dashboard exposé sur Internet et une dépendance transitive au CVE connu.

## Résumé des constats

| # | Sévérité | Composant | Constat | Statut |
|---|---|---|---|---|
| 1 | **Élevée** | `deploy/install.sh`, `dashboard/app.py` | Dashboard exposé publiquement en HTTP pur (pas de TLS) ; le jeton d'accès et le cookie de session voyagent en clair | ⚠️ Non corrigé automatiquement — décision d'infrastructure (domaine/certificat) |
| 2 | Moyenne | `dashboard/app.py` | Aucun en-tête de sécurité HTTP (`Referrer-Policy`, `X-Frame-Options`, `X-Content-Type-Options`, CSP) | ✅ Corrigé |
| 3 | Moyenne | `dashboard/app.py` | Cookie d'authentification sans attribut `Secure` | ✅ Corrigé |
| 4 | Faible | `requirements.txt` / `uv.lock` | `setuptools==82.0.1` — CVE connu `PYSEC-2026-3447` (contournement d'exclusion `MANIFEST.in` par non-normalisation Unicode, sdist) | ✅ Corrigé (bump `ccxt` → 4.5.68, qui entraîne `setuptools` → 83.0.0) |
| 5 | Info | Ensemble du dépôt | Aucun secret commis, aucune injection (SQL/commande/YAML/`eval`), aucune désérialisation dangereuse | ✅ Pas d'action requise |

---

## 1. [Élevée] Dashboard exposé sans TLS — jeton et cookie en clair

**Fichiers** : `deploy/install.sh`, `dashboard/app.py`, `deploy/btcquant-dashboard.service`

Le dashboard implémente un modèle d'authentification par « capability URL » : un jeton long et aléatoire (`DASHBOARD_TOKEN`, généré par `openssl rand -hex 24`) donne un accès en lecture pendant un an via un cookie persistant (`COOKIE_MAX_AGE = 365 * 24 * 3600`). Le code documente explicitement que ce jeton est le seul rempart de confidentialité (`app.py:29-39`) : « Le dashboard est exposé sur Internet ».

Or `deploy/install.sh` sert ce dashboard en clair :
```bash
systemctl enable --now btcquant-trend btcquant-carry btcquant-dashboard
...
echo " ⚠ Ouvrir le port si pare-feu actif :  ufw allow 8666/tcp"
echo "   http://$(hostname -I | awk '{print $1}'):8666/?k=${TOKEN}"
```
Flask écoute directement sur `0.0.0.0:8666` (`dashboard/app.py:1001-1003`), sans reverse proxy ni certificat TLS dans aucun fichier de `deploy/`.

**Impact** : un attaquant en position d'observer le trafic réseau (Wi-Fi public, routeur compromis, FAI, tout point sur le chemin) peut capturer le jeton — soit dans l'URL de la première visite (`?k=...`), soit dans l'en-tête `Set-Cookie` — et obtenir un accès en lecture permanent (équity, positions ouvertes, stops, historique de trades). Le risque est une divulgation d'informations financières personnelles, pas une prise de contrôle (toutes les routes sont en lecture seule, cf. commentaire `app.py:35-36`), mais reste réel compte tenu de l'exposition publique documentée.

**Recommandation (non appliquée automatiquement — décision d'infrastructure)** :
- Mettre un reverse proxy (Caddy ou nginx) devant Flask avec un certificat TLS automatique (Let's Encrypt), puis lier Flask à `127.0.0.1` uniquement (`DASHBOARD_HOST=127.0.0.1`) ;
- Rediriger tout le trafic HTTP vers HTTPS ;
- Si un reverse proxy est ajouté, penser à configurer `werkzeug.middleware.proxy_fix.ProxyFix` (ou à faire confiance à `X-Forwarded-Proto`) pour que `request.is_secure` reflète le protocole vu par le client — sinon le correctif du point 3 ci-dessous ne posera jamais l'attribut `Secure`.

Ce point n'a pas été corrigé dans ce commit car il s'agit d'un choix d'infrastructure (nom de domaine, autorité de certification, topologie réseau) qui dépasse ce qu'un correctif de code peut trancher en sécurité.

---

## 2. [Moyenne] En-têtes de sécurité HTTP absents — corrigé

**Fichier** : `dashboard/app.py`

Aucune route ne posait d'en-tête `Referrer-Policy`, `X-Content-Type-Options`, `X-Frame-Options` ou `Content-Security-Policy`. Conséquences concrètes :
- la page charge une feuille de style Google Fonts cross-origin (`index.html:11-13`) ; sans `Referrer-Policy`, le comportement de fuite du `Referer` (qui contiendrait `?k=<jeton>` sur la première visite) dépend uniquement du réglage par défaut du navigateur, jamais garanti explicitement par l'application ;
- aucune protection de repli contre le clickjacking (`X-Frame-Options`) sur une page qui affiche des données financières ;
- aucune `Content-Security-Policy` pour limiter les origines de script/style/connexion en cas d'injection future.

**Correctif appliqué** — nouveau hook `_security_headers` (`dashboard/app.py`) :
```python
resp.headers["Referrer-Policy"] = "no-referrer"
resp.headers["X-Content-Type-Options"] = "nosniff"
resp.headers["X-Frame-Options"] = "DENY"
resp.headers.setdefault("Content-Security-Policy", "default-src 'self'; ...")
```
La CSP autorise explicitement les origines déjà utilisées par la page (`fonts.googleapis.com`, `fonts.gstatic.com`) et conserve `'unsafe-inline'` pour script/style — la page est un fichier HTML unique sans étape de build capable d'ajouter un nonce ; c'est un strict renforcement par rapport à l'absence totale de CSP précédente, sans rien casser.

Vérifié par un test manuel (`test_client().get(...)`) : les quatre en-têtes sont bien présents sur toutes les réponses, `pytest` (88/88) et `ruff` restent au vert.

---

## 3. [Moyenne] Cookie de session sans attribut `Secure` — corrigé

**Fichier** : `dashboard/app.py`, fonction `_persist_token`

Le cookie `tandem_key` (jeton d'authentification, valable un an) était posé avec `httponly=True, samesite="Lax"` mais **sans `secure=True`**. Tant que le service reste en HTTP pur, ça ne change rien de plus que le point 1 ; mais si TLS est ajouté un jour sans revoir ce détail, le navigateur continuerait d'envoyer le cookie même sur une connexion HTTP accidentelle (page rechargée sur un lien `http://` au lieu de `https://`, proxy mal configuré, etc.), ce qui annulerait une partie du bénéfice du TLS.

**Correctif appliqué** :
```python
resp.set_cookie(
    COOKIE_NAME, AUTH_TOKEN, max_age=COOKIE_MAX_AGE,
    httponly=True, samesite="Lax", secure=request.is_secure,
)
```
`request.is_secure` vaut aujourd'hui toujours `False` (pas de TLS), donc **aucun changement de comportement actuel** — mais le cookie se durcira automatiquement dès qu'un TLS sera mis en place (point 1), sans qu'il faille repenser au correctif à ce moment-là. Rappel : cela suppose que `request.is_secure` reflète bien le protocole vu par le client (voir note `ProxyFix` au point 1 si un reverse proxy est ajouté).

---

## 4. [Faible] CVE connu sur une dépendance transitive (`setuptools`) — corrigé

**Fichiers** : `requirements.txt`, `uv.lock`

`pip-audit -r requirements.txt` a signalé :

```
setuptools 82.0.1  PYSEC-2026-3447  fix: 83.0.0
```

Résumé de l'avis : sur macOS (APFS/HFS+), l'algorithme de correspondance des règles `exclude`/`global-exclude` de `MANIFEST.in` ne normalise pas l'Unicode (NFC vs NFD), ce qui peut faire échouer silencieusement une exclusion et publier un fichier destiné à rester privé dans une distribution source (`sdist`) publiée sur PyPI.

**Applicabilité à ce projet** : nulle en pratique — `btcquant` ne construit ni ne publie de `sdist` ; `setuptools` n'est présent que comme dépendance transitive de `ccxt` (probablement pour `pkg_resources` à l'exécution). Le risque réel est donc très faible, mais il s'agit d'un CVE identifié dans l'arbre de dépendances figé et le correctif est gratuit.

**Cause du blocage initial** : `ccxt==4.5.66` épingle `setuptools` à une version exacte (`>=82.0.1, <82.0.1+`, c'est-à-dire `==82.0.1`) dans ses propres métadonnées — un simple `uv lock --upgrade-package setuptools` ne suffisait donc pas.

**Correctif appliqué** :
```bash
uv lock --upgrade-package ccxt   # 4.5.66 → 4.5.68 (ccxt épingle désormais setuptools==83.0.0)
uv export --no-dev --no-hashes --no-emit-project -o requirements.txt
```
Diff résultant (`requirements.txt`) :
```diff
-ccxt==4.5.66
+ccxt==4.5.68
-setuptools==82.0.1
+setuptools==83.0.0
```
`ccxt>=4.3` reste respecté (`pyproject.toml`), c'est un bump de patch mineur. Vérifications après correctif :
- `pip-audit -r requirements.txt` → **0 vulnérabilité connue** ;
- `uv run pytest -q` → **88/88 tests passent** ;
- `uv run ruff check .` → **aucun défaut**.

---

## 5. Points vérifiés sans anomalie

Revue exhaustive des zones à risque, sans correctif nécessaire :

- **Secrets** : aucune clé API, jeton ou identifiant commis dans le dépôt. `BINANCE_API_KEY` / `BINANCE_API_SECRET` (`ccxt_broker.py`, `carry_broker.py`), `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` (`notify.py`) et `DASHBOARD_TOKEN` (`dashboard/app.py`) sont systématiquement lus via `os.environ.get(...)`, jamais codés en dur. `.gitignore` exclut `.env`, `state/`, `backups/`, `data/`.
- **Injection** : aucun `eval`/`exec`/`os.system`/`shell=True` ; le seul `subprocess.run` (`scripts/watchdog.py`) utilise une liste d'arguments fixes (noms de services systemd codés en dur, aucune entrée externe). `yaml.safe_load` (jamais `yaml.load`/`unsafe_load`) pour la configuration.
- **Autorisation du dashboard** : garde `before_request` globale (`app.py:_guard`), comparaison du jeton en temps constant (`hmac.compare_digest`), 404 plutôt que 401 en cas d'échec (n'indique pas qu'une route protégée existe), repli sur `127.0.0.1` uniquement si `DASHBOARD_TOKEN` n'est pas défini. Toutes les routes sont des `GET` en lecture seule : pas de surface CSRF exploitable (aucune mutation d'état accessible depuis un navigateur).
- **XSS** : le rendu côté client (`dashboard/index.html`) construit son HTML par template JS ; les seuls champs texte à contenu potentiellement variable (`msg` du journal d'événements) sont échappés (`e.msg.replace(/</g, "&lt;")`). Les autres champs interpolés (`strategy`, `direction`, `reason`) proviennent exclusivement de fichiers d'état écrits par le moteur lui-même (`trades.csv`, `*_state.json`), jamais d'une entrée utilisateur réseau — aucun canal d'injection identifié dans le modèle de menace actuel.
- **Logique financière** (le point le plus sensible de ce projet) : dimensionnement des positions, coupe-circuits (drawdown/perte journalière), persistance atomique de l'état (écriture `tmp` + `os.replace`), identifiants de client d'ordre déterministes (idempotence anti-doublon), comptabilisation du fill réellement exécuté (`filled`, jamais la quantité demandée en repli), et logique de rattrapage en cas d'échec d'une jambe du carry (`CarryBroker.open_position`) sont tous couverts par des tests dédiés (`tests/test_audit_fixes.py`, `tests/test_carry_financing.py`, `tests/test_funding_parity.py`, `tests/test_venue.py`). Le stop suiveur ne peut jamais être desserré (`BacktestEngine`/`LiveRunner`, testé explicitement). Aucune régression identifiée.
- **CI/CD** : `.github/workflows/tests.yml` utilise des actions épinglées par tag majeur (`actions/checkout@v4`, `astral-sh/setup-uv@v5`) sans permissions élevées ni secrets exposés dans les logs. La CI est actuellement en sommeil (quota GitHub Actions épuisé jusqu'au 1er août 2026) ; le hook local `.githooks/pre-push` compense en attendant (lint + tests, y compris Python 3.12).
- **Déploiement** : `deploy/update.sh` referme explicitement les permissions `o-rwx` après chaque `rsync` (corrige un umask permissif potentiel) ; `.env` et `state/` sont exclus des synchronisations qui pourraient les écraser ou les exposer.

---

## Correctifs appliqués — récapitulatif

| Fichier | Changement |
|---|---|
| `dashboard/app.py` | Ajout des en-têtes `Referrer-Policy`, `X-Content-Type-Options`, `X-Frame-Options`, `Content-Security-Policy` ; cookie de session `secure=request.is_secure` |
| `uv.lock` | `ccxt` 4.5.66 → 4.5.68 (entraîne `setuptools` 82.0.1 → 83.0.0, corrige `PYSEC-2026-3447`) |
| `requirements.txt` | Régénéré depuis `uv.lock` via `uv export --no-dev --no-hashes --no-emit-project -o requirements.txt` (procédure documentée du README) |

**Vérifications post-correctifs** :
```
uv run pytest -q         → 88 passed
uv run ruff check .      → All checks passed!
pip-audit -r requirements.txt → No known vulnerabilities found
```

## Non corrigé automatiquement — nécessite une décision humaine

1. **TLS pour le dashboard** (§1) — nécessite un nom de domaine et un choix de reverse proxy/autorité de certification, hors périmètre d'un correctif de code sûr.
2. Si TLS est ajouté ultérieurement derrière un reverse proxy, configurer `ProxyFix` pour que `request.is_secure` (utilisé au §3) reflète correctement le protocole côté client.
