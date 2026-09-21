(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.BTCQuantDashboardState = factory();
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  class SourceRequestState {
    constructor() {
      this.sequence = 0;
      this.status = "idle";
      this.lastSuccessAt = null;
      this.lastError = null;
    }

    begin() {
      this.sequence += 1;
      this.status = "loading";
      return this.sequence;
    }

    isCurrent(sequence) {
      return sequence === this.sequence;
    }

    succeed(sequence, receivedAt) {
      if (!this.isCurrent(sequence)) return false;
      this.status = "available";
      this.lastSuccessAt = receivedAt;
      this.lastError = null;
      return true;
    }

    fail(sequence, error) {
      if (!this.isCurrent(sequence)) return false;
      this.status = "unavailable";
      this.lastError = error || null;
      return true;
    }
  }

  class LatestRequestGate {
    constructor() {
      this.sequence = 0;
    }

    begin() {
      this.sequence += 1;
      return this.sequence;
    }

    isCurrent(sequence) {
      return sequence === this.sequence;
    }
  }

  function formatQuantity(value, {unit = "BTC", locale = "fr-FR", maximumFractionDigits = 12} = {}) {
    if (value == null || value === "") return `N/A ${unit}`;
    const number = Number(value);
    if (!Number.isFinite(number)) return `N/A ${unit}`;
    if (Object.is(number, -0) || number === 0) return `0 ${unit}`;
    const formatted = Math.abs(number) < 10 ** -maximumFractionDigits
      ? number.toLocaleString(locale, {maximumSignificantDigits: 6})
      : number.toLocaleString(locale, {maximumFractionDigits});
    return `${formatted} ${unit}`;
  }

  return { SourceRequestState, LatestRequestGate, formatQuantity };
});
