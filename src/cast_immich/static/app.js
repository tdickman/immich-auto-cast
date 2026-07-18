"use strict";

const state = {
  csrf: "", revision: 0, config: null, dirty: false, timer: null, countdownTimer: null,
  mode: "setup", error: "", outputs: new Map(), histories: new Map(), historySignatures: new Map(),
  selectedOutputId: localStorage.getItem("cast-immich-output"), pendingCommands: new Map(), commandInFlight: new Set(),
  sourceDrafts: new Map(), autocastDeadlines: new Map(), thumbnailCache: new Map(),
  thumbnailKeys: new Map(), devices: [],
};
const form = document.querySelector("#settings-form");
const toast = document.querySelector("#toast");
const outputList = document.querySelector("#output-list");
const outputSettingsList = document.querySelector("#output-settings-list");

const mutationHeaders = () => ({
  "Content-Type": "application/json",
  "X-Cast-Immich-Request": "1",
  "X-CSRF-Token": state.csrf,
});

async function request(path, options = {}) {
  const response = await fetch(path, { cache: "no-store", ...options });
  const type = response.headers.get("content-type") || "";
  const body = type.includes("application/json") ? await response.json() : null;
  if (!response.ok) {
    const error = new Error(body?.error || body?.outcome || `Request failed (${response.status})`);
    error.status = response.status;
    error.payload = body;
    throw error;
  }
  return body;
}

function mutate(path, body) {
  return request(path, {
    method: path === "/api/config" ? "PUT" : "POST",
    headers: mutationHeaders(),
    body: JSON.stringify(body),
  });
}

function outputPath(outputId, suffix) {
  return `/api/outputs/${encodeURIComponent(outputId)}${suffix}`;
}

function notify(message, error = false) {
  toast.textContent = message;
  toast.className = `toast visible${error ? " error" : ""}`;
  window.clearTimeout(toast.timeout);
  toast.timeout = window.setTimeout(() => { toast.className = "toast"; }, 3600);
}

function titleCase(value) {
  return String(value || "unknown").replaceAll("_", " ").replace(/\b\w/g, character => character.toUpperCase());
}

function sourceSummary(source) {
  if (!source) return "Timeline";
  if (source.kind === "search") return source.query ? `Search: ${source.query}` : "AI search";
  if (source.kind === "album" || source.kind === "person") return source.name || source.label || titleCase(source.kind);
  return titleCase(source.kind || "timeline");
}

function actionAvailable(output, command) {
  return output?.available_actions?.[command] === true;
}

function rotationEnabled(output) {
  return state.histories.get(output.id)?.rotation_enabled ?? output.coordinator?.rotation_enabled ?? true;
}

function sourceDraft(outputId) {
  if (!state.sourceDrafts.has(outputId)) {
    state.sourceDrafts.set(outputId, { pendingKind: null, searchQuery: "", searchDirty: false, changing: false });
  }
  return state.sourceDrafts.get(outputId);
}

function setSelectedOutput(outputId) {
  if (!state.outputs.has(outputId)) return;
  document.activeElement?.blur();
  state.selectedOutputId = outputId;
  state.historySignatures.delete(outputId);
  localStorage.setItem("cast-immich-output", outputId);
  renderOverview();
  renderSelectedWorkspace();
  renderHistory(outputId);
  loadHistory(outputId).catch(error => notify(error.message, true));
}

function outputStateLabel(output) {
  return titleCase(output.coordinator?.state || state.mode);
}

function makeQuickButton(output, command, label, mark) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "output-quick";
  button.dataset.outputAction = "control";
  button.dataset.outputId = output.id;
  button.dataset.command = command;
  button.disabled = state.commandInFlight.has(`${output.id}:${command}`) || !actionAvailable(output, command);
  const symbol = document.createElement("span");
  symbol.setAttribute("aria-hidden", "true");
  symbol.textContent = mark;
  button.append(symbol, document.createTextNode(label));
  return button;
}

