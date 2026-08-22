(function (root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.BTCQuantDashboardUx = api;
})(globalThis, function () {
  const HEALTH_KEYS = new Set([
    "integrity",
    "unresolved",
    "incidents",
    "killswitch",
    "trend_freshness",
    "carry_freshness",
  ]);
  const EXEC_KEYS = new Set([
    "rejections",
    "partials",
    "slippage",
    "orders",
    "orders_trend",
    "orders_carry",
  ]);
  const META_KEYS = new Set(["campaign", "qualification_age", "protocol_version"]);
  const QUAL_PRIORITY = [
    "days",
    "uptime",
    "trend_uptime",
    "carry_uptime",
    "trades",
    "orders",
    "orders_trend",
  ];

  function formatDuration(seconds) {
    if (seconds == null || seconds === "" || !Number.isFinite(Number(seconds))) return null;
    const negative = Number(seconds) < 0;
    let sec = Math.floor(Math.abs(Number(seconds)));
    const sign = negative ? "-" : "";
    if (sec < 60) return `${sign}${sec} s`;
    const days = Math.floor(sec / 86400);
    const hours = Math.floor((sec % 86400) / 3600);
    const minutes = Math.floor((sec % 3600) / 60);
    const rest = sec % 60;
    if (days) return hours ? `${sign}${days} j ${hours} h` : `${sign}${days} j`;
    if (hours) return minutes ? `${sign}${hours} h ${minutes} min` : `${sign}${hours} h`;
    if (rest) return `${sign}${minutes} min ${rest} s`;
    return `${sign}${minutes} min`;
  }

  function isBlank(check) {
    if (!check) return true;
    const value = check.value;
    return value == null || String(value).trim() === "" || String(value).trim() === "—";
  }

  function groupFor(check) {
    const key = check && check.key;
    if (META_KEYS.has(key)) return "meta";
    if (EXEC_KEYS.has(key) || (typeof key === "string" && key.startsWith("orders_"))) return "execution";
    if (key === "drawdown") return isBlank(check) ? "qualification" : "health";
    if (HEALTH_KEYS.has(key) || (typeof key === "string" && key.endsWith("_freshness"))) return "health";
    return "qualification";
  }

  function toneFor(check) {
    if (isBlank(check)) return "na";
    if (check.passed) return "ok";
    if (groupFor(check) === "health" || META_KEYS.has(check.key)) return "block";
    return "wait";
  }

  function campaignLine(report, labels) {
    const L = labels || {};
    const id = report && report.campaign_id != null ? `#${report.campaign_id}` : "";
    const running = report && report.campaign_status === "RUNNING" ? ` · ${L.running || "En cours"}` : "";
    const proto = report && report.protocol_version != null
      ? ` · ${L.protocol || "protocole"} v${report.protocol_version}`
      : "";
    if (!id && !running && !proto) return "";
    return `${L.campaign || "Campagne"} ${id}${running}${proto}`.replace(/\s+/g, " ").trim();
  }

  function naCopy(check, labels) {
    const L = labels || {};
    if (!isBlank(check)) return null;
    if (check.key === "rejections" || check.key === "partials" || (typeof check.key === "string" && check.key.startsWith("orders"))) {
      return L.naOrders || "N/A — aucun ordre terminal";
    }
    if (check.key === "slippage") return L.naFills || "N/A — aucun fill exploitable";
    return L.na || "N/A";
  }

  function displayValue(check, labels) {
    if (check.key === "killswitch") {
      return check.passed ? (labels && labels.inactive) || "Inactif" : (labels && labels.triggered) || "Déclenché";
    }
    const na = naCopy(check, labels);
    if (na) return na;
    const hours = String(check.value).match(/^(-?\d+(?:\.\d+)?)\s*h$/i);
    if (hours) {
      const formatted = formatDuration(Number(hours[1]) * 3600);
      if (formatted) return formatted;
    }
    return String(check.value);
  }

  function displayTarget(check) {
    if (!check.target || check.target === "ok" || check.target === "non" || check.target === "0") return "";
    const hours = String(check.target).match(/^(≥|≤)\s*(-?\d+(?:\.\d+)?)\s*h$/i);
    if (hours) {
      const formatted = formatDuration(Number(hours[2]) * 3600);
      if (formatted) return `${hours[1]} ${formatted}`;
    }
    return String(check.target);
  }

  function partition(checks) {
    const groups = {health: [], qualification: [], execution: [], meta: []};
    for (const check of checks || []) {
      groups[groupFor(check)].push(check);
    }
    return groups;
  }

  function scoreLabel(items) {
    const known = items.filter((item) => toneFor(item) !== "na");
    if (!known.length) return `—/${items.length}`;
    const ok = known.filter((item) => item.passed).length;
    return `${ok}/${items.length}`;
  }

  function scoreTone(items) {
    if (items.some((item) => toneFor(item) === "block")) return "block";
    if (items.every((item) => toneFor(item) === "na")) return "na";
    const known = items.filter((item) => toneFor(item) !== "na");
    if (known.length && known.every((item) => item.passed)) return "ok";
    return "wait";
  }

  function decision(report) {
    const checks = (report && report.checks) || [];
    const groups = partition(checks);
    const healthBroken = groups.health.some((item) => toneFor(item) === "block");
    if (!report || report.status === "SOURCE_UNAVAILABLE" || !Array.isArray(report.checks)) {
      return {verdict: "UNAVAILABLE", tone: "block", healthBroken: false};
    }
    if (healthBroken) return {verdict: "BLOQUÉ", tone: "block", healthBroken: true};
    if (report.ready) return {verdict: "PRÊT", tone: "ok", healthBroken: false};
    return {verdict: "NON PRÊT", tone: "wait", healthBroken: false};
  }

  function blockers(report) {
    const checks = (report && report.checks) || [];
    const groups = partition(checks);
    const health = groups.health.filter((item) => toneFor(item) === "block");
    const rest = [...groups.qualification, ...groups.execution].filter((item) => {
      if (item.passed || toneFor(item) === "na") return false;
      return true;
    });
    rest.sort((a, b) => {
      const ia = QUAL_PRIORITY.indexOf(a.key);
      const ib = QUAL_PRIORITY.indexOf(b.key);
      return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib);
    });
    return [...health, ...rest].slice(0, 5);
  }

  function carryPresentation(carry) {
    const state = carry || {};
    const position = ["OPEN", "FLAT", "UNKNOWN"].includes(state.position_status)
      ? state.position_status
      : state.position_known === false || state.in_position == null
        ? "UNKNOWN"
        : state.in_position === true
          ? "OPEN"
          : "FLAT";
    return {
      mode: "Paper synthétique",
      position,
      modeled: true,
      live: false,
      qty: state.qty,
      spot_qty: state.spot_qty,
      perp_qty: state.perp_qty,
      spot_notional: state.spot_notional,
      perp_notional: state.perp_notional,
      accounting_uncertain: !!state.accounting_uncertain,
    };
  }

  return {
    HEALTH_KEYS,
    EXEC_KEYS,
    META_KEYS,
    formatDuration,
    groupFor,
    toneFor,
    campaignLine,
    naCopy,
    displayValue,
    displayTarget,
    partition,
    scoreLabel,
    scoreTone,
    decision,
    blockers,
    carryPresentation,
    isBlank,
  };
});
