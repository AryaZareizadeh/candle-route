"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const app = fs.readFileSync(path.join(__dirname, "../crypto_forecast/web/app.js"), "utf8");
const prefix = app.slice(0, app.indexOf('  $("training-form").addEventListener("submit"'));

function dashboard({ confirm = true, bootstrap = {}, deletionError = null } = {}) {
  const elements = new Map(), calls = [], confirmations = [];
  function element(tag = "div") {
    const result = { tagName: tag.toUpperCase(), children: [], value: "", textContent: "", hidden: false,
      disabled: false, dataset: {}, style: {}, classList: { toggle() {} },
      append(...nodes) { this.children.push(...nodes); },
      replaceChildren(...nodes) { this.children = [...nodes]; },
      addEventListener(name, callback) { this[name] = callback; },
      setAttribute(name, value) { this[name] = value; },
      querySelectorAll() { return []; }, closest() { return element(); },
      contains() { return false; } };
    Object.defineProperty(result, "innerHTML", { set() {
      throw new Error("Saved checkpoint names must render as text.");
    } });
    return result;
  }
  function field(id) {
    if (!elements.has(id)) elements.set(id, element());
    return elements.get(id);
  }
  const context = vm.createContext({
    document: { getElementById: field, createElement: element, querySelectorAll: () => [] },
    Option: function(text, value) { this.text = text; this.value = value; },
    localStorage: { getItem() { return null; }, setItem() {} },
    window: { confirm(message) { confirmations.push(message); return confirm; } },
    console, AbortController, setTimeout: () => 1, clearTimeout() {},
    fetch: async (url, options = {}) => {
      calls.push({ url, options });
      const deleting = url.includes("/delete");
      return { ok: !(deleting && deletionError), status: deleting && deletionError ? 409 : 200,
        json: async () => deleting
          ? deletionError ? { error: deletionError } : { id: "saved-run", removed: true }
          : bootstrap };
    },
  });
  vm.runInContext(`${prefix}
    renderChart = () => {};
    renderLogs = () => {};
    renderMonitor = () => {};
    renderJobs = () => {};
    globalThis.ui = { state, renderSavedRuns, removeSavedRun };
  })();`, context);
  context.ui.state.token = "csrf-token";
  context.ui.state.savedRuns = [{ id: "saved-run", name: "Saved <checkpoint>", removable: true,
    artifact_types: ["predictor"], status: "failed", size_bytes: 100 }];
  return { ui: context.ui, calls, confirmations, field };
}

function descendants(element) {
  return [element, ...element.children.flatMap((child) => typeof child === "object" ? descendants(child) : [])];
}

test("inventory renders checkpoint names as text and disables protected removal", () => {
  const { ui, field } = dashboard();
  ui.state.savedRuns.push({ id: "active-run", name: "Active training", removable: false,
    status: "running", artifacts: ["model.pt"] });
  ui.renderSavedRuns();
  const nodes = descendants(field("saved-run-list"));
  assert(nodes.some((node) => node.textContent.includes("Saved <checkpoint>")));
  const buttons = nodes.filter((node) => node.tagName === "BUTTON");
  assert.equal(buttons.length, 2);
  assert.equal(buttons[0].disabled, false);
  assert.equal(buttons[1].disabled, true);
});

for (const { status, artifacts, label } of [
  { status: "failed", artifacts: ["model.pt", "predictor.pt"], label: "Predictor weights · evaluation incomplete" },
  { status: "completed", artifacts: ["model.pt", "summary.json"], label: "Model and evaluation" },
  { status: "saved", artifacts: ["model.pt", "summary.json"], label: "Model and evaluation" },
  { status: "saved", artifacts: ["model.pt"], label: "Saved model files" },
]) {
  test(`${status} run with ${artifacts.join(" and ")} reports ${label}`, () => {
    const { ui, field } = dashboard();
    ui.state.savedRuns = [{ id: "saved-run", name: "Saved checkpoint", removable: true, status, artifacts }];
    ui.renderSavedRuns();
    const text = descendants(field("saved-run-list")).map((node) => node.textContent).join(" ");
    assert(text.includes(label), `Expected ${label}; rendered ${text}`);
  });
}

test("canceling artifact removal leaves saved checkpoint and makes no request", async () => {
  const { ui, calls, confirmations } = dashboard({ confirm: false });
  await ui.removeSavedRun("saved-run");
  assert.equal(confirmations.length, 1);
  assert.match(confirmations[0], /Saved <checkpoint>|saved-run/);
  assert.equal(calls.length, 0);
  assert.equal(ui.state.savedRuns[0].id, "saved-run");
});

test("confirmed removal uses CSRF JSON and refreshes inventories and job history", async () => {
  const retainedJob = { id: "saved-run", status: "failed", artifacts_removed_at: "2026-10-23T00:00:00Z" };
  const bootstrap = { saved_runs: [], jobs: [retainedJob], tokenizers: [],
    chart_models: { pretrained: [], runs: [] } };
  const { ui, calls, confirmations, field } = dashboard({ bootstrap });
  field("kronos_tokenizer_run_id").value = "saved-run";
  await ui.removeSavedRun("saved-run");
  assert.equal(confirmations.length, 1);
  assert.equal(calls[0].url, "/api/runs/saved-run/delete");
  assert.equal(calls[0].options.method, "POST");
  assert.equal(calls[0].options.headers["X-CSRF-Token"], "csrf-token");
  assert.equal(calls[0].options.headers["Content-Type"], "application/json");
  assert.equal(calls[0].options.body, "{}");
  assert.equal(calls[1].url, "/api/bootstrap");
  assert.equal(ui.state.savedRuns.length, 0);
  assert.equal(ui.state.tokenizers.length, 0);
  assert.equal(ui.state.jobs[0].artifacts_removed_at, "2026-10-23T00:00:00Z");
  assert.equal(field("kronos_tokenizer_run_id").value, "");
});

test("protected or active artifact cannot trigger confirmation or deletion", async () => {
  const { ui, calls, confirmations } = dashboard();
  ui.state.savedRuns[0].removable = false;
  await ui.removeSavedRun("saved-run");
  assert.equal(confirmations.length, 0);
  assert.equal(calls.length, 0);
  assert.equal(ui.state.savedRuns.length, 1);
});

test("unknown identifier cannot trigger confirmation or deletion", async () => {
  const { ui, calls, confirmations } = dashboard();
  await ui.removeSavedRun("unknown-run");
  assert.equal(confirmations.length, 0);
  assert.equal(calls.length, 0);
});

test("server rejection keeps the saved checkpoint in the inventory", async () => {
  const { ui, calls, field } = dashboard({ deletionError: "A running forecast uses this checkpoint." });
  await ui.removeSavedRun("saved-run");
  assert.equal(calls.length, 1);
  assert.equal(ui.state.savedRuns[0].id, "saved-run");
  assert.match(field("saved-runs-error").textContent, /running forecast/);
});