function renderOverview() {
  const outputs = [...state.outputs.values()];
  document.querySelector("#output-total").textContent = `${outputs.length} output${outputs.length === 1 ? "" : "s"}`;
  outputList.replaceChildren(...outputs.map(output => {
    const card = document.createElement("article");
    card.className = `output-card${output.id === state.selectedOutputId ? " selected" : ""}`;
    const select = document.createElement("button");
    select.type = "button";
    select.className = "output-select";
    select.dataset.outputAction = "select";
    select.dataset.outputId = output.id;
    if (output.id === state.selectedOutputId) select.setAttribute("aria-current", "true");
    const heading = document.createElement("strong");
    heading.textContent = output.name || output.id;
    const receiver = document.createElement("span");
    receiver.className = "output-receiver";
    receiver.textContent = `${output.receiver?.friendly_name || output.receiver?.name || output.receiver?.uuid || "No receiver"} · ${outputStateLabel(output)}`;
    const summary = document.createElement("span");
    summary.className = "output-summary";
    summary.textContent = `${sourceSummary(output.source)} · ${output.autocast_enabled ? "Autocast on" : "Autocast off"}`;
    select.append(heading, receiver, summary);
    const actions = document.createElement("div");
    actions.className = "output-actions";
    const autocastCommand = output.autocast_enabled ? "autocast_disable" : "autocast_enable";
    actions.append(
      makeQuickButton(output, autocastCommand, output.autocast_enabled ? "Autocast off" : "Autocast on", "A"),
      makeQuickButton(output, "next", "Next", "→"),
    );
    card.append(select, actions);
    return card;
  }));

  const empty = outputs.length === 0;
  document.querySelector("#no-output-state").hidden = !empty;
  document.querySelector("#selected-workspace").hidden = empty;
}

function renderCountdowns() {
  const outputId = state.selectedOutputId;
  const output = state.outputs.get(outputId);
  if (!output) return;
  const button = document.querySelector("#autocast-toggle");
  const status = document.querySelector("#autocast-status");
  button.classList.toggle("autocast-off", !output.autocast_enabled);
  button.querySelector("span:last-child").textContent = output.autocast_enabled ? "Autocast on" : "Autocast off";
  if (!output.autocast_enabled) status.textContent = "Autocast is off";
  else if (state.autocastDeadlines.has(outputId)) {
    const remaining = Math.max(0, Math.ceil((state.autocastDeadlines.get(outputId) - Date.now()) / 1000));
    status.textContent = `Casting in ${remaining}s`;
  } else status.textContent = "Autocast is on";
}

function renderSelectedWorkspace() {
  const outputId = state.selectedOutputId;
  const output = state.outputs.get(outputId);
  if (!output) return;
  const coordinator = output.coordinator || {};
  const active = state.mode === "active";
  const owned = coordinator.state === "owned";
  const rotationIsEnabled = rotationEnabled(output);
  const draft = sourceDraft(outputId);
  document.querySelector("#workspace-output-name").textContent = output.name || output.id;
  document.querySelector("#current-title").textContent = `Now showing / ${output.name || output.id}`;
  document.querySelector("#history-title").textContent = `Previous / ${output.name || output.id}`;
  document.querySelector("#upcoming-title").textContent = `Up next / ${output.name || output.id}`;
  document.querySelector("#cast-state").textContent = outputStateLabel(output);
  document.querySelector("#cast-detail").textContent = active
    ? rotationIsEnabled ? "Automatic rotation is on" : "Automatic rotation is paused"
    : "Open settings to finish setup";
  document.querySelector("#ownership-note").textContent = state.error || coordinator.error || (
    owned ? `Ready to control ${output.name || output.id}.` : "Waiting until this receiver is available."
  );
  const rotation = document.querySelector("#rotation-toggle");
  rotation.dataset.command = rotationIsEnabled ? "pause" : "enable";
  rotation.querySelector(".button-mark").textContent = rotationIsEnabled ? "Ⅱ" : "▶";
  rotation.querySelector("span:last-child").textContent = rotationIsEnabled ? "Pause rotation" : "Enable rotation";
  rotation.disabled = state.commandInFlight.has(`${outputId}:${rotation.dataset.command}`) || !actionAvailable(output, rotation.dataset.command);
  document.querySelector("#next-button").disabled = state.commandInFlight.has(`${outputId}:next`) || !actionAvailable(output, "next");
  const autocastCommand = output.autocast_enabled ? "autocast_disable" : "autocast_enable";
  const autocast = document.querySelector("#autocast-toggle");
  autocast.dataset.command = autocastCommand;
  autocast.disabled = state.commandInFlight.has(`${outputId}:${autocastCommand}`) || !actionAvailable(output, autocastCommand);
  document.querySelector("#reconnect-button").disabled = state.commandInFlight.has(`${outputId}:reconnect`) || !actionAvailable(output, "reconnect");
  document.querySelector(".source-apply").disabled = !active || draft.changing;
  ["#source-kind", "#album-select", "#person-select", "#search-query"].forEach(selector => {
    document.querySelector(selector).disabled = !active || draft.changing;
  });
  const source = output.source || { kind: "timeline" };
  const kind = draft.pendingKind || source.kind || "timeline";
  document.querySelector("#source-kind").value = kind;
  document.querySelector("#album-select").value = kind === "album" ? source.id || "" : "";
  document.querySelector("#person-select").value = kind === "person" ? source.id || "" : "";
  const search = document.querySelector("#search-query");
  if (!draft.searchDirty && document.activeElement !== search) search.value = source.kind === "search" ? source.query || "" : draft.searchQuery;
  showSourceControl(kind);
  renderCountdowns();
}

