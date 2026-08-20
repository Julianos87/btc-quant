"use strict";
const $ = id => document.getElementById(id);
const esc = value => String(value ?? "").replace(
  /[&<>"']/g,
  character => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[character])
);

// ── préférences persistées ─────────────────────────────────
const PREFS_DEFAULT = {
  lang: "fr", currency: "usd", accent: "#2a78d6", range: 0, refresh: 30000,
  notif: false, notifPos: true, ddAlert: 12, view: "monitor",
  hidden: {}, // { cardKey: true }
};
let PREFS = Object.assign({}, PREFS_DEFAULT, JSON.parse(localStorage.getItem("btcq-prefs") || "{}"));
const savePrefs = () => localStorage.setItem("btcq-prefs", JSON.stringify(PREFS));
const LOCALE = () => PREFS.lang === "en" ? "en-US" : "fr-FR";

// ── i18n ───────────────────────────────────────────────────
const I18N = {
  fr: {
    paper:"PAPER TRADING", theme:"Thème", settings:"Réglages", btcusdt:"BTC-PERP / USDC",
    h24:"24 heures", funding_ann:"Funding annualisé", live_perf:"Performance en direct",
    realized:"réalisé", exposure_health:"Exposition & santé", gross_exposure:"Exposition brute",
    leverage_note:"levier effectif · repère = 1× (notionnel = équity)", next_bar:"Prochaine bougie 4 h",
    next_funding:"Prochain funding", api_latency:"Latence API Hyperliquid", uptime:"Uptime dashboard",
    protocol:"Protocole", phase:"Phase actuelle", next_step:"Étape suivante",
    rebalance:"Rééquilibrage 60/40", monthly:"mensuel", golden_rule:"Règle d’or",
    no_change:"ne rien modifier en route", collecting:"Collecte en cours…",
    grp_display:"Affichage", pref_lang:"Langue", pref_currency:"Devise d’affichage",
    pref_accent:"Couleur d’accent", pref_range:"Plage par défaut", grp_refresh:"Rafraîchissement",
    pref_refresh:"Fréquence", manual:"Manuel", grp_alerts:"Alertes & notifications",
    pref_notif:"Notifications navigateur", pref_notif_pos:"Prévenir à l’ouverture d’une position",
    pref_notif_dd:"Alerte si drawdown dépasse", grp_cards:"Cartes visibles",
    base100:"Base 100", dollars:"Dollars", drawdown:"Drawdown", vs_bh:"vs Buy&Hold",
    portfolio:"Portefeuille", all:"Tout", closed_trades:"Trades clôturés", from:"Du", to:"au",
    reset:"Réinitialiser", no_trades:"Aucun trade clôturé pour l’instant — le journal se remplit à la première sortie de position.",
    auto_refresh:"Actualisation automatique", real_data:"données réelles Hyperliquid, exécution simulée",
    equity_total:"Équity totale", pnl_day:"PnL du jour · UTC", since_midnight:"depuis 00:00 UTC",
    alloc_real:"Allocation réelle", pending_deposit:"Apport en attente", online:"EN LIGNE", offline:"ARRÊTÉ",
    workspace:"Vue", view_monitor:"Surveillance", view_performance:"Performance", view_risk:"Risque",
    status_good:"Tout est nominal", status_warn:"Surveillance requise", status_crit:"Action requise",
    status_nominal_detail:"Trend et Carry répondent · protections armées",
    status_trend_down:"Trend ne répond plus", status_carry_down:"Carry ne répond plus",
    status_kill:"Kill-switch actif", status_lockout:"Lockout journalier actif",
    data_now:"données à l’instant", data_ago:"mis à jour il y a",
    performance_snapshot:"Synthèse de performance", performance_snapshot_note:"Le bilan réalisé, sans bruit opérationnel.",
    performance_total:"Depuis le départ", performance_day:"Aujourd’hui", performance_dd:"Drawdown actuel", performance_sharpe:"Sharpe réalisé",
    risk_radar:"Radar de risque", risk_radar_note:"Ce qui peut réellement dégrader le portefeuille maintenant.",
    risk_stop:"Risque jusqu’aux stops", risk_margin:"Marge au stop la plus proche", risk_leverage:"Levier effectif", risk_policy:"Protection active",
    risk_no_stop:"aucun stop trend ouvert", risk_no_position:"aucune position trend", risk_armed:"limites armées", risk_lockout:"lockout journalier", risk_halted:"kill-switch déclenché", risk_engine_down:"moteur hors ligne",
    monitor_pulse:"Pulse opérationnel", monitor_pulse_note:"Les quatre signaux qui comptent pendant la surveillance.",
    monitor_trend:"Moteur Trend", monitor_carry:"Moteur Carry", monitor_next_bar:"Prochaine décision", monitor_binance:"Flux Hyperliquid",
    monitor_ready:"opérationnel", monitor_silent:"ne répond plus", monitor_latency:"latence API", monitor_funding:"prochain funding",
    sharpe:"Sharpe", sortino:"Sortino", calmar:"Calmar", vol:"Volatilité",
    yearly_title:"Années précédentes", yearly_sub:"backtest · mêmes réglages que le live",
    yearly_note:"Simulation avec frais, slippage et funding réels — pas des résultats réalisés. Rendements par année civile ; pire creux de l’année au survol.",
    yearly_partial:"année incomplète", yearly_missing:"Référence annuelle absente — lancer scripts/make_yearly_reference.py",
    carry_synthetic:"paper synthétique · pas de position venue",
    carry_mode:"Mode", carry_mode_value:"Paper synthétique",
    carry_modeled_qty:"Perp qty modélisé", carry_modeled_spot:"Spot notionnel modélisé",
    carry_modeled_perp:"Perp notionnel modélisé",
    carry_uncertain:"Comptabilité Carry incertaine — pas d'exposition venue",
    paper_vs_bt:"Hyperliquid 1 m · backtest Binance 4 h",
    readiness_title:"Testnet", readiness_note:"Critères fixés à froid — le passage ne se décide pas au feeling.",
    rdy_ready:"PRÊT", rdy_not_ready:"NON PRÊT", rdy_blocked:"BLOQUÉ",
    rdy_ready_why:"Tous les critères sont au vert. La décision de passer au testnet reste humaine.",
    rdy_wait_why:"Le système est sain, mais la campagne n'a pas encore assez de preuves.",
    rdy_block_why:"Un critère opérationnel bloque le passage, indépendamment de la durée de campagne.",
    rdy_health:"Santé système", rdy_stats:"Qualification", rdy_exec:"Exécution",
    rdy_blockers:"Principaux critères manquants", rdy_show:"Voir tous les critères", rdy_hide:"Masquer le détail",
    rdy_objective:"objectif", rdy_na:"N/A", rdy_na_orders:"N/A — aucun ordre terminal",
    rdy_na_fills:"N/A — aucun fill exploitable",
    rdy_campaign:"Campagne", rdy_running:"En cours", rdy_protocol:"protocole",
    rdy_inactive:"Inactif", rdy_triggered:"Déclenché", rdy_source:"Source de readiness",
    rdy_counts:"critères validés", rdy_remain:"critères restent à satisfaire",
    signal_wait:"ATTENTE · AUCUN SIGNAL",
    signal_threshold:"SEUIL FRANCHI EN COURS · ATTENDRE LA CLÔTURE 4 H",
    signal_position:"POSITION TREND OUVERTE",
    signal_unknown_mode:"CALCUL EN COURS",
    signal_long_rule:"Régime haussier : LONG autorisé au-dessus des lignes pleines. Les lignes SHORT grises sont inactives. Une mèche ne suffit pas ; ADX et funding restent contrôlés.",
    signal_short_rule:"Régime baissier : SHORT autorisé sous les lignes pleines. Les lignes LONG grises sont inactives. Une mèche ne suffit pas ; ADX et funding restent contrôlés.",
    signal_unknown_rule:"Aucune direction n’est affichée tant que le régime 4 h n’est pas disponible.",
    threshold_active:"direction autorisée",
    threshold_inactive:"direction inactive",
    waiting_zone:"zone d’attente",
    inactive_suffix:"INACTIF",
    waiting_label:"ATTENTE",
    cards:{performance_brief:"Synthèse de performance", risk_radar:"Radar de risque", monitor_pulse:"Pulse opérationnel",
      chart:"Courbe d’équity", price:"Graphe prix", events:"Journal", trend:"Moteur Trend",
      carry:"Moteur Carry", breakdown:"Répartition & records", conformity:"Est-ce normal ?", yearly:"Années précédentes",
      trades:"Trades clôturés", metrics:"Performance en direct", exposure:"Exposition & santé", protocol:"Protocole",
      readiness:"Testnet"},
  },
  en: {
    paper:"PAPER TRADING", theme:"Theme", settings:"Settings", btcusdt:"BTC-PERP / USDC",
    h24:"24 hours", funding_ann:"Annualized funding", live_perf:"Live performance",
    realized:"realized", exposure_health:"Exposure & health", gross_exposure:"Gross exposure",
    leverage_note:"effective leverage · marker = 1× (notional = equity)", next_bar:"Next 4h candle",
    next_funding:"Next funding", api_latency:"Hyperliquid API latency", uptime:"Dashboard uptime",
    protocol:"Protocol", phase:"Current phase", next_step:"Next step",
    rebalance:"Rebalancing 60/40", monthly:"monthly", golden_rule:"Golden rule",
    no_change:"never change mid-course", collecting:"Collecting…",
    grp_display:"Display", pref_lang:"Language", pref_currency:"Display currency",
    pref_accent:"Accent color", pref_range:"Default range", grp_refresh:"Refresh",
    pref_refresh:"Frequency", manual:"Manual", grp_alerts:"Alerts & notifications",
    pref_notif:"Browser notifications", pref_notif_pos:"Notify when a position opens",
    pref_notif_dd:"Alert if drawdown exceeds", grp_cards:"Visible cards",
    base100:"Base 100", dollars:"Dollars", drawdown:"Drawdown", vs_bh:"vs Buy&Hold",
    portfolio:"Portfolio", all:"All", closed_trades:"Closed trades", from:"From", to:"to",
    reset:"Reset", no_trades:"No closed trades yet — the log fills on the first position exit.",
    auto_refresh:"Auto refresh", real_data:"real Hyperliquid data, simulated execution",
    equity_total:"Total equity", pnl_day:"Daily PnL · UTC", since_midnight:"since 00:00 UTC",
    alloc_real:"Real allocation", pending_deposit:"Pending deposit", online:"ONLINE", offline:"STOPPED",
    workspace:"View", view_monitor:"Monitor", view_performance:"Performance", view_risk:"Risk",
    yearly_title:"Previous years", yearly_sub:"backtest · same settings as live",
    yearly_note:"Simulation with real fees, slippage and funding — not realized results. Calendar-year returns; each year's worst drawdown on hover.",
    yearly_partial:"partial year", yearly_missing:"Yearly reference missing — run scripts/make_yearly_reference.py",
    carry_synthetic:"synthetic paper · no venue position",
    carry_mode:"Mode", carry_mode_value:"Synthetic paper",
    carry_modeled_qty:"Modeled perp qty", carry_modeled_spot:"Modeled spot notional",
    carry_modeled_perp:"Modeled perp notional",
    carry_uncertain:"Carry accounting uncertain — no venue exposure",
    paper_vs_bt:"Hyperliquid 1m · Binance 4h backtest",
    readiness_title:"Testnet", readiness_note:"Criteria set in advance — the transition is not a gut call.",
    rdy_ready:"READY", rdy_not_ready:"NOT READY", rdy_blocked:"BLOCKED",
    rdy_ready_why:"Every criterion is green. The testnet decision remains a human call.",
    rdy_wait_why:"The system is healthy, but the campaign does not yet have enough evidence.",
    rdy_block_why:"An operational criterion blocks promotion, independent of campaign duration.",
    rdy_health:"System health", rdy_stats:"Qualification", rdy_exec:"Execution",
    rdy_blockers:"Outstanding criteria", rdy_show:"Show all criteria", rdy_hide:"Hide details",
    rdy_objective:"target", rdy_na:"N/A", rdy_na_orders:"N/A — no terminal orders",
    rdy_na_fills:"N/A — no usable fill",
    rdy_campaign:"Campaign", rdy_running:"Running", rdy_protocol:"protocol",
    rdy_inactive:"Inactive", rdy_triggered:"Triggered", rdy_source:"Readiness source",
    rdy_counts:"criteria passed", rdy_remain:"criteria still outstanding",
    signal_wait:"WAITING · NO SIGNAL",
    signal_threshold:"THRESHOLD CROSSED · WAIT FOR THE 4H CLOSE",
    signal_position:"TREND POSITION OPEN",
    signal_unknown_mode:"CALCULATING",
    signal_long_rule:"Bullish regime: LONG is allowed above solid lines. Grey SHORT lines are inactive. A wick is not enough; ADX and funding are still checked.",
    signal_short_rule:"Bearish regime: SHORT is allowed below solid lines. Grey LONG lines are inactive. A wick is not enough; ADX and funding are still checked.",
    signal_unknown_rule:"No direction is shown until the 4h regime is available.",
    threshold_active:"allowed direction",
    threshold_inactive:"inactive direction",
    waiting_zone:"waiting zone",
    inactive_suffix:"INACTIVE",
    waiting_label:"WAITING",
    status_good:"All systems nominal", status_warn:"Monitoring required", status_crit:"Action required",
    status_nominal_detail:"Trend and Carry are responding · safeguards armed",
    status_trend_down:"Trend is not responding", status_carry_down:"Carry is not responding",
    status_kill:"Kill switch is active", status_lockout:"Daily lockout is active",
    data_now:"data received just now", data_ago:"updated",
    performance_snapshot:"Performance snapshot", performance_snapshot_note:"Realized results, without operational noise.",
    performance_total:"Since inception", performance_day:"Today", performance_dd:"Current drawdown", performance_sharpe:"Realized Sharpe",
    risk_radar:"Risk radar", risk_radar_note:"What can actually degrade the portfolio right now.",
    risk_stop:"Risk to trend stops", risk_margin:"Nearest stop distance", risk_leverage:"Effective leverage", risk_policy:"Active safeguard",
    risk_no_stop:"no open trend stop", risk_no_position:"no trend position", risk_armed:"limits armed", risk_lockout:"daily lockout", risk_halted:"kill switch triggered", risk_engine_down:"engine offline",
    monitor_pulse:"Operations pulse", monitor_pulse_note:"The four signals that matter while monitoring.",
    monitor_trend:"Trend engine", monitor_carry:"Carry engine", monitor_next_bar:"Next decision", monitor_binance:"Hyperliquid flow",
    monitor_ready:"operational", monitor_silent:"not responding", monitor_latency:"API latency", monitor_funding:"next funding",
    sharpe:"Sharpe", sortino:"Sortino", calmar:"Calmar", vol:"Volatility",
    cards:{performance_brief:"Performance snapshot", risk_radar:"Risk radar", monitor_pulse:"Operations pulse",
      chart:"Equity curve", price:"Price chart", events:"Event log", trend:"Trend engine",
      carry:"Carry engine", breakdown:"Breakdown & records", conformity:"Is this normal?", yearly:"Previous years",
      trades:"Closed trades", metrics:"Live performance", exposure:"Exposure & health", protocol:"Protocol",
      readiness:"Testnet"},
  },
};
const t = k => (I18N[PREFS.lang] || I18N.fr)[k] || k;
function applyI18n() {
  document.documentElement.lang = PREFS.lang;
  document.querySelectorAll("[data-i18n]").forEach(el => {
    const v = el.dataset.i18n.split(".").reduce((o, kk) => (o && o[kk] != null) ? o[kk] : null, I18N[PREFS.lang] || I18N.fr);
    if (typeof v === "string") el.textContent = v;
  });
  $("f-refresh").textContent = PREFS.refresh
    ? t("auto_refresh") + " " + (PREFS.refresh/1000) + " s" : (PREFS.lang==="en"?"Manual refresh":"Rafraîchissement manuel");
  if (lastSummary) { renderCockpitStatus(lastSummary); renderViewFocus(lastSummary); }
  if (pcData) drawPChart();
  updateDataFreshness();
}

