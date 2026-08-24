"use strict";
/* ── couche additive : fond WebGL, spotlight, transitions de vue ──────────
   Cette section ne modifie pas la logique de données/rendu précédente :
   elle ajoute des écouteurs et enveloppe les handlers de thème et de vues
   afin d'utiliser View Transitions quand le navigateur le permet. */

if (PREFS.fx === undefined) PREFS.fx = true;

// ── bascule "fond animé" (drawer de réglages) ───────────────────────────
function applyFxPref() { document.documentElement.classList.toggle("fx-off", !PREFS.fx); }
const fxCheckbox = $("pref-fx");
if (fxCheckbox) {
  fxCheckbox.checked = PREFS.fx;
  fxCheckbox.onchange = e => { PREFS.fx = e.target.checked; savePrefs(); applyFxPref(); };
}
applyFxPref();
// buildDrawer() régénère #pref-cards mais pas ce groupe statique : on
// resynchronise juste la case à chaque ouverture du tiroir.
if ($("settings-btn")) $("settings-btn").addEventListener("click", () => { if (fxCheckbox) fxCheckbox.checked = PREFS.fx; });

// ── spotlight + tilt 3D des cartes : suivent le pointeur, tout le rendu est
// du CSS pur (--mx/--my/--rx/--ry sont juste posées ici). Amplitude de tilt
// volontairement faible (±2,2°) : un effet "magnétique" discret, pas un gadget.
if (matchMedia("(hover:hover)").matches && !matchMedia("(prefers-reduced-motion: reduce)").matches) {
  const TILT_MAX = 2.2;
  document.querySelectorAll(".card").forEach(card => {
    card.addEventListener("pointermove", e => {
      const r = card.getBoundingClientRect();
      const mx = e.clientX - r.left, my = e.clientY - r.top;
      card.style.setProperty("--mx", mx + "px");
      card.style.setProperty("--my", my + "px");
      const px = mx / r.width - 0.5, py = my / r.height - 0.5;
      card.style.setProperty("--ry", (px * TILT_MAX * 2) + "deg");
      card.style.setProperty("--rx", (py * -TILT_MAX * 2) + "deg");
    });
    card.addEventListener("pointerleave", () => {
      card.style.setProperty("--rx", "0deg");
      card.style.setProperty("--ry", "0deg");
    });
  });
} else {
  // pointeur tactile ou mouvement réduit : le halo reste centré, jamais de tilt
  document.querySelectorAll(".card").forEach(card => {
    card.style.setProperty("--rx", "0deg"); card.style.setProperty("--ry", "0deg");
  });
}

// ── compteur animé (odomètre) sur l'équity héro et le prix BTC ──────────
// On enveloppe refreshSummary (comme pour le thème/les vues) plutôt que de
// re-parser le texte affiché : lastSummary porte déjà les nombres bruts,
// donc aucun risque de mauvaise lecture selon la devise/langue affichée.
(() => {
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) return;

  function ticker(el, format) {
    if (!el) return () => {};
    let shown = null, animating = false;
    return target => {
      if (target == null || animating) { if (target != null) shown = target; return; }
      if (shown == null) { shown = target; return; }
      if (Math.abs(target - shown) < 0.005) { shown = target; return; }
      const from = shown, to = target, dur = 700, t0 = performance.now();
      animating = true;
      (function step(now) {
        const p = Math.min(1, (now - t0) / dur);
        const eased = 1 - Math.pow(1 - p, 3);
        el.textContent = format(from + (to - from) * eased);
        if (p < 1) requestAnimationFrame(step);
        else { shown = to; animating = false; }
      })(t0);
    };
  }
  const tickEquity = ticker($("t-equity"), v => fmt$(v, 0));
  const tickPrice = ticker($("h-price"), v => v.toLocaleString(LOCALE(), {maximumFractionDigits:0}) + " $");

  const _origRefreshSummary = refreshSummary;
  refreshSummary = async function (...args) {
    await _origRefreshSummary(...args);
    if (lastSummary) {
      tickEquity(lastSummary.totals.equity);
      if (lastSummary.btc.price != null) tickPrice(lastSummary.btc.price);
    }
  };
})();