function renderStatus(payload) {
  state.csrf = payload.csrf_token || state.csrf;
  state.mode = payload.mode || "setup";
  state.error = payload.error || "";
  state.outputs = new Map((payload.outputs || []).map(output => [output.id, output]));
  for (const output of state.outputs.values()) {
    if (typeof output.autocast_remaining_seconds === "number") {
      state.autocastDeadlines.set(output.id, Date.now() + output.autocast_remaining_seconds * 1000);
    } else state.autocastDeadlines.delete(output.id);
  }
  for (const outputId of [...state.autocastDeadlines.keys()]) {
    if (!state.outputs.has(outputId)) state.autocastDeadlines.delete(outputId);
  }
  if (!state.outputs.has(state.selectedOutputId)) state.selectedOutputId = state.outputs.keys().next().value || null;
  if (state.selectedOutputId) localStorage.setItem("cast-immich-output", state.selectedOutputId);
  document.querySelector("#service-mode").textContent = titleCase(state.mode);
  document.querySelector("#signal-dot").className = `signal-dot ${state.mode === "active" ? "active" : state.error ? "error" : ""}`;
  renderOverview();
  renderSelectedWorkspace();
}

function thumbnailKey(outputId, record) {
  return `${outputId}\u0000${record.thumbnail_url}`;
}

function preloadThumbnail(outputId, record) {
  const key = thumbnailKey(outputId, record);
  const existing = state.thumbnailCache.get(key);
  if (existing) return existing.promise;
  const entry = { url: null, promise: null };
  entry.promise = (async () => {
    const response = await fetch(record.thumbnail_url, { cache: "no-store" });
    if (!response.ok) throw new Error("Thumbnail unavailable");
    const url = URL.createObjectURL(await response.blob());
    if (!state.thumbnailKeys.get(outputId)?.has(key)) {
      URL.revokeObjectURL(url);
      throw new Error("Thumbnail no longer needed");
    }
    entry.url = url;
    const image = new Image();
    image.src = url;
    await image.decode();
    if (!state.thumbnailKeys.get(outputId)?.has(key) || state.thumbnailCache.get(key) !== entry) throw new Error("Thumbnail no longer needed");
    return url;
  })().catch(error => {
    if (entry.url) URL.revokeObjectURL(entry.url);
    if (state.thumbnailCache.get(key) === entry) state.thumbnailCache.delete(key);
    throw error;
  });
  state.thumbnailCache.set(key, entry);
  return entry.promise;
}

function usePreloadedThumbnail(image, outputId, record) {
  const key = thumbnailKey(outputId, record);
  image.dataset.thumbnailKey = key;
  const entry = state.thumbnailCache.get(key);
  if (entry?.url) image.src = entry.url;
  else preloadThumbnail(outputId, record).then(url => {
    if (image.dataset.thumbnailKey === key) image.src = url;
  }).catch(() => {
    if (image.dataset.thumbnailKey === key) image.src = record.thumbnail_url;
  });
}

