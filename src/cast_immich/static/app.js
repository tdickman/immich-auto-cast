"use strict";

const state = { csrf: "", revision: 0, config: null, dirty: false, timer: null, countdownTimer: null, gallerySignature: "", pendingCommands: {}, changingSource: false, pendingSourceKind: null, autocastEnabled: true, autocastDeadline: null, thumbnailCache: new Map(), thumbnailAssetIds: new Set() };
const form = document.querySelector("#settings-form");
const toast = document.querySelector("#toast");

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

function notify(message, error = false) {
  toast.textContent = message;
  toast.className = `toast visible${error ? " error" : ""}`;
  window.clearTimeout(toast.timeout);
  toast.timeout = window.setTimeout(() => { toast.className = "toast"; }, 3600);
}

function titleCase(value) {
  return String(value || "unknown").replaceAll("_", " ").replace(/\b\w/g, c => c.toUpperCase());
}

function renderAutocastCountdown() {
  const button = document.querySelector("#autocast-toggle");
  const status = document.querySelector("#autocast-status");
  button.classList.toggle("autocast-off", !state.autocastEnabled);
  button.querySelector("span:last-child").textContent = state.autocastEnabled ? "Autocast on" : "Autocast off";
  if (!state.autocastEnabled) {
    status.textContent = "Autocast is off";
    return;
  }
  if (state.autocastDeadline !== null) {
    const remaining = Math.max(0, Math.ceil((state.autocastDeadline - Date.now()) / 1000));
    status.textContent = `Casting in ${remaining}s`;
    return;
  }
  status.textContent = "Autocast is on";
}

function renderStatus(payload) {
  state.csrf = payload.csrf_token || state.csrf;
  const coordinator = payload.coordinator;
  const mode = payload.mode || "setup";
  const active = mode === "active";
  document.querySelector("#service-mode").textContent = titleCase(mode);
  document.querySelector("#signal-dot").className = `signal-dot ${active ? "active" : payload.error ? "error" : ""}`;
  document.querySelector("#cast-state").textContent = titleCase(coordinator?.state || mode);
  document.querySelector("#cast-detail").textContent = active
    ? coordinator?.rotation_enabled ? "Automatic rotation is on" : "Automatic rotation is paused"
    : "Open settings to finish setup";
  document.querySelector("#ownership-note").textContent = payload.error || coordinator?.error || (
    coordinator?.state === "owned"
      ? "Ready to control this slideshow."
      : "Waiting until the receiver is available."
  );

  const owned = coordinator?.state === "owned";
  const enabled = coordinator?.rotation_enabled ?? true;
  const toggle = document.querySelector("#rotation-toggle");
  toggle.disabled = !active;
  toggle.dataset.command = enabled ? "pause" : "enable";
  toggle.querySelector(".button-mark").textContent = enabled ? "Ⅱ" : "▶";
  toggle.querySelector("span:last-child").textContent = enabled ? "Pause rotation" : "Enable rotation";
  document.querySelector("#next-button").disabled = !owned;
  document.querySelector("#source-kind").disabled = !active || state.changingSource;
  document.querySelector("#album-select").disabled = !active || state.changingSource;
  document.querySelector("#person-select").disabled = !active || state.changingSource;
  const autocast = payload.autocast_enabled ?? true;
  state.autocastEnabled = autocast;
  state.autocastDeadline = typeof payload.autocast_remaining_seconds === "number"
    ? Date.now() + payload.autocast_remaining_seconds * 1000
    : null;
  const autocastToggle = document.querySelector("#autocast-toggle");
  autocastToggle.disabled = !active;
  autocastToggle.dataset.command = autocast ? "autocast_disable" : "autocast_enable";
  renderAutocastCountdown();
  if (state.pendingSourceKind) {
    document.querySelector("#source-kind").value = state.pendingSourceKind;
    showSourceControl(state.pendingSourceKind);
  } else if (!state.changingSource && payload.source) {
    document.querySelector("#source-kind").value = payload.source.kind;
    document.querySelector("#album-select").value = payload.source.kind === "album" ? payload.source.id : "";
    document.querySelector("#person-select").value = payload.source.kind === "person" ? payload.source.id : "";
    if (payload.source.kind === "search") document.querySelector("#search-query").value = payload.source.query || "";
    showSourceControl(payload.source.kind);
  }
}

function setFormValues(values) {
  state.config = structuredClone(values);
  if (state.dirty) return;
  for (const [section, fields] of Object.entries(values)) {
    for (const [key, value] of Object.entries(fields)) {
      const control = form.elements.namedItem(`${section}.${key}`);
      if (control) control.value = value ?? "";
    }
  }
  syncConfiguredReceiver(values.chromecast?.uuid || "");
}

