"use strict";

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
let state = { blinds: {}, groups: {}, adapters: {}, _web: {} };
let authSession = {
  authenticated: false,
  csrf_token: "",
  username: "",
  role: "owner",
  setup_required: false,
};
const pending = new Map();
const sliderDrafts = new Map();
const railSelections = new Map();
const expandedSections = new Set();

function escapeHtml(value) {
  const entities = {
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  };
  return String(value ?? "").replace(
    /[&<>"']/g,
    (character) => entities[character],
  );
}
function toast(message, type = "success", timeout = 4500) {
  const item = document.createElement("div");
  item.className = `toast ${type}`;
  item.textContent = message;
  $("#toasts").append(item);
  setTimeout(() => item.remove(), timeout);
}
function formatTime(epoch) {
  if (!epoch) return "Waiting for signal";
  return new Date(epoch * 1000).toLocaleTimeString([], {
    hour: "numeric",
    minute: "2-digit",
  });
}
function draftKey(id, action) {
  return `${id}:${action}`;
}
function draftValue(id, action, fallback) {
  const key = draftKey(id, action);
  return sliderDrafts.has(key) ? sliderDrafts.get(key) : fallback;
}
function pendingKey(targetType, id, action, position = null) {
  return `${targetType}:${id}:${action}${position == null ? "" : `:${position}`}`;
}
function targetPending(targetType, id) {
  return [...pending.keys()].some((key) =>
    key.startsWith(`${targetType}:${id}:`),
  );
}
function requireAuth(response) {
  if (response.status === 401) {
    window.location.replace("/login");
    throw new Error("Session expired.");
  }
  return response;
}
function csrfHeaders() {
  return { "X-CSRF-Token": authSession.csrf_token };
}
async function loadAuthSession() {
  const response = requireAuth(await fetch("/api/auth/session"));
  if (!response.ok) throw new Error("Authentication unavailable");
  authSession = await response.json();
  if (authSession.setup_required) window.location.replace("/login");
}
async function getState() {
  const response = requireAuth(await fetch("/api/state"));
  if (!response.ok) throw new Error("Controller unavailable");
  return response.json();
}

async function command(id, action, position = null, targetType = "blind") {
  if (!state._web.live_controls) {
    toast("Live controls are disabled.", "error");
    return;
  }
  const activeKey = pendingKey(targetType, id, action, position);
  if (pending.has(activeKey)) return;
  const targetKey = `${targetType}:${id}`;
  if (action === "stop") {
    for (const [key, request] of pending) {
      if (key.startsWith(`${targetKey}:`)) {
        request.abort();
        pending.delete(key);
      }
    }
  }
  const requestController = new AbortController();
  pending.set(activeKey, requestController);
  render();
  let completed = false;
  try {
    const response = requireAuth(
      await fetch(
        `/api/${targetType === "group" ? "groups" : "blinds"}/${encodeURIComponent(id)}/commands`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json", ...csrfHeaders() },
          body: JSON.stringify({
            action,
            ...(position == null ? {} : { position }),
          }),
          signal: requestController.signal,
        },
      ),
    );
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || "Command rejected");
    const result = body.result;
    const outcomes = result?.results
      ? Object.values(result.results).map((member) => member.outcome)
      : [result?.outcome];
    const hasPreparationErrors =
      Object.keys(result?.preparation_errors || {}).length > 0;
    completed =
      action === "refresh_status" ||
      (!hasPreparationErrors &&
        outcomes.length > 0 &&
        outcomes.every((outcome) => outcome === "completed"));
    const duplicate =
      !hasPreparationErrors &&
      outcomes.includes("duplicate") &&
      outcomes.every(
        (outcome) => outcome === "completed" || outcome === "duplicate",
      );
    if (action === "refresh_status") toast("Status updated.", "success", 6000);
    else if (completed) toast("Movement confirmed.", "success", 6000);
    else if (duplicate)
      toast("Matching commands were already accepted.", "info", 6000);
    else
      toast(
        result?.error || "Command finished; check the reported position.",
        "error",
        6000,
      );
  } catch (error) {
    if (error.name !== "AbortError") toast(error.message, "error", 6000);
  } finally {
    if (
      completed &&
      position != null &&
      sliderDrafts.get(draftKey(id, action)) === position
    )
      sliderDrafts.delete(draftKey(id, action));
    if (pending.get(activeKey) === requestController) pending.delete(activeKey);
    await refresh();
  }
}