function updateThumbnailCache(outputId, records) {
  const current = new Set(records.map(record => thumbnailKey(outputId, record)));
  state.thumbnailKeys.set(outputId, current);
  for (const [key, entry] of state.thumbnailCache) {
    if (!key.startsWith(`${outputId}\u0000`) || current.has(key)) continue;
    if (entry.url) URL.revokeObjectURL(entry.url);
    state.thumbnailCache.delete(key);
  }
  records.forEach(record => { preloadThumbnail(outputId, record).catch(() => {}); });
}

async function loadHistory(outputId) {
  if (!outputId) return;
  const payload = await request(outputPath(outputId, "/history"));
  if (!state.outputs.has(outputId)) return;
  state.histories.set(outputId, payload);
  const records = [...(payload.current ? [payload.current] : []), ...(payload.records || []), ...(payload.upcoming || [])];
  updateThumbnailCache(outputId, records);
  renderOverview();
  if (state.selectedOutputId === outputId) {
    renderSelectedWorkspace();
    renderHistory(outputId);
  }
}

function renderGallery(selector, records, emptyMessage, renderRecord) {
  const list = document.querySelector(selector);
  if (!records.length) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = emptyMessage;
    list.replaceChildren(empty);
  } else list.replaceChildren(...records.map(renderRecord));
}

function photoButton(outputId, kind, id, index, extraClass = "") {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `photo-record photo-jump ${extraClass}`.trim();
  button.style.animationDelay = `${index * 45}ms`;
  button.setAttribute("aria-label", `Show this ${kind} photo now`);
  button.addEventListener("click", () => performSeek(outputId, kind, id));
  return button;
}

function renderHistory(outputId) {
  if (state.selectedOutputId !== outputId) return;
  const payload = state.histories.get(outputId);
  if (!payload) {
    document.querySelector("#current-count").textContent = "0";
    document.querySelector("#history-count").textContent = "0";
    document.querySelector("#upcoming-count").textContent = "0";
    renderGallery("#current-list", [], "Loading this output…", () => {});
    renderGallery("#history-list", [], "Loading this output…", () => {});
    renderGallery("#upcoming-list", [], "Loading this output…", () => {});
    return;
  }
  const records = payload.records || [];
  const upcoming = payload.upcoming || [];
  document.querySelector("#current-count").textContent = payload.current ? "1" : "0";
  document.querySelector("#history-count").textContent = String(records.length);
  document.querySelector("#upcoming-count").textContent = String(upcoming.length);
  const signature = JSON.stringify([
    [payload.current?.asset_id, payload.current?.thumbnail_url],
    records.map(record => [record.event_id, record.confirmed_at, record.thumbnail_url]),
    upcoming.map(record => [record.asset_id, record.thumbnail_url]),
  ]);
  if (state.historySignatures.get(outputId) === signature) return;
  state.historySignatures.set(outputId, signature);
  renderGallery("#current-list", payload.current ? [payload.current] : [], "No photo is currently casting.", record => {
    const figure = document.createElement("figure");
    figure.className = "photo-record current-record";
    const image = document.createElement("img");
    usePreloadedThumbnail(image, outputId, record);
    image.alt = "Currently displayed Immich photo";
    const caption = document.createElement("div");
    caption.className = "photo-caption";
    caption.textContent = record.confirmed_at ? new Date(record.confirmed_at).toLocaleString() : "NOW CASTING";
    figure.append(image, caption);
    return figure;
  });
  renderGallery("#history-list", records, "No confirmed photos yet.", (record, index) => {
    const figure = photoButton(outputId, "history", record.event_id, index);
    const image = document.createElement("img");
    usePreloadedThumbnail(image, outputId, record);
    image.alt = "Recently displayed Immich photo";
    const caption = document.createElement("div");
    caption.className = "photo-caption";
    const time = document.createElement("time");
    time.dateTime = record.confirmed_at;
    time.textContent = new Date(record.confirmed_at).toLocaleString();
    caption.append(time);
    figure.append(image, caption);
    return figure;
  });
  renderGallery("#upcoming-list", upcoming, "The next photos will appear when rotation begins.", (record, index) => {
    const figure = photoButton(outputId, "upcoming", record.asset_id, index, "upcoming-record");
    const image = document.createElement("img");
    usePreloadedThumbnail(image, outputId, record);
    image.alt = `Upcoming Immich photo ${index + 1}`;
    const caption = document.createElement("div");
    caption.className = "photo-caption";
    const position = document.createElement("strong");
    position.textContent = `NEXT ${String(index + 1).padStart(2, "0")}`;
    caption.append(position);
    figure.append(image, caption);
    return figure;
  });
}

