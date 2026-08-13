(function (root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.BTCQuantOperationalState = api;
})(globalThis, function () {
  function requiredComponents(health) {
    return new Set((health && health.required_components) || ["trend"]);
  }

  function componentAvailabilitySeverity(name, item, health) {
    const required = requiredComponents(health).has(name);
    if (!item || !item.alive || ["UNKNOWN", "UNAVAILABLE"].includes(item.freshness))
      return required ? "crit" : "warn";
    if (item.freshness === "STALE") return "warn";
    return null;
  }

  function shouldNotifyFreshnessTransition(name, previousConfirmed, currentConfirmed, health) {
    return requiredComponents(health).has(name) && previousConfirmed && !currentConfirmed;
  }

  function shouldNotifySafetyFailureTransition(previousStatus, currentStatus) {
    return previousStatus !== "FAIL" && currentStatus === "FAIL";
  }

  return {
    requiredComponents,
    componentAvailabilitySeverity,
    shouldNotifyFreshnessTransition,
    shouldNotifySafetyFailureTransition,
  };
});