// ── liseré d'alerte critique sur le header : classe posée sur <html>,
// observée depuis #alert plutôt qu'un sélecteur :has() sur un attribut style
// (le style inline est reposé à chaque tick — un MutationObserver est plus sûr) ──
(() => {
  const alertEl = $("alert");
  if (!alertEl) return;
  const sync = () => document.documentElement.classList.toggle("has-alert", alertEl.style.display === "flex");
  new MutationObserver(sync).observe(alertEl, {attributes:true, attributeFilter:["style","class"]});
  sync();
})();

// Les transitions restent locales à leur composant (curseur d'onglet,
// drawer, modal). Une transition plein écran peut expirer sur ce dashboard
// dense et n'apporte aucun repère spatial supplémentaire.

// ── équilibrage des deux colonnes : DÉPLACE une carte plutôt que d'en
// étirer une artificiellement (un fond de carte étiré sur des centaines de
// pixels de vide, c'est moche). La colonne de droite est plus étroite donc
// généralement plus haute à contenu égal ; quand l'écart est net, jusqu'à
// deux cartes de la colonne la plus haute glissent vers l'autre — chacune
// choisie par une estimation rapide puis GARDÉE UNIQUEMENT si une vraie
// remesure confirme qu'elle rapproche les deux colonnes (voir tryOneMove).
// Chaque carte retourne d'abord dans sa colonne d'origine avant tout
// recalcul, pour ne jamais accumuler les déplacements d'une vue à l'autre.
// Se redéclenche au changement de vue, au redimensionnement, et une seule
// fois après le premier chargement de données (les cartes n'ont que leur
// hauteur "vide" tant que les API n'ont pas répondu).
(function setupColumnBalance() {
  const colLeft = document.querySelector(".col-left"), engines = document.querySelector(".engines");
  if (!colLeft || !engines) return;
  [...colLeft.children].forEach(c => { if (c.classList.contains("card")) c.dataset.homeCol = "left"; });
  [...engines.children].forEach(c => { if (c.classList.contains("card")) c.dataset.homeCol = "right"; });
  const allCards = [...colLeft.children, ...engines.children].filter(c => c.classList.contains("card"));
  const THRESHOLD = 70;

  // scrollHeight des conteneurs de colonnes est inutilisable ici : CSS Grid
  // étire déjà .col-left/.engines l'un vers l'autre (align-self:stretch par
  // défaut), donc leur scrollHeight reflète surtout CETTE hauteur étirée, pas
  // le vrai besoin de contenu — exactement l'écart qu'on cherche à mesurer.
  // On somme plutôt la hauteur propre de chaque carte visible (elles, en
  // revanche, gardent leur taille naturelle : align-content:start).
  function visibleCards(col) {
    return [...col.children].filter(c => c.classList.contains("card") && c.dataset.viewHidden !== "1");
  }
  function contentHeight(col) {
    const cards = visibleCards(col);
    if (!cards.length) return 0;
    return cards.reduce((s, c) => s + c.getBoundingClientRect().height, 0) + (cards.length - 1) * 16;
  }

  // Chaque déplacement est JOUÉ POUR DE VRAI puis vérifié par une nouvelle
  // mesure réelle (pas par une formule approchée) : s'il n'améliore pas
  // vraiment l'écart, il est annulé immédiatement. Une boucle qui ne se fie
  // qu'à une estimation peut accepter un geste qui, une fois réellement
  // appliqué, aide moins que prévu — et enchaîner ainsi vers un optimum local
  // qui ne se stabilise jamais (observé en test : gauche trop haute → une
  // carte part à droite → droite devient trop haute → une autre repart à
  // gauche → sans converger). Ici, deux essais au plus, chacun garanti de ne
  // jamais empirer la situation grâce à la vérification après coup.
  function tryOneMove() {
    const diff = contentHeight(engines) - contentHeight(colLeft);
    if (Math.abs(diff) < THRESHOLD) return false;
    const fromCol = diff > 0 ? engines : colLeft, toCol = diff > 0 ? colLeft : engines;
    const visible = visibleCards(fromCol);
    if (visible.length < 2) return false; // ne jamais vider une colonne
    const sign = diff > 0 ? 1 : -1;
    let candidate = null, bestApprox = Math.abs(diff);
    for (const c of visible) {
      const approx = Math.abs(diff - sign * 2 * c.getBoundingClientRect().height);
      if (approx < bestApprox) { bestApprox = approx; candidate = c; }
    }
    if (!candidate) return false;
    const originalParent = candidate.parentElement, originalNext = candidate.nextSibling;
    toCol.appendChild(candidate);
    const newDiff = contentHeight(engines) - contentHeight(colLeft);
    if (Math.abs(newDiff) < Math.abs(diff)) return true; // vérifié : ça aide vraiment
    if (originalNext) originalParent.insertBefore(candidate, originalNext);
    else originalParent.appendChild(candidate);
    return false;
  }
  function rebalanceNow() {
    allCards.forEach(c => {
      const home = c.dataset.homeCol === "left" ? colLeft : engines;
      if (c.parentElement !== home) home.appendChild(c);
    });
    if (tryOneMove()) tryOneMove();
  }
  // Ni View Transition, ni requestAnimationFrame ici, volontairement : tous
  // deux se sont révélés peu fiables pour ce cas précis en test (rAF ne se
  // déclenche pas de façon fiable quand document.hidden vaut true — même
  // symptôme que rencontré plus haut pour le fond WebGL — et
  // startViewTransition rejette parfois en "invalid state"). Lire la hauteur
  // des cartes force de toute façon un recalcul de mise en page immédiat, donc
  // un appel synchrone est tout aussi exact et ne dépend d'aucun des deux.
  const rebalance = rebalanceNow;

  if (typeof applyDashboardView === "function") {
    const _origApplyDashboardView2 = applyDashboardView;
    applyDashboardView = function (...args) {
      const result = _origApplyDashboardView2(...args);
      rebalance();
      return result;
    };
  }
  let didInitialRebalance = false;
  if (typeof tick === "function") {
    const _origTick = tick;
    tick = async function (...args) {
      const result = await _origTick(...args);
      if (!didInitialRebalance) { didInitialRebalance = true; rebalance(); }
      return result;
    };
  }
  let resizeTimer = null;
  addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(rebalance, 200); });
  rebalance(); // état initial, avant même la première réponse API
})();