// ── formatage devise / pourcentage ─────────────────────────
let fx = {eur_usd:null}, btcPrice = null;
function fmt$(v, dp=0) {
  if (v == null || isNaN(v)) return "—";
  const c = PREFS.currency;
  if (c === "eur" && fx.eur_usd) return (v/fx.eur_usd).toLocaleString(LOCALE(), {minimumFractionDigits:dp, maximumFractionDigits:dp}) + " €";
  if (c === "btc" && btcPrice) return (v/btcPrice).toLocaleString(LOCALE(), {minimumFractionDigits:4, maximumFractionDigits:6}) + " ₿";
  return v.toLocaleString(LOCALE(), {minimumFractionDigits:dp, maximumFractionDigits:dp}) + " $";
}
const fmtPct = (v, dp=2) => v == null || isNaN(v) ? "—" :
  (v >= 0 ? "+" : "") + (v*100).toFixed(dp).replace(".", PREFS.lang==="en"?".":",") + " %";
const fmtDrawdown = (v, dp=1) => v == null || isNaN(v) ? "—" :
  (v*100).toFixed(dp).replace(".", PREFS.lang==="en"?".":",") + " %";
const fmtNum = (v, dp=2) => v == null || isNaN(v) ? "—" : v.toFixed(dp).replace(".", PREFS.lang==="en"?".":",");
const cls = (el, v) => { el.classList.toggle("up", v > 0); el.classList.toggle("down", v < 0); };
let range = PREFS.range, unit = "pct", chartData = null, showBH = false, lastSummary = null, lastTradeRows = [];
let lastMetrics = null;
let lastSummaryUpdatedAt = null;

// ── couleur d'accent ───────────────────────────────────────
function applyAccent() { document.documentElement.style.setProperty("--s1", PREFS.accent); }

const VIEW_CARDS = {
  monitor: new Set(["monitor_pulse", "price", "events", "trend", "carry", "exposure", "readiness"]),
  performance: new Set(["performance_brief", "chart", "breakdown", "conformity", "yearly", "trades", "metrics", "readiness"]),
  risk: new Set(["risk_radar", "chart", "price", "events", "trend", "carry", "exposure"]),
};
const VIEW_TILES = {
  monitor: new Set(["equity", "day", "funding", "allocation"]),
  performance: new Set(),
  risk: new Set(),
};

function applyDashboardView() {
  const view = VIEW_CARDS[PREFS.view] ? PREFS.view : "monitor";
  PREFS.view = view;
  document.body.dataset.view = view;
  if (view === "risk") unit = "dd";
  if (view === "performance") unit = "pct";
  document.querySelectorAll("[data-card]").forEach(card => {
    card.dataset.viewHidden = VIEW_CARDS[view].has(card.dataset.card) ? "0" : "1";
  });
  document.querySelectorAll("[data-view-tile]").forEach(tile => {
    tile.dataset.viewHidden = VIEW_TILES[view].has(tile.dataset.viewTile) ? "0" : "1";
  });
  $("top-tiles").classList.toggle("view-empty", VIEW_TILES[view].size === 0);
  document.querySelectorAll("#dashboard-view [data-view]").forEach(button => {
    const active = button.dataset.view === view;
    button.classList.toggle("on", active);
    button.setAttribute("aria-selected", String(active));
  });
  document.querySelectorAll("#unit .chip").forEach(button => button.classList.toggle("on", button.dataset.u === unit));
  if (typeof drawYearly === "function") drawYearly(); // la carte devient visible en vue performance
}

function setChartUnit(next) {
  unit = next;
  document.querySelectorAll("#unit .chip").forEach(button => button.classList.toggle("on", button.dataset.u === unit));
  drawChart();
}

document.querySelectorAll("#dashboard-view [data-view]").forEach(button => button.onclick = () => {
  PREFS.view = button.dataset.view;
  savePrefs();
  applyDashboardView();
  if (PREFS.view === "risk") setChartUnit("dd");
  if (PREFS.view === "performance") setChartUnit("pct");
});

function markSummaryUnavailable() {
  const freshness = $("data-freshness");
  freshness.classList.remove("stale");
  freshness.classList.add("unknown");
  $("data-age").textContent = "source indisponible — UNKNOWN";
  setState($("trend-state"), false, "UNKNOWN");
  setState($("carry-state"), false, "UNKNOWN");
  $("alert").style.display = "flex";
  $("alert").className = "";
  $("alert-msg").textContent = "Données opérationnelles indisponibles — aucun état LIVE confirmé";
}

let summaryRequestSequence = 0;
async function refreshSummary() {
  const requestSequence = ++summaryRequestSequence;
  try {
    const response = await fetch("/api/summary");
    if (!response.ok) throw new Error("summary_http_" + response.status);
    const s = await response.json();
    if (requestSequence !== summaryRequestSequence) return;
    btcPrice = s.btc.price; if (s.fx) fx = s.fx; lastSummary = s; lastSummaryUpdatedAt = Date.now();
    $("h-price").textContent = s.btc.price ? s.btc.price.toLocaleString(LOCALE(), {maximumFractionDigits:0}) + " $" : "—";
  $("h-change").textContent = fmtPct(s.btc.change24h); cls($("h-change"), s.btc.change24h);
  $("h-funding").textContent = fmtPct(s.funding.annualized, 1); cls($("h-funding"), s.funding.annualized);

  $("t-equity").textContent = fmt$(s.totals.equity);
  const d = $("t-delta");
  d.style.display = "inline-flex";
  d.textContent = (s.totals.pnl >= 0 ? "▲ " : "▼ ") + fmtPct(s.totals.pnl_pct);
  d.className = "delta num " + (s.totals.pnl >= 0 ? "pos" : "neg");
  $("t-sub").textContent = `départ ${fmt$(s.totals.initial)}${s.totals.deposits > 0 ? " + apports " + fmt$(s.totals.deposits) : ""} · PnL ${(s.totals.pnl >= 0 ? "+" : "") + fmt$(s.totals.pnl, 2)}`;
  $("t-day").textContent = fmtPct(s.totals.day_pnl_pct); cls($("t-day"), s.totals.day_pnl_pct);

  if (s.funding.next_ts) {
    const mins = Math.max(0, Math.round((s.funding.next_ts - Date.now()) / 60000));
    $("t-next").textContent = Math.floor(mins/60) + " h " + String(mins%60).padStart(2, "0");
    $("t-nextsub").textContent = "taux actuel " + fmtPct(s.funding.rate, 4);
  }
  const at = s.totals.allocation_trend;
  if (at != null) {
    $("t-alloc").textContent = Math.round(at*100) + " / " + Math.round((1-at)*100);
    $("ab-t").style.width = (at*100).toFixed(1) + "%";
    $("ab-c").style.width = ((1-at)*100).toFixed(1) + "%";
    $("al-t").textContent = fmt$(s.trend.equity); $("al-c").textContent = fmt$(s.carry.equity);
  }
  const pendingDeposit = s.totals.pending_deposits || 0;
  $("pending-deposit").style.display = pendingDeposit > 0 ? "block" : "none";
  $("pending-deposit").textContent = pendingDeposit > 0
    ? `${t("pending_deposit")} : ${fmt$(pendingDeposit)} · ${s.totals.pending_deposit_count}`
    : "";

  setState($("trend-state"), s.trend.alive, s.trend.freshness);
  setState($("carry-state"), s.carry.alive, s.carry.freshness);
  $("trend-beat").textContent = beat(s.trend.age_s);
  $("carry-beat").textContent = beat(s.carry.age_s);

  // bannière d'alerte : l'anormal doit sauter aux yeux
  const issues = [];
  const health = s.health || {};
  const componentIssue = (name, item, label) => {
    const severity = BTCQuantOperationalState.componentAvailabilitySeverity(name, item, health);
    if (severity) issues.push([severity, label + " " + (item && item.freshness ? item.freshness : "UNKNOWN")]);
  };
  // Les commandes affichées ici sont celles réellement exécutables sur le VPS :
  // les moteurs tournent sous systemd, pas via un script Python.
  componentIssue("trend", s.trend, "état Trend");
  componentIssue("carry", s.carry, "état Carry");
  if (health.safety_status === "FAIL") issues.push(["crit", "Execution safety UNSAFE"]);
  else if (health.safety_status === "UNKNOWN") issues.push(["crit", "Execution safety UNKNOWN"]);
  if (s.btc.freshness !== "FRESH") issues.push([s.btc.freshness === "STALE" ? "warn" : "crit", "prix BTC non frais — valorisation non LIVE"]);
  if (s.trend.halted) issues.push(["crit", "KILL-SWITCH Trend déclenché : drawdown maximal atteint, positions liquidées"]);
  if (s.carry.halted) issues.push(["crit", "KILL-SWITCH Carry déclenché : drawdown maximal atteint, position fermée"]);
  if (s.carry.daily_lockout) issues.push(["warn", "Carry : limite de perte journalière atteinte — plus d'entrées avant demain (UTC)"]);
  if (s.trend.daily_lockout) issues.push(["warn", "limite de perte journalière atteinte — plus de nouvelles entrées avant demain (UTC)"]);
  if (pendingDeposit > 0) issues.push(["warn", `apport en attente : ${fmt$(pendingDeposit)} — nouvelle tentative quotidienne à 04:30 UTC`]);
  const a = $("alert");
  if (issues.length) {
    a.style.display = "flex";
    a.className = issues.some(i => i[0] === "crit") ? "" : "warn";
    $("alert-msg").textContent = issues.map(i => i[1]).join(" · ");
  } else a.style.display = "none";
  $("trend-eq").textContent = fmt$(s.trend.equity, 2);
  $("trend-guard").textContent = s.trend.halted ? "⛔ KILL-SWITCH DÉCLENCHÉ" : s.trend.daily_lockout ? "⚠ lockout journalier" : "coupe-circuits armés";
  $("carry-eq").textContent = fmt$(s.carry.equity, 2);
  renderCarryCard(s.carry);
  if ($("carry-last")) $("carry-last").textContent = s.carry.last_funding_ts
    ? new Date(s.carry.last_funding_ts).toLocaleString(LOCALE(), {day:"2-digit", month:"2-digit", hour:"2-digit", minute:"2-digit"}) : "—";

  $("slots").innerHTML = s.trend.slots.map(sl => {
    const badge = sl.state === "LONG" ? "long" : sl.state === "SHORT" ? "short" : "flat";
    const arrow = sl.state === "LONG" ? "▲ " : sl.state === "SHORT" ? "▼ " : "";
    const pnl = sl.upnl == null ? "—" : (sl.upnl >= 0 ? "+" : "") + sl.upnl.toFixed(1) + " $";
    const pnlCls = sl.upnl > 0 ? "up" : sl.upnl < 0 ? "down" : "";
    const f = v => v ? v.toLocaleString("fr-FR", {maximumFractionDigits:0}) : "—";
    const stopGap = s.btc.price && sl.stop && sl.state !== "FLAT"
      ? (sl.state === "LONG" ? (s.btc.price - sl.stop) : (sl.stop - s.btc.price)) / s.btc.price : null;
    const closeToStop = stopGap != null && stopGap < 0.01;
    const gap = stopGap == null ? "" : `<div class="stop-gap ${closeToStop ? "urgent" : ""}">
      <i style="width:${Math.max(3, Math.min(100, Math.max(0, stopGap) / .08 * 100)).toFixed(0)}%"></i>
      <span>${stopGap < 0 ? "stop dépassé" : `${(stopGap * 100).toFixed(1).replace(".", ",")} % au stop`}</span></div>`;
    return `<tr class="slotclick" data-name="${esc(sl.name)}" title="Voir le détail"><td style="font-weight:600">${esc(sl.name).replace("trend_ls_", "Donchian ")}</td>
      <td><span class="badge ${badge}">${arrow}${sl.state}</span></td>
      <td class="num">${f(sl.entry)}</td><td class="num"><div>${f(sl.stop)}</div>${gap}</td>
      <td class="num ${pnlCls}" style="font-weight:600">${pnl}</td></tr>`;
  }).join("");
  document.querySelectorAll("#slots .slotclick").forEach(tr => tr.onclick = () => openDrill(tr.dataset.name));
  renderExposureHealth(s);
  renderViewFocus(s);
  renderCockpitStatus(s);
  checkAlerts(s);
  updateDataFreshness();
  $("f-updated").textContent = (PREFS.lang==="en"?"updated ":"mis à jour ") + new Date().toLocaleTimeString(LOCALE());
  } catch (error) {
    console.error(error);
    if (requestSequence === summaryRequestSequence) markSummaryUnavailable();
    throw error;
  }
}

