"use strict";

(() => {
  const frame = window.frameElement;
  const embedded = frame?.id === "forecastingFrame";
  const accountWorkspace = embedded || window.location?.pathname === "/forecasting/";
  const path = (url) => "." + url;
  if (embedded) window.document?.documentElement.classList.add("embedded-workspace");

  async function api(url, options = {}, token = null) {
    const headers = {Accept: "application/json", ...options.headers};
    const sessionToken = token || (embedded && window.parent.forecastingParentToken?.(frame));
    if (embedded && !sessionToken) throw new Error("Your session ended. Sign in again.");
    if (sessionToken) headers["X-CSRF-Token"] = sessionToken;
    if (options.body) headers["Content-Type"] = "application/json";
    const response = await fetch(path(url), {...options, headers, credentials: "same-origin", cache: "no-store"});
    if (response.status === 401 && embedded) {
      window.parent.postMessage("forecasting-session-ended", window.location.origin);
      throw new Error("Your session ended. Sign in again.");
    }
    let data;
    try { data = await response.json(); }
    catch (_) { throw new Error(`The server returned an unreadable response (${response.status}).`); }
    if (!response.ok) throw new Error(data.error?.message || data.error || data.message || `Request failed (${response.status}).`);
    return data;
  }

  window.ForecastConnection = {api, path, accountWorkspace};
})();