function showSourceControl(kind) {
  document.querySelector("#album-source").style.display = kind === "album" ? "grid" : "none";
  document.querySelector("#person-source").style.display = kind === "person" ? "grid" : "none";
  document.querySelector("#search-source").style.display = kind === "search" ? "block" : "none";
}

async function applySource(outputId, source, message) {
  const draft = sourceDraft(outputId);
  draft.changing = true;
  if (state.selectedOutputId === outputId) renderSelectedWorkspace();
  try {
    await mutate(outputPath(outputId, "/source"), source);
    if (source.kind === "search") draft.searchDirty = false;
    draft.pendingKind = null;
    state.historySignatures.delete(outputId);
    notify(message);
    await refreshOutput(outputId);
  } catch (error) {
    draft.pendingKind = null;
    notify(error.message, true);
  } finally {
    draft.changing = false;
    if (state.selectedOutputId === outputId) renderSelectedWorkspace();
  }
}

async function performControl(outputId, command) {
  const key = `${outputId}:${command}`;
  if (state.commandInFlight.has(key)) return;
  const requestId = state.pendingCommands.get(key) || crypto.randomUUID();
  state.pendingCommands.set(key, requestId);
  state.commandInFlight.add(key);
  renderOverview();
  renderSelectedWorkspace();
  try {
    const suffix = command === "reconnect" ? "/reconnect" : `/controls/${command}`;
    const body = command === "reconnect" ? {} : { request_id: requestId };
    const result = await mutate(outputPath(outputId, suffix), body);
    state.pendingCommands.delete(key);
    state.commandInFlight.delete(key);
    notify(titleCase(result.outcome));
    await refreshOutput(outputId);
  } catch (error) {
    if (error.status) state.pendingCommands.delete(key);
    state.commandInFlight.delete(key);
    notify(titleCase(error.message), true);
    await refreshOutput(outputId);
  }
}

async function performSeek(outputId, targetKind, targetId) {
  document.querySelectorAll(".photo-jump").forEach(button => { button.disabled = true; });
  try {
    const result = await mutate(outputPath(outputId, "/seek"), { request_id: crypto.randomUUID(), target_kind: targetKind, target_id: targetId });
    notify(titleCase(result.outcome));
    await refreshOutput(outputId);
  } catch (error) {
    notify(titleCase(error.message), true);
    await refreshOutput(outputId);
  }
}

async function refreshOutput(outputId) {
  const status = await request("/api/status");
  renderStatus(status);
  if (state.outputs.has(outputId)) await loadHistory(outputId);
}

async function refresh() {
  try {
    const status = await request("/api/status");
    renderStatus(status);
    const outputId = state.selectedOutputId;
    if (outputId) await loadHistory(outputId);
  } catch (error) {
    if (error.status === 409) {
      await loadConfig();
      notify("Settings changed elsewhere. Your draft is preserved; review and apply again.", true);
    } else notify(error.message, true);
  }
}

function setReceiverOptions(select, selectedUuid) {
  select.replaceChildren(new Option("Choose a receiver", ""));
  state.devices.forEach(device => select.add(new Option(`${device.friendly_name} · ${device.uuid}`, device.uuid)));
  if (selectedUuid && ![...select.options].some(option => option.value === selectedUuid)) {
    select.add(new Option(`Configured · ${selectedUuid}`, selectedUuid));
  }
  select.value = selectedUuid || "";
}

const outputNumberFields = new Set([
  "discovery_timeout", "load_timeout", "interval", "idle_debounce", "cooldown",
  "recent_history", "candidate_batch", "autocast_delay",
]);

function outputField(labelText, field, value, options = {}) {
  const label = document.createElement("label");
  label.textContent = labelText;
  let control;
  if (field === "uuid") {
    control = document.createElement("select");
    setReceiverOptions(control, value);
  } else {
    control = document.createElement("input");
    if (outputNumberFields.has(field)) {
      control.type = "number";
      control.min = options.min || "0.1";
      control.step = options.step || "0.1";
    }
    control.value = value ?? "";
  }
  control.required = true;
  control.dataset.outputField = field;
  label.append(control);
  return label;
}