// ── exposition + santé + countdowns ────────────────────────
let nextBarTs = null, nextFundingTs = null;
function renderExposureHealth(s) {
  const lev = s.totals.leverage || 0;
  $("exp-val").textContent = fmt$(s.totals.gross_notional) + "  ·  " + fmtNum(lev, 2) + "×";
  const g = $("exp-gauge");
  g.style.width = Math.min(100, lev / 2 * 100) + "%";  // pleine largeur = 2×
  g.style.background = lev > 1.5 ? "var(--crit)" : lev > 1.05 ? "var(--warn)" : "var(--s2)";
  const h = s.health || {};
  nextBarTs = h.next_bar_ts; nextFundingTs = s.funding && s.funding.next_ts;
  $("h-latency").textContent = h.api_latency_ms != null ? Math.round(h.api_latency_ms) + " ms" : "—";
  $("h-uptime").textContent = h.server_uptime_s != null ? fmtDur(h.server_uptime_s) : "—";
  updateCountdowns();
}
function fmtDur(sec) {
  return BTCQuantDashboardUx.formatDuration(sec) || "—";
}

function renderCarryCard(carry) {
  const view = BTCQuantDashboardUx.carryPresentation(carry);
  const open = view.position === "OPEN";
  if ($("carry-mode")) $("carry-mode").textContent = t("carry_mode_value");
  $("carry-pos").innerHTML = open
    ? `<span class="badge long">● ${esc(view.position)}</span><span class="carry-synth">${esc(t("carry_synthetic"))}</span>`
    : `<span class="badge flat">${esc(view.position)}</span><span class="carry-synth">${esc(t("carry_synthetic"))}</span>`;
  const qty = $("carry-perp-qty");
  if (qty) qty.textContent = view.perp_qty == null ? "—" : String(view.perp_qty);
  const spot = $("carry-spot-notional");
  if (spot) spot.textContent = view.spot_notional == null ? "—" : fmt$(view.spot_notional, 2);
  const perp = $("carry-perp-notional");
  if (perp) perp.textContent = view.perp_notional == null ? "—" : fmt$(view.perp_notional, 2);
  const note = $("carry-uncertain");
  if (note) note.style.display = view.accounting_uncertain ? "block" : "none";
}
function cdText(ts) {
  if (!ts) return "—";
  let ms = ts - Date.now();
  if (ms < 0) ms = 0;
  const h = Math.floor(ms/3600000), m = Math.floor(ms%3600000/60000), sec = Math.floor(ms%60000/1000);
  return (h ? h+" h " : "") + String(m).padStart(2,"0") + " min " + String(sec).padStart(2,"0") + " s";
}
function updateCountdowns() {
  if ($("cd-bar")) $("cd-bar").textContent = cdText(nextBarTs);
  if ($("cd-funding")) $("cd-funding").textContent = cdText(nextFundingTs);
  if ($("pulse-next-bar")) $("pulse-next-bar").textContent = cdText(nextBarTs);
  if ($("pulse-next-funding")) $("pulse-next-funding").textContent = cdText(nextFundingTs);
}
setInterval(() => { updateCountdowns(); updateDataFreshness(); }, 1000);
function setState(el, alive, freshness) {
  const state = freshness || (alive ? "FRESH" : "UNAVAILABLE");
  const fresh = state === "FRESH";
  const stale = state === "STALE";
  el.className = "estate " + (fresh ? "on" : stale ? "warn" : "off");
  el.innerHTML = '<span class="dot"></span>' + (fresh ? t("online") : stale ? "STALE" : "UNKNOWN");
}
function beat(age) {
  if (age == null) return "";
  if (age < 90) return `· données fraîches (il y a ${Math.round(age)} s)`;
  if (age < 3600) return `· dernière activité il y a ${Math.round(age/60)} min`;
  return `· dernière activité il y a ${(age/3600).toFixed(1)} h`;
}

function renderCockpitStatus(s) {
  const critical = [], warnings = [];
  const health = s.health || {};
  const incidents = health.open_incidents || [];
  const safety = health.safety_status || "UNKNOWN";
  if (safety === "FAIL") critical.push("Execution safety UNSAFE");
  else if (safety === "UNKNOWN") critical.push("Execution safety UNKNOWN");
  const component = (name, item) => {
    const severity = BTCQuantOperationalState.componentAvailabilitySeverity(name, item, health);
    const state = item && item.freshness ? item.freshness : "UNKNOWN";
    if (severity === "crit") critical.push(name + " state " + state);
    else if (severity === "warn") warnings.push(name + " state " + state);
  };
  component("trend", s.trend);
  component("carry", s.carry);
  if (s.trend.halted) critical.push(t("status_kill"));
  if (s.trend.daily_lockout) warnings.push(t("status_lockout"));
  for (const incident of incidents) {
    const label = `Execution ${incident.engine || "système"}: ${incident.message}`;
    if (incident.severity === "CRITICAL") critical.push(label);
    else warnings.push(label);
  }
  const status = critical.length ? "crit" : warnings.length ? "warn" : "good";
  $("cockpit-status").dataset.status = status;
  $("status-title").textContent = t(status === "crit" ? "status_crit" : status === "warn" ? "status_warn" : "status_good");
  $("status-detail").textContent = [...critical, ...warnings].join(" · ") || t("status_nominal_detail");
}

function updateDataFreshness() {
  if (!lastSummaryUpdatedAt) return;
  const age = Math.max(0, Math.floor((Date.now() - lastSummaryUpdatedAt) / 1000));
  const stamp = $("data-age"), freshness = $("data-freshness");
  let elapsed;
  if (age < 2) elapsed = t("data_now");
  else if (age < 60) elapsed = PREFS.lang === "en" ? `${t("data_ago")} ${age}s ago` : `${t("data_ago")} ${age} s`;
  else {
    const mins = Math.floor(age / 60), secs = age % 60;
    elapsed = PREFS.lang === "en" ? `${t("data_ago")} ${mins}m ${secs}s ago` : `${t("data_ago")} ${mins} min ${secs} s`;
  }
  stamp.textContent = elapsed;
  const staleAfter = PREFS.refresh ? Math.max(45, PREFS.refresh / 1000 + 15) : 90;
  const sourceStatus = lastSummary?.btc?.freshness || "UNKNOWN";
  freshness.classList.toggle("stale", sourceStatus === "STALE" || age > staleAfter);
  freshness.classList.toggle("unknown", sourceStatus === "UNKNOWN" || sourceStatus === "UNAVAILABLE");
  if (sourceStatus !== "FRESH") stamp.textContent += " · " + sourceStatus;
}

function focusMetric(label, value, note = "", tone = "") {
  return `<div class="focus-metric ${tone}"><div class="fl">${label}</div><div class="fv num">${value}</div><div class="fs">${note}</div></div>`;
}

function trendStopProfile(s) {
  const price = Number(s.btc.price || 0);
  const positions = s.trend.slots.filter(slot => slot.state !== "FLAT" && slot.qty && slot.stop);
  let risk = 0, nearest = null;
  for (const slot of positions) {
    const gap = price ? (slot.state === "LONG" ? price - slot.stop : slot.stop - price) / price : null;
    const loss = price ? Math.max(0, Number(slot.qty) * (slot.state === "LONG" ? price - slot.stop : slot.stop - price)) : 0;
    risk += loss;
    if (gap != null) nearest = nearest == null ? gap : Math.min(nearest, gap);
  }
  return {positions, risk, nearest};
}

function renderPerformanceBrief(s) {
  const m = lastMetrics || {};
  const days = m.days ? `${m.days} j` : t("collecting");
  $("performance-brief").innerHTML =
    focusMetric(t("performance_total"), fmtPct(s.totals.pnl_pct), `${(s.totals.pnl >= 0 ? "+" : "") + fmt$(s.totals.pnl, 0)}`, s.totals.pnl < 0 ? "warn" : "") +
    focusMetric(t("performance_day"), fmtPct(s.totals.day_pnl_pct), t("since_midnight"), s.totals.day_pnl_pct < 0 ? "warn" : "") +
    focusMetric(t("performance_dd"), fmtDrawdown(m.cur_dd, 1), m.max_dd == null ? "—" : `max ${fmtDrawdown(m.max_dd, 1)}`, m.cur_dd < -0.08 ? "crit" : m.cur_dd < -0.04 ? "warn" : "") +
    focusMetric(t("performance_sharpe"), fmtNum(m.sharpe, 2), days, m.sharpe < 0 ? "warn" : "");
}

function renderRiskRadar(s) {
  const profile = trendStopProfile(s), m = lastMetrics || {};
  const lev = s.totals.leverage || 0;
  const health = s.health || {};
  const safety = health.safety_status || "UNKNOWN";
  const required = new Set(health.required_components || ["trend"]);
  const componentDown = (name, item) => required.has(name)
    && (!item || !item.alive || item.freshness !== "FRESH");
  const engineDown = componentDown("trend", s.trend) || componentDown("carry", s.carry);
  const protection = safety === "FAIL"
    ? "UNSAFE"
    : safety === "UNKNOWN"
      ? "UNKNOWN"
      : engineDown
        ? t("risk_engine_down")
        : s.trend.halted
          ? t("risk_halted")
          : s.trend.daily_lockout
            ? t("risk_lockout")
            : t("risk_armed");
  const protectionTone = safety !== "PASS" || engineDown || s.trend.halted
    ? "crit"
    : s.trend.daily_lockout
      ? "warn"
      : "";
  const margin = profile.nearest == null ? "—" : fmtPct(profile.nearest, 1);
  const marginTone = profile.nearest != null && profile.nearest < .01 ? "crit" : profile.nearest != null && profile.nearest < .03 ? "warn" : "";
  $("risk-radar").innerHTML =
    focusMetric(t("performance_dd"), fmtDrawdown(m.cur_dd, 1), m.max_dd == null ? "—" : `max ${fmtDrawdown(m.max_dd, 1)}`, m.cur_dd < -0.08 ? "crit" : m.cur_dd < -0.04 ? "warn" : "") +
    focusMetric(t("risk_stop"), fmt$(profile.risk, 0), profile.positions.length ? `${profile.positions.length} position${profile.positions.length > 1 ? "s" : ""} trend` : t("risk_no_stop"), profile.risk > s.totals.equity * .03 ? "warn" : "") +
    focusMetric(t("risk_margin"), margin, profile.nearest == null ? t("risk_no_position") : `${profile.positions.length} stop${profile.positions.length > 1 ? "s" : ""} actif${profile.positions.length > 1 ? "s" : ""}`, marginTone) +
    focusMetric(t("risk_policy"), protection, `${t("risk_leverage")} · ${fmtNum(lev, 2)}×`, protectionTone || (lev > 1.5 ? "crit" : lev > 1.05 ? "warn" : ""));
}