function button(label, action, id, kind = "secondary", targetType = "blind") {
  const blocked =
    action === "refresh_status"
      ? targetPending(targetType, id)
      : pending.has(pendingKey(targetType, id, action));
  return `<button class="button ${kind}"
    data-command="${escapeHtml(action)}"
    data-target="${escapeHtml(id)}"
    data-target-type="${escapeHtml(targetType)}"
    ${blocked || !state._web.live_controls ? "disabled" : ""}>${escapeHtml(label)}</button>`;
}
function bindCommands(root = document) {
  root
    .querySelectorAll("[data-command]")
    .forEach((node) =>
      node.addEventListener("click", () =>
        command(
          node.dataset.target,
          node.dataset.command,
          node.dataset.position == null ? null : Number(node.dataset.position),
          node.dataset.targetType,
        ),
      ),
    );
}

function groupCard(id, item) {
  const card = document.createElement("article");
  card.className = "group-card";
  card.innerHTML = `
    <h3>${escapeHtml(item.name || id)}</h3>
    <p>${item.members?.length || 0} shades · ${escapeHtml(item.mode || "best effort").replace("_", " ")}</p>
    <div class="quick-actions">
      ${button("Open", "open", id, "secondary", "group")}
      ${button("Stop", "stop", id, "stop", "group")}
      ${button("Close", "close", id, "secondary", "group")}
    </div>`;
  bindCommands(card);
  return card;
}

function slider(id, label, action, value, startLabel, endLabel) {
  const safe = draftValue(id, action, value ?? 50);
  const unknown = value == null && !sliderDrafts.has(draftKey(id, action));
  const blocked =
    pending.has(pendingKey("blind", id, action, safe)) ||
    !state._web.live_controls;
  return `<div class="slider-row" data-position-control="${escapeHtml(action)}">
    <label><span>${escapeHtml(label)}</span><output>${escapeHtml(unknown ? "Unknown" : `${safe}%`)}</output></label>
    <input type="range" min="0" max="100" value="${safe}" data-slider
      data-action="${escapeHtml(action)}" aria-label="${escapeHtml(label)}">
    <div class="range-labels"><span>${escapeHtml(startLabel)}</span><span>${escapeHtml(endLabel)}</span></div>
    <div class="slider-actions"><button class="button secondary compact"
      data-command="${escapeHtml(action)}" data-target="${escapeHtml(id)}"
      data-target-type="blind" data-position="${safe}" ${blocked ? "disabled" : ""}>Set</button></div>
  </div>`;
}

function applyVisual(card, id, item) {
  const visual = card.querySelector(".shade-visual");
  const bottom = draftValue(id, "set_bottom_position", item.bottom ?? 50);
  if (!item.dual_rail) {
    visual.style.setProperty("--shade-height", `${Math.max(6, 100 - bottom)}%`);
    return;
  }
  const top = draftValue(id, "set_top_position", item.top ?? 0);
  const topY = Math.max(3, Math.min(97, top));
  const bottomY = Math.max(3, Math.min(97, 100 - bottom));
  visual.style.setProperty("--top-y", `${topY}%`);
  visual.style.setProperty("--bottom-y", `${bottomY}%`);
  visual.style.setProperty(
    "--fabric-height",
    `${Math.max(0, bottomY - topY)}%`,
  );
}

function tdbuPositionControl(id, item) {
  const selected = railSelections.get(id) || "bottom";
  const action =
    selected === "top" ? "set_top_position" : "set_bottom_position";
  const value = selected === "top" ? item.top : item.bottom;
  const labels =
    selected === "top"
      ? ["0% · Top", "100% · Bottom"]
      : ["0% · Closed", "100% · Open"];
  return `<div class="tdbu-position">
    <label class="rail-select"><span>Adjust rail</span>
      <select data-rail-select data-target="${escapeHtml(id)}">
        <option value="bottom" ${selected === "bottom" ? "selected" : ""}>Bottom rail</option>
        <option value="top" ${selected === "top" ? "selected" : ""}>Top rail</option>
      </select>
    </label>
    ${slider(id, selected === "top" ? "Top rail position" : "Bottom rail position", action, value, labels[0], labels[1])}
    <p class="rail-note">Setting one rail may reposition the other. Use Move both to preserve their spacing.</p>
  </div>`;
}