function makeOutputSettingsRow(output) {
  const row = document.createElement("article");
  row.className = "output-settings-row";
  const heading = document.createElement("div");
  heading.className = "output-row-heading";
  const title = document.createElement("strong");
  title.textContent = output.name || output.id || "New output";
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "text-button remove-output";
  remove.dataset.settingsAction = "remove";
  remove.textContent = "Remove";
  heading.append(title, remove);
  const basics = document.createElement("div");
  basics.className = "output-fields";
  basics.append(
    outputField("Stable ID", "id", output.id), outputField("Display name", "name", output.name),
    outputField("Receiver", "uuid", output.uuid), outputField("Seconds per photo", "interval", output.interval),
  );
  const advanced = document.createElement("details");
  advanced.className = "output-row-advanced";
  const summary = document.createElement("summary");
  summary.textContent = "Output advanced settings";
  const fields = document.createElement("div");
  fields.className = "output-fields advanced-output-fields";
  fields.append(
    outputField("Discovery timeout", "discovery_timeout", output.discovery_timeout),
    outputField("Load timeout", "load_timeout", output.load_timeout),
    outputField("Idle status debounce", "idle_debounce", output.idle_debounce),
    outputField("Failure cooldown", "cooldown", output.cooldown),
    outputField("Repeat exclusion", "recent_history", output.recent_history, { min: "1", step: "1" }),
    outputField("Candidate batch", "candidate_batch", output.candidate_batch, { min: "1", step: "1" }),
    outputField("Autocast idle delay", "autocast_delay", output.autocast_delay),
  );
  advanced.append(summary, fields);
  row.append(heading, basics, advanced);
  return row;
}

function updateRemoveButtons() {
  const buttons = outputSettingsList.querySelectorAll('[data-settings-action="remove"]');
  buttons.forEach(button => { button.disabled = buttons.length === 1; });
}

function renderOutputSettings(outputs) {
  outputSettingsList.replaceChildren(...outputs.map(makeOutputSettingsRow));
  updateRemoveButtons();
}

function nextOutputId(name, existingIds) {
  const base = name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "output";
  let candidate = base;
  let suffix = 2;
  while (existingIds.has(candidate)) candidate = `${base}-${suffix++}`;
  existingIds.add(candidate);
  return candidate;
}

function outputFromTemplate(template, { id, name, uuid }) {
  return {
    id, name, uuid,
    discovery_timeout: template.discovery_timeout ?? 10,
    load_timeout: template.load_timeout ?? 15,
    interval: template.interval ?? 60,
    idle_debounce: template.idle_debounce ?? 3,
    cooldown: template.cooldown ?? 15,
    recent_history: template.recent_history ?? 25,
    candidate_batch: template.candidate_batch ?? 50,
    autocast_delay: template.autocast_delay ?? 30,
  };
}

function addDiscoveredOutputs(devices) {
  const blankRows = [...outputSettingsList.querySelectorAll(".output-settings-row")]
    .filter(row => !row.querySelector('[data-output-field="uuid"]').value);
  const configuredUuids = new Set(
    [...outputSettingsList.querySelectorAll('[data-output-field="uuid"]')]
      .map(select => select.value)
      .filter(Boolean),
  );
  const existingIds = new Set(
    [...outputSettingsList.querySelectorAll('[data-output-field="id"]')].map(input => input.value),
  );
  const template = collectConfig().outputs[0] || {};
  let added = 0;
  for (const device of devices) {
    if (!device.uuid || configuredUuids.has(device.uuid)) continue;
    const name = device.friendly_name || `Output ${outputSettingsList.children.length + 1}`;
    const blankRow = blankRows.shift();
    if (blankRow) {
      blankRow.querySelector('[data-output-field="uuid"]').value = device.uuid;
      const nameInput = blankRow.querySelector('[data-output-field="name"]');
      if (!nameInput.value || nameInput.value === "Chromecast" || /^Output \d+$/.test(nameInput.value)) {
        nameInput.value = name;
        blankRow.querySelector(".output-row-heading strong").textContent = name;
      }
      configuredUuids.add(device.uuid);
      added += 1;
      continue;
    }
    const id = nextOutputId(name, existingIds);
    outputSettingsList.append(makeOutputSettingsRow(outputFromTemplate(template, { id, name, uuid: device.uuid })));
    configuredUuids.add(device.uuid);
    added += 1;
  }
  if (added) {
    state.dirty = true;
    updateRemoveButtons();
  }
  return added;
}