function renderMonitorPulse(s) {
  const h = s.health || {};
  const engine = (name, label, alive, age, freshness) => {
    const fresh = freshness === "FRESH" && alive;
    const stale = freshness === "STALE";
    const status = fresh ? t("monitor_ready") : stale ? "STALE" : "UNKNOWN";
    const tone = fresh ? "" : BTCQuantOperationalState.componentAvailabilitySeverity(name, {alive, freshness}, h);
    return '<div class="ops-item ' + tone + '"><span class="ops-dot"></span><div class="ops-copy"><div class="ops-label">'
      + label + '</div><div class="ops-value">' + status + '</div><div class="ops-note">'
      + (beat(age).replace(/^·\s*/, "") || "—") + '</div></div></div>';
  };
  $("monitor-pulse").innerHTML =
    engine("trend", t("monitor_trend"), s.trend.alive, s.trend.age_s, s.trend.freshness) +
    engine("carry", t("monitor_carry"), s.carry.alive, s.carry.age_s, s.carry.freshness) +
    '<div class="ops-item"><span class="ops-dot"></span><div class="ops-copy"><div class="ops-label">'
      + t("monitor_next_bar") + '</div><div class="ops-value num" id="pulse-next-bar">'
      + cdText(h.next_bar_ts) + '</div><div class="ops-note">' + t("next_bar")
      + '</div></div></div>' +
    '<div class="ops-item ' + (h.api_latency_ms > 1000 ? "warn" : "")
      + '"><span class="ops-dot"></span><div class="ops-copy"><div class="ops-label">'
      + t("monitor_binance") + '</div><div class="ops-value num">'
      + (h.api_latency_ms != null ? Math.round(h.api_latency_ms) + " ms" : "—")
      + '</div><div class="ops-note">' + t("monitor_funding")
      + ' · <span id="pulse-next-funding">' + cdText(s.funding.next_ts)
      + '</span></div></div></div>';
  }

function renderViewFocus(s) {
  renderPerformanceBrief(s);
  renderRiskRadar(s);
  renderMonitorPulse(s);
}

async function refreshTrades() {
  const p = new URLSearchParams();
  const f = $("tr-from") && $("tr-from").value, to = $("tr-to") && $("tr-to").value;
  if (f) p.set("from", f); if (to) p.set("to", to);
  const tr = await (await fetch("/api/trades?" + p.toString())).json();
  const st = $("tr-stats");
  lastTradeRows = tr.rows || [];
  drawChart();
  if (!tr.stats.n) { st.textContent = "0 trade"; $("trades").innerHTML = `<div class="empty">${t("no_trades")}</div>`; return; }
  const wr = Math.round(tr.stats.wins / tr.stats.n * 100);
  st.textContent = `${tr.stats.n} trades · ${wr} % ✓ · ${(tr.stats.pnl >= 0 ? "+" : "") + fmt$(tr.stats.pnl, 0)}`;
  $("trades").innerHTML = `<table>
    <thead><tr><th>Sortie</th><th>Système</th><th>Sens</th><th>PnL</th><th>Motif</th></tr></thead>
    <tbody>` + tr.rows.map(r => {
      const pnlCls = r.pnl > 0 ? "up" : r.pnl < 0 ? "down" : "";
      const badge = r.direction === "LONG" ? "long" : "short";
      const arrow = r.direction === "LONG" ? "▲" : "▼";
      return `<tr>
        <td class="num">${String(r.exit_ts).slice(5, 16).replace("T", " ")}</td>
        <td>${esc(String(r.strategy).replace("trend_ls_", "D"))}</td>
        <td><span class="badge ${badge}">${arrow} ${esc(r.direction)}</span></td>
        <td class="num ${pnlCls}" style="font-weight:650">${(r.pnl >= 0 ? "+" : "") + fmt$(r.pnl, 1)}</td>
        <td style="color:var(--muted);font-size:11.5px">${esc(r.reason)}</td></tr>`;
    }).join("") + "</tbody></table>";
}

let evFilter = "all", lastEvents = [];
function renderEvents() {
  const evs = lastEvents.filter(e => evFilter === "all" || e.source === evFilter);
  $("events").innerHTML = evs.length ? evs.map(e => {
    const lvl = e.level === "ERROR" ? "err" : e.level === "WARNING" ? "warn" : "info";
    return `<div class="ev ${lvl}"><span class="lvl"></span>
      <span class="ts num">${e.ts.slice(5, 16)}</span>
      <span class="src" style="color:var(${e.source === "trend" ? "--s2" : "--s3"})">${esc(e.source)}</span>
      <span class="msg">${esc(e.msg)}</span></div>`;
  }).join("") : '<div class="empty">Aucun événement — les moteurs sont en veille, c’est normal.</div>';
}
async function refreshEvents() {
  lastEvents = await (await fetch("/api/events")).json();
  renderEvents();
  drawChart();
}
document.querySelectorAll("#evfilter .chip").forEach(b => b.onclick = () => {
  document.querySelectorAll("#evfilter .chip").forEach(x => x.classList.remove("on"));
  b.classList.add("on"); evFilter = b.dataset.f; renderEvents();
});

async function refreshAnalytics() {
  const a = await (await fetch("/api/analytics")).json();
  const money = v => (v >= 0 ? "+" : "") + v.toLocaleString("fr-FR", {maximumFractionDigits:0}) + " $";
  // barres de répartition : largeur ∝ |PnL| relatif, couleur = signe
  function bars(rows) {
    if (!rows || !rows.length) return '<div class="empty" style="padding:10px 0">en attente de trades</div>';
    const max = Math.max(...rows.map(r => Math.abs(r.pnl)), 1);
    return rows.map(r => {
      const c = r.pnl >= 0 ? "var(--s2)" : "var(--crit)";
      const wr = r.n ? Math.round(r.wins / r.n * 100) : 0;
      return `<div class="brk">
        <div class="brk-top"><span><b>${esc(r.name)}</b> <span class="n">${r.n} trades · ${wr}% ✓</span></span>
          <span class="num" style="color:${c};font-weight:650">${money(r.pnl)}</span></div>
        <div class="brk-bar"><i style="width:${Math.abs(r.pnl)/max*100}%;background:${c}"></i></div></div>`;
    }).join("");
  }
  $("brk-strat").innerHTML = bars(a.by_strategy);
  $("brk-dir").innerHTML = bars((a.by_direction || []).map(r => ({...r, name: r.name === "LONG" ? "Longs" : "Shorts"})));

  const rec = a.records || {};
  const cards = [];
  const push = (label, val, sub, cls_) => cards.push(
    `<div class="rec"><div class="rl">${label}</div><div class="rv ${cls_||''}">${val}</div><div class="rs">${sub||""}</div></div>`);
  if (rec.biggest_win != null) push("Meilleur trade", money(rec.biggest_win), esc(rec.biggest_win_strat), "up");
  if (rec.biggest_loss != null) push("Pire trade", money(rec.biggest_loss), esc(rec.biggest_loss_strat), "down");
  if (rec.best_day != null) push("Meilleur jour", fmtPct(rec.best_day, 1), "", "up");
  if (rec.worst_day != null) push("Pire jour", fmtPct(rec.worst_day, 1), "", "down");
  if (rec.longest_win_streak != null) push("Série gagnante", rec.longest_win_streak, "d'affilée");
  if (rec.longest_loss_streak != null) push("Série perdante", rec.longest_loss_streak, "d'affilée");
  $("records").innerHTML = cards.join("");

  // funding cumulé (carry)
  $("carry-funding").textContent = rec.funding_total != null ? money(rec.funding_total) : "—";
  drawFunding(a.funding_cum || []);
}