function syncConfiguredReceiver(uuid) {
  const select = document.querySelector("#chromecast-select");
  if (!uuid) return;
  let option = [...select.options].find(item => item.value === uuid);
  if (!option) {
    option = new Option(`Configured · ${uuid}`, uuid);
    select.add(option);
  }
  select.value = uuid;
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

function preloadThumbnail(record) {
  const existing = state.thumbnailCache.get(record.asset_id);
  if (existing) return existing.promise;
  const entry = { url: null, promise: null };
  entry.promise = (async () => {
    const response = await fetch(record.thumbnail_url, { cache: "no-store" });
    if (!response.ok) throw new Error("Thumbnail unavailable");
    const url = URL.createObjectURL(await response.blob());
    if (!state.thumbnailAssetIds.has(record.asset_id)) {
      URL.revokeObjectURL(url);
      throw new Error("Thumbnail no longer needed");
    }
    entry.url = url;
    const image = new Image();
    image.src = url;
    await image.decode();
    if (!state.thumbnailAssetIds.has(record.asset_id) || state.thumbnailCache.get(record.asset_id) !== entry) {
      throw new Error("Thumbnail no longer needed");
    }
    return url;
  })().catch(error => {
    if (entry.url) URL.revokeObjectURL(entry.url);
    entry.url = null;
    if (state.thumbnailCache.get(record.asset_id) === entry) {
      state.thumbnailCache.delete(record.asset_id);
    }
    throw error;
  });
  state.thumbnailCache.set(record.asset_id, entry);
  return entry.promise;
}

function usePreloadedThumbnail(image, record) {
  image.dataset.assetId = record.asset_id;
  const entry = state.thumbnailCache.get(record.asset_id);
  if (entry?.url) {
    image.src = entry.url;
    return;
  }
  preloadThumbnail(record).then(url => {
    if (image.dataset.assetId === record.asset_id) image.src = url;
  }).catch(() => {
    if (image.dataset.assetId === record.asset_id) image.src = record.thumbnail_url;
  });
}

function updateThumbnailCache(records) {
  state.thumbnailAssetIds = new Set(records.map(record => record.asset_id));
  for (const [assetId, entry] of state.thumbnailCache) {
    if (state.thumbnailAssetIds.has(assetId)) continue;
    if (entry.url) URL.revokeObjectURL(entry.url);
    state.thumbnailCache.delete(assetId);
  }
  records.forEach(record => { preloadThumbnail(record).catch(() => {}); });
}

async function loadHistory() {
  const payload = await request("/api/history");
  document.querySelector("#history-count").textContent = String(payload.records.length);
  document.querySelector("#upcoming-count").textContent = String(payload.upcoming.length);
  document.querySelector("#current-count").textContent = payload.current ? "1" : "0";
  updateThumbnailCache([
    ...(payload.current ? [payload.current] : []),
    ...payload.records,
    ...payload.upcoming,
  ]);
  const signature = JSON.stringify([
    payload.current?.asset_id,
    payload.records.map(record => [record.event_id, record.confirmed_at]),
    payload.upcoming.map(record => record.asset_id),
  ]);
  if (signature === state.gallerySignature) return;
  state.gallerySignature = signature;
  renderGallery("#current-list", payload.current ? [payload.current] : [], "No photo is currently casting.", record => {
    const figure = document.createElement("figure");
    figure.className = "photo-record current-record";
    const image = document.createElement("img");
    usePreloadedThumbnail(image, record);
    image.alt = "Currently displayed Immich photo";
    const caption = document.createElement("div");
    caption.className = "photo-caption";
    caption.textContent = record.confirmed_at ? new Date(record.confirmed_at).toLocaleString() : "NOW CASTING";
    figure.append(image, caption);
    return figure;
  });
  renderGallery("#history-list", payload.records, "No confirmed photos yet.", (record, index) => {
    const figure = photoButton("history", record.event_id, index);
    const image = document.createElement("img");
    usePreloadedThumbnail(image, record);
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
  renderGallery("#upcoming-list", payload.upcoming, "The next photos will appear when rotation begins.", (record, index) => {
    const figure = photoButton("upcoming", record.asset_id, index, "upcoming-record");
    const image = document.createElement("img");
    usePreloadedThumbnail(image, record);
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

function photoButton(kind, id, index, extraClass = "") {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `photo-record photo-jump ${extraClass}`.trim();
  button.style.animationDelay = `${index * 45}ms`;
  button.setAttribute("aria-label", `Show this ${kind} photo now`);
  button.addEventListener("click", () => performSeek(kind, id));
  return button;
}

async function loadAlbums() {
  const payload = await request("/api/albums");
  const select = document.querySelector("#album-select");
  select.replaceChildren(new Option("Any photo from the timeline", ""));
  payload.albums.forEach(album => select.add(new Option(`${album.name} (${album.asset_count})`, album.id)));
  select.value = payload.selected_album_id || "";
}

async function loadPeople() {
  const payload = await request("/api/people");
  const select = document.querySelector("#person-select");
  select.replaceChildren(new Option("Choose a person", ""));
  payload.people.forEach(person => select.add(new Option(person.name, person.id)));
  select.value = payload.selected_person_id || "";
}

function showSourceControl(kind) {
  document.querySelector("#album-source").style.display = kind === "album" ? "grid" : "none";
  document.querySelector("#person-source").style.display = kind === "person" ? "grid" : "none";
  document.querySelector("#search-source").style.display = kind === "search" ? "block" : "none";
}

async function applySource(source, message) {
  state.changingSource = true;
  try {
    await mutate("/api/source", source);
    state.pendingSourceKind = null;
    state.gallerySignature = "";
    notify(message);
    await refresh();
  } catch (error) {
    state.pendingSourceKind = null;
    notify(error.message, true);
    await Promise.all([loadAlbums(), loadPeople()]);
  } finally {
    state.changingSource = false;
  }
}

function renderGallery(selector, records, emptyMessage, renderRecord) {
  const list = document.querySelector(selector);
  if (!records.length) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = emptyMessage;
    list.replaceChildren(empty);
    return;
  }
  list.replaceChildren(...records.map(renderRecord));
}

function collectConfig() {
  const result = structuredClone(state.config || {});
  for (const control of form.elements) {
    if (!control.name || control.disabled) continue;
    const [section, key] = control.name.split(".");
    result[section] ||= {};
    result[section][key] = control.type === "number" ? Number(control.value) : control.value;
  }
  return result;
}

async function mutate(path, body) {
  return request(path, { method: path === "/api/config" ? "PUT" : "POST", headers: mutationHeaders(), body: JSON.stringify(body) });
}

async function performControl(command) {
  const buttons = [...document.querySelectorAll(".transport button")];
  buttons.forEach(button => { button.disabled = true; });
  const requestId = state.pendingCommands[command] || crypto.randomUUID();
  state.pendingCommands[command] = requestId;
  try {
    const result = await mutate(`/api/controls/${command}`, { request_id: requestId });
    delete state.pendingCommands[command];
    notify(titleCase(result.outcome));
    await refresh();
  } catch (error) {
    if (error.status) delete state.pendingCommands[command];
    notify(titleCase(error.message), true);
    await refresh();
  }
}

async function performSeek(targetKind, targetId) {
  const requestId = crypto.randomUUID();
  document.querySelectorAll(".photo-jump").forEach(button => { button.disabled = true; });
  try {
    const result = await mutate("/api/seek", { request_id: requestId, target_kind: targetKind, target_id: targetId });
    notify(titleCase(result.outcome));
    await refresh();
  } catch (error) {
    notify(titleCase(error.message), true);
    await refresh();
  }
}

async function refresh() {
  try {
    const [status] = await Promise.all([request("/api/status"), loadHistory()]);
    renderStatus(status);
  } catch (error) {
    if (error.status === 409) {
      await loadConfig();
      notify("Settings changed elsewhere. Your draft is preserved; review and apply again.", true);
    } else {
      notify(error.message, true);
    }
  }
}

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
    } else {
      notify(error.message, true);
    }
  }
  finally { button.disabled = false; }
});

document.querySelector("#reset-button").addEventListener("click", () => {
  state.dirty = false;
  setFormValues(state.config);
  notify("Draft reset");
});
document.querySelector("#rotation-toggle").addEventListener("click", event => performControl(event.currentTarget.dataset.command));
document.querySelector("#autocast-toggle").addEventListener("click", event => performControl(event.currentTarget.dataset.command));
document.querySelector("#next-button").addEventListener("click", () => performControl("next"));
document.querySelector("#source-kind").addEventListener("change", event => {
  const kind = event.currentTarget.value;
  state.pendingSourceKind = kind === "timeline" ? null : kind;
  showSourceControl(kind);
  if (kind === "timeline") applySource({ kind }, "Using the full timeline");
});
document.querySelector("#album-select").addEventListener("change", event => {
  if (event.currentTarget.value) applySource({ kind: "album", id: event.currentTarget.value }, `Using ${event.currentTarget.selectedOptions[0].textContent}`);
});
document.querySelector("#person-select").addEventListener("change", event => {
  if (event.currentTarget.value) applySource({ kind: "person", id: event.currentTarget.value }, `Showing ${event.currentTarget.selectedOptions[0].textContent}`);
});
document.querySelector("#search-source").addEventListener("submit", event => {
  event.preventDefault();
  const query = document.querySelector("#search-query").value.trim();
  if (query) applySource({ kind: "search", query }, `Searching for “${query}”`);
});
document.querySelector("#discover-button").addEventListener("click", async event => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "Scanning…";
  try {
    const payload = await mutate("/api/discovery", {});
    const select = document.querySelector("#chromecast-select");
    const current = select.value;
    select.replaceChildren(new Option("Choose a receiver", ""));
    payload.devices.forEach(device => select.add(new Option(`${device.friendly_name} · ${device.uuid}`, device.uuid)));
    syncConfiguredReceiver(current);
    notify(payload.devices.length ? `Found ${payload.devices.length} receiver${payload.devices.length === 1 ? "" : "s"}` : "No receivers found");
  } catch (error) { notify(error.message, true); }
  finally { button.disabled = false; button.textContent = "Scan the network"; }
});

async function boot() {
  try { await loadConfig(); await Promise.all([loadAlbums(), loadPeople(), refresh()]); }
  catch (error) { notify(error.message, true); }
  document.querySelector("#discover-button").click();
  state.timer = window.setInterval(refresh, 3500);
  state.countdownTimer = window.setInterval(renderAutocastCountdown, 250);
}

boot();
