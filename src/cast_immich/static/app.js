"use strict";

const state = { csrf: "", revision: 0, config: null, dirty: false, timer: null, gallerySignature: "", pendingCommands: {} };
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
  document.querySelector("#stop-button").disabled = !owned;
  document.querySelector("#reconnect-button").disabled = !active;
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

async function loadHistory() {
  const payload = await request("/api/history");
  document.querySelector("#history-count").textContent = String(payload.records.length);
  document.querySelector("#upcoming-count").textContent = String(payload.upcoming.length);
  const signature = JSON.stringify([
    payload.records.map(record => [record.event_id, record.confirmed_at]),
    payload.upcoming.map(record => record.asset_id),
  ]);
  if (signature === state.gallerySignature) return;
  state.gallerySignature = signature;
  renderGallery("#history-list", payload.records, "No confirmed photos yet.", (record, index) => {
    const figure = document.createElement("figure");
    figure.className = "photo-record";
    figure.style.animationDelay = `${index * 45}ms`;
    const image = document.createElement("img");
    image.src = record.thumbnail_url;
    image.alt = "Recently displayed Immich photo";
    image.loading = "lazy";
    const caption = document.createElement("figcaption");
    const time = document.createElement("time");
    time.dateTime = record.confirmed_at;
    time.textContent = new Date(record.confirmed_at).toLocaleString();
    caption.append(time);
    figure.append(image, caption);
    return figure;
  });
  renderGallery("#upcoming-list", payload.upcoming, "The next photos will appear when rotation begins.", (record, index) => {
    const figure = document.createElement("figure");
    figure.className = "photo-record upcoming-record";
    figure.style.animationDelay = `${index * 45}ms`;
    const image = document.createElement("img");
    image.src = record.thumbnail_url;
    image.alt = `Upcoming Immich photo ${index + 1}`;
    image.loading = "lazy";
    const caption = document.createElement("figcaption");
    const position = document.createElement("strong");
    position.textContent = `NEXT ${String(index + 1).padStart(2, "0")}`;
    caption.append(position);
    figure.append(image, caption);
    return figure;
  });
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
document.querySelector("#next-button").addEventListener("click", () => performControl("next"));
document.querySelector("#stop-button").addEventListener("click", () => performControl("stop"));
document.querySelector("#reconnect-button").addEventListener("click", async () => {
  const button = document.querySelector("#reconnect-button");
  button.disabled = true;
  try { await mutate("/api/reconnect", {}); notify("Reconnect requested"); }
  catch (error) { notify(error.message, true); }
  finally { button.disabled = false; }
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
  try { await loadConfig(); await refresh(); }
  catch (error) { notify(error.message, true); }
  document.querySelector("#discover-button").click();
  state.timer = window.setInterval(refresh, 3500);
}

boot();