function drawFunding(pts) {
  const svg = $("fundchart"), W = svg.clientWidth || 300, H = svg.clientHeight || 52;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  if (pts.length < 2) { svg.innerHTML = ""; return; }
  const ys = pts.map(p => p[1]), xs = pts.map(p => p[0]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  let y0 = Math.min(...ys, 0), y1 = Math.max(...ys, 0.01);
  const X = t => (t - x0) / Math.max(1, x1 - x0) * (W - 4) + 2;
  const Y = v => H - 3 - (v - y0) / (y1 - y0) * (H - 6);
  const c = col("--s3");
  const d = pts.map((p, i) => (i ? "L" : "M") + X(p[0]).toFixed(1) + " " + Y(p[1]).toFixed(1)).join("");
  svg.innerHTML = `<defs><linearGradient id="fg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="${c}" stop-opacity=".3"/><stop offset="100%" stop-color="${c}" stop-opacity="0"/></linearGradient></defs>
    <path d="${d} L${X(pts[pts.length-1][0])} ${H} L${X(pts[0][0])} ${H} Z" fill="url(#fg)"/>
    <path d="${d}" fill="none" stroke="${c}" stroke-width="1.6" stroke-linejoin="round"/>`;
}

// ── thème manuel (persistant) ──
(function initTheme() {
  const saved = localStorage.getItem("btcq-theme");
  if (saved) document.documentElement.setAttribute("data-theme", saved);
  $("theme-btn").onclick = () => {
    const cur = document.documentElement.getAttribute("data-theme")
      || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    const next = cur === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    localStorage.setItem("btcq-theme", next);
    drawChart(); drawSpark(); refreshPrice(); refreshAnalytics(); drawYearly();
  };
})();

async function refreshEquity() {
  const response = await fetch("/api/equity");
  if (!response.ok) throw new Error("equity_http_" + response.status);
  chartData = await response.json();
  drawChart(); drawSpark();
}

async function refreshConformity() {
  const c = await (await fetch("/api/conformity")).json();
  const ref = c.reference, rz = c.realized, dd = c.drawdown;
  if (!ref) { $("conformity").innerHTML = '<div class="empty">Référence backtest absente.</div>'; return; }
  const pct = v => (v*100).toFixed(1).replace(".", ",") + " %";
  let rows = "";

  if (dd && dd.current != null) {
    const share = dd.backtest_time_at_least_as_deep;
    const normal = share != null && share > 0.02;
    rows += kvRow("Drawdown actuel", `<b class="${dd.current < -0.001 ? "down" : ""}">${pct(dd.current)}</b>`,
      share == null ? "" :
      share >= 1 ? "niveau courant, rien d'anormal" :
      `le backtest a passé ${pct(share)} du temps au moins aussi bas ${normal ? "→ normal" : "→ zone extrême"}`,
      share == null || normal ? "ok" : "warn");
  }
  if (rz) {
    const [lo, hi] = rz.win_rate_ci;
    const overlap = ref.win_rate >= lo && ref.win_rate <= hi;
    rows += kvRow("Win rate", `${pct(rz.win_rate)} <span style="color:var(--muted)">(${rz.n} trades)</span>`,
      rz.n < 20 ? `échantillon trop petit pour conclure (attendu : ${pct(ref.win_rate)})`
        : overlap ? `cohérent avec le backtest (${pct(ref.win_rate)})`
        : `s'écarte du backtest (${pct(ref.win_rate)}) — à surveiller`,
      rz.n < 20 || overlap ? "ok" : "warn");
    rows += kvRow("Série de pertes en cours", `${rz.current_loss_streak}`,
      `pire série du backtest : ${ref.worst_loss_streak} pertes d'affilée`,
      rz.current_loss_streak <= ref.worst_loss_streak ? "ok" : "warn");
  } else {
    rows += kvRow("Trades", "0", `attendu : ~${ref.trades_per_year}/an — patience, c'est le design`, "ok");
  }
  rows += kvRow("Mois attendus", `${pct(ref.monthly_return_p10)} à ${pct(ref.monthly_return_p90)}`,
    `pire mois historique : ${pct(ref.worst_month)}`, "ok");
  $("conformity").innerHTML = rows;
}
function kvRow(k, v, note, status) {
  const dot = status === "warn" ? "var(--warn)" : "var(--good)";
  return `<div class="kv" style="flex-wrap:wrap">
    <span class="k"><span style="display:inline-block;width:7px;height:7px;border-radius:99px;background:${dot};margin-right:8px"></span>${k}</span>
    <span class="num">${v}</span>
    ${note ? `<div style="flex-basis:100%;font-size:11.5px;color:var(--muted);padding:2px 0 0 15px">${note}</div>` : ""}
  </div>`;
}

// ── critères go/no-go paper → testnet (évalués côté serveur) ──
let lastReadiness = null;
let readinessDetailOpen = false;

function readinessLabels() {
  return {
    running: t("rdy_running"),
    protocol: t("rdy_protocol"),
    campaign: t("rdy_campaign"),
    inactive: t("rdy_inactive"),
    triggered: t("rdy_triggered"),
    na: t("rdy_na"),
    naOrders: t("rdy_na_orders"),
    naFills: t("rdy_na_fills"),
  };
}

function rdyRow(check, {showBar = false} = {}) {
  const ux = BTCQuantDashboardUx;
  const tone = ux.toneFor(check);
  const target = ux.displayTarget(check);
  const note = check.note || ux.naCopy(check, readinessLabels()) || "";
  const shownNote = ux.isBlank(check) ? note : check.note;
  return `<div class="rdy-row" data-tone="${tone}" data-key="${esc(check.key)}">
    <div class="rdy-main">
      <span class="rdy-label"><i class="rdy-mark" aria-hidden="true"></i>${esc(check.label)}</span>
      <span class="rdy-val">${esc(ux.displayValue(check, readinessLabels()))}${target ? ` <span class="rdy-obj">${esc(t("rdy_objective"))} ${esc(target)}</span>` : ""}</span>
    </div>
    ${shownNote ? `<div class="rdy-note">${esc(shownNote)}</div>` : ""}
  </div>`;
}

function rdySection(title, score, checks) {
  if (!checks.length) return "";
  return `<section class="rdy-section">
    <h3><span>${esc(title)}</span><span>${esc(score)}</span></h3>
    ${checks.map(check => rdyRow(check)).join("")}
  </section>`;
}

function renderReadiness(report) {
  const root = $("readiness");
  const badge = $("rdy-badge");
  if (!root || !badge) return;
  const ux = BTCQuantDashboardUx;
  if (!report || report.status === "SOURCE_UNAVAILABLE" || !Array.isArray(report.checks)) {
    badge.textContent = "—";
    badge.className = "estate";
    badge.style.color = "var(--crit)";
    root.innerHTML = `<div class="kv"><span class="k">${esc(t("rdy_source"))}</span><span class="num" style="color:var(--crit)">${esc((report && report.status) || "UNKNOWN")}</span></div>`;
    return;
  }
  const decided = ux.decision(report);
  const groups = ux.partition(report.checks);
  const nOk = Number(report.n_ok);
  const nTotal = Number(report.n_total);
  const remain = Number.isFinite(nOk) && Number.isFinite(nTotal) ? Math.max(0, nTotal - nOk) : 0;
  const verdictLabel = decided.verdict === "PRÊT" ? t("rdy_ready")
    : decided.verdict === "BLOQUÉ" ? t("rdy_blocked") : t("rdy_not_ready");
  const why = decided.tone === "block" ? t("rdy_block_why") : decided.tone === "ok" ? t("rdy_ready_why") : t("rdy_wait_why");
  badge.textContent = `TESTNET — ${verdictLabel}`;
  badge.className = "estate " + (decided.tone === "ok" ? "on" : decided.tone === "block" ? "off" : "");
  badge.style.color = decided.tone === "wait" ? "var(--warn)" : "";
  const outstanding = ux.blockers(report);
  root.innerHTML = `
    <div class="rdy-banner" data-tone="${decided.tone}">
      <div class="rdy-kicker">${esc(ux.campaignLine(report, readinessLabels()))}</div>
      <div class="rdy-verdict">${esc(`TESTNET — ${verdictLabel}`)}</div>
      <div class="rdy-why">${esc(why)}</div>
      <div class="rdy-counts">${esc(`${nOk} / ${nTotal} ${t("rdy_counts")}`)} · ${esc(`${remain} ${t("rdy_remain")}`)}</div>
      <div class="rdy-scores">
        <span class="rdy-chip" data-tone="${ux.scoreTone(groups.health)}">${esc(t("rdy_health"))} ${esc(ux.scoreLabel(groups.health))}</span>
        <span class="rdy-chip" data-tone="${ux.scoreTone(groups.qualification)}">${esc(t("rdy_stats"))} ${esc(ux.scoreLabel(groups.qualification))}</span>
        <span class="rdy-chip" data-tone="${ux.scoreTone(groups.execution)}">${esc(t("rdy_exec"))} ${esc(ux.scoreLabel(groups.execution))}</span>
      </div>
    </div>
    ${outstanding.length && !report.ready ? rdySection(t("rdy_blockers"), "", outstanding) : ""}
    ${rdySection(t("rdy_health"), ux.scoreLabel(groups.health), groups.health)}
    ${rdySection(t("rdy_stats"), ux.scoreLabel(groups.qualification), groups.qualification)}
    ${rdySection(t("rdy_exec"), ux.scoreLabel(groups.execution), groups.execution)}
    <button type="button" class="rdy-toggle" id="rdy-toggle">${esc(readinessDetailOpen ? t("rdy_hide") : t("rdy_show"))}</button>
    <div class="rdy-detail" id="rdy-detail" ${readinessDetailOpen ? "" : "hidden"}>
      ${report.checks.map(check => rdyRow(check)).join("")}
    </div>`;
  const toggle = $("rdy-toggle");
  if (toggle) {
    toggle.onclick = () => {
      readinessDetailOpen = !readinessDetailOpen;
      renderReadiness(report);
    };
  }
}

async function refreshReadiness() {
  try {
    const response = await fetch("/api/readiness");
    lastReadiness = await response.json();
    if (!response.ok && !Array.isArray(lastReadiness && lastReadiness.checks)) {
      lastReadiness = lastReadiness || {status: "UNKNOWN", checks: null};
    }
  } catch (error) {
    console.error(error);
    lastReadiness = {status: "UNKNOWN", checks: null};
  }
  renderReadiness(lastReadiness);
}

// ── années précédentes (backtest) : barres annuelles portefeuille vs BTC ──
let yearlyData = null;
async function refreshYearly() {
  try { yearlyData = await (await fetch("/api/yearly")).json(); } catch (e) { yearlyData = null; }
  drawYearly();
}
function drawYearly() {
  const svg = $("ychart");
  if (!svg) return;
  const W = svg.clientWidth, H = svg.clientHeight;
  if (!W) return; // carte masquée : redessinée au prochain changement de vue
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  const years = (yearlyData && yearlyData.years) || [];
  if (!years.length) {
    svg.innerHTML = `<text x="${W/2}" y="${H/2}" text-anchor="middle" fill="${col("--muted")}" font-size="12">${t("yearly_missing")}</text>`;
    $("ynote").textContent = "";
    return;
  }
  const M = {t: 22, r: 6, b: 24, l: 6};
  const vals = years.flatMap(y => [y.portfolio, y.btc]).filter(v => v != null);
  let vmax = Math.max(0, ...vals), vmin = Math.min(0, ...vals);
  const pad = (vmax - vmin) * 0.12;
  vmax += pad;
  // un rendement ne descend pas sous -100 % : le pad affichait « -108 % » en axe
  if (vmin < 0) vmin = Math.max(vmin - pad, -1);
  const Y = v => M.t + (vmax - v) / (vmax - vmin) * (H - M.t - M.b);
  const cell = (W - M.l - M.r) / years.length;
  const bw = Math.min(26, Math.max(7, (cell * 0.6 - 2) / 2));
  let g = "";
  // graduations « rondes » multiples d'un pas 1/2/2,5/5×10^k, ancrées sur 0 %
  // (la division uniforme min→max donnait des ticks arbitraires : +5 %, +119 %…)
  const rawStep = (vmax - vmin) / 4;
  const mag = Math.pow(10, Math.floor(Math.log10(rawStep)));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => (vmax - vmin) / s <= 5.5) || 10 * mag;
  for (let v = Math.ceil(vmin / step) * step; v <= vmax + 1e-9; v += step) {
    const y = Y(v);
    g += `<line x1="${M.l}" x2="${W - M.r}" y1="${y}" y2="${y}" stroke="${col("--grid")}"/>`;
    g += `<text x="${M.l + 2}" y="${y - 4}" fill="${col("--muted")}" font-size="10" style="font-variant-numeric:tabular-nums">${fmtPct(v, 0)}</text>`;
  }
  const y0 = Y(0);
  g += `<line x1="${M.l}" x2="${W - M.r}" y1="${y0}" y2="${y0}" stroke="${col("--axis")}" stroke-width="1.2"/>`;
  years.forEach((yr, i) => {
    const cx = M.l + cell * (i + 0.5);
    const title = escapeSvg(`${yr.year}${yr.partial ? " (" + t("yearly_partial") + ")" : ""} · ${t("portfolio")} ${fmtPct(yr.portfolio, 1)}`
      + ` · Trend ${fmtPct(yr.trend, 1)} · Carry ${fmtPct(yr.carry, 1)}`
      + (yr.btc != null ? ` · BTC ${fmtPct(yr.btc, 1)}` : "") + ` · max DD ${fmtDrawdown(yr.max_dd)}`);
    g += `<g><title>${title}</title>`;
    [[yr.portfolio, col("--s1")], [yr.btc, col("--muted")]].forEach(([v, c], j) => {
      if (v == null) return;
      const x = cx - bw - 1 + j * (bw + 2);
      g += `<rect x="${x.toFixed(1)}" y="${Math.min(Y(v), y0).toFixed(1)}" width="${bw.toFixed(1)}"
        height="${Math.max(1.5, Math.abs(Y(v) - y0)).toFixed(1)}" fill="${c}" rx="2"/>`;
    });
    // étiquette de valeur du portefeuille, au-dessus (ou dessous) des deux barres du groupe
    const lp = yr.portfolio;
    const both = yr.btc != null && (yr.btc >= 0) === (lp >= 0);
    const ref = both ? (lp >= 0 ? Math.min(Y(lp), Y(yr.btc)) : Math.max(Y(lp), Y(yr.btc))) : Y(lp);
    g += `<text x="${cx.toFixed(1)}" y="${(lp >= 0 ? ref - 6 : ref + 12).toFixed(1)}" text-anchor="middle"
      fill="${col("--ink-2")}" font-size="9.5" font-weight="700" style="font-variant-numeric:tabular-nums">${fmtPct(lp, 0)}</text>`;
    g += `<text x="${cx.toFixed(1)}" y="${H - 7}" text-anchor="middle" fill="${col("--muted")}" font-size="10.5">${yr.year}${yr.partial ? "*" : ""}</text>`;
    g += `</g>`;
  });
  svg.innerHTML = g;
  $("ynote").textContent = t("yearly_note")
    + (yearlyData.span ? ` · ${yearlyData.span[0]} → ${yearlyData.span[1]}` : "")
    + (years.some(y => y.partial) ? ` · * ${t("yearly_partial")}` : "");
}

// ── graphique prix : données + vue (plage zoomable, survol) ─────────────────
let pcData = null, pcRange = 200, pcView = null;
const PC_CHCOL = {D20: "#3b82f6", D55: "#f59e0b", D100: "#a855f7"};

async function refreshPrice() {
  pcData = await (await fetch("/api/price")).json();
  drawPChart();
}