function setFormValues(values) {
  state.config = structuredClone(values);
  if (state.dirty) return;
  for (const section of ["immich", "relay", "service"]) {
    for (const [key, value] of Object.entries(values[section] || {})) {
      const control = form.elements.namedItem(`${section}.${key}`);
      if (control) control.value = value ?? "";
    }
  }
  renderOutputSettings(values.outputs || []);
}

async function loadConfig() {
  const payload = await request("/api/config");
  state.csrf = payload.csrf_token || state.csrf;
  state.revision = payload.revision;
  setFormValues(payload.values);
  const keyInput = form.elements.namedItem("immich.api_key");
  const managed = payload.api_key_source === "environment";
  keyInput.disabled = managed;
  document.querySelector("#key-status").textContent = managed
    ? "Managed by CAST_IMMICH_API_KEY; browser replacement is disabled."
    : payload.api_key_configured ? "A key is configured. Leave blank to preserve it." : "No key configured.";
}

function collectConfig() {
  const result = structuredClone(state.config || {});
  for (const section of ["immich", "relay", "service"]) {
    result[section] ||= {};
    form.querySelectorAll(`[name^="${section}."]`).forEach(control => {
      if (!control.disabled) result[section][control.name.slice(section.length + 1)] = control.type === "number" ? Number(control.value) : control.value;
    });
  }
  result.outputs = [...outputSettingsList.querySelectorAll(".output-settings-row")].map(row => {
    const output = {};
    row.querySelectorAll("[data-output-field]").forEach(control => {
      output[control.dataset.outputField] = control.type === "number" ? Number(control.value) : control.value;
    });
    return output;
  });
  delete result.chromecast;
  delete result.rotation;
  return result;
}

async function loadAlbums() {
  const albums = await request("/api/albums");
  const select = document.querySelector("#album-select");
  select.replaceChildren(new Option("Choose an album", ""));
  albums.forEach(album => select.add(new Option(`${album.name} (${album.asset_count})`, album.id)));
}

async function loadPeople() {
  const people = await request("/api/people");
  const select = document.querySelector("#person-select");
  select.replaceChildren(new Option("Choose a person", ""));
  people.forEach(person => select.add(new Option(person.name, person.id)));
}

outputList.addEventListener("click", event => {
  const action = event.target.closest("[data-output-action]");
  if (!action) return;
  if (action.dataset.outputAction === "select") setSelectedOutput(action.dataset.outputId);
  else performControl(action.dataset.outputId, action.dataset.command);
});

outputSettingsList.addEventListener("click", event => {
  const action = event.target.closest("[data-settings-action]");
  if (action?.dataset.settingsAction !== "remove") return;
  if (outputSettingsList.children.length <= 1) return;
  action.closest(".output-settings-row").remove();
  state.dirty = true;
  updateRemoveButtons();
});
outputSettingsList.addEventListener("input", event => {
  if (event.target.dataset.outputField === "name" || event.target.dataset.outputField === "id") {
    const row = event.target.closest(".output-settings-row");
    row.querySelector(".output-row-heading strong").textContent = row.querySelector('[data-output-field="name"]').value || row.querySelector('[data-output-field="id"]').value || "New output";
  }
});

form.addEventListener("input", () => { state.dirty = true; });
form.addEventListener("submit", async event => {
  event.preventDefault();
  const button = document.querySelector("#save-button");
  button.disabled = true;
  try {
    const payload = await mutate("/api/config", { revision: state.revision, config: collectConfig() });
    state.dirty = false;
    state.revision = payload.config.revision;
    state.config = payload.config.values;
    notify("Settings applied");
    await Promise.all([loadConfig(), refresh()]);
  } catch (error) {
    if (error.status === 409) {
      await loadConfig();
      notify("Settings changed elsewhere. Your draft is preserved; review and apply again.", true);
    } else notify(error.message, true);
  } finally { button.disabled = false; }
});