function blindCard(id, item) {
  const card = document.createElement("article");
  card.className = "blind-card";
  const battery = item.battery == null ? "—" : `${escapeHtml(item.battery)}%`;
  const positionText =
    item.bottom == null
      ? "Position unknown"
      : item.dual_rail
        ? `Bottom ${escapeHtml(item.bottom)}%${item.top == null ? "" : ` · Top ${escapeHtml(item.top)}%`}`
        : `${escapeHtml(item.bottom)}% open`;
  const positionControls = item.dual_rail
    ? tdbuPositionControl(id, item)
    : slider(
        id,
        "Position",
        "set_bottom_position",
        item.bottom,
        "0% · Closed",
        "100% · Open",
      );
  const statusControl =
    item.bottom == null || item.battery == null
      ? button("Refresh status", "refresh_status", id, "ghost compact")
      : `<span>Updated ${formatTime(item.updated_at)}</span>`;
  const visual = item.dual_rail
    ? `<div class="tdbu-fabric"></div>
       <span class="tdbu-rail top-rail"></span>
       <span class="tdbu-rail bottom-rail"></span>`
    : '<div class="shade-panel"></div>';
  const coupledControls = item.dual_rail
    ? `<details class="more-controls" ${expandedSections.has(id) ? "open" : ""}>
         <summary>Coupled movement</summary>
         <div>
           ${button("Move both up 5%", "stack_up", id)}
           ${button("Move both down 5%", "stack_down", id)}
           ${button("Favorite", "favorite", id)}
         </div>
       </details>`
    : "";
  card.innerHTML = `
    <div class="blind-top">
      <div>
        <h3>${escapeHtml(item.name || id)}</h3>
        <p class="card-meta">${item.dual_rail ? "TDBU shade" : "Single-rail shade"} · Battery
          <span title="Battery requires a Bluetooth connection">${battery}</span>
        </p>
      </div>
      <span class="badge ${item.last_command_status === "completed" ? "ready" : ""}">${targetPending("blind", id) ? "Working…" : escapeHtml(item.last_command_status || "Idle")}</span>
    </div>
    <div class="shade-visual ${item.dual_rail ? "tdbu-visual" : ""}">${visual}</div>
    <div class="blind-state"><span>${positionText}</span>${statusControl}</div>
    <div class="blind-controls">
      ${button("Open", "open", id)}
      ${button("Stop", "stop", id, "danger")}
      ${button("Close", "close", id)}
    </div>
    ${positionControls}
    ${coupledControls}`;
  card
    .querySelector("details.more-controls")
    ?.addEventListener("toggle", (event) => {
      if (event.target.open) expandedSections.add(id);
      else expandedSections.delete(id);
    });
  card
    .querySelector("[data-rail-select]")
    ?.addEventListener("change", (event) => {
      sliderDrafts.delete(draftKey(id, "set_bottom_position"));
      sliderDrafts.delete(draftKey(id, "set_top_position"));
      railSelections.set(id, event.target.value);
      renderBlinds();
    });
  card.querySelectorAll("[data-slider]").forEach((range) =>
    range.addEventListener("input", () => {
      const action = range.dataset.action;
      if (item.dual_rail)
        sliderDrafts.delete(
          draftKey(
            id,
            action === "set_top_position"
              ? "set_bottom_position"
              : "set_top_position",
          ),
        );
      sliderDrafts.set(draftKey(id, action), Number(range.value));
      const row = range.closest(".slider-row");
      row.querySelector("output").textContent = `${range.value}%`;
      const applyButton = row.querySelector("[data-position]");
      applyButton.dataset.position = range.value;
      applyButton.disabled =
        !state._web.live_controls ||
        pending.has(pendingKey("blind", id, action, Number(range.value)));
      applyVisual(card, id, item);
    }),
  );
  applyVisual(card, id, item);
  bindCommands(card);
  return card;
}

function render() {
  const blinds = Object.entries(state.blinds || {}),
    groups = Object.entries(state.groups || {}),
    adapters = Object.entries(state.adapters || {});
  $("#controllerStatus").className = "status-pill online";
  $("#controllerStatus").innerHTML = "<i></i>Online";
  $("#dashboardSummary").textContent =
    `${blinds.length} shades · ${groups.length} groups`;
  $("#controlNotice").classList.toggle("is-hidden", state._web.live_controls);
  $("#groupSection").classList.toggle("is-hidden", groups.length === 0);
  const groupGrid = $("#groupGrid");
  groupGrid.innerHTML = "";
  groups.forEach(([id, item]) => groupGrid.append(groupCard(id, item)));
  renderBlinds();
  const adapterGrid = $("#adapterGrid");
  adapterGrid.innerHTML = "";
  adapters.forEach(([id, item]) => {
    const card = document.createElement("article");
    card.className = "adapter-card";
    card.innerHTML = `
      <strong>${escapeHtml(id)}</strong>
      <p>Status: ${escapeHtml(item.status || "unknown")}</p>
      <p>Connections: ${escapeHtml(item.connection_successes ?? 0)} / ${escapeHtml(item.connection_attempts ?? 0)}</p>
      <p>Failures: ${escapeHtml(item.failures ?? 0)} ${item.last_error ? `· ${escapeHtml(item.last_error)}` : ""}</p>`;
    adapterGrid.append(card);
  });
  $("#adapterSummary").textContent = adapters.length
    ? `${adapters.filter(([, a]) => a.status === "healthy" || a.status === "online" || a.available).length} of ${adapters.length} healthy`
    : "No telemetry";
}