function drawPChart() {
  const data = pcData;
  if (!data) return;
  const svg = $("pchart"), W = svg.clientWidth, H = svg.clientHeight;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  const all = data.candles || [];
  $("pos-note").textContent = data.positions.length
    ? "— " + data.positions.map(p => `${p.name} ${p.direction === 1 ? "LONG" : "SHORT"} @ ${Math.round(p.entry).toLocaleString("fr-FR")}`).join(" · ")
    : "— aucune position ouverte";
  const sideKnown = data.regime_up === true || data.regime_up === false;
  const isShort = data.regime_up === false;
  const direction = isShort ? "SHORT" : "LONG";
  const guide = $("signal-guide");
  guide.dataset.side = sideKnown ? direction.toLowerCase() : "unknown";
  const sourceChannels = data.channels || [];
  const lastValue = values => {
    for (let i = values.length - 1; i >= 0; i--) if (values[i] != null) return values[i];
    return null;
  };
  const latestPrice = all.length ? Number(all[all.length - 1][4]) : null;
  const activeThresholds = sourceChannels
    .map(ch => lastValue(isShort ? ch.low : ch.high))
    .filter(value => value != null);
  const crossedNow = sideKnown && latestPrice != null && activeThresholds.some(
    value => isShort ? latestPrice < value : latestPrice > value
  );
  const hasPosition = (data.positions || []).length > 0;
  guide.dataset.status = hasPosition ? "position" : crossedNow ? "threshold" : "waiting";
  $("signal-mode").textContent = t(!sideKnown
    ? "signal_unknown_mode"
    : hasPosition ? "signal_position" : crossedNow ? "signal_threshold" : "signal_wait");
  $("signal-rule").textContent = t(sideKnown
    ? (isShort ? "signal_short_rule" : "signal_long_rule")
    : "signal_unknown_rule");
  if (all.length < 10 || !sideKnown) { svg.innerHTML = ""; pcView = null; return; }
  // fenêtre affichée : les pcRange dernières bougies 1h (chips 2 j / 4 j / 8 j)
  const start = Math.max(0, all.length - pcRange);
  const candles = all.slice(start);
  const rawChans = sourceChannels.map(ch => ({
    name: ch.name,
    high: (ch.high || []).slice(start),
    low: (ch.low || []).slice(start),
  }));
  const chans = rawChans.flatMap(ch => [
    {name: ch.name, direction:"LONG", active:!isShort, vals:ch.high},
    {name: ch.name, direction:"SHORT", active:isShort, vals:ch.low},
  ]).map(ch => ({
    ...ch,
    color: ch.active ? (PC_CHCOL[ch.name] || col("--s1")) : col("--muted"),
    label: `${ch.name} ${ch.direction}${ch.active ? "" : ` · ${t("inactive_suffix")}`}`,
  }));
  const M = {t: 12, r: 132, b: 24, l: 10};
  let lo = Math.min(...candles.map(c => c[3])), hi = Math.max(...candles.map(c => c[2]));
  const channelValues = rawChans.flatMap(ch => [...ch.high, ...ch.low]).filter(v => v != null);
  if (channelValues.length) {
    lo = Math.min(lo, ...channelValues);
    hi = Math.max(hi, ...channelValues);
  }
  for (const p of data.positions) { lo = Math.min(lo, p.stop, p.entry); hi = Math.max(hi, p.stop, p.entry); }
  const pad = (hi - lo) * .06; lo -= pad; hi += pad;
  const X = i => M.l + i / (candles.length - 1) * (W - M.l - M.r);
  const Y = v => H - M.b - (v - lo) / (hi - lo) * (H - M.t - M.b);
  const up = col("--s2"), down = col("--crit");
  const bw = Math.max(1.5, (W - M.l - M.r) / candles.length * 0.62);

  let g = "";
  g += `<clipPath id="pclip"><rect x="${M.l}" y="${M.t}" width="${W - M.l - M.r}" height="${H - M.t - M.b}"/></clipPath>`;
  for (let i = 0; i <= 4; i++) {
    const v = lo + (hi - lo) * i / 4, y = Y(v);
    g += `<line x1="${M.l}" x2="${W - M.r}" y1="${y}" y2="${y}" stroke="${col("--grid")}"/>`;
    g += `<text x="${W - M.r + 8}" y="${y + 4}" fill="${col("--muted")}" font-size="10.5" style="font-variant-numeric:tabular-nums">${Math.round(v).toLocaleString("fr-FR")}</text>`;
  }
  // Corridor sans signal : sous le plus proche seuil LONG et au-dessus du
  // plus proche seuil SHORT. Les enveloppes suivent les paliers 4 h dans le
  // temps, elles ne projettent donc jamais les niveaux actuels dans le passé.
  const waiting = candles.map((_, i) => {
    const highs = rawChans.map(ch => ch.high[i]).filter(v => v != null);
    const lows = rawChans.map(ch => ch.low[i]).filter(v => v != null);
    return highs.length && lows.length ? {i, high:Math.min(...highs), low:Math.max(...lows)} : null;
  }).filter(Boolean);
  if (waiting.length > 1) {
    const top = waiting.map(p => `${X(p.i).toFixed(1)},${Y(p.high).toFixed(1)}`).join(" ");
    const bottom = [...waiting].reverse().map(p => `${X(p.i).toFixed(1)},${Y(p.low).toFixed(1)}`).join(" ");
    g += `<polygon points="${top} ${bottom}" fill="${col("--muted")}" opacity=".055" clip-path="url(#pclip)"/>`;
    const last = waiting[waiting.length - 1], middle = (Y(last.high) + Y(last.low)) / 2;
    g += `<text x="${W - M.r - 8}" y="${middle.toFixed(1)}" text-anchor="end" fill="${col("--muted")}" font-size="9.5" font-weight="700" opacity=".8">${t("waiting_label")}</text>`;
  }
  candles.forEach((c, i) => {
    const [ts, o, h, l, cl] = c, x = X(i), colr = cl >= o ? up : down;
    g += `<line x1="${x}" x2="${x}" y1="${Y(h)}" y2="${Y(l)}" stroke="${colr}" stroke-width="1"/>`;
    g += `<rect x="${x - bw/2}" y="${Y(Math.max(o, cl))}" width="${bw}" height="${Math.max(1, Math.abs(Y(o) - Y(cl)))}" fill="${colr}" rx="0.5"/>`;
  });
  for (let i = 0; i < 5; i++) {
    const idx = Math.round(i / 4 * (candles.length - 1));
    const d = new Date(candles[idx][0]);
    g += `<text x="${X(idx)}" y="${H - 7}" text-anchor="middle" fill="${col("--muted")}" font-size="10.5">${d.toLocaleDateString("fr-FR", {day:"2-digit", month:"2-digit"})} ${d.getHours()}h</text>`;
  }
  // canaux de Donchian 20/55/100 (calculés sur 4h, le timeframe de décision).
  // Les deux côtés sont visibles : plein/couleur pour la direction autorisée
  // par le régime EMA, gris/pointillé pour la direction inactive. Escalier :
  // un palier par barre 4 h. Les libellés sont empilés si les seuils coïncident.
  const labels = [];
  chans.forEach(ch => {
    const c = ch.color;
    let d = "", prev = null, last = null;
    ch.vals.forEach((v, i) => {
      if (v == null) { prev = null; return; }
      const x = X(i).toFixed(1), y = Y(v).toFixed(1);
      d += prev == null ? `M${x} ${y}` : `L${x} ${Y(prev).toFixed(1)}L${x} ${y}`;
      prev = v; last = v;
    });
    if (!d) return;
    g += `<path d="${d}" fill="none" stroke="${c}" stroke-width="${ch.active ? 1.35 : 1}" opacity="${ch.active ? .78 : .36}" ${ch.active ? "" : 'stroke-dasharray="3 4"'} clip-path="url(#pclip)"/>`;
    labels.push({name: ch.label, color: c, active:ch.active, y: Y(last)});
  });
  // étiquettes empilées : si deux niveaux sont confondus, on les écarte de 12px
  labels.sort((a, b) => a.y - b.y);
  for (let i = 1; i < labels.length; i++)
    labels[i].y = Math.max(labels[i].y, labels[i - 1].y + 12);
  for (const l of labels)
    g += `<text x="${W - M.r - 4}" y="${(l.y - 3).toFixed(1)}" text-anchor="end" fill="${l.color}" font-size="${l.active ? 10 : 9.2}" font-weight="${l.active ? 700 : 600}" opacity="${l.active ? 1 : .72}">${l.name}</text>`;
  // lignes d'entrée/stop + étiquettes à gauche, empilées comme les canaux à
  // droite : quand plusieurs systèmes sont entrés à des niveaux proches (cas
  // fréquent, les trois Donchian suivent souvent le même mouvement), les
  // libellés se chevauchaient sans repère de collision — même traitement que
  // les étiquettes de canaux (tri par y, écart mini 11px).
  //
  // Couleur : la ligne d'entrée reprend la couleur DU SYSTÈME (PC_CHCOL, la
  // même que son canal) pour qu'on relie canal ↔ entrée d'un coup d'œil. Le
  // stop garde le rouge universel (convention "danger" à ne pas diluer) mais
  // porte une pastille de la couleur du système pour dire QUI stoppe où.
  const posLabels = [];
  for (const p of data.positions) {
    const sysColor = PC_CHCOL[p.name] || col("--s1"), sc = col("--crit");
    g += `<line x1="${M.l}" x2="${W - M.r}" y1="${Y(p.entry)}" y2="${Y(p.entry)}" stroke="${sysColor}" stroke-width="1.4" stroke-dasharray="5 4"/>`;
    g += `<line x1="${M.l}" x2="${W - M.r}" y1="${Y(p.stop)}" y2="${Y(p.stop)}" stroke="${sc}" stroke-width="1.3" stroke-dasharray="2 3"/>`;
    posLabels.push({text: `${p.name} ENTRÉE`, textColor: sysColor, dotColor: sysColor, y: Y(p.entry)});
    posLabels.push({text: `${p.name} STOP`, textColor: sc, dotColor: sysColor, y: Y(p.stop)});
  }
  posLabels.sort((a, b) => a.y - b.y);
  for (let i = 1; i < posLabels.length; i++)
    posLabels[i].y = Math.max(posLabels[i].y, posLabels[i - 1].y + 11);
  for (const l of posLabels) {
    g += `<circle cx="${M.l + 3}" cy="${(l.y - 6.5).toFixed(1)}" r="3" fill="${l.dotColor}" stroke="${col("--surface-solid")}" stroke-width="1"/>`;
    g += `<text x="${M.l + 10}" y="${(l.y - 3).toFixed(1)}" fill="${l.textColor}" font-size="10" font-weight="700">${l.text}</text>`;
  }
  // pastille SUR la bougie où le trade a réellement été pris (distincte de la
  // ligne horizontale, qui ne dit que "le niveau" — pas "quand"). Nécessite
  // entry_ts dans la fenêtre affichée ; sinon (entrée plus ancienne que la
  // plage 2j/4j/8j sélectionnée) on n'affiche rien ici, la pastille de marge
  // ci-dessus reste le seul repère — pas de marqueur trompeur en bord de graphe.
  for (const p of data.positions) {
    if (!p.entry_ts) continue;
    const entryMs = Date.parse(String(p.entry_ts).replace(" ", "T"));
    if (isNaN(entryMs) || entryMs < candles[0][0] || entryMs > candles[candles.length - 1][0]) continue;
    let idx = 0, bestDiff = Infinity;
    for (let i = 0; i < candles.length; i++) {
      const diff = Math.abs(candles[i][0] - entryMs);
      if (diff < bestDiff) { bestDiff = diff; idx = i; }
    }
    const sysColor = PC_CHCOL[p.name] || col("--s1");
    const ex = X(idx).toFixed(1), ey = Y(p.entry).toFixed(1);
    const tri = p.direction === 1 ? "M0 -7 L6 4.5 L-6 4.5 Z" : "M0 7 L6 -4.5 L-6 -4.5 Z";
    const title = escapeSvg(`${p.name} ${p.direction === 1 ? "LONG" : "SHORT"} — entrée réelle @ ${Math.round(p.entry).toLocaleString("fr-FR")}`);
    g += `<g transform="translate(${ex} ${ey})" fill="${sysColor}" stroke="${col("--surface-solid")}" stroke-width="1.4"><title>${title}</title><path d="${tri}"/></g>`;
  }
  g += `<g id="pcursor"></g>`;
  svg.innerHTML = g;
  pcView = {candles, chans, positions: data.positions, X, Y, M, W, H};
}

// survol : réticule aimanté à la bougie + infobulle OHLC / canaux / position
(() => {
  const svg = $("pchart"), tip = $("ptip");
  const hide = () => { tip.style.display = "none"; const c = svg.querySelector("#pcursor"); if (c) c.innerHTML = ""; };
  svg.addEventListener("mouseleave", hide);
  svg.addEventListener("mousemove", e => {
    if (!pcView) return;
    const {candles, chans, positions, X, Y, M, W, H} = pcView;
    const r = svg.getBoundingClientRect(), mx = e.clientX - r.left;
    if (mx < M.l || mx > W - M.r) { hide(); return; }
    const i = Math.max(0, Math.min(candles.length - 1,
      Math.round((mx - M.l) / (W - M.l - M.r) * (candles.length - 1))));
    const [ts, o, h, l, cl] = candles[i], x = X(i);
    const cur = svg.querySelector("#pcursor");
    if (cur) cur.innerHTML =
      `<line x1="${x}" x2="${x}" y1="${M.t}" y2="${H - M.b}" stroke="${col("--muted")}" stroke-width="1" stroke-dasharray="2 3" opacity=".7"/>` +
      `<circle cx="${x}" cy="${Y(cl)}" r="3.2" fill="${cl >= o ? col("--s2") : col("--crit")}"/>`;
    const d = new Date(ts);
    const fp = v => Math.round(v).toLocaleString("fr-FR");
    let html = `<div class="t">${d.toLocaleDateString("fr-FR", {weekday:"short", day:"2-digit", month:"2-digit"})} ${String(d.getHours()).padStart(2,"0")}h</div>`;
    html += `<div class="row"><span>O / C</span><span class="num">${fp(o)} / <b style="color:${cl >= o ? col("--s2") : col("--crit")}">${fp(cl)}</b></span></div>`;
    html += `<div class="row"><span>H / B</span><span class="num">${fp(h)} / ${fp(l)}</span></div>`;
    for (const ch of chans) {
      const v = ch.vals[i];
      if (v != null) html += `<div class="row"><span style="color:${ch.color}">${ch.active ? "■" : "□"} ${ch.label}</span><span class="num">${fp(v)}</span></div>`;
    }
    for (const p of positions) {
      html += `<div class="row"><span>${p.name} entrée</span><span class="num">${fp(p.entry)}</span></div>`;
      html += `<div class="row"><span>${p.name} stop suiveur</span><span class="num">${fp(p.stop)}</span></div>`;
    }
    tip.innerHTML = html;
    tip.style.display = "block";
    const tw = tip.offsetWidth || 200;
    tip.style.left = (x + 14 + tw > W ? x - tw - 14 : x + 14) + "px";
    tip.style.top = Math.max(4, Math.min(e.clientY - r.top - 30, H - tip.offsetHeight - 8)) + "px";
  });
})();

document.querySelectorAll("#prange .chip").forEach(b => b.onclick = () => {
  document.querySelectorAll("#prange .chip").forEach(x => x.classList.remove("on"));
  b.classList.add("on"); pcRange = +b.dataset.n; drawPChart();
});

document.querySelectorAll("#range .chip").forEach(b => b.onclick = () => {
  document.querySelectorAll("#range .chip").forEach(x => x.classList.remove("on"));
  b.classList.add("on"); range = +b.dataset.r; drawChart();
});
document.querySelectorAll("#unit .chip").forEach(b => b.onclick = () => {
  setChartUnit(b.dataset.u);
});

const col = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

