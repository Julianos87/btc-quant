(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.BTCQuantDashboardState = factory();
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  class SourceRequestState {
    constructor() {
      this.sequence = 0;
      this.status = "idle";
      this.inFlight = false;
      this.lastSuccessAt = null;
      this.lastError = null;
    }

    begin() {
      this.sequence += 1;
      this.inFlight = true;
      // Keep a confirmed outage visible while a retry is in progress. A
      // request lifecycle must not masquerade as a successful source state.
      if (this.status !== "unavailable") this.status = "loading";
      return this.sequence;
    }

    isCurrent(sequence) {
      return sequence === this.sequence;
    }

    succeed(sequence, receivedAt) {
      if (!this.isCurrent(sequence)) return false;
      this.inFlight = false;
      this.status = "available";
      this.lastSuccessAt = receivedAt;
      this.lastError = null;
      return true;
    }

    fail(sequence, error) {
      if (!this.isCurrent(sequence)) return false;
      this.inFlight = false;
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

  function timeoutError(url) {
    const error = new Error(`dashboard_timeout_${url}`);
    error.name = "TimeoutError";
    return error;
  }

  // Keep the deadline alive until the response body has been consumed. The
  // browser resolves fetch() after headers, while response.json() can still
  // wait indefinitely on a stalled stream.
  function fetchResponseWithTimeout(fetchImpl, url, options = {}, {
    timeoutMs = 12000,
    setTimeoutImpl = globalThis.setTimeout,
    clearTimeoutImpl = globalThis.clearTimeout,
  } = {}) {
    const controller = new AbortController();
    let timedOut = false;
    let released = false;
    const externalSignal = options.signal;
    const abort = () => controller.abort();
    const release = () => {
      if (released) return;
      released = true;
      clearTimeoutImpl(timeout);
      if (externalSignal) externalSignal.removeEventListener("abort", abort);
    };
    const timeout = setTimeoutImpl(() => {
      timedOut = true;
      controller.abort();
    }, timeoutMs);
    if (externalSignal) {
      if (externalSignal.aborted) controller.abort();
      else externalSignal.addEventListener("abort", abort, {once: true});
    }

    return Promise.resolve().then(() => fetchImpl(url, {...options, signal: controller.signal})).then(response => {
      if (!response || response.ok === false || typeof response.json !== "function") {
        release();
        return response;
      }
      const json = response.json.bind(response);
      response.json = (...args) => Promise.resolve().then(() => json(...args)).catch(error => {
        if (timedOut) throw timeoutError(url);
        throw error;
      }).finally(release);
      return response;
    }).catch(error => {
      release();
      if (timedOut) throw timeoutError(url);
      throw error;
    });
  }

  return {SourceRequestState, LatestRequestGate, formatQuantity, fetchResponseWithTimeout};
});
