"use strict";
// Presentation only: number transitions and the persistent alert indicator.

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