function drawSpark() {
  const svg = $("spark"), pts = (chartData && chartData.combined) || [];
  const W = svg.clientWidth || 240, H = svg.clientHeight || 64;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  if (pts.length < 3) { svg.innerHTML = ""; return; }
  const xs = pts.map(p => p[0]), ys = pts.map(p => p[1]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  let y0 = Math.min(...ys), y1 = Math.max(...ys);
  const pad = Math.max((y1 - y0) * .2, 1); y0 -= pad; y1 += pad;
  const X = t => (t - x0) / Math.max(1, x1 - x0) * (W - 6) + 2;
  const Y = v => H - 4 - (v - y0) / (y1 - y0) * (H - 10);
  const c = col("--s1");
  const d = pts.map((p, i) => (i ? "L" : "M") + X(p[0]).toFixed(1) + " " + Y(p[1]).toFixed(1)).join("");
  svg.innerHTML = `<defs><linearGradient id="sg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="${c}" stop-opacity=".28"/><stop offset="100%" stop-color="${c}" stop-opacity="0"/></linearGradient></defs>
    <path d="${d} L${X(pts[pts.length-1][0])} ${H} L${X(pts[0][0])} ${H} Z" fill="url(#sg)"/>
    <path d="${d}" fill="none" stroke="${c}" stroke-width="1.8" stroke-linejoin="round"/>`;
}

const escapeSvg = value => String(value || "").replace(/[&<>"']/g, char => ({
  "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&apos;"
}[char]));

function nearestPoint(points, ts) {
  let lo = 0, hi = points.length - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (points[mid][0] < ts) lo = mid; else hi = mid;
  }
  return Math.abs(points[lo][0] - ts) < Math.abs(points[hi][0] - ts) ? points[lo] : points[hi];
}