function renderBlinds() {
  const query = $("#blindSearch").value.trim().toLowerCase(),
    grid = $("#blindGrid");
  grid.innerHTML = "";
  Object.entries(state.blinds || {})
    .filter(
      ([id, item]) =>
        !query || `${id} ${item.name}`.toLowerCase().includes(query),
    )
    .forEach(([id, item]) => grid.append(blindCard(id, item)));
}

async function refresh() {
  if (document.activeElement?.type === "range") return;
  try {
    state = await getState();
    render();
  } catch (error) {
    $("#controllerStatus").className = "status-pill error";
    $("#controllerStatus").innerHTML = "<i></i>Controller unavailable";
  }
}
function openAccount() {
  const recovery = authSession.role === "recovery";
  $("#accountName").textContent = recovery
    ? `${authSession.username} · Recovery`
    : authSession.username;
  $("#passwordError").textContent = "";
  $("#ownerAccountError").textContent = "";
  $("#passwordForm").reset();
  $("#ownerAccountForm").reset();
  $("#passwordForm").classList.toggle("is-hidden", recovery);
  $("#ownerAccountForm").classList.toggle("is-hidden", !recovery);
  $("#accountModal").classList.remove("is-hidden");
  $(recovery ? "#ownerUsername" : "#currentPassword").focus();
}
function closeAccount() {
  $("#accountModal").classList.add("is-hidden");
}
async function editSetup() {
  const button = $("#setupButton");
  button.disabled = true;
  button.textContent = "Opening…";
  try {
    const response = requireAuth(
      await fetch("/api/portal/setup", {
        method: "POST",
        headers: csrfHeaders(),
      }),
    );
    const text = await response.text();
    if (!response.ok) throw new Error(text || "Setup is unavailable");
    window.location.replace("/");
  } catch (error) {
    toast(error.message, "error", 6000);
    button.disabled = false;
    button.textContent = "Edit setup";
  }
}
async function changePassword(event) {
  event.preventDefault();
  const current = $("#currentPassword").value,
    newPassword = $("#newPassword").value,
    confirmation = $("#confirmPassword").value,
    error = $("#passwordError");
  error.textContent = "";
  if (newPassword !== confirmation) {
    error.textContent = "Passwords do not match.";
    return;
  }
  const button = event.submitter;
  button.disabled = true;
  try {
    const response = requireAuth(
      await fetch("/api/auth/password", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...csrfHeaders() },
        body: JSON.stringify({
          current_password: current,
          new_password: newPassword,
        }),
      }),
    );
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || "Password change failed.");
    if (body.csrf_token) authSession = body;
    closeAccount();
    toast("Password changed.");
  } catch (reason) {
    error.textContent = reason.message;
  } finally {
    button.disabled = false;
  }
}
async function saveOwner(event) {
  event.preventDefault();
  const username = $("#ownerUsername").value.trim(),
    password = $("#ownerPassword").value,
    confirmation = $("#ownerPasswordConfirm").value,
    error = $("#ownerAccountError");
  error.textContent = "";
  if (password !== confirmation) {
    error.textContent = "Passwords do not match.";
    return;
  }
  const button = event.submitter;
  button.disabled = true;
  try {
    const response = requireAuth(
      await fetch("/api/auth/account", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...csrfHeaders() },
        body: JSON.stringify({ username, password }),
      }),
    );
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || "Owner update failed.");
    if (body.csrf_token) authSession = body;
    closeAccount();
    toast("Owner saved.");
  } catch (reason) {
    error.textContent = reason.message;
  } finally {
    button.disabled = false;
  }
}
async function logout() {
  const button = $("#logoutButton");
  button.disabled = true;
  try {
    await fetch("/api/auth/logout", { method: "POST", headers: csrfHeaders() });
  } finally {
    window.location.replace("/login");
  }
}

$("#setupButton").addEventListener("click", editSetup);
$("#accountButton").addEventListener("click", openAccount);
$("#passwordForm").addEventListener("submit", changePassword);
$("#ownerAccountForm").addEventListener("submit", saveOwner);
$("#logoutButton").addEventListener("click", logout);
$$("[data-close-modal]").forEach((button) =>
  button.addEventListener("click", closeAccount),
);
$("#blindSearch").addEventListener("input", renderBlinds);
loadAuthSession()
  .then(() => {
    refresh();
    setInterval(refresh, 3000);
  })
  .catch(() => {
    $("#controllerStatus").className = "status-pill error";
    $("#controllerStatus").innerHTML = "<i></i>Unavailable";
  });
