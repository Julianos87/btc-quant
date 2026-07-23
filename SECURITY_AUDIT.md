# Audit de sécurité applicative — TANDEM (btc-quant)

**Date** : 2026-07-23
**Périmètre** : intégralité du dépôt (`src/btcquant`, `dashboard/`, `scripts/`, `deploy/`, `tests/`, config, CI, dépendances)
**Méthode** : revue manuelle fichier par fichier (OWASP Top 10, secrets exposés, erreurs d'autorisation, dépendances vulnérables, bugs à risque de perte financière) + `pip-audit` sur l'environnement figé (`uv.lock` / `requirements.txt`) + `ruff` + suite de tests (`pytest`).
**Verdict global** : dépôt dans un état déjà mature — plusieurs classes de bugs à risque financier ont visiblement déjà été corrigées et couvertes par des tests de non-régression (`tests/test_audit_fixes.py`, `tests/test_funding_parity.py`, `tests/test_carry_financing.py`). Aucun secret, aucune injection, aucune désérialisation dangereuse trouvés. Les problèmes identifiés ici concernent le durcissement du dashboard exposé sur Internet et une dépendance transitive au CVE connu.

**Mise à jour 2026-07-23 (suite)** : domaine `tandemalgo.duckdns.org` fourni par l'utilisateur → le point 1 (absence de TLS) est maintenant traité dans le dépôt (Caddy en reverse proxy, TLS automatique, Flask replié en local). Reste à **activer sur le VPS** (voir « Activation sur le VPS » en fin de section 1) : cette partie ne peut pas être vérifiée depuis cet environnement, qui n'a pas d'accès SSH à la machine de production.

## Résumé des constats

| # | Sévérité | Composant | Constat | Statut |
|---|---|---|---|---|
| 1 | **Élevée** | `deploy/install.sh`, `dashboard/app.py` | Dashboard exposé publiquement en HTTP pur (pas de TLS) ; le jeton d'accès et le cookie de session voyagent en clair | ✅ Corrigé dans le dépôt (Caddy + `tandemalgo.duckdns.org`) — ⚠️ **à activer sur le VPS**, non vérifiable depuis cette session |
| 2 | Moyenne | `dashboard/app.py` | Aucun en-tête de sécurité HTTP (`Referrer-Policy`, `X-Frame-Options`, `X-Content-Type-Options`, CSP) | ✅ Corrigé |
| 3 | Moyenne | `dashboard/app.py` | Cookie d'authentification sans attribut `Secure` | ✅ Corrigé (et vérifié `Secure` effectif derrière Caddy, voir §3) |
| 4 | Faible | `requirements.txt` / `uv.lock` | `setuptools==82.0.1` — CVE connu `PYSEC-2026-3447` (contournement d'exclusion `MANIFEST.in` par non-normalisation Unicode, sdist) | ✅ Corrigé (bump `ccxt` → 4.5.68, qui entraîne `setuptools` → 83.0.0) |
| 5 | Info | Ensemble du dépôt | Aucun secret commis, aucune injection (SQL/commande/YAML/`eval`), aucune désérialisation dangereuse | ✅ Pas d'action requise |

---

## 1. [Élevée] Dashboard exposé sans TLS — jeton et cookie en clair

**Fichiers** : `deploy/install.sh`, `deploy/update.sh`, `deploy/Caddyfile` (nouveau), `dashboard/app.py`, `deploy/btcquant-dashboard.service`

Le dashboard implémente un modèle d'authentification par « capability URL » : un jeton long et aléatoire (`DASHBOARD_TOKEN`, généré par `openssl rand -hex 24`) donne un accès en lecture pendant un an via un cookie persistant (`COOKIE_MAX_AGE = 365 * 24 * 3600`). Le code documente explicitement que ce jeton est le seul rempart de confidentialité (`app.py:29-39`) : « Le dashboard est exposé sur Internet ».

Or `deploy/install.sh` servait ce dashboard en clair, Flask écoutant directement sur `0.0.0.0:8666`, sans reverse proxy ni certificat TLS dans aucun fichier de `deploy/`.

**Impact** : un attaquant en position d'observer le trafic réseau (Wi-Fi public, routeur compromis, FAI, tout point sur le chemin) peut capturer le jeton — soit dans l'URL de la première visite (`?k=...`), soit dans l'en-tête `Set-Cookie` — et obtenir un accès en lecture permanent (équity, positions ouvertes, stops, historique de trades). Le risque est une divulgation d'informations financières personnelles, pas une prise de contrôle (toutes les routes sont en lecture seule, cf. commentaire `app.py:35-36`), mais reste réel compte tenu de l'exposition publique documentée.

### Correctif appliqué (domaine `tandemalgo.duckdns.org` fourni par l'utilisateur)

- **`deploy/Caddyfile`** (nouveau) : reverse proxy Caddy vers `127.0.0.1:8666`, TLS Let's Encrypt automatique (émission + renouvellement) pour `tandemalgo.duckdns.org` — aucun certificat à gérer à la main.
- **`deploy/install.sh`** : installe Caddy (dépôt officiel, absent des dépôts Debian/Ubuntu de base), déploie le Caddyfile, bascule `DASHBOARD_HOST=0.0.0.0` → `127.0.0.1` dans le `.env` généré (Flask n'est plus joignable que depuis Caddy, sur la même machine), met à jour le message final (URL en `https://`, pare-feu 80/443 au lieu de 8666).
- **`deploy/update.sh`** : redéploie le Caddyfile et recharge Caddy à chaque mise à jour (best-effort, ne bloque pas la mise à jour si Caddy n'est pas encore installé sur un VPS existant).
- **`dashboard/app.py`** : ajout de `ProxyFix` (`werkzeug.middleware.proxy_fix`, `x_for=1, x_proto=1, x_host=1`) pour que `request.is_secure` reflète le `X-Forwarded-Proto` posé par Caddy — c'est ce qui permet au cookie `secure=request.is_secure` (point 3) de passer effectivement à `Secure` une fois derrière TLS. Ne fait confiance qu'à **un seul** hop de proxy, ce qui est correct et sûr puisque Flask n'écoute plus qu'en local (seul Caddy, sur la même machine, peut lui parler directement — un client externe ne peut pas forger ces en-têtes en contournant Caddy).
- Vérifié par un test simulant une requête derrière Caddy (`X-Forwarded-Proto: https`) : le cookie de session obtient bien l'attribut `Secure` ; sans cet en-tête (accès direct/dev local), le comportement est inchangé.

### ⚠️ Activation sur le VPS — action manuelle requise, non vérifiable depuis cette session

Cette session n'a **pas d'accès SSH au VPS de production** : le correctif ci-dessus n'existe pour l'instant que dans le dépôt Git, sur cette branche. Pour qu'il s'applique réellement, une fois la branche fusionnée/déployée :

1. Vérifier que `tandemalgo.duckdns.org` pointe bien vers l'IP publique du VPS (`dig +short tandemalgo.duckdns.org`) et que les ports **80** et **443** sont ouverts sur le pare-feu (443 pour le TLS, 80 pour la validation ACME + redirection HTTP→HTTPS que Caddy gère seul) ;
2. Sur le VPS : `sudo bash /opt/btcquant/deploy/update.sh` ne suffit **pas** à installer Caddy la première fois (il ne fait que recopier le Caddyfile s'il est déjà installé) — repasser par `sudo bash deploy/install.sh` depuis un clone à jour, qui est conçu pour être rejouable ;
3. **Le `.env` existant n'est pas régénéré automatiquement** (le script ne le touche que s'il est absent, pour ne pas perdre le jeton) : éditer à la main `/opt/btcquant/.env` sur le VPS et changer `DASHBOARD_HOST=0.0.0.0` en `DASHBOARD_HOST=127.0.0.1`, puis `sudo systemctl restart btcquant-dashboard` ;
4. Vérifier `systemctl status caddy` et `curl -I https://tandemalgo.duckdns.org` (doit répondre, certificat valide) ;
5. Si le pare-feu ouvrait `8666/tcp` publiquement, le refermer (`ufw delete allow 8666/tcp`) une fois le nouveau chemin `https://tandemalgo.duckdns.org/?k=<jeton>` confirmé fonctionnel ;
6. Si l'IP du VPS n'est pas fixe, s'assurer que le client de mise à jour DuckDNS tourne bien côté serveur (hors périmètre de ce dépôt — jeton DuckDNS non géré ici).

Tant que ces étapes n'ont pas été rejouées sur le VPS lui-même, l'ancien accès `http://<ip>:8666/?k=...` reste probablement actif en parallèle.

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
Tant que le VPS n'a pas Caddy devant lui (voir « Activation sur le VPS », point 1), `request.is_secure` vaut `False` et le comportement est inchangé. Une fois Caddy actif, `ProxyFix` (ajouté au point 1) fait remonter le `X-Forwarded-Proto: https` posé par Caddy, et le cookie obtient bien `Secure` — vérifié par un test simulant cet en-tête (§1).

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
| `dashboard/app.py` | En-têtes `Referrer-Policy`, `X-Content-Type-Options`, `X-Frame-Options`, `Content-Security-Policy` ; `ProxyFix` (1 hop) ; cookie de session `secure=request.is_secure` |
| `deploy/Caddyfile` (nouveau) | Reverse proxy TLS automatique pour `tandemalgo.duckdns.org` → `127.0.0.1:8666` |
| `deploy/install.sh` | Installe Caddy, déploie le Caddyfile, `DASHBOARD_HOST` → `127.0.0.1`, message final en `https://`, pare-feu 80/443 |
| `deploy/update.sh` | Redéploie le Caddyfile et recharge Caddy à chaque mise à jour (best-effort) |
| `uv.lock` | `ccxt` 4.5.66 → 4.5.68 (entraîne `setuptools` 82.0.1 → 83.0.0, corrige `PYSEC-2026-3447`) |
| `requirements.txt` | Régénéré depuis `uv.lock` via `uv export --no-dev --no-hashes --no-emit-project -o requirements.txt` (procédure documentée du README) |

**Vérifications post-correctifs** :
```
uv run pytest -q                → 88 passed
uv run ruff check .             → All checks passed!
pip-audit -r requirements.txt   → No known vulnerabilities found
test simulé X-Forwarded-Proto   → cookie Secure posé correctement derrière proxy
```

## Non corrigé automatiquement — nécessite une action humaine

1. **Activation sur le VPS** (§1) — cette session n'a pas d'accès SSH à la machine de production : le Caddyfile, l'installation de Caddy et le passage de `DASHBOARD_HOST` en `127.0.0.1` existent dans le dépôt mais doivent être rejoués sur le VPS lui-même (séquence détaillée en fin de §1). Non vérifiable depuis cet environnement.
2. Vérifier que le DNS `tandemalgo.duckdns.org` pointe bien vers l'IP du VPS et que le client de mise à jour DuckDNS (si l'IP n'est pas fixe) tourne côté serveur — hors périmètre de ce dépôt.