function chartEventKind(event) {
  const text = String(event.msg || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
  if (text.includes("funding")) return "funding";
  if (text.includes("entree")) return "entry";
  if (text.includes("sortie") || text.includes("stop")) return "exit";
  if (text.includes("kill") || event.level === "ERROR" || event.level === "WARNING") return "alert";
  return null;
}

function eventTimestamp(event) {
  return Number.isFinite(event.ts_ms)
    ? event.ts_ms
    : Date.parse(String(event.ts || "").replace(" ", "T") + "Z");
}

function drawChart() {
  const svg = $("chart");
  const W = svg.clientWidth, H = svg.clientHeight;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  const cutoff = range ? Date.now() - range * 3600e3 : 0;

  let series;
  if (unit === "dd") {
    // vue drawdown : distance au plus haut historique du portefeuille, en %
    let pts = ((chartData && chartData.combined) || []).filter(p => p[0] >= cutoff);
    let peak = -Infinity;
    pts = pts.map(p => { peak = Math.max(peak, p[1]); return [p[0], (p[1]/peak - 1) * 100]; });
    series = [{key: "dd", label: "Drawdown", color: col("--crit"), w: 2.2, area: true, pulse: true, pts}];
  } else {
    series = [
      {key: "combined", label: "Portefeuille", color: col("--s1"), w: 2.4, area: true, pulse: true},
      {key: "trend",    label: "Trend",        color: col("--s2"), w: 1.7},
      {key: "carry",    label: "Carry",        color: col("--s3"), w: 1.7},
    ].map(s => {
      let pts = ((chartData && chartData[s.key]) || []).filter(p => p[0] >= cutoff);
      if (unit === "pct" && pts.length) { const b = pts[0][1]; pts = pts.map(p => [p[0], p[1]/b*100]); }
      return {...s, pts};
    });
    // overlay buy & hold (BTC) pour comparaison
    if (showBH && chartData && chartData.buyhold && chartData.buyhold.length) {
      let bh = chartData.buyhold.filter(p => p[0] >= cutoff);
      if (unit === "pct" && bh.length) { const b = bh[0][1]; bh = bh.map(p => [p[0], p[1]/b*100]); }
      else if (unit === "usd" && bh.length && series[0].pts.length) {
        const scale = series[0].pts[0][1] / bh[0][1]; bh = bh.map(p => [p[0], p[1]*scale]);
      }
      series.push({key:"buyhold", label:"Buy&Hold", color:col("--muted"), w:1.5, dash:true, pts:bh});
    }
  }

  const all = series.flatMap(s => s.pts);
  if (all.length < 4) {
    svg.innerHTML = `<text x="${W/2}" y="${H/2}" text-anchor="middle" fill="${col("--muted")}" font-size="13">
      Historique en construction — les courbes se dessinent après quelques minutes de fonctionnement.</text>`;
    return;
  }
  const M = {t: 18, r: 108, b: 32, l: 20};
  const xs = all.map(p => p[0]), ys = all.map(p => p[1]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  let y0 = Math.min(...ys), y1 = Math.max(...ys);
  const pad = Math.max((y1 - y0) * .15, unit === "pct" ? .05 : 2); y0 -= pad; y1 += pad;
  const X = t => M.l + (t - x0) / Math.max(1, x1 - x0) * (W - M.l - M.r);
  const Y = v => H - M.b - (v - y0) / (y1 - y0) * (H - M.t - M.b);
  const fmtY = v => unit === "pct" ? v.toFixed(2)
    : unit === "dd" ? v.toFixed(1) + " %" : Math.round(v).toLocaleString("fr-FR");
  if (unit === "dd") y1 = Math.max(y1, 0.3);

  const fadeFlip = unit === "dd";  // en drawdown, le remplissage part du haut (zéro)
  let g = `<defs><linearGradient id="fade" x1="0" y1="${fadeFlip ? 1 : 0}" x2="0" y2="${fadeFlip ? 0 : 1}">
      <stop offset="0%" stop-color="${series[0].color}" stop-opacity=".18"/>
      <stop offset="100%" stop-color="${series[0].color}" stop-opacity="0"/></linearGradient></defs>`;
  for (let i = 0; i <= 5; i++) {
    const v = y0 + (y1 - y0) * i / 5, y = Y(v);
    g += `<line x1="${M.l}" x2="${W - M.r}" y1="${y}" y2="${y}" stroke="${col("--grid")}"/>`;
    g += `<text x="${M.l + 2}" y="${y - 5}" fill="${col("--muted")}" font-size="10.5" style="font-variant-numeric:tabular-nums">${fmtY(v)}</text>`;
  }
  const nx = Math.min(6, Math.floor(W / 150));
  for (let i = 0; i <= nx; i++) {
    const t = x0 + (x1 - x0) * i / nx, d = new Date(t);
    const lab = (x1 - x0 < 86400e3 * 2)
      ? d.toLocaleTimeString("fr-FR", {hour: "2-digit", minute: "2-digit"})
      : d.toLocaleDateString("fr-FR", {day: "2-digit", month: "short"});
    g += `<text x="${X(t)}" y="${H - 10}" text-anchor="middle" fill="${col("--muted")}" font-size="10.5">${lab}</text>`;
  }
  // courbes
  for (const s of series) {
    if (s.pts.length < 2) continue;
    const d = s.pts.map((p, i) => (i ? "L" : "M") + X(p[0]).toFixed(1) + " " + Y(p[1]).toFixed(1)).join("");
    if (s.area) g += `<path d="${d} L${X(s.pts[s.pts.length-1][0]).toFixed(1)} ${H-M.b} L${X(s.pts[0][0]).toFixed(1)} ${H-M.b} Z" fill="url(#fade)"/>`;
    g += `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="${s.w}" ${s.dash?'stroke-dasharray="6 4"':''} stroke-linejoin="round" stroke-linecap="round"/>`;
  }
  // marqueurs de trades clôturés sur la courbe (portefeuille)
  if (unit !== "dd" && lastTradeRows.length && series[0] && series[0].pts.length) {
    const cp = series[0].pts;
    for (const r of lastTradeRows) {
      const tms = Date.parse(r.exit_ts);
      if (isNaN(tms) || tms < (cutoff || cp[0][0]) || tms > cp[cp.length-1][0]) continue;
      let lo = 0, hi = cp.length - 1;
      while (hi - lo > 1) { const m = (lo+hi)>>1; (cp[m][0] < tms) ? lo=m : hi=m; }
      const yv = cp[lo][1], mc = r.pnl >= 0 ? col("--good") : col("--crit");
      g += `<circle cx="${X(tms).toFixed(1)}" cy="${Y(yv).toFixed(1)}" r="3" fill="${mc}" stroke="${col("--surface-solid")}" stroke-width="1.2" opacity=".85"/>`;
    }
  }
  // événements des runners : entrées, sorties/stops, funding et alertes.
  // Ils se recalculent à chaque rafraîchissement et suivent toujours la série affichée.
  if (lastEvents.length && series[0] && series[0].pts.length) {
    const cp = series[0].pts;
    const events = lastEvents.map(event => ({event, ts:eventTimestamp(event), kind:chartEventKind(event)}))
      .filter(row => row.kind && !isNaN(row.ts) && row.ts >= x0 && row.ts <= x1 && row.ts >= cutoff)
      .sort((a, b) => a.ts - b.ts).slice(-18);
    for (const row of events) {
      const point = nearestPoint(cp, row.ts), px = X(row.ts), py = Y(point[1]);
      const title = escapeSvg(`${row.event.source.toUpperCase()} · ${row.event.ts} · ${row.event.msg}`);
      const common = `<title>${title}</title>`;
      if (row.kind === "entry") {
        g += `<g transform="translate(${px.toFixed(1)} ${py.toFixed(1)})" fill="${col("--s2")}" stroke="${col("--surface-solid")}" stroke-width="1.2">${common}<path d="M0 -6 L5 4 L-5 4 Z"/></g>`;
      } else if (row.kind === "funding") {
        g += `<g transform="translate(${px.toFixed(1)} ${py.toFixed(1)})" fill="${col("--s3")}" stroke="${col("--surface-solid")}" stroke-width="1.2">${common}<rect x="-4" y="-4" width="8" height="8" rx="1" transform="rotate(45)"/></g>`;
      } else if (row.kind === "exit") {
        g += `<g transform="translate(${px.toFixed(1)} ${py.toFixed(1)})" fill="${col("--crit")}" stroke="${col("--surface-solid")}" stroke-width="1.2">${common}<path d="M0 6 L5 -4 L-5 -4 Z"/></g>`;
      } else {
        g += `<g transform="translate(${px.toFixed(1)} ${py.toFixed(1)})" fill="${col("--crit")}" stroke="${col("--surface-solid")}" stroke-width="1.2">${common}<circle r="4.5"/><path d="M0 -2.3 V.7 M0 2.4 V2.5" stroke="${col("--surface-solid")}" stroke-width="1.4" stroke-linecap="round"/></g>`;
      }
    }
  }
  // étiquettes de valeur à droite (façon terminal), anti-chevauchement
  const tags = series.filter(s => s.pts.length).map(s => {
    const last = s.pts[s.pts.length - 1];
    return {s, x: X(last[0]), y: Y(last[1]), v: last[1]};
  }).sort((a, b) => a.y - b.y);
  for (let i = 1; i < tags.length; i++) if (tags[i].y - tags[i-1].y < 22) tags[i].y = tags[i-1].y + 22;
  for (const t of tags) {
    if (t.s.pulse) g += `<circle cx="${t.x}" cy="${Y(t.v)}" r="4" fill="${t.s.color}" opacity=".9">
        <animate attributeName="r" values="3.5;8;3.5" dur="2.6s" repeatCount="indefinite"/>
        <animate attributeName="opacity" values=".9;.15;.9" dur="2.6s" repeatCount="indefinite"/></circle>`;
    g += `<circle cx="${t.x}" cy="${Y(t.v)}" r="3.2" fill="${t.s.color}" stroke="${col("--surface-solid")}" stroke-width="1.6"/>`;
    g += `<g transform="translate(${W - M.r + 10}, ${t.y})">
      <rect x="0" y="-11" width="${M.r - 16}" height="22" rx="7" fill="${t.s.color}" opacity=".14"/>
      <rect x="0" y="-11" width="3" height="22" rx="1.5" fill="${t.s.color}"/>
      <text x="9" y="-1" fill="${t.s.color}" font-size="9.5" font-weight="700" letter-spacing=".5">${t.s.label.toUpperCase()}</text>
      <text x="9" y="9" fill="${col("--ink")}" font-size="10.5" font-weight="600" style="font-variant-numeric:tabular-nums">${fmtY(t.v)}${unit === "usd" ? " $" : ""}</text>
    </g>`;
  }
  g += `<line id="xhair" y1="${M.t}" y2="${H - M.b}" stroke="${col("--axis")}" stroke-dasharray="3 3" visibility="hidden"/>`;
  svg.innerHTML = g;

  const tip = $("tooltip"), xhair = svg.querySelector("#xhair");
  svg.onmousemove = e => {
    const r = svg.getBoundingClientRect(), mx = e.clientX - r.left;
    if (mx < M.l || mx > W - M.r) { tip.style.display = "none"; xhair.setAttribute("visibility", "hidden"); return; }
    const t = x0 + (mx - M.l) / (W - M.l - M.r) * (x1 - x0);
    xhair.setAttribute("x1", mx); xhair.setAttribute("x2", mx); xhair.setAttribute("visibility", "visible");
    let rows = "";
    for (const s of series) {
      if (!s.pts.length) continue;
      let lo = 0, hi = s.pts.length - 1;
      while (hi - lo > 1) { const m = (lo + hi) >> 1; (s.pts[m][0] < t) ? lo = m : hi = m; }
      const p = (Math.abs(s.pts[lo][0] - t) < Math.abs(s.pts[hi][0] - t)) ? s.pts[lo] : s.pts[hi];
      rows += `<div class="row"><span><span class="swatch" style="background:${s.color};width:8px;height:8px;display:inline-block;margin-right:7px;border-radius:3px"></span>${s.label}</span>
        <span class="num" style="font-weight:650">${fmtY(p[1])}${unit === "usd" ? " $" : ""}</span></div>`;
    }
    tip.innerHTML = `<div class="t">${new Date(t).toLocaleString("fr-FR")}</div>` + rows;
    tip.style.display = "block";
    tip.style.left = Math.min(mx + 16, W - tip.offsetWidth - 10) + "px";
    tip.style.top = "12px";
  };
  svg.onmouseleave = () => { tip.style.display = "none"; xhair.setAttribute("visibility", "hidden"); };
}

// ── métriques live (Sharpe/Sortino/Calmar) ─────────────────
let lastCurDD = 0;
async function refreshMetrics() {
  const m = await (await fetch("/api/metrics")).json();
  lastMetrics = m;
  lastCurDD = m.cur_dd || 0;
  if (lastSummary) renderViewFocus(lastSummary);
  if (m.sharpe == null && m.days < 1) {
    $("metrics").innerHTML = `<div class="empty" style="grid-column:1/-1">${t("collecting")}</div>`;
    return;
  }
  const tile = (label, val, sub, cl) =>
    `<div class="metric"><div class="ml">${label}</div><div class="mv ${cl||''}">${val}</div><div class="ms">${sub||""}</div></div>`;
  $("metrics").innerHTML =
    tile(t("sharpe"), fmtNum(m.sharpe, 2), m.days + " j", m.sharpe >= 1 ? "up" : m.sharpe < 0 ? "down" : "") +
    tile(t("sortino"), fmtNum(m.sortino, 2), "", m.sortino >= 1 ? "up" : "") +
    tile(t("calmar"), fmtNum(m.calmar, 2), "", m.calmar >= 1 ? "up" : "") +
    tile(t("vol"), fmtPct(m.vol_annual, 0), PREFS.lang==="en"?"annualized":"annualisée", "");
}

// ── drill-down par stratégie ───────────────────────────────
async function openDrill(name) {
  const d = await (await fetch("/api/strategy/" + encodeURIComponent(name))).json();
  $("modal-title").textContent = name.replace("trend_ls_", "Donchian ");
  const st = d.stats || {};
  const money = v => v == null ? "—" : (v >= 0 ? "+" : "") + fmt$(v, 1);
  let body = "";
  if (st.n) {
    body += `<div class="mstat">
      ${["n","win_rate","pnl","avg_pnl","best","worst"].map(k => {
        const map = {n:["Trades",st.n],win_rate:[t("realized")+" ✓",Math.round(st.win_rate*100)+" %"],
          pnl:["PnL",money(st.pnl)],avg_pnl:["PnL moyen",money(st.avg_pnl)],best:["Meilleur",money(st.best)],worst:["Pire",money(st.worst)]};
        return `<div class="rec"><div class="rl">${map[k][0]}</div><div class="rv">${map[k][1]}</div></div>`;
      }).join("")}</div>`;
  }
  if (d.position) {
    const p = d.position;
    body += `<div class="kv"><span class="k">Position courante</span><span class="badge ${p.direction===1?"long":"short"}">${p.direction===1?"▲ LONG":"▼ SHORT"}</span></div>
      <div class="kv"><span class="k">Entrée / Stop</span><span class="num">${Math.round(p.entry_price).toLocaleString(LOCALE())} / ${Math.round(p.stop_price).toLocaleString(LOCALE())}</span></div>`;
  } else {
    body += `<div class="kv"><span class="k">Position courante</span><span class="badge flat">EN ATTENTE</span></div>`;
  }
  if (d.trades && d.trades.length) {
    body += `<h2 style="margin:16px 0 8px;font-size:12px">Derniers trades</h2><table>
      <thead><tr><th>Sortie</th><th>Sens</th><th>PnL</th><th>Motif</th></tr></thead><tbody>` +
      d.trades.map(r => `<tr><td class="num">${String(r.exit_ts).slice(5,16).replace("T"," ")}</td>
        <td><span class="badge ${r.direction==="LONG"?"long":"short"}">${esc(r.direction)}</span></td>
        <td class="num ${r.pnl>0?"up":"down"}" style="font-weight:650">${money(r.pnl)}</td>
        <td style="color:var(--muted);font-size:11.5px">${esc(r.reason)}</td></tr>`).join("") + "</tbody></table>";
  } else {
    body += `<div class="empty">Aucun trade clôturé pour ce sous-système.</div>`;
  }
  $("modal-body").innerHTML = body;
  $("modal-bd").classList.add("open");
}
$("modal-close").onclick = () => $("modal-bd").classList.remove("open");
$("modal-bd").onclick = e => { if (e.target === $("modal-bd")) $("modal-bd").classList.remove("open"); };

// ── notifications ──────────────────────────────────────────
function notify(title, body, tag) {
  if (!PREFS.notif || !("Notification" in window) || Notification.permission !== "granted") return;
  if (navigator.serviceWorker && navigator.serviceWorker.controller)
    navigator.serviceWorker.controller.postMessage({type:"notify", title, body, tag});
  else try { new Notification(title, {body, icon:"/icon.svg", tag}); } catch (e) {}
}
let alertState = {trendConfirmed:true, carryConfirmed:true, safetyStatus:"PASS", halted:false, ddNotified:false, posKeys:new Set()};
function checkAlerts(s) {
  const health = s.health || {};
  const trendConfirmed = !!s.trend.alive && s.trend.freshness === "FRESH";
  const carryConfirmed = !!s.carry.alive && s.carry.freshness === "FRESH";
  if (BTCQuantOperationalState.shouldNotifyFreshnessTransition("trend", alertState.trendConfirmed, trendConfirmed, health))
    notify("⚠ Moteur Trend non confirmé", "Le runner Trend est arrêté ou ses données sont périmées.", "trend-down");
  if (BTCQuantOperationalState.shouldNotifyFreshnessTransition("carry", alertState.carryConfirmed, carryConfirmed, health))
    notify("⚠ Moteur Carry non confirmé", "Le runner Carry est arrêté ou ses données sont périmées.", "carry-down");
  if (BTCQuantOperationalState.shouldNotifySafetyFailureTransition(alertState.safetyStatus, health.safety_status))
    notify("Execution safety", "A financial unsafe condition was detected.", "execution-safety");
  alertState.trendConfirmed = trendConfirmed;
  alertState.safetyStatus = health.safety_status || "UNKNOWN";
  alertState.carryConfirmed = carryConfirmed;
  if (!alertState.halted && s.trend.halted) notify("⛔ KILL-SWITCH", "Drawdown maximal atteint, positions liquidées.", "kill");
  alertState.halted = s.trend.halted;
  if (PREFS.notifPos) {
    const cur = new Set();
    for (const sl of s.trend.slots) if (sl.state !== "FLAT") {
      const k = sl.name + ":" + sl.state; cur.add(k);
      if (!alertState.posKeys.has(k))
        notify("● Position ouverte", `${sl.name.replace("trend_ls_","Donchian ")} ${sl.state} @ ${Math.round(sl.entry).toLocaleString(LOCALE())}`, k);
    }
    alertState.posKeys = cur;
  }
  // seuil de drawdown
  const thr = -Math.abs(PREFS.ddAlert) / 100;
  if (lastCurDD <= thr && !alertState.ddNotified) {
    notify("▼ Drawdown", `Le portefeuille est à ${fmtPct(lastCurDD,1)} (seuil ${PREFS.ddAlert}%).`, "dd");
    alertState.ddNotified = true;
  } else if (lastCurDD > thr * 0.7) alertState.ddNotified = false;
}

// ── panneau de préférences ─────────────────────────────────
const ACCENTS = ["#2a78d6","#149467","#c88800","#6b4de0","#d03b3b","#0ca3a3"];
function buildDrawer() {
  const acc = $("pref-accent");
  acc.innerHTML = ACCENTS.map(c => `<span class="sw-dot ${c===PREFS.accent?"on":""}" data-c="${c}" style="background:${c}"></span>`).join("");
  acc.querySelectorAll(".sw-dot").forEach(d => d.onclick = () => {
    PREFS.accent = d.dataset.c; savePrefs(); applyAccent(); buildDrawer(); redrawAll();
  });
  $("pref-lang").value = PREFS.lang;
  $("pref-currency").value = PREFS.currency;
  $("pref-range").value = String(PREFS.range);
  $("pref-refresh").value = String(PREFS.refresh);
  $("pref-notif").checked = PREFS.notif;
  $("pref-notif-pos").checked = PREFS.notifPos;
  $("pref-dd").value = PREFS.ddAlert;
  // cartes visibles
  const CARDS = ["performance_brief","risk_radar","monitor_pulse","chart","price","events","trend","carry","breakdown","conformity","yearly","trades","metrics","exposure","protocol","readiness"];
  $("pref-cards").innerHTML = CARDS.map(k =>
    `<div class="setrow"><span class="sk">${(t("cards")||{})[k]||k}</span>
     <label class="toggle"><input type="checkbox" data-card-k="${k}" ${PREFS.hidden[k]?"":"checked"}><span class="sl"></span></label></div>`).join("");
  $("pref-cards").querySelectorAll("[data-card-k]").forEach(inp => inp.onchange = () => {
    PREFS.hidden[inp.dataset.cardK] = !inp.checked; savePrefs(); applyCardVisibility();
  });
}
function applyCardVisibility() {
  document.querySelectorAll("[data-card]").forEach(c => {
    c.setAttribute("data-hidden", PREFS.hidden[c.dataset.card] ? "1" : "0");
  });
}
function redrawAll() { drawChart(); drawSpark(); drawPChart(); drawYearly(); if (lastSummary) renderExposureHealth(lastSummary); }

$("settings-btn").onclick = () => { buildDrawer(); $("drawer").classList.add("open"); $("drawer-bd").classList.add("open"); };
$("drawer-close").onclick = () => { $("drawer").classList.remove("open"); $("drawer-bd").classList.remove("open"); };
$("drawer-bd").onclick = () => { $("drawer").classList.remove("open"); $("drawer-bd").classList.remove("open"); };
$("pref-lang").onchange = e => { PREFS.lang = e.target.value; savePrefs(); applyI18n(); buildDrawer(); refreshMetrics(); redrawAll(); };
$("pref-currency").onchange = e => { PREFS.currency = e.target.value; savePrefs(); if (lastSummary) refreshSummary(); refreshTrades(); refreshAnalytics(); };
$("pref-range").onchange = e => { PREFS.range = +e.target.value; range = PREFS.range;
  document.querySelectorAll("#range .chip").forEach(x => x.classList.toggle("on", +x.dataset.r === range)); savePrefs(); drawChart(); };
$("pref-refresh").onchange = e => { PREFS.refresh = +e.target.value; savePrefs(); applyI18n(); restartTimer(); };
$("pref-notif").onchange = async e => {
  if (e.target.checked && "Notification" in window) {
    const p = await Notification.requestPermission();
    if (p !== "granted") { e.target.checked = false; return; }
  }
  PREFS.notif = e.target.checked; savePrefs();
};
$("pref-notif-pos").onchange = e => { PREFS.notifPos = e.target.checked; savePrefs(); };
$("pref-dd").onchange = e => { PREFS.ddAlert = Math.max(1, Math.min(30, +e.target.value || 12)); savePrefs(); };

// buy&hold + filtre de dates
$("bh-toggle").onclick = () => { showBH = !showBH; $("bh-toggle").classList.toggle("on", showBH); drawChart(); };
$("tr-from").onchange = refreshTrades; $("tr-to").onchange = refreshTrades;
$("tr-clear").onclick = () => { $("tr-from").value = ""; $("tr-to").value = ""; refreshTrades(); };

// ── boucle de rafraîchissement (fréquence configurable) ────
let timer = null;
function restartTimer() { if (timer) clearInterval(timer); if (PREFS.refresh) timer = setInterval(tick, PREFS.refresh); }
window.addEventListener("resize", () => { drawChart(); drawSpark(); drawYearly(); });
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { drawChart(); drawSpark(); drawYearly(); });
// PWA/onglet remis au premier plan : rafraîchir tout de suite plutôt que
// d'afficher des données figées jusqu'au prochain tick (et remettre le timer
// à zéro pour ne pas cumuler un tick immédiat + un tick programmé)
document.addEventListener("visibilitychange", () => { if (!document.hidden) { tick(); restartTimer(); } });
async function tick() {
  try { await Promise.all([refreshSummary(), refreshEvents(), refreshEquity(), refreshTrades(), refreshConformity(), refreshPrice(), refreshAnalytics(), refreshMetrics(), refreshReadiness()]); }
  catch (e) { console.error(e); }
}

// ── init ───────────────────────────────────────────────────
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
applyAccent(); applyI18n(); applyCardVisibility(); applyDashboardView();
document.querySelectorAll("#range .chip").forEach(x => x.classList.toggle("on", +x.dataset.r === range));
tick();
refreshYearly(); // référence statique : un seul chargement, redessinée aux changements de vue/thème
restartTimer();

"use strict";