// ── fond WebGL : champ de flux réagissant au régime EMA (Donchian), à la
// volatilité annualisée réalisée et au PnL du jour — un fond décoratif qui
// reste informatif plutôt que purement esthétique. Repli total si WebGL est
// indisponible, désactivé par préférence, ou si le mouvement réduit est
// demandé : le canvas reste invisible et les dégradés CSS animés du body
// portent seuls l'ambiance de fond. ─────────────────────────────────────
(function initFx() {
  const canvas = $("fx");
  if (!canvas) return;
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) return;

  const gl = canvas.getContext("webgl", {alpha:true, antialias:false, premultipliedAlpha:true})
    || canvas.getContext("experimental-webgl", {alpha:true, antialias:false});
  if (!gl) { document.documentElement.classList.add("no-webgl"); return; }

  const VERT = `
    attribute vec2 a_pos;
    void main(){ gl_Position = vec4(a_pos, 0.0, 1.0); }
  `;
  const FRAG = `
    precision highp float;
    uniform vec2 u_resolution;
    uniform float u_time;
    uniform float u_regime;
    uniform float u_vol;
    uniform float u_pnl;
    uniform float u_theme;
    uniform float u_intensity;

    vec2 hash2(vec2 p){
      p = vec2(dot(p, vec2(127.1,311.7)), dot(p, vec2(269.5,183.3)));
      return -1.0 + 2.0*fract(sin(p)*43758.5453123);
    }
    float vnoise(vec2 p){
      vec2 i = floor(p), f = fract(p);
      vec2 u = f*f*(3.0-2.0*f);
      return mix(
        mix(dot(hash2(i+vec2(0.0,0.0)), f-vec2(0.0,0.0)), dot(hash2(i+vec2(1.0,0.0)), f-vec2(1.0,0.0)), u.x),
        mix(dot(hash2(i+vec2(0.0,1.0)), f-vec2(0.0,1.0)), dot(hash2(i+vec2(1.0,1.0)), f-vec2(1.0,1.0)), u.x),
        u.y);
    }
    float fbm(vec2 p){
      float v = 0.0, a = 0.5;
      mat2 m = mat2(1.6,1.2,-1.2,1.6);
      for (int i=0; i<5; i++){ v += a*vnoise(p); p = m*p; a *= 0.5; }
      return v;
    }
    void main(){
      vec2 uv = (gl_FragCoord.xy - 0.5*u_resolution.xy) / u_resolution.y;
      float t = u_time * (0.06 + 0.09*u_vol);
      vec2 p = uv * 1.6;
      vec2 warp = vec2(fbm(p + vec2(0.0, t)), fbm(p + vec2(5.2, -t)));
      float n1 = fbm(p + warp*1.5 + t*0.35);
      float n2 = fbm(p*2.0 - warp*1.2 - t*0.55);
      float field = (n1*0.6 + n2*0.4) * 1.25; // contraste renforcé entre creux et crêtes

      vec3 warm = mix(vec3(0.22,0.06,0.09), vec3(0.78,0.40,0.09), smoothstep(-0.15,0.55,field));
      vec3 cool = mix(vec3(0.04,0.08,0.20), vec3(0.08,0.56,0.68), smoothstep(-0.15,0.55,field));
      vec3 base = mix(warm, cool, u_regime);
      base += max(0.0, u_pnl) * vec3(0.05,0.11,0.03);
      base -= max(0.0,-u_pnl) * vec3(0.03,0.06,0.11);

      // point chaud qui dérive lentement sur sa propre orbite : casse la
      // monotonie d'un pur champ de bruit, comme une source qui traverse la
      // brume — teinté à l'opposé du régime pour un contraste complémentaire
      vec2 corePos = vec2(sin(u_time*0.055)*0.55, cos(u_time*0.041)*0.4 - 0.05);
      float core = smoothstep(0.5, 0.0, length(uv - corePos));
      vec3 coreColor = mix(vec3(0.35,0.55,0.95), vec3(0.95,0.65,0.25), 1.0 - u_regime);
      base += core * core * coreColor * 0.5;

      float vign = smoothstep(1.15, 0.1, length(uv*vec2(1.0,1.35)));
      vec3 color = base * (0.3 + 1.05*field) * vign;
      float luma = dot(color, vec3(0.299,0.587,0.114));
      color = mix(vec3(luma), color, 1.3);
      // thème clair : on penche vers un blanc cassé teinté plutôt que de garder
      // une palette sombre à pleine opacité (sinon ça fait des taches grises
      // sur fond clair) — l'intensité elle-même reste pilotée par --fx-strength
      vec3 lightColor = mix(vec3(0.97,0.975,0.985), color, 0.45);
      color = mix(lightColor, color, u_theme);

      gl_FragColor = vec4(color, clamp(u_intensity, 0.0, 1.0));
    }
  `;

  function compile(type, src) {
    const sh = gl.createShader(type);
    gl.shaderSource(sh, src);
    gl.compileShader(sh);
    if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) {
      console.error("shader fx:", gl.getShaderInfoLog(sh));
      gl.deleteShader(sh);
      return null;
    }
    return sh;
  }
  const vs = compile(gl.VERTEX_SHADER, VERT), fs = compile(gl.FRAGMENT_SHADER, FRAG);
  if (!vs || !fs) { document.documentElement.classList.add("no-webgl"); return; }
  const prog = gl.createProgram();
  gl.attachShader(prog, vs); gl.attachShader(prog, fs); gl.linkProgram(prog);
  if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
    console.error("link fx:", gl.getProgramInfoLog(prog));
    document.documentElement.classList.add("no-webgl");
    return;
  }
  gl.useProgram(prog);

  const buf = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, buf);
  // triangle unique couvrant tout le viewport (évite la diagonale d'un quad à deux triangles)
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1,-1, 3,-1, -1,3]), gl.STATIC_DRAW);
  const aPos = gl.getAttribLocation(prog, "a_pos");
  gl.enableVertexAttribArray(aPos);
  gl.vertexAttribPointer(aPos, 2, gl.FLOAT, false, 0, 0);

  const U = name => gl.getUniformLocation(prog, name);
  const uRes = U("u_resolution"), uTime = U("u_time"), uRegime = U("u_regime"),
        uVol = U("u_vol"), uPnl = U("u_pnl"), uTheme = U("u_theme"), uIntensity = U("u_intensity");

  gl.enable(gl.BLEND);
  gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);

  // rendu sous-échantillonné (jamais à la pleine résolution physique) : un
  // champ de bruit flou n'a rien à gagner du DPR natif, et le compositeur
  // lisse l'agrandissement gratuitement — le coût GPU s'en trouve divisé
  const SCALE = Math.min(1, 1.6 / Math.max(1, window.devicePixelRatio || 1));
  function resize() {
    const w = Math.max(1, Math.round(innerWidth * SCALE)), h = Math.max(1, Math.round(innerHeight * SCALE));
    if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
  }
  resize();
  addEventListener("resize", resize);

  let regime = 0.5, vol = 0.5, pnl = 0, lost = false;
  const start = performance.now();
  let lastFrame = 0;
  const FRAME_MS = 1000 / 30; // 30 fps : largement suffisant pour un champ de bruit, ménage la batterie

  canvas.addEventListener("webglcontextlost", e => { e.preventDefault(); lost = true; });
  canvas.addEventListener("webglcontextrestored", () => { lost = false; resize(); });
  // pas de gate manuel sur document.hidden : requestAnimationFrame est déjà
  // nativement throttlé/suspendu par le navigateur en arrière-plan (c'est
  // exactement ce pour quoi rAF existe) — un gate applicatif en plus s'est
  // révélé produire un premier rendu jamais déclenché dans certains contextes
  // d'aperçu où visibilityState reste "hidden" en permanence malgré un onglet
  // au premier plan pour l'utilisateur.

  function targetRegime() {
    if (typeof pcData !== "undefined" && pcData && pcData.regime_up != null) return pcData.regime_up ? 1 : 0;
    return 0.5;
  }
  function targetVol() {
    const v = (typeof lastMetrics !== "undefined" && lastMetrics) ? lastMetrics.vol_annual : null;
    if (v == null) return 0.5;
    return Math.max(0, Math.min(1.6, v / 0.75));
  }
  function targetPnl() {
    const p = (typeof lastSummary !== "undefined" && lastSummary) ? lastSummary.totals.day_pnl_pct : null;
    if (p == null) return 0;
    return Math.max(-1, Math.min(1, p * 9));
  }
  function themeVal() {
    const forced = document.documentElement.getAttribute("data-theme");
    if (forced === "dark") return 1;
    if (forced === "light") return 0;
    return matchMedia("(prefers-color-scheme: dark)").matches ? 1 : 0;
  }
  const strength = () => parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--fx-strength")) || 0.5;

  function frame(now) {
    requestAnimationFrame(frame);
    if (lost || !PREFS.fx) return;
    if (now - lastFrame < FRAME_MS) return;
    lastFrame = now;
    regime += (targetRegime() - regime) * 0.015;
    vol += (targetVol() - vol) * 0.02;
    pnl += (targetPnl() - pnl) * 0.02;
    gl.viewport(0, 0, canvas.width, canvas.height);
    gl.uniform2f(uRes, canvas.width, canvas.height);
    gl.uniform1f(uTime, (now - start) / 1000);
    gl.uniform1f(uRegime, regime);
    gl.uniform1f(uVol, vol);
    gl.uniform1f(uPnl, pnl);
    gl.uniform1f(uTheme, themeVal());
    gl.uniform1f(uIntensity, strength());
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    canvas.classList.add("ready");
  }
  requestAnimationFrame(frame);
})();
