const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../crypto_forecast/web/connection.js"), "utf8");

test("embedded forecasting requests use scoped paths and the current parent session", async () => {
  const frame = { id: "forecastingFrame" };
  const requests = [];
  const window = { frameElement: frame, location: { origin: "http://candleroute.test" },
    parent: { forecastingParentToken: owner => owner === frame ? "initial-session" : null } };
  vm.runInNewContext(source, { window, fetch: async (url, options) => {
    requests.push({ url, options });
    return { ok: true, status: 200, json: async () => ({}) };
  } });

  await window.ForecastConnection.api("/api/bootstrap");
  await window.ForecastConnection.api("/api/jobs", {}, "session-after-bootstrap");
  assert.equal(requests[0].url, "./api/bootstrap");
  assert.equal(requests[0].options.headers["X-CSRF-Token"], "initial-session");
  assert.equal(requests[1].options.headers["X-CSRF-Token"], "session-after-bootstrap");
  assert.equal(requests[1].options.credentials, "same-origin");
  assert.equal(window.ForecastConnection.path("/api/chart/forecasts/test"), "./api/chart/forecasts/test");
  window.parent.forecastingParentToken = () => null;
  await assert.rejects(window.ForecastConnection.api("/api/bootstrap"), /Sign in again/);
  assert.equal(requests.length, 2);
});

test("a stale forecast request notifies the parent on the app origin", async () => {
  const messages = [];
  const window = { frameElement: { id: "forecastingFrame" }, location: { origin: "http://candleroute.test" },
    parent: { postMessage: (...args) => messages.push(args) } };
  vm.runInNewContext(source, { window, fetch: async () => ({ ok: false, status: 401 }) });
  await assert.rejects(window.ForecastConnection.api("/api/jobs", {}, "stale"), /Sign in again/);
  assert.deepEqual(messages, [["forecasting-session-ended", "http://candleroute.test"]]);
});
