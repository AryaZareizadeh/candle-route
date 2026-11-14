"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const timeframeHours = (frame) => ({ "15m": 0.25, "1h": 1, "4h": 4, "1d": 24 })[frame];
  function durationLabel(days) {
    if (!Number.isFinite(days) || days <= 0) return "Choose a forecast length";
    const hours = days * 24;
    if (hours < 1) return `${number(hours * 60, 0)} minutes`;
    if (hours < 24) return `${number(hours, 2)} ${hours === 1 ? "hour" : "hours"}`;
    return `${number(days, 2)} ${days === 1 ? "day" : "days"}`;
  }
  const state = { token: null, settingsScope: null, models: [], jobs: [], tokenizers: [], savedRuns: [], savedRunsSupported: false, removingRun: null, separateStagesSupported: false, selectedJob: null, busy: false, timer: null, online: false, logSignature: "", pollCount: 0, submittedDecision: null, seenDecisions: new Set() };
  const active = (job) => job && ["queued", "running", "awaiting_decision"].includes(job.status);
  const waiting = (job) => job?.status === "awaiting_decision" && Boolean(job.pending_decision);
  const decisionKey = (job) => waiting(job) ? `${job.id}:${job.pending_decision.decision_id}` : null;
  const number = (value, digits = 2) => value == null || !Number.isFinite(Number(value)) ? "—" : Number(value).toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: digits });
  const count = (value) => number(value, 0);
  const ordinal = (value) => { const n = Number(value); return Number.isInteger(n) ? `${n}${n % 100 >= 10 && n % 100 <= 20 ? "th" : { 1: "st", 2: "nd", 3: "rd" }[n % 10] || "th"}` : "—"; };
  const percent = (value) => value == null || !Number.isFinite(Number(value)) ? "—" : `${number(Number(value) * 100, 1)}%`;
  const date = (value, detailed = false) => {
    if (!value) return "—";
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleString(undefined, detailed ? { month: "short", day: "numeric", year: "numeric", hour: "2-digit", minute: "2-digit", timeZone: "UTC" } : { month: "short", day: "numeric", year: "numeric" });
  };
  const duration = (seconds) => {
    if (seconds == null || !Number.isFinite(Number(seconds))) return "—";
    seconds = Math.max(0, Math.floor(Number(seconds)));
    const hours = Math.floor(seconds / 3600), minutes = Math.floor((seconds % 3600) / 60);
    return hours ? `${hours}h ${minutes}m` : minutes ? `${minutes}m ${seconds % 60}s` : `${seconds}s`;
  };
  const node = (tag, text, className) => { const el = document.createElement(tag); if (text != null) el.textContent = text; if (className) el.className = className; return el; };
  const showError = (id, message) => { $(id).textContent = message || ""; $(id).hidden = !message; };
  const coinStorageKey = "crypto-forecast.coin-choices.v1";
  const validCoin = (coin) => typeof coin === "string" && /^[A-Z0-9]{1,20}$/.test(coin) && coin !== "USDT" && !/^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$/.test(coin);
  const schedulerFields = ["lr_scheduler", "lr_min_ratio", "lr_warmup_fraction", "lr_factor", "lr_step_epochs", "lr_patience"];
  const isKronos = (model) => ["kronos-small", "kronos-base"].includes(model);
  const tokenizerDataFields = ["start", "end", "lookback", "train_fraction", "validation_fraction", "kronos_timeframe", "kronos_prediction_length", "kronos_train_stride", "kronos_eval_stride"];
  const formStorageKey = "crypto-forecast.training-settings.v2";
  function workspaceStorageKey(key) {
    return state.settingsScope ? `${key}.${state.settingsScope}` : key;
  }
  function applySession(bootstrap) {
    state.token = bootstrap.csrf_token;
    state.settingsScope = bootstrap.settings_scope || null;
  }
  let restoredTokenizerId = "";
  function saveFormSettings() {
    if (window.ForecastConnection.accountWorkspace && !state.settingsScope) return;
    const fields = Array.from($("training-form").querySelectorAll("input[id]:not([type=checkbox]), select[id]"));
    try { localStorage.setItem(workspaceStorageKey(formStorageKey), JSON.stringify({ values: Object.fromEntries(fields.filter((field) => field.id !== "extra-coins").map((field) => [field.id, field.value])), profiles: trainingProfiles })); } catch (_) { /* Training remains available without storage. */ }
  }
  function restoreFormSettings() {
    try {
      const saved = JSON.parse(localStorage.getItem(workspaceStorageKey(formStorageKey)));
      if (!saved?.values) return;
      for (const [id, value] of Object.entries(saved.values)) {
        const field = $(id);
        if (!field || !$("training-form").contains(field) || typeof value !== "string") continue;
        if (id === "kronos_tokenizer_run_id") restoredTokenizerId = value;
        else field.value = value;
      }
      Object.assign(trainingProfiles, saved.profiles || {});
      previousModel = $("model_type").value;
    } catch (_) { /* Ignore unavailable storage or malformed settings. */ }
  }
  function renderTokenizers(tokenizers = state.tokenizers) {
    state.tokenizers = tokenizers;
    const select = $("kronos_tokenizer_run_id"), selected = select.value || restoredTokenizerId;
    select.replaceChildren(new Option("Original pretrained tokenizer", ""), ...tokenizers.map((item) => new Option(`${item.name || item.id} · epoch ${item.best_epoch || "—"}${item.complete ? "" : " · best saved checkpoint; training stopped"}`, item.id)));
    if (tokenizers.some((item) => item.id === selected)) select.value = selected;
    else if (selected) { const unavailable = new Option(`Unavailable saved tokenizer · ${selected}`, selected); unavailable.disabled = true; select.append(unavailable); select.value = selected; }
    restoredTokenizerId = "";
    renderTokenizerDetails();
  }
  function applyTokenizerBootstrap(bootstrap) {
    state.separateStagesSupported = Array.isArray(bootstrap.tokenizers);
    renderTokenizers(bootstrap.tokenizers || []);
  }
  function renderTokenizerDetails() {
    const selected = $("kronos_tokenizer_run_id").value, source = state.tokenizers.find((item) => item.id === selected);
    $("use-tokenizer-settings").disabled = !source || $("kronos_training_stage").value !== "predictor";
    $("saved-tokenizer-note").textContent = source ? `${source.name || source.id} · ${date(source.created_at)} · best epoch ${source.best_epoch || "—"} · validation MSE ${number(source.best_validation_loss, 5)}. ${source.complete ? "Training complete." : "Best saved checkpoint; training stopped."} ${(source.config.coins || []).join(", ")} · ${source.config.kronos_timeframe || "4h"} candles · ${source.config.start || "—"} to ${source.config.end || "latest available"}. Selecting this tokenizer copies its coins, dates, candle windows, strides, and partition shares. Predictor epochs, learning rate, schedule, and batch size stay independent.` : selected ? "This saved tokenizer is unavailable. Choose another checkpoint or the original pretrained tokenizer." : "Use the original pretrained tokenizer with your selected data settings.";
  }
  function useTokenizerSettings() {
    const source = state.tokenizers.find((item) => item.id === $("kronos_tokenizer_run_id").value);
    if (!source) return;
    for (const field of tokenizerDataFields) if (Object.prototype.hasOwnProperty.call(source.config, field)) {
      if (source.config[field] != null || field === "end") $(field).value = source.config[field] ?? "";
    }
    if (source.config.kronos_prediction_length === null) $("kronos_prediction_length").value = 30 * 24 / timeframeHours(source.config.kronos_timeframe || "4h");
    for (const coin of source.config.coins || []) { removedCoins.delete(coin); coinChoice(coin); }
    for (const input of $("coin-picks").querySelectorAll("input")) input.checked = (source.config.coins || []).includes(input.value);
    $("extra-coins").value = "";
    saveCoinChoices(); renderArchitecture(); saveFormSettings();
    $("saved-tokenizer-note").textContent += " Data settings reused from this tokenizer.";
  }
  const rateText = (value) => value == null || !Number.isFinite(Number(value)) ? "—" : Number(value).toExponential(3);
  function schedulerConfig(prefix = "") {
    return Object.fromEntries(schedulerFields.filter((field) => !$(prefix + field).disabled).map((field) => [prefix + field, field === "lr_scheduler" ? $(prefix + field).value : Number($(prefix + field).value)]));
  }
  function syncSchedulers() {
    const kronos = ["kronos-small", "kronos-base"].includes($("model_type").value);
    for (const prefix of ["", "kronos_tokenizer_"]) {
      const enabled = prefix ? kronos && $("kronos_training_stage").value !== "predictor" : !kronos || $("kronos_training_stage").value !== "tokenizer";
      const policy = $(prefix + "lr_scheduler").value;
      $(prefix + "lr_scheduler").disabled = !enabled;
      const fields = {lr_min_ratio: policy !== "constant", lr_warmup_fraction: ["warmup_cosine", "onecycle"].includes(policy), lr_factor: ["plateau", "step"].includes(policy), lr_step_epochs: policy === "step", lr_patience: policy === "plateau"};
      for (const [field, shown] of Object.entries(fields)) {
        $(prefix + field).disabled = !enabled || !shown;
        $(prefix + field).required = enabled && shown;
        $(prefix + field).closest("[data-schedule-field]").hidden = !shown;
      }
      const peak = Number($(prefix ? "kronos_tokenizer_learning_rate" : "learning_rate").value);
      const minimum = peak * Number($(prefix + "lr_min_ratio").value);
      const note = $(prefix + "schedule-note");
      note.textContent = policy === "constant" ? "Keep the same rate throughout training." : `Peak ${rateText(peak)} · minimum ${rateText(minimum)}. ` + (policy === "plateau" ? "Watch validation loss only; reduce after patience is exceeded." : policy === "step" ? "Reduce at the selected epoch interval." : policy === "linear" ? "Decrease smoothly across the planned updates." : `Warm up over ${(Number($(prefix + "lr_warmup_fraction").value) * 100).toFixed(1)}% of planned updates, then decrease smoothly. Very short runs shorten or skip warmup.`);
      if (["warmup_cosine", "linear", "onecycle"].includes(policy)) note.textContent += " Extra epochs hold the minimum rate.";
      const early = Number($(prefix ? "kronos_tokenizer_patience" : "patience").value);
      if (policy === "plateau" && early <= Number($(prefix + "lr_patience").value) + 1) note.textContent += " Increase early-stop patience to give a reduced rate time to train.";
    }
  }
  const trainingProfiles = {};
  let previousModel = "gru";
  let removedCoins = new Set(), menuCoin = null, menuTrigger = null;
  function coinChoice(coin, selected = false) {
    if (!validCoin(coin) || removedCoins.has(coin)) return null;
    const existing = Array.from($("coin-picks").querySelectorAll("input")).find((input) => input.value === coin);
    if (existing) return existing;
    const label = node("label"), input = node("input");
    input.type = "checkbox"; input.value = coin; input.checked = selected;
    label.title = coin;
    label.append(input, node("span", coin)); $("coin-picks").append(label);
    return input;
  }
  function saveCoinChoices() {
    organizeCoinChoices();
    if (window.ForecastConnection.accountWorkspace && !state.settingsScope) return;
    const inputs = Array.from($("coin-picks").querySelectorAll("input"));
    try { localStorage.setItem(workspaceStorageKey(coinStorageKey), JSON.stringify({ coins: inputs.map((input) => input.value), selected: inputs.filter((input) => input.checked).map((input) => input.value), removed: [...removedCoins] })); } catch (_) { /* Coin controls still work when browser storage is unavailable. */ }
  }
  function updateCoinSelection() {
    const inputs = Array.from($("coin-picks").querySelectorAll("input")), selected = inputs.filter((input) => input.checked).length;
    $("coin-selection-count").textContent = `${selected} of ${inputs.length} selected`;
    $("select-all-coins").disabled = !inputs.length || selected === inputs.length;
    $("clear-coins").disabled = selected === 0;
  }
  function organizeCoinChoices() {
    const container = $("coin-picks"), inputs = Array.from(container.querySelectorAll("input"));
    const sorted = [...inputs].sort((first, second) => first.value.localeCompare(second.value));
    if (sorted.some((input, index) => input !== inputs[index])) {
      for (const input of sorted) container.append(input.closest("label"));
    }
    updateCoinSelection();
  }
  function setCoinSelection(selected) {
    for (const input of $("coin-picks").querySelectorAll("input")) input.checked = selected;
    saveCoinChoices(); renderArchitecture();
  }
  function discoverCoinChoices(bootstrap) {
    const coins = [ ...(bootstrap.data?.coins || []).map((entry) => entry.coin), ...(bootstrap.jobs || []).flatMap((job) => job.config?.coins || []) ];
    for (const coin of coins) coinChoice(coin);
    organizeCoinChoices();
  }
  function restoreCoinChoices(bootstrap, persist = true) {
    let saved = null;
    try { saved = JSON.parse(localStorage.getItem(workspaceStorageKey(coinStorageKey))); } catch (_) { /* Ignore unavailable storage or an invalid saved selection. */ }
    if (saved && Array.isArray(saved.removed)) removedCoins = new Set(saved.removed.filter(validCoin));
    for (const input of $("coin-picks").querySelectorAll("input")) {
      if (removedCoins.has(input.value)) input.closest("label").remove();
    }
    discoverCoinChoices(bootstrap);
    if (Array.isArray(saved?.coins) && Array.isArray(saved?.selected)) {
      for (const coin of saved.coins) {
        const input = coinChoice(coin);
        if (input) input.checked = saved.selected.includes(coin);
      }
    }
    if (persist) saveCoinChoices();
    else organizeCoinChoices();
  }
  function addCoinsFromInput() {
    const coins = [...new Set($("extra-coins").value.split(/[\s,]+/).map((coin) => coin.trim().toUpperCase()).filter(Boolean))];
    if (coins.some((coin) => !validCoin(coin))) throw new Error("Use base coin symbols such as DOGE, separated with commas. USDT is the quote currency.");
    for (const coin of coins) { removedCoins.delete(coin); coinChoice(coin).checked = true; }
    if (coins.length) {
      $("extra-coins").value = ""; saveCoinChoices();
      $("coin-feedback").textContent = `${coins.join(", ")} added to the coin buttons. Click a coin to select or deselect it.`;
      $("coin-feedback").hidden = false;
    }
    renderArchitecture();
    return coins;
  }
  function addCoins() {
    showError("form-error", "");
    try { addCoinsFromInput(); } catch (error) { showError("form-error", error.message); }
  }
  function closeCoinMenu(restoreFocus = false) {
    $("coin-menu").hidden = true;
    if (restoreFocus && menuTrigger?.isConnected) menuTrigger.focus();
    menuCoin = null; menuTrigger = null;
  }
  function openCoinMenu(event) {
    const label = event.target.closest("label"), input = label?.querySelector("input");
    if (!input || !$("coin-picks").contains(label)) return;
    event.preventDefault();
    menuCoin = input.value; menuTrigger = input;
    const menu = $("coin-menu"), bounds = label.getBoundingClientRect();
    $("remove-coin").textContent = `Remove ${menuCoin}`;
    menu.hidden = false;
    const x = event.type === "keydown" || (!event.clientX && !event.clientY) ? bounds.left : event.clientX;
    const y = event.type === "keydown" || (!event.clientX && !event.clientY) ? bounds.bottom : event.clientY;
    menu.style.left = `${Math.max(8, Math.min(x, window.innerWidth - menu.offsetWidth - 8))}px`;
    menu.style.top = `${Math.max(8, Math.min(y, window.innerHeight - menu.offsetHeight - 8))}px`;
    $("remove-coin").focus();
  }
  function removeMenuCoin() {
    if (!menuCoin) return;
    const coin = menuCoin;
    const input = Array.from($("coin-picks").querySelectorAll("input")).find((choice) => choice.value === coin);
    removedCoins.add(coin); input?.closest("label").remove();
    closeCoinMenu(); saveCoinChoices(); renderArchitecture();
    $("coin-feedback").textContent = `${coin} removed from the coin buttons. Add its symbol again to restore it.`;
    $("coin-feedback").hidden = false; $("extra-coins").focus();
  }
  function connection(online) {
    state.online = online;
    $("connection").className = `connection ${online ? "online" : "offline"}`;
    $("connection-text").textContent = online ? "Connected" : "Forecasting unavailable";
    updateActions();
  }
  async function api(url, options = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 12000);
    try { return await window.ForecastConnection.api(url, {...options, signal: controller.signal}, state.token); }
    finally { clearTimeout(timeout); }
  }
  function applyDefaults(defaults = {}) {
    for (const id of [...schedulerFields, ...schedulerFields.map((field) => "kronos_tokenizer_" + field), "start", "end", "model_type", "lookback", "hidden_size", "epochs", "patience", "batch_size", "learning_rate", "device", "seed", "train_fraction", "validation_fraction", "kronos_timeframe", "kronos_prediction_length", "kronos_sample_count", "kronos_eval_stride", "kronos_train_stride", "kronos_preparation_workers", "kronos_finetune_tokenizer", "kronos_tokenizer_epochs", "kronos_tokenizer_patience", "kronos_tokenizer_learning_rate", "kronos_tokenizer_batch_size"]) {
      if (Object.prototype.hasOwnProperty.call(defaults, id)) $(id).value = defaults[id] == null ? "" : defaults[id];
    }
    $("kronos_training_stage").value = isKronos(defaults.model_type) ? defaults.kronos_finetune_tokenizer === false && (!defaults.kronos_training_stage || defaults.kronos_training_stage === "sequential") ? "predictor" : defaults.kronos_training_stage || "sequential" : "tokenizer";
    if (defaults.kronos_tokenizer_batch_size == null) $("kronos_tokenizer_batch_size").value = "36";
    if (defaults.kronos_prediction_length === null) $("kronos_prediction_length").value = isKronos(defaults.model_type) ? 30 * 24 / timeframeHours(defaults.kronos_timeframe || "4h") : "24";
    restoredTokenizerId = defaults.kronos_tokenizer_run_id || "";
    if (Array.isArray(defaults.coins)) {
      for (const coin of defaults.coins) coinChoice(coin);
      document.querySelectorAll(".coin-picks input").forEach((input) => { input.checked = defaults.coins.includes(input.value); });
    }
    previousModel = $("model_type").value;
    syncModelControls();
  }
  function applyModelOptions(models) {
    const selected = $("model_type").value;
    state.models = Array.isArray(models) && models.length ? models : [{ id: "gru", name: "Multi-timeframe GRU", n_features: 11, embedding_size: 8, dropout: 0.15, encoder_gates: 3 }];
    $("model_type").replaceChildren(...state.models.map((model) => new Option(model.name, model.id)));
    if (state.models.some((model) => model.id === selected)) $("model_type").value = selected;
  }
  function renderCoverage(data) {
    const coins = data?.coins || [];
    const container = $("data-coverage"); container.replaceChildren();
    $("data-count").textContent = coins.length ? `${coins.length} coins` : "";
    if (!coins.length) { container.append(node("p", "No saved candles yet. Select coins and download their history.", "empty-small")); return; }
    for (const coin of coins) {
      const row = node("div", null, "coverage-row"), top = node("div");
      top.append(node("strong", coin.coin), node("span", (coin.timeframes || []).join(" / "), "coverage-frames"));
      const frame = coin.timeframes?.[0] || "15m";
      row.append(top, node("p", `${count(coin.rows?.[frame])} ${frame} candles · ${date(coin.last_close?.[frame], true)} UTC`));
      container.append(row);
    }
  }
  function configFromForm(operation = "train") {
    addCoinsFromInput();
    const selected = Array.from(document.querySelectorAll(".coin-picks input:checked"), (input) => input.value);
    const coins = [...new Set(selected)];
    if (!coins.length) throw new Error("Select at least one coin to train or download.");
    if (coins.some((coin) => !validCoin(coin))) throw new Error("Use base coin symbols such as BTC or DOGE, separated with commas.");
    const config = { coins, start: $("start").value, end: $("end").value || null };
    if (config.end && config.end <= config.start) throw new Error("The end date must be after the start date.");
    if (operation === "download") return config;
    Object.assign(config, { device: $("device").value, model_type: $("model_type").value });
    for (const id of ["lookback", "hidden_size", "epochs", "patience", "batch_size", "learning_rate", "seed", "train_fraction", "validation_fraction"]) config[id] = Number($(id).value);
    Object.assign(config, schedulerConfig());
    if (["kronos-small", "kronos-base"].includes(config.model_type)) {
      Object.assign(config, schedulerConfig("kronos_tokenizer_"));
      delete config.hidden_size;
      config.kronos_timeframe = $("kronos_timeframe").value;
      for (const id of ["kronos_prediction_length", "kronos_sample_count", "kronos_eval_stride", "kronos_train_stride", "kronos_preparation_workers"]) config[id] = Number($(id).value);
      config.kronos_training_stage = $("kronos_training_stage").value;
      config.kronos_finetune_tokenizer = config.kronos_training_stage !== "predictor";
      for (const id of ["kronos_tokenizer_epochs", "kronos_tokenizer_patience", "kronos_tokenizer_learning_rate", "kronos_tokenizer_batch_size"]) config[id] = Number($(id).value);
      if (config.kronos_training_stage === "predictor" && $("kronos_tokenizer_run_id").value) {
        const id = $("kronos_tokenizer_run_id").value;
        if (!state.tokenizers.some((item) => item.id === id)) throw new Error("Choose an available saved tokenizer or the original pretrained tokenizer.");
        config.kronos_tokenizer_run_id = id;
        const source = state.tokenizers.find((item) => item.id === id);
        if (source.config.kronos_prediction_length === null && config.kronos_prediction_length === 30 * 24 / timeframeHours(config.kronos_timeframe)) config.kronos_prediction_length = null;
      }
    }
    if (!(config.train_fraction > 0 && config.validation_fraction > 0 && config.train_fraction + config.validation_fraction < 1)) throw new Error("Training and validation shares must be positive and add to less than 1, leaving data for the final test.");
    return config;
  }
  function updateActions() {
    const current = state.jobs.find(active), selected = state.jobs.find((job) => job.id === state.selectedJob);
    $("train-button").disabled = state.busy || !state.online || Boolean(current) || (isKronos($("model_type").value) && !state.separateStagesSupported);
    $("download-button").disabled = state.busy || !state.online || Boolean(current);
    $("stop-button").disabled = state.busy || !state.online || !active(selected);
    $("stop-button").textContent = selected?.operation === "download" ? "Stop download" : "Stop training";
    const decisionDisabled = state.busy || !state.online || !waiting(selected) || state.submittedDecision === decisionKey(selected);
    $("continue-training").disabled = decisionDisabled;
    $("finish-training").disabled = decisionDisabled;
  }
  function selectPendingDecision(scroll = false) {
    const pending = state.jobs.find((job) => waiting(job) && !state.seenDecisions.has(decisionKey(job)));
    if (!pending) return;
    state.seenDecisions.add(decisionKey(pending));
    state.selectedJob = pending.id; state.logSignature = "";
    showError("decision-error", "");
    if (scroll) $("monitor-title").closest("section").scrollIntoView({ block: "start" });
  }
  async function decideTraining(action) {
    const selected = state.jobs.find((job) => job.id === state.selectedJob), key = decisionKey(selected);
    if (!key || state.busy || !state.online || state.submittedDecision === key) return;
    state.submittedDecision = key; state.busy = true; updateActions(); showError("decision-error", "");
    try {
      const job = await api(`/api/jobs/${encodeURIComponent(selected.id)}/decision`, { method: "POST", body: JSON.stringify({ decision_id: selected.pending_decision.decision_id, action }) });
      state.jobs = state.jobs.map((existing) => existing.id === job.id ? job : existing);
      renderJobs(); renderMonitor();
    } catch (error) { state.submittedDecision = null; showError("decision-error", error.message); }
    finally { state.busy = false; updateActions(); schedulePoll(400); }
  }
  function renderDecision(job) {
    const show = waiting(job);
    $("training-decision").hidden = !show;
    if (!show) { showError("decision-error", ""); return; }
    const decision = job.pending_decision, epoch = decision.epoch ?? job.progress?.epoch, extension = decision.extension_epochs || 10;
    const title = decision.reason === "extension_complete" ? `${extension} extra epochs completed` : "Early-stop patience reached";
    const patience = job.config?.patience;
    const reason = decision.reason === "extension_complete" ? `The extension finished at epoch ${epoch}.` : `Validation loss has not improved for ${patience || "the configured number of"} ${patience === 1 ? "epoch" : "consecutive epochs"}. Training paused at epoch ${epoch}.`;
    const message = `${reason} Train ${extension} more epochs, or finish and save the best validation epoch. You will be asked again after each extension.`;
    if ($("decision-title").textContent !== title) $("decision-title").textContent = title;
    if ($("decision-message").textContent !== message) $("decision-message").textContent = message;
    $("continue-training").textContent = `Train ${extension} more epochs`;
  }
  async function createJob(operation) {
    showError("form-error", "");
    if (operation === "train" && isKronos($("model_type").value) && !state.separateStagesSupported) { showError("form-error", "Restart the dashboard server to enable separate tokenizer and predictor tuning."); return; }
    if (operation === "train" ? !$("training-form").reportValidity() : !$("start").reportValidity() || !$("end").reportValidity()) return;
    let config;
    try { config = configFromForm(operation); } catch (error) { showError("form-error", error.message); return; }
    state.busy = true; updateActions();
    const button = $(operation === "train" ? "train-button-label" : "download-button-label"), original = button.textContent;
    button.textContent = operation === "train" ? "Starting training…" : "Starting download…";
    try {
      const job = await api("/api/jobs", { method: "POST", body: JSON.stringify({ operation, name: $("run-name").value.trim() || undefined, config }) });
      state.jobs = [job, ...state.jobs.filter((existing) => existing.id !== job.id)]; state.selectedJob = job.id;
      state.logSignature = ""; renderJobs(); renderMonitor(); schedulePoll(400);
    } catch (error) { showError("form-error", error.message); }
    finally { state.busy = false; button.textContent = original; updateActions(); }
  }
  async function stopJob() {
    if (!state.selectedJob) return;
    state.busy = true; updateActions(); showError("job-error", "");
    try {
      const job = await api(`/api/jobs/${encodeURIComponent(state.selectedJob)}/stop`, { method: "POST", body: "{}" });
      state.jobs = state.jobs.map((existing) => existing.id === job.id ? job : existing); renderJobs(); renderMonitor();
    } catch (error) { showError("job-error", error.message); }
    finally { state.busy = false; updateActions(); schedulePoll(600); }
  }
  function renderSavedRuns(runs = state.savedRuns) {
    state.savedRuns = runs;
    const list = $("saved-run-list"); list.replaceChildren();
    if (!runs.length) { list.append(node("p", "No saved training runs yet.", "empty-small")); return; }
    for (const run of runs) {
      const row = node("div", null, "saved-run-row"), details = node("div");
      details.append(node("strong", run.name || run.id));
      const artifacts = run.artifacts || [];
      const evaluated = artifacts.includes("model.pt") && artifacts.includes("summary.json") && ["completed", "saved"].includes(run.status || "saved");
      const kind = evaluated ? "Model and evaluation" : artifacts.includes("predictor.pt") ? "Predictor weights · evaluation incomplete" : artifacts.includes("model.pt") ? "Saved model files" : artifacts.includes("tokenizer.pt") ? "Tokenizer" : "Partial training files";
      details.append(node("p", `${kind} · ${run.status || "saved"}${run.created_at ? " · " + date(run.created_at) : ""}`));
      const button = node("button", "Remove", "button stop remove-saved-run");
      button.type = "button"; button.disabled = run.removable === false || Boolean(state.removingRun);
      button.setAttribute("aria-label", `Remove saved run ${run.name || run.id}`);
      if (run.removable === false) button.title = "Stop the active job before removing this run.";
      button.addEventListener("click", () => removeSavedRun(run.id));
      row.append(details, button); list.append(row);
    }
  }
  async function removeSavedRun(identifier) {
    const run = state.savedRuns.find((item) => item.id === identifier);
    if (!run || run.removable === false || state.removingRun) return;
    if (!window.confirm(`Remove "${run.name || run.id}"?\n\nThis permanently deletes this run's saved model, tokenizer, predictions, and reports. Its job history will remain. Original pretrained models and candle data are kept.`)) return;
    state.removingRun = identifier; showError("saved-runs-error", ""); $("saved-runs-feedback").textContent = "Removing saved files…"; renderSavedRuns();
    try {
      await api(`/api/runs/${encodeURIComponent(identifier)}/delete`, { method: "POST", body: "{}" });
      state.savedRuns = state.savedRuns.filter((item) => item.id !== identifier);
      const bootstrap = await api("/api/bootstrap");
      state.jobs = bootstrap.jobs || []; state.savedRuns = bootstrap.saved_runs || state.savedRuns;
      if ($("kronos_tokenizer_run_id").value === identifier) { $("kronos_tokenizer_run_id").value = ""; restoredTokenizerId = ""; }
      applyTokenizerBootstrap(bootstrap); renderJobs(); renderMonitor(); saveFormSettings();
      if (window.modelWorkspace?.refresh) await window.modelWorkspace.refresh();
      $("saved-runs-feedback").textContent = "Saved files removed. Job history is still available.";
    } catch (error) { showError("saved-runs-error", error.message); $("saved-runs-feedback").textContent = ""; }
    finally { state.removingRun = null; renderSavedRuns(); schedulePoll(600); }
  }
  function renderJobs() {
    const select = $("job-select"); select.replaceChildren();
    const jobs = state.jobs.filter((job) => job.operation !== "forecast");
    select.append(new Option(jobs.length ? "Choose a job" : "No training jobs", ""));
    for (const job of jobs) select.append(new Option(`${job.name || (job.operation === "download" ? "Candle download" : "Training run")} · ${job.status === "awaiting_decision" ? "waiting for your choice" : job.status}${job.artifacts_removed_at ? " · saved files removed" : ""}`, job.id));
    select.value = state.selectedJob || "";
    updateActions();
  }
  function renderLogs(logs = [], fallback = "Download and training messages will appear here.") {
    const signature = JSON.stringify(logs);
    if (signature === state.logSignature) return;
    state.logSignature = signature;
    const container = $("activity-log"), atBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 50;
    container.replaceChildren();
    $("log-count").textContent = logs.length ? `(${logs.length})` : "";
    if (!logs.length) container.append(node("p", fallback, "empty-small"));
    for (const log of logs) {
      const row = node("div", null, "log-row");
      const parsed = new Date(log.timestamp);
      const timestamp = Number.isNaN(parsed.getTime()) ? "" : parsed.toLocaleTimeString(undefined, { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });
      row.append(node("span", timestamp, "log-time"), node("span", log.message, "log-message")); container.append(row);
    }
    if (atBottom) container.scrollTop = container.scrollHeight;
  }
  const svgNS = "http://www.w3.org/2000/svg";
  function svgElement(tag, attributes, text) {
    const element = document.createElementNS(svgNS, tag);
    for (const [name, value] of Object.entries(attributes || {})) element.setAttribute(name, value);
    if (text != null) element.textContent = text;
    return element;
  }
  function syncModelControls() {
    const kronos = isKronos($("model_type").value), frame = $("kronos_timeframe").value, stage = $("kronos_training_stage").value;
    $("kronos-settings").hidden = !kronos;
    $("kronos_finetune_tokenizer").value = stage !== "predictor" ? "true" : "false";
    $("saved-tokenizer-settings").hidden = !kronos || stage !== "predictor";
    $("kronos_tokenizer_run_id").disabled = !kronos || stage !== "predictor";
    $("kronos_training_stage").disabled = !kronos;
    $("kronos-stage-note").textContent = !state.separateStagesSupported ? "Restart the dashboard server to enable separate tokenizer and predictor tuning." : stage === "tokenizer" ? "Train reconstruction + binary spherical quantization (BSQ), select by full reconstruction validation MSE, and save a reusable tokenizer. The predictor stays unchanged." : stage === "predictor" ? "Freeze the original or saved tokenizer and train the predictor with autoregressive token cross-entropy. Select its best validation epoch, then evaluate forecasts." : "Tune the tokenizer first, freeze its best reconstruction checkpoint, then train and evaluate the predictor.";
    syncSchedulers();
    for (const id of ["kronos_timeframe", "kronos_prediction_length", "kronos_sample_count", "kronos_eval_stride", "kronos_train_stride", "kronos_preparation_workers", "kronos_finetune_tokenizer"]) $(id).disabled = !kronos;
    for (const id of ["kronos_tokenizer_epochs", "kronos_tokenizer_patience", "kronos_tokenizer_learning_rate", "kronos_tokenizer_batch_size"]) $(id).disabled = !kronos || stage === "predictor";
    for (const id of ["epochs", "patience", "batch_size", "learning_rate", "kronos_sample_count"]) $(id).disabled = id === "kronos_sample_count" ? !kronos || stage === "tokenizer" : kronos && stage === "tokenizer";
    $("schedule-controls").querySelector("label").textContent = kronos ? "Predictor learning-rate schedule" : "Model learning-rate schedule";
    $("batch_size").closest("div").querySelector("label").firstChild.textContent = kronos ? "Predictor batch size " : "Batch size ";
    renderTokenizerDetails();
    $("hidden_size").disabled = kronos;
    $("hidden_size").closest("div").querySelector("label").dataset.help = kronos ? "Kronos uses its pretrained architecture. Hidden size is fixed." : "The number of learned features in each timeframe encoder. Larger values increase model capacity, parameter count, and training work.";
    const future = Number($("kronos_prediction_length").value), maximum = Math.max(1, Math.min(512, 513 - future));
    const lookbackHelp = kronos ? `Completed ${frame} candles per input. Maximum ${maximum} input bars with ${future} forecast candles. Input plus forecast minus one must fit the 512-token training context.` : "The number of completed candles each timeframe sees at a forecast origin. For example, 60 bars cover 15 hours at 15 minutes and 60 days at daily resolution.";
    $("lookback").max = kronos ? String(maximum) : "1000";
    $("kronos_prediction_length").max = String(Math.max(1, Math.min(512, 513 - Number($("lookback").value))));
    $("kronos-target-note").textContent = `${future} future ${frame} candles · ${durationLabel(future * timeframeHours(frame) / 24)} ahead. Each future candle is a training target.`;
    document.querySelector(".forecast-horizons").textContent = kronos ? stage === "tokenizer" ? "Reconstruct candle windows" : `${future} future ${frame} candles` : "1 / 7 / 30 days ahead";
    $("model-flow").setAttribute("aria-label", kronos ? stage === "tokenizer" ? `Reconstruct completed ${frame} candle windows to tune the tokenizer` : `Completed ${frame} candles predict ${future} future candles` : "Completed candles predict one, seven, and thirty days ahead");
    $("model-parameter-label").textContent = kronos && stage === "tokenizer" ? "Predictor parameters (frozen)" : kronos && stage === "sequential" ? "Predictor parameters" : "Trainable parameters";
    $("lookback").dataset.help = lookbackHelp;
    const label = $("lookback").closest("div").querySelector("label");
    label.dataset.help = lookbackHelp; label.firstChild.textContent = kronos ? "Input bars " : "Bars per timeframe ";
    $("origin-tag").textContent = kronos ? `${frame} origins` : "4-hour origins";
    $("candle-group").replaceChildren(...(kronos ? [frame] : ["15m", "1h", "4h", "1d"]).map((value) => node("span", value)));
    $("setup-note").textContent = kronos ? "One candle stream per coin includes volume. Fine-tuning, validation, and testing follow time order." : "All four candle streams include volume. Training, validation, and testing follow time order.";
    $("train-button-label").textContent = kronos ? ({ tokenizer: "Tune tokenizer", predictor: "Tune predictor", sequential: "Tune tokenizer, then predictor" })[stage] : "Train model";
    updateActions();
  }
  function changeModel() {
    const oldProfile = ["kronos-small", "kronos-base"].includes(previousModel) ? "kronos" : "recurrent", nextProfile = ["kronos-small", "kronos-base"].includes($("model_type").value) ? "kronos" : "recurrent";
    if (oldProfile !== nextProfile) {
      trainingProfiles[oldProfile] = Object.fromEntries(["epochs", "batch_size", "learning_rate"].map((id) => [id, $(id).value]));
      const values = trainingProfiles[nextProfile] || { epochs: 5, batch_size: 16, learning_rate: 0.00001 };
      for (const [id, value] of Object.entries(values)) $(id).value = value;
    }
    previousModel = $("model_type").value;
    renderArchitecture();
    saveFormSettings();
  }
  function renderKronosArchitecture(chart, spec) {
    const frame = $("kronos_timeframe").value, bars = Number($("lookback").value), future = Number($("kronos_prediction_length").value);
    const validShape = Number.isInteger(bars) && bars > 0 && Number.isInteger(future) && future > 0 && bars + future - 1 <= 512;
    const train = Number($("train_fraction").value), validation = Number($("validation_fraction").value);
    $("model-preview-title").textContent = spec.name;
    $("model-parameters").textContent = `≈${number(spec.parameters / 1000000, 1)} million`;
    $("model-input-window").textContent = validShape ? `${count(bars)} ${frame} bars · ${durationLabel(bars * timeframeHours(frame) / 24)}` : "Input + forecast − 1 must fit 512 tokens";
    const stage = $("kronos_training_stage").value, trainTokenizer = stage !== "predictor", tokenizerOnly = stage === "tokenizer";
    const savedTokenizer = state.tokenizers.find((item) => item.id === $("kronos_tokenizer_run_id").value);
    $("architecture-caption").textContent = tokenizerOnly ? `One ${frame} OHLCV stream per coin → fine-tune tokenizer with reconstruction + BSQ → select by full reconstruction validation MSE → save reusable tokenizer. Reserve the final test partition. No predictor training or forecast evaluation.` : `One ${frame} OHLCV stream per coin → ${trainTokenizer ? "fine-tune tokenizer, then freeze its best checkpoint" : savedTokenizer ? "freeze saved tokenizer: " + savedTokenizer.name : "freeze original pretrained tokenizer"} → fine-tune Transformer with autoregressive token cross-entropy. Learn ${future} future candles and evaluate their prices and direction.`;
    $("architecture-caption").textContent += " The tokenizer's extra turnover channel is set to zero because the CSV has no quote turnover. No learned coin embedding.";
    chart.setAttribute("aria-label", $("architecture-caption").textContent);
    chart.setAttribute("viewBox", "0 0 460 410");
    const box = (x, y, w, height, kind, title, detail) => {
      chart.append(svgElement("rect", { x, y, width: w, height, rx: 7, class: `architecture-${kind}` }), svgElement("text", { x: x + w / 2, y: y + 22, "text-anchor": "middle", class: "architecture-label" }, title), svgElement("text", { x: x + w / 2, y: y + 41, "text-anchor": "middle", class: "architecture-detail" }, detail));
    };
    [80, 166, 252].forEach((y) => chart.append(svgElement("path", { d: `M 230 ${y} V ${y + 26}`, class: "architecture-link" })));
    box(70, 24, 320, 56, "input", `${frame} completed candles`, `${validShape ? bars : "—"} bars · OHLCV + calendar time`);
    box(70, 106, 320, 60, "encoder", trainTokenizer ? "Fine-tune tokenizer" : savedTokenizer ? "Saved tokenizer · frozen" : "Pretrained tokenizer · frozen", tokenizerOnly ? "Reconstruction + BSQ training objective" : "Per-window normalization → discrete tokens");
    box(70, 192, 320, 60, "fusion", tokenizerOnly ? "Reconstruction validation" : spec.name.split(" (")[0] + " Transformer", tokenizerOnly ? "Select best full reconstruction MSE" : "Autoregressive token cross-entropy");
    box(70, 278, 320, 56, "output", tokenizerOnly ? "Save best tokenizer checkpoint" : `${future} generated ${frame} candles`, tokenizerOnly ? "Weights + partition settings + learning curves" : `${$("kronos_sample_count").value || "—"} forecast path(s) · average predicted closes`);
    chart.append(svgElement("path", { d: "M 230 334 V 355", class: "architecture-link" }));
    box(70, 355, 320, 48, "output", tokenizerOnly ? "Reuse for predictor tuning" : "Price and market direction", tokenizerOnly ? "Frozen checkpoint · independent predictor controls" : `${future} candles · ${durationLabel(future * timeframeHours(frame) / 24)} ahead`);
    const tokenizerNote = `Tokenizer: up to ${$("kronos_tokenizer_epochs").value} epochs · batch ${$("kronos_tokenizer_batch_size").value} · peak rate ${$("kronos_tokenizer_learning_rate").value} · ${$("kronos_tokenizer_lr_scheduler").selectedOptions[0]?.text}.`;
    const predictorNote = `Predictor: up to ${$("epochs").value} epochs · batch ${$("batch_size").value} · peak rate ${$("learning_rate").value} · ${$("lr_scheduler").selectedOptions[0]?.text}.`;
    $("model-training-note").textContent = !validShape ? "Input candles plus forecast candles minus one must fit the pretrained 512-token training context." : !(train > 0 && validation > 0 && train + validation < 1) ? "Training and validation shares must add to less than 1, leaving a final test." : `${tokenizerOnly ? tokenizerNote : stage === "sequential" ? tokenizerNote + " Then " + predictorNote : predictorNote} Use every ${ordinal($("kronos_train_stride").value)} training origin and every ${ordinal($("kronos_eval_stride").value)} held-out origin per coin. Chronological shares: ${percent(train)} train / ${percent(validation)} validation / ${percent(1 - train - validation)} final test, with future labels purged at boundaries.`;
  }
  function renderArchitecture() {
    syncModelControls();
    const chart = $("architecture-chart"), spec = state.models.find((model) => model.id === $("model_type").value);
    chart.replaceChildren();
    if (!spec) { $("model-parameters").textContent = "—"; return; }
    if (["kronos-small", "kronos-base"].includes(spec.id)) { renderKronosArchitecture(chart, spec); return; }
    const cell = spec.id.toUpperCase(), features = spec.n_features, embedding = spec.embedding_size;
    const hidden = Number($("hidden_size").value), bars = Number($("lookback").value);
    const validShape = Number.isInteger(hidden) && hidden >= 1 && hidden <= 1024 && Number.isInteger(bars) && bars >= 1 && bars <= 1000;
    const h = validShape ? hidden : null, window = validShape ? bars : null;
    const chosen = Array.from($("coin-picks").querySelectorAll("input:checked"), (input) => input.value);
    const pending = $("extra-coins").value.toUpperCase().split(/[\s,]+/).filter(validCoin);
    const coins = new Set([...chosen, ...pending]).size;
    const width = h == null ? "—" : count(4 * h + embedding), dense = h == null ? "—" : count(2 * h);
    const parameters = h == null ? null : 4 * spec.encoder_gates * (features * h + h * h + 2 * h) + embedding * coins + (4 * h + embedding) * 2 * h + 2 * h + 2 * h * 3 + 3;
    const ranges = window == null ? ["—", "—", "—", "—"] : [`${number(window / 4, 2).replace(/\.00$/, "")} hours`, `${count(window)} hours`, `${number(window / 6, 2).replace(/\.00$/, "")} days`, `${count(window)} days`];
    $("model-preview-title").textContent = spec.name;
    $("model-parameters").textContent = count(parameters);
    $("model-input-window").textContent = window == null ? "Enter valid bars and hidden size" : `${count(window)} bars per timeframe`;
    $("architecture-caption").textContent = `${coins} selected ${coins === 1 ? "coin" : "coins"} · ${features} features per candle, including volume. Preview of your next run; saved models keep their settings.`;
    chart.setAttribute("aria-label", `${spec.name}: four streams of ${window ?? "unspecified"} candles with ${features} features including volume, ${h ?? "unspecified"} hidden features per encoder, ${coins} coin embeddings, and 1, 7, and 30 day forecasts.`);
    const link = (path) => chart.append(svgElement("path", { d: path, class: "architecture-link" }));
    const box = (x, y, w, height, kind, title, detail, extra) => {
      const group = svgElement("g");
      group.append(svgElement("rect", { x, y, width: w, height, rx: 7, class: `architecture-${kind}` }));
      group.append(svgElement("text", { x: x + w / 2, y: y + (extra ? 20 : 19), "text-anchor": "middle", class: "architecture-label" }, title));
      group.append(svgElement("text", { x: x + w / 2, y: y + (extra ? 35 : 34), "text-anchor": "middle", class: "architecture-detail" }, detail));
      if (extra) group.append(svgElement("text", { x: x + w / 2, y: y + 50, "text-anchor": "middle", class: "architecture-detail" }, extra));
      chart.append(group);
    };
    const frames = ["15 minutes", "1 hour", "4 hours", "1 day"];
    if (chart.parentElement.clientWidth < 600) {
      chart.setAttribute("viewBox", "0 0 460 380");
      chart.append(svgElement("text", { x: 12, y: 17, class: "architecture-detail" }, "FOUR COMPLETED CANDLE STREAMS"));
      frames.forEach((frame, index) => {
        const x = 12 + index * 112, center = x + 50;
        link(`M ${center} 76 V 95`); link(`M ${center} 142 V 155 H 230 V 174`);
        box(x, 29, 100, 47, "input", frame, `${window ?? "—"} bars`);
        box(x, 95, 100, 47, "encoder", cell, `${h ?? "—"} hidden`);
      });
      link("M 156 206 H 172"); link("M 230 238 V 253"); link("M 230 317 V 331");
      [86, 230, 374].forEach((x) => link(`M 230 331 H ${x} V 340`));
      box(12, 174, 144, 64, "input", "Coin embedding", `${coins} coins × ${embedding} features`, "Learned asset identity");
      box(172, 174, 116, 64, "fusion", "Concatenate", `${width} features`, "4 encoders + coin");
      box(310, 174, 138, 64, "input", "Price + volume", `${features} features / candle`, "Train-only normalization");
      box(165, 253, 130, 64, "fusion", "Dense + ReLU", `${dense} features`, `${count(spec.dropout * 100)}% dropout`);
      ["1 day", "7 days", "30 days"].forEach((label, index) => box(30 + index * 144, 340, 112, 40, "output", label, "Return → USDT"));
    } else {
      chart.setAttribute("viewBox", "0 0 800 390");
    chart.append(svgElement("text", { x: 16, y: 23, class: "architecture-detail" }, "COMPLETED CANDLES"), svgElement("text", { x: 185, y: 23, class: "architecture-detail" }, "TIMEFRAME ENCODERS"), svgElement("text", { x: 673, y: 81, class: "architecture-detail" }, "FORECASTS"));
    frames.forEach((frame, index) => {
      const y = 42 + index * 60, center = y + 23;
      link(`M 161 ${center} H 185`); link(`M 331 ${center} H 347 V 176 H 363`);
      box(16, y, 145, 47, "input", frame, `${window ?? "—"} bars · ${ranges[index]}`);
      box(185, y, 146, 47, "encoder", `${cell} encoder`, `${h ?? "—"} hidden features`);
    });
    link("M 331 329 H 347 V 176 H 363");
    link("M 479 176 H 509"); link("M 634 176 H 651");
    [127, 184, 241].forEach((y) => link(`M 651 176 V ${y} H 671`));
    box(185, 298, 146, 62, "input", "Coin embedding", `${coins} coins × ${embedding} features`, "Learned asset identity");
    box(363, 143, 116, 67, "fusion", "Concatenate", `${width} features`, "4 encoders + coin");
    box(509, 143, 125, 67, "fusion", "Dense + ReLU", `${dense} features`, `${count(spec.dropout * 100)}% dropout`);
    ["1 day", "7 days", "30 days"].forEach((label, index) => box(671, 104 + index * 57, 113, 47, "output", label, "Return → USDT"));
    chart.append(svgElement("text", { x: 16, y: 283, class: "architecture-detail" }, `${features} features per candle · price + volume`), svgElement("text", { x: 16, y: 383, class: "architecture-detail" }, "Forecast origin: every 4 hours · completed candles only"));
    }
    if (window != null) $("architecture-caption").textContent += ` Input spans: 15m ${ranges[0]}, 1h ${ranges[1]}, 4h ${ranges[2]}, 1d ${ranges[3]}.`;
    const train = Number($("train_fraction").value), validation = Number($("validation_fraction").value);
    const splitValid = train > 0 && validation > 0 && train + validation < 1;
    $("model-training-note").textContent = !validShape ? "Enter valid architecture settings to see the parameter count." : !coins ? "Select at least one coin for your next training run." : !splitValid ? "Training and validation shares must add to less than 1, leaving a final test." : `Up to ${$("epochs").value || "—"} epochs · early-stop patience ${$("patience").value || "—"} · batch ${$("batch_size").value || "—"} · learning rate ${$("learning_rate").value || "—"} · ${$("device").selectedOptions[0]?.text || "Automatic"} · seed ${$("seed").value || "—"}. Chronological shares: ${percent(train)} train / ${percent(validation)} validation / ${percent(1 - train - validation)} final test, with future labels purged at boundaries.`;
  }
  let hoveredSetting = null, focusedSetting = null, describedControl = null;
  function hideSettingTooltip() {
    $("setting-tooltip").hidden = true;
    if (describedControl) {
      const ids = (describedControl.getAttribute("aria-describedby") || "").split(/\s+/).filter((id) => id && id !== "setting-tooltip");
      if (ids.length) describedControl.setAttribute("aria-describedby", ids.join(" ")); else describedControl.removeAttribute("aria-describedby");
      describedControl = null;
    }
  }
  function showSettingTooltip() {
    hideSettingTooltip();
    const anchor = hoveredSetting || focusedSetting;
    if (!anchor?.isConnected) return;
    const tooltip = $("setting-tooltip"), bounds = anchor.getBoundingClientRect();
    tooltip.textContent = anchor.dataset.help; tooltip.hidden = false;
    tooltip.style.left = `${Math.max(12, Math.min(bounds.left, window.innerWidth - tooltip.offsetWidth - 12))}px`;
    const top = bounds.bottom + 8;
    tooltip.style.top = `${Math.max(12, top + tooltip.offsetHeight > window.innerHeight - 12 ? bounds.top - tooltip.offsetHeight - 8 : top)}px`;
    describedControl = anchor.matches("input, select") ? anchor : $(anchor.htmlFor);
    if (describedControl) describedControl.setAttribute("aria-describedby", [...new Set((describedControl.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean).concat("setting-tooltip"))].join(" "));
  }
  function renderChart(history = [], rate = false) {
    const chart = $(rate ? "lr-chart" : "loss-chart"); chart.replaceChildren();
    const series = rate ? [["learning_rate", "train-line", "#2361c6"]] : [["train_loss", "train-line", "#2361c6"], ["validation_loss", "validation-line", "#087f87"]];
    const rows = history.filter((row) => !rate || (row.learning_rate != null && Number.isFinite(Number(row.learning_rate)))).filter((row) => Number.isFinite(Number(row.epoch)) && row.train_loss != null && row.validation_loss != null && Number.isFinite(Number(row.train_loss)) && Number.isFinite(Number(row.validation_loss)));
    const left = 48, right = 780, top = 17, bottom = 214;
    const maxY = rows.length ? Math.max(...rows.flatMap((row) => series.map(([key]) => Number(row[key]))), rate ? 1e-12 : 0.001) * 1.12 : 1;
    const minY = rows.length ? Math.min(...rows.flatMap((row) => series.map(([key]) => Number(row[key]))), 0) * 1.12 : 0;
    const minX = rows.length ? Math.min(...rows.map((row) => Number(row.epoch))) : 1;
    const maxX = rows.length ? Math.max(...rows.map((row) => Number(row.epoch)), minX + 1) : 5;
    const x = (epoch) => left + ((epoch - minX) / (maxX - minX)) * (right - left), y = (loss) => bottom - ((loss - minY) / (maxY - minY)) * (bottom - top);
    for (let i = 0; i < 5; i++) {
      const level = minY + ((maxY - minY) * i) / 4, position = y(level);
      chart.append(svgElement("line", { x1: left, x2: right, y1: position, y2: position, class: "grid-line" }));
      chart.append(svgElement("text", { x: left - 10, y: position + 4, "text-anchor": "end" }, rate ? Number(level).toExponential(1) : number(level, maxY < 0.1 ? 3 : 2)));
    }
    const ticks = [...new Set(Array.from({ length: Math.min(5, maxX - minX + 1) }, (_, index) => Math.round(minX + (index / Math.max(1, Math.min(5, maxX - minX + 1) - 1)) * (maxX - minX))))];
    for (const tick of ticks) chart.append(svgElement("text", { x: x(tick), y: bottom + 21, "text-anchor": "middle" }, tick));
    chart.append(svgElement("text", { x: right, y: 252, "text-anchor": "end" }, "Epoch"));
    for (const [key, className, color] of series) {
      if (rows.length) chart.append(svgElement("polyline", { points: rows.map((row) => `${x(Number(row.epoch))},${y(Number(row[key]))}`).join(" "), class: `curve-line ${className}` }));
      for (const row of rows) {
        const circle = svgElement("circle", { cx: x(Number(row.epoch)), cy: y(Number(row[key])), r: rows.length > 80 ? 1.7 : 3, fill: color, stroke: "#fff", "stroke-width": 1 });
        circle.append(svgElement("title", {}, `Epoch ${row.epoch}: ${rate ? "mean learning rate" : key === "train_loss" ? "training loss" : "validation loss"} ${rate ? rateText(row[key]) : number(row[key], 5)}`)); chart.append(circle);
      }
    }
    $(rate ? "lr-empty" : "chart-empty").hidden = rows.length > 0;
  }
  function downloadCoinView(job, coin) {
    const progress = job.progress || {}, coins = job.config?.coins || [];
    const position = coins.indexOf(coin), row = progress.coin_progress?.[coin] || {
      status: position < (progress.completed_coins || 0) ? "ready" : progress.coin === coin ? "running" : "queued",
      phase: progress.coin === coin ? "checking_source" : "queued"
    };
    const phases = {checking_local:"Checking saved candles",checking_source:"Checking available history",monthly_archives:"Monthly archives",daily_archives:"Daily archives",
      history_pages:"History pages",verifying:"Verifying candles",timeframes:"Building candle timeframes",recent_pages:"Latest candle pages",prepared:"Verified",queued:"Waiting"};
    let percent = row.total > 0 && Number.isFinite(row.completed) ? Math.max(0, Math.min(100, 100 * row.completed / row.total)) : null;
    let status = row.status === "queued" ? "Waiting" : row.status === "ready" ? "Waiting to save" : row.status === "failed" ? "Failed" : phases[row.phase] || "Downloading";
    let detail = [row.source, row.detail || phases[row.phase], row.total > 0 && row.completed != null ? `${count(row.completed)} of ${count(row.total)}` : null].filter(Boolean).join(" · ");
    if (row.status === "queued") percent = 0;
    if (row.status === "ready") percent = 100;
    if (row.status === "failed" && percent == null) percent = 0;
    if (row.error) detail = row.error;
    if (row.status === "saved" || job.status === "completed") { status = "Saved"; percent = 100; detail = row.rows ? `${count(row.rows)} native 15m candles` : "Candle history saved"; }
    else if (["stopped", "interrupted", "failed"].includes(job.status) && row.status !== "failed") {
      status = row.status === "queued" ? "Not started" : row.status === "ready" ? "Not saved" : job.status === "failed" ? "Interrupted" : "Stopped";
      if (percent == null) percent = 0;
    }
    return {status, detail, percent};
  }
  let downloadRows = new Map(), downloadSignature = "";
  function renderDownloadCoins(job) {
    const panel = $("coin-download-progress"), container = $("coin-download-list");
    panel.hidden = job?.operation !== "download";
    if (panel.hidden) return;
    const coins = job.config?.coins || [], signature = `${job.id}:${coins.join(",")}`;
    if (signature !== downloadSignature) {
      downloadSignature = signature; downloadRows = new Map(); container.replaceChildren();
      for (const coin of coins) {
        const row = node("div", null, "download-coin-row"), header = node("div", null, "download-coin-header"), status = node("span"), detail = node("p", null, "download-coin-detail");
        row.dataset.coin = coin; header.append(node("strong", coin), status);
        const track = node("div", null, "download-coin-track"), fill = node("span");
        track.setAttribute("role", "progressbar"); track.setAttribute("aria-label", `${coin} download progress`);
        track.setAttribute("aria-valuemin", "0"); track.setAttribute("aria-valuemax", "100"); track.append(fill);
        row.append(header, track, detail); container.append(row); downloadRows.set(coin, {row, status, detail, track, fill});
      }
    }
    for (const coin of coins) {
      const view = downloadCoinView(job, coin), parts = downloadRows.get(coin);
      parts.status.textContent = view.percent == null ? view.status : `${view.status} · ${number(view.percent, 0)}%`;
      parts.detail.textContent = view.detail;
      parts.row.classList.toggle("failed", view.status === "Failed");
      parts.track.classList.toggle("indeterminate", view.percent == null && active(job));
      parts.fill.style.width = view.percent == null ? "28%" : `${view.percent}%`;
      if (view.percent == null) parts.track.removeAttribute("aria-valuenow");
      else parts.track.setAttribute("aria-valuenow", String(Math.round(view.percent)));
      parts.track.setAttribute("aria-valuetext", `${view.status}. ${view.detail}`);
    }
  }
  function renderMonitor() {
    const job = state.jobs.find((item) => item.id === state.selectedJob);
    renderDownloadCoins(job);
    const tokenizerOnly = job?.config?.kronos_training_stage === "tokenizer";
    const tokenizerStage = job?.stage === "training_tokenizer" || tokenizerOnly;
    const tokenizerCurve = $("monitor-component").value === "tokenizer" || ($("monitor-component").value === "auto" && tokenizerStage);
    const monitorHistory = tokenizerStage ? job?.tokenizer_history || [] : job?.history || [];
    const progress = job?.progress || {}, history = tokenizerCurve ? job?.tokenizer_history || [] : job?.history || [], counts = progress.counts || {};
    const status = job?.status || "idle";
    $("status-badge").className = `status-badge ${status}`;
    $("status-badge").textContent = ({ idle: "Ready", queued: "Queued", running: "Running", awaiting_decision: "Waiting for you", completed: "Completed", failed: "Failed", stopped: "Stopped", interrupted: "Interrupted" })[status] || status;
    $("monitor-caption").textContent = job ? `${job.name || (job.operation === "download" ? "Candle download" : "Training run")} · ${(job.config?.coins || []).join(", ")}` : "Your learning curve will appear here.";
    if (job?.artifacts_removed_at) $("monitor-caption").textContent += " · saved files removed; history retained";
    const stages = { starting: "Starting job", loading: "Loading candles", preparing: "Preparing samples", training: "Training model", awaiting_decision: "Waiting for your choice", evaluating: "Evaluating model", saving: "Saving results", downloading: "Downloading candles", publishing: "Saving candles", publishing_coin: "Saving coin candles", completed: "Completed", stopped: "Stopped", interrupted: "Interrupted", failed: "Run failed" };
    stages.training_tokenizer = "Fine-tuning tokenizer";
    stages.loading_model = "Loading pretrained model";
    const preparationLabels = { windows: "Building candle examples", sorting: "Sorting candle examples", splitting: "Splitting training, validation and test data", eligibility: "Checking usable coin history", sampling: "Selecting training and evaluation examples", materializing: `Preparing selected windows · ${progress.preparation_workers || 1} CPU workers`, tokenizing: `Tokenizing ${progress.preparation_partition || "training"} samples` };
    if (job?.stage === "preparing" && preparationLabels[progress.preparation_phase]) stages.preparing = preparationLabels[progress.preparation_phase];
    if (["kronos-small", "kronos-base"].includes(job?.config?.model_type)) stages.training = "Fine-tuning predictor";
    if (tokenizerCurve) $("monitor-caption").textContent += " · tokenizer: reconstruction + BSQ training loss; full reconstruction validation MSE";
    $("stage-value").textContent = stages[job?.stage] || job?.stage || "Waiting for a run";
    const epoch = progress.epoch || monitorHistory.at(-1)?.epoch;
    const maxEpochs = progress.max_epochs || (tokenizerStage ? job?.config?.kronos_tokenizer_epochs : job?.config?.epochs);
    $("epoch-value").textContent = epoch ? `${epoch}${maxEpochs ? ` / ${maxEpochs}` : ""}` : "—";
    const bestEpoch = progress.best_epoch;
    const bestLoss = progress.best_validation_loss ?? monitorHistory.find((row) => row.epoch === bestEpoch)?.validation_loss;
    $("best-value").textContent = bestLoss != null ? number(bestLoss, 4) : "—";
    $("best-value").title = bestEpoch ? `Selected epoch ${bestEpoch}` : "";
    $("elapsed-value").textContent = duration(job?.elapsed_seconds);
    const hasBatches = Boolean(progress.total_batches && (job?.stage === "training" || tokenizerStage || job?.status === "completed")), hasCoins = Boolean(job?.operation === "download" && progress.total_coins);
    const hasPreparation = Boolean(job?.stage === "preparing" && progress.preparation_total > 0 && ["windows", "materializing", "tokenizing"].includes(progress.preparation_phase));
    const ratio = hasPreparation ? Math.min(100, Math.max(0, (progress.preparation_completed || 0) / progress.preparation_total * 100)) : hasBatches ? Math.min(100, Math.max(0, (progress.batch || 0) / progress.total_batches * 100)) : hasCoins ? Math.min(100, (progress.completed_coins || 0) / progress.total_coins * 100) : 0;
    $("progress-fill").style.width = `${ratio}%`;
    $("progress-track").setAttribute("aria-valuenow", Math.round(ratio));
    $("progress-track").setAttribute("aria-label", hasPreparation ? stages.preparing : "Current stage progress");
    $("progress-track").classList.toggle("indeterminate", Boolean(active(job) && !waiting(job) && !hasPreparation && !hasBatches && !hasCoins));
    $("progress-percent").textContent = hasPreparation || hasBatches || hasCoins ? `${Math.round(ratio)}%` : "";
    $("progress-text").textContent = active(job) ? hasBatches ? `Batch ${count(progress.batch || 0)} of ${count(progress.total_batches)}${progress.train_loss != null ? ` · training loss ${number(progress.train_loss, 4)}` : ""}` : hasCoins ? `${count(progress.completed_coins || 0)} of ${count(progress.total_coins)} coins saved${progress.coin ? ` · downloading ${progress.coin}` : ""}` : job.operation === "download" ? "Downloading and verifying historical candles…" : "Preparing candles and training samples…" : job ? job.status === "completed" ? job.operation === "download" ? "Candles saved. You can now train a model." : "Training finished. The best validation epoch was saved." : job.status === "failed" ? "This run ended with an error. See the activity log." : "This job has stopped." : "Choose your coins and start training.";
    if (active(job) && job.stage === "preparing" && preparationLabels[progress.preparation_phase]) {
      $("progress-text").textContent = hasPreparation ? progress.preparation_phase === "windows" ? `${count(progress.preparation_completed || 0)} of ${count(progress.preparation_total)} candle windows checked${progress.coin ? ` · ${progress.coin}` : ""} · ${count(progress.usable_samples || 0)} usable examples` : `${stages.preparing}: ${count(progress.preparation_completed || 0)} of ${count(progress.preparation_total)}` : `${stages.preparing}…`;
    }
    if (active(job) && job.operation === "download" && hasCoins) $("progress-text").textContent = `${count(progress.completed_coins || 0)} of ${count(progress.total_coins)} coins prepared${progress.coin ? ` · ${progress.coin}` : ""}`;
    if (active(job) && job.stage === "loading_model") $("progress-text").textContent = "Loading pretrained Kronos predictor and tokenizer…";
    if (active(job) && job.stage === "evaluating") $("progress-text").textContent = "Evaluating the selected model on held-out validation and test data…";
    if (active(job) && job.stage === "saving") $("progress-text").textContent = "Saving model weights, forecasts, and the evaluation report…";
    if (tokenizerOnly && job.status === "completed") $("progress-text").textContent = "Tokenizer tuning finished. Its best reconstruction checkpoint is available for predictor tuning.";
    if (tokenizerOnly && active(job) && job.stage === "saving") $("progress-text").textContent = "Saving the best tokenizer checkpoint, data settings, and learning curves…";
    if (active(job) && job.stage === "publishing") $("progress-text").textContent = "Saving verified candle data to the local data folder…";
    if (waiting(job)) $("progress-text").textContent = "Training is paused until you choose to continue or finish.";
    $("train-count").textContent = count(counts.train); $("validation-count").textContent = count(counts.validation); $("test-count").textContent = count(counts.test);
    $("device-value").textContent = progress.device || job?.config?.device || "—";
    showError("job-error", job?.error ? typeof job.error === "string" ? job.error : job.error.message || JSON.stringify(job.error) : "");
    $("curve-component-note").textContent = tokenizerCurve ? "Tokenizer: reconstruction + BSQ training loss; full reconstruction validation MSE." : "Model / predictor training and validation losses.";
    const liveRate = tokenizerCurve === tokenizerStage ? progress.learning_rate : null;
    $("learning-rate-value").textContent = rateText(liveRate ?? history.at(-1)?.lr_end ?? history.at(-1)?.learning_rate);
    renderDecision(job); renderChart(history); renderChart(history, true); renderLogs(job?.logs || []); updateActions();
  }
  function schedulePoll(delay) { clearTimeout(state.timer); state.timer = setTimeout(poll, delay); }
  async function poll() {
    try {
      if (!state.online && state.token) {
        const recovered = await api("/api/bootstrap");
        applySession(recovered); applyModelOptions(recovered.models); applyTokenizerBootstrap(recovered); renderCoverage(recovered.data); discoverCoinChoices(recovered); renderArchitecture();
        state.savedRunsSupported = Array.isArray(recovered.saved_runs); renderSavedRuns(recovered.saved_runs || []);
      }
      if (!state.token) {
        const bootstrap = await api("/api/bootstrap");
        applySession(bootstrap); applyModelOptions(bootstrap.models); applyDefaults(bootstrap.defaults); restoreFormSettings(); applyTokenizerBootstrap(bootstrap); restoreCoinChoices(bootstrap); renderCoverage(bootstrap.data); renderArchitecture();
        state.jobs = bootstrap.jobs || [];
        state.savedRunsSupported = Array.isArray(bootstrap.saved_runs); renderSavedRuns(bootstrap.saved_runs || []);
        state.selectedJob = state.jobs.find((job) => job.operation !== "forecast" && active(job))?.id || state.jobs.find((job) => job.operation !== "forecast")?.id || null;
        selectPendingDecision(true);
      }
      const previous = state.jobs, response = await api("/api/jobs");
      state.jobs = response.jobs || [];
      selectPendingDecision(true);
      renderJobs(); renderMonitor(); connection(true); showError("page-error", "");
      const justFinished = state.jobs.find((job) => ["completed", "failed", "stopped", "interrupted"].includes(job.status) && active(previous.find((old) => old.id === job.id)));
      if (justFinished || ++state.pollCount % 20 === 0) {
        if (state.separateStagesSupported) { const response = await api("/api/tokenizers"); renderTokenizers(response.tokenizers || []); }
        if (state.savedRunsSupported) { const saved = await api("/api/saved-runs"); renderSavedRuns(saved.saved_runs || []); }
        if (justFinished?.operation === "download") { const fresh = await api("/api/bootstrap"); renderCoverage(fresh.data); discoverCoinChoices(fresh); renderArchitecture(); }
      }
    } catch (error) { connection(false); showError("page-error", error.message); }
    finally { schedulePoll(state.jobs.some(active) ? 1500 : 3000); }
  }
  async function initialize() {
    organizeCoinChoices(); renderChart(); updateActions();
    try {
      const bootstrap = await api("/api/bootstrap"); applySession(bootstrap); applyModelOptions(bootstrap.models); applyDefaults(bootstrap.defaults); restoreFormSettings(); applyTokenizerBootstrap(bootstrap); restoreCoinChoices(bootstrap); renderCoverage(bootstrap.data); renderArchitecture();
      $("hardware-note").textContent = bootstrap.hardware?.cuda_available === true ? "CUDA GPU available. Automatic will use the GPU." : bootstrap.hardware?.cuda_available === false ? "CPU training available. No CUDA GPU was detected." : "Automatic checks GPU availability when training starts.";
      state.jobs = bootstrap.jobs || [];
      state.savedRunsSupported = Array.isArray(bootstrap.saved_runs); renderSavedRuns(bootstrap.saved_runs || []);
      state.selectedJob = state.jobs.find((job) => job.operation !== "forecast" && active(job))?.id || state.jobs.find((job) => job.operation !== "forecast")?.id || null;
      selectPendingDecision(true);
      connection(true); renderJobs(); renderMonitor();
    } catch (error) { connection(false); showError("page-error", `Could not load the training workspace. ${error.message}`); }
    finally { schedulePoll(1500); }
  }
  $("training-form").addEventListener("submit", (event) => { event.preventDefault(); createJob("train"); });
  $("training-form").addEventListener("input", renderArchitecture);
  $("training-form").addEventListener("change", renderArchitecture);
  $("training-form").addEventListener("input", saveFormSettings);
  $("training-form").addEventListener("change", saveFormSettings);
  $("model_type").addEventListener("change", changeModel);
  $("kronos_tokenizer_run_id").addEventListener("change", useTokenizerSettings);
  $("use-tokenizer-settings").addEventListener("click", useTokenizerSettings);
  $("monitor-component").addEventListener("change", renderMonitor);
  new ResizeObserver(renderArchitecture).observe($("architecture-chart").parentElement);
  $("add-coins").addEventListener("click", addCoins);
  $("extra-coins").addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); addCoins(); } });
  $("coin-picks").addEventListener("change", saveCoinChoices);
  $("select-all-coins").addEventListener("click", () => setCoinSelection(true));
  $("clear-coins").addEventListener("click", () => setCoinSelection(false));
  $("coin-picks").addEventListener("contextmenu", openCoinMenu);
  $("coin-picks").addEventListener("keydown", (event) => { if (event.key === "ContextMenu" || (event.shiftKey && event.key === "F10")) openCoinMenu(event); });
  $("remove-coin").addEventListener("click", removeMenuCoin);
  document.addEventListener("pointerdown", (event) => { if (!$("coin-menu").contains(event.target)) closeCoinMenu(); });
  document.addEventListener("keydown", (event) => { if (!$("coin-menu").hidden && (event.key === "Escape" || event.key === "Tab")) { if (event.key === "Escape") event.preventDefault(); closeCoinMenu(true); } });
  document.addEventListener("pointerover", (event) => { const anchor = event.target.closest("[data-help]"); if (anchor && anchor !== hoveredSetting) { hoveredSetting = anchor; showSettingTooltip(); } });
  document.addEventListener("pointerout", (event) => { if (hoveredSetting && event.target.closest("[data-help]") === hoveredSetting && !hoveredSetting.contains(event.relatedTarget)) { hoveredSetting = null; showSettingTooltip(); } });
  document.addEventListener("focusin", (event) => { focusedSetting = event.target.closest("[data-help]"); showSettingTooltip(); });
  document.addEventListener("focusout", () => { focusedSetting = null; showSettingTooltip(); });
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") { hoveredSetting = null; focusedSetting = null; hideSettingTooltip(); } });
  window.addEventListener("resize", hideSettingTooltip);
  document.addEventListener("scroll", hideSettingTooltip, true);
  window.addEventListener("resize", () => closeCoinMenu());
  document.addEventListener("scroll", () => closeCoinMenu(), true);
  $("download-button").addEventListener("click", () => createJob("download"));
  $("stop-button").addEventListener("click", stopJob);
  $("continue-training").addEventListener("click", () => decideTraining("continue"));
  $("finish-training").addEventListener("click", () => decideTraining("finish"));
  $("job-select").addEventListener("change", (event) => { state.selectedJob = event.target.value || null; state.logSignature = ""; renderMonitor(); });
  initialize();
})();