document.querySelector("[data-settings-action='add']").addEventListener("click", () => {
  const template = state.config?.outputs?.[0] || {};
  const existing = new Set([...outputSettingsList.querySelectorAll('[data-output-field="id"]')].map(input => input.value));
  let number = outputSettingsList.children.length + 1;
  while (existing.has(`output-${number}`)) number += 1;
  const output = outputFromTemplate(template, {
    id: `output-${number}`,
    name: `Output ${number}`,
    uuid: "",
  });
  outputSettingsList.append(makeOutputSettingsRow(output));
  state.dirty = true;
  updateRemoveButtons();
});

document.querySelector("#reset-button").addEventListener("click", () => {
  state.dirty = false;
  setFormValues(state.config || {});
  notify("Draft reset");
});
document.querySelector("#open-settings-button").addEventListener("click", () => {
  const settings = document.querySelector(".settings-section");
  settings.open = true;
  settings.scrollIntoView({ behavior: "auto", block: "start" });
});
document.querySelector("#rotation-toggle").addEventListener("click", event => performControl(state.selectedOutputId, event.currentTarget.dataset.command));
document.querySelector("#autocast-toggle").addEventListener("click", event => performControl(state.selectedOutputId, event.currentTarget.dataset.command));
document.querySelector("#next-button").addEventListener("click", () => performControl(state.selectedOutputId, "next"));
document.querySelector("#reconnect-button").addEventListener("click", () => performControl(state.selectedOutputId, "reconnect"));
document.querySelector("#source-kind").addEventListener("change", event => {
  const outputId = state.selectedOutputId;
  const kind = event.currentTarget.value;
  sourceDraft(outputId).pendingKind = kind === "timeline" ? null : kind;
  showSourceControl(kind);
  if (kind === "timeline") applySource(outputId, { kind }, "Using the full timeline");
});
document.querySelector("#album-select").addEventListener("change", event => {
  const outputId = state.selectedOutputId;
  if (event.currentTarget.value) applySource(outputId, { kind: "album", id: event.currentTarget.value }, `Using ${event.currentTarget.selectedOptions[0].textContent}`);
});
document.querySelector("#person-select").addEventListener("change", event => {
  const outputId = state.selectedOutputId;
  if (event.currentTarget.value) applySource(outputId, { kind: "person", id: event.currentTarget.value }, `Showing ${event.currentTarget.selectedOptions[0].textContent}`);
});
document.querySelector("#search-source").addEventListener("submit", event => {
  event.preventDefault();
  const outputId = state.selectedOutputId;
  const query = document.querySelector("#search-query").value.trim();
  if (query) applySource(outputId, { kind: "search", query }, `Searching for “${query}”`);
});
document.querySelector("#search-query").addEventListener("input", event => {
  const draft = sourceDraft(state.selectedOutputId);
  draft.searchDirty = true;
  draft.searchQuery = event.currentTarget.value;
});
document.querySelector("#discover-button").addEventListener("click", async event => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "Scanning…";
  try {
    const payload = await mutate("/api/discovery", {});
    state.devices = payload.devices || [];
    outputSettingsList.querySelectorAll('[data-output-field="uuid"]').forEach(select => setReceiverOptions(select, select.value));
    const added = event.isTrusted ? addDiscoveredOutputs(state.devices) : 0;
    if (added) {
      notify(`Found ${state.devices.length} receivers; added ${added} output${added === 1 ? "" : "s"}. Save changes to activate.`);
    } else if (state.devices.length) {
      notify(`Found ${state.devices.length} receiver${state.devices.length === 1 ? "" : "s"}${event.isTrusted ? "; all are already configured" : ""}`);
    } else notify("No receivers found");
  } catch (error) { notify(error.message, true); }
  finally { button.disabled = false; button.textContent = "Scan the network"; }
});

async function boot() {
  try { await loadConfig(); await Promise.all([loadAlbums(), loadPeople(), refresh()]); }
  catch (error) { notify(error.message, true); }
  document.querySelector("#discover-button").click();
  state.timer = window.setInterval(refresh, 3500);
  state.countdownTimer = window.setInterval(renderCountdowns, 250);
}

boot();
