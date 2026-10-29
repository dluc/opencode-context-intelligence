"use strict";

const $ = (id) => document.getElementById(id);
const fragment = new URLSearchParams(location.hash.slice(1));
const token = fragment.get("token") || sessionStorage.getItem("opencode-ci-token") || "";
if (token) sessionStorage.setItem("opencode-ci-token", token);
history.replaceState(null, "", "/");
const selected = new Set();
let sessions = [];
let total = 0;
let config = {};
let busy = false;
let generation = 0;

function node(tag, text, cls) {
  const el = document.createElement(tag);
  if (text !== undefined) el.textContent = text;
  if (cls) el.className = cls;
  return el;
}
function status(text, error = false) {
  $("status").textContent = text;
  $("status").className = error ? "error" : "";
}
async function api(path, body) {
  const response = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: { "X-Local-Token": token, ...(body === undefined ? {} : { "Content-Type": "application/json" }) },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
  return result;
}
function updateSelection() {
  $("selected").textContent = `${selected.size} selected`;
  $("export").disabled = !selected.size || busy;
  $("save-settings").disabled = busy;
  $("test-connection").disabled = busy;
  $("connection-url").disabled = busy;
  $("connection-resource").disabled = busy;
  $("include-children").disabled = busy;
  $("reasoning").disabled = busy;
  $("select-all").checked = sessions.length > 0 && sessions.every(s => selected.has(s.id));
  $("select-all").indeterminate = sessions.some(s => selected.has(s.id)) && !$("select-all").checked;
}
function renderSessions() {
  const body = $("sessions");
  body.replaceChildren();
  for (const s of sessions) {
    const row = node("tr");
    const pick = node("td");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = selected.has(s.id);
    box.setAttribute("aria-label", `Select ${s.title || s.id}`);
    box.onchange = () => { box.checked ? selected.add(s.id) : selected.delete(s.id); updateSelection(); };
    pick.append(box);
    const title = node("td", undefined, "title");
    title.append(node("strong", s.title || "Untitled session"), node("code", s.directory), node("code", s.id));
    if (s.children) title.append(node("small", `${s.children} child session(s)`, "muted"));
    if (s.parent_id) title.append(node("small", `Child of ${s.parent_id}`, "muted"));
    const state = s.last_message_type === "idle" ? `Idle · ${s.idle_outcome || "unknown"}` : "May be active";
    row.append(pick, title, node("td", new Date(s.time_updated).toLocaleString()), node("td", String(s.messages)), node("td", state));
    body.append(row);
  }
  if (!sessions.length) {
    const cell = node("td", "No sessions match these filters.", "muted");
    cell.colSpan = 5;
    const row = node("tr"); row.append(cell); body.append(row);
  }
  $("count").textContent = String(total);
  $("more").hidden = sessions.length >= total;
  updateSelection();
}
async function loadSessions(more = false) {
  const request = ++generation;
  const params = new URLSearchParams({ search: $("search").value, since: $("since").value,
    children: String($("show-children").checked), offset: String(more ? sessions.length : 0) });
  const result = await api(`/api/sessions?${params}`);
  if (request !== generation) return;
  sessions = more ? [...sessions, ...result.sessions] : result.sessions;
  total = result.total;
  renderSessions();
}
async function job(path, body) {
  const { job: id } = await api(path, body);
  for (;;) {
    await new Promise(resolve => setTimeout(resolve, 600));
    const result = await api(`/api/job/${id}`);
    if (result.status === "running" && result.events_total !== undefined) {
      const newEvents = result.events_total - result.events_skipped;
      const skipped = result.events_skipped ? ` · ${result.events_skipped} skipped—uploaded before` : "";
      status(`Uploading: ${result.events_sent} / ${newEvents} events sent${skipped}`);
    }
    if (result.status === "failed") throw new Error(result.error);
    if (result.status === "completed") return result.result;
  }
}
async function download(id) {
  const result = await fetch(`/api/download/${id}`, { headers: { "X-Local-Token": token } });
  if (!result.ok) throw new Error("Download failed");
  const url = URL.createObjectURL(await result.blob());
  const a = document.createElement("a"); a.href = url; a.download = `opencode-ci-${id}.zip`; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}
function guard(fn) {
  return async () => { try { await fn(); } catch (e) { status(e.message, true); } };
}
async function loadExports() {
  const result = await api("/api/exports");
  const container = $("exports"); container.replaceChildren();
  if (!result.exports.length) container.append(node("p", "No exports yet.", "muted"));
  for (const report of result.exports) {
    const card = node("div", undefined, "export-card");
    const count = report.sessions.reduce((n, s) => n + s.events, 0);
    card.append(node("h3", `${report.sessions.length} session(s) · ${count.toLocaleString()} events`));
    card.append(node("p", report.sessions.map(s => s.title || s.id).join(" · ")));
    card.append(node("code", `SHA-256 ${report.fingerprint}`));
    const buttons = node("div", undefined, "export-buttons");
    const dl = node("button", "Download ZIP"); dl.onclick = guard(() => download(report.id));
    dl.disabled = busy;
    const upload = node("button", "Upload this export");
    upload.disabled = !config.upload_available || busy;
    upload.title = config.upload_available ? config.destination : "Enter url and auth_resource in Connection settings to enable uploads";
    upload.onclick = guard(async () => {
      if (!confirm(`Upload ${report.sessions.length} sessions / ${count} events to ${config.destination}?\n\nThis may include sensitive conversation and tool data. Nothing is sent until you confirm.`)) return;
      busy = true; updateSelection(); await loadExports(); status("Uploading new events with stable idempotency keys…");
      try {
        const result = await job("/api/upload", { export_id: report.id, confirm: true, url: config.url, auth_resource: config.auth_resource });
        const skipped = result.events_skipped ? ` · ${result.events_skipped} skipped—uploaded before` : "";
        status(result.status === "already_accepted" ? "Nothing new to upload."
          : `Upload finished: ${result.events_sent} events sent${skipped}.\n${result.note}`);
      } finally { busy = false; updateSelection(); await loadExports(); }
    });
    const remove = node("button", "Delete", "danger");
    remove.disabled = busy;
    remove.onclick = guard(async () => {
      if (!confirm(`Delete this local export (${report.sessions.length} sessions)?\n\nThis removes its local files only. OpenCode sessions, upload receipts, and uploaded data are kept.`)) return;
      busy = true;
      updateSelection();
      try {
        await loadExports();
        await job("/api/delete-export", { export_id: report.id, confirm: true });
        status("Local export deleted.");
      } finally {
        busy = false;
        updateSelection();
        await loadExports();
      }
    });
    buttons.append(dl, upload, remove); card.append(buttons);
    const details = node("details"); details.append(node("summary", `${report.warnings.length} mapping note(s) · file details`));
    details.append(node("pre", JSON.stringify({ notes: report.warnings, sessions: report.sessions }, null, 2)));
    card.append(details); container.append(card);
  }
}
$("refresh").onclick = guard(() => loadSessions());
$("more").onclick = guard(() => loadSessions(true));
let debounce;
$("search").oninput = () => { clearTimeout(debounce); debounce = setTimeout(guard(() => loadSessions()), 200); };
$("since").onchange = guard(() => loadSessions());
$("show-children").onchange = guard(() => loadSessions());
$("today").onclick = guard(() => { const d = new Date(); $("since").value = `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`; return loadSessions(); });
$("all-dates").onclick = guard(() => { $("since").value = ""; return loadSessions(); });
$("select-all").onchange = () => {
  for (const s of sessions) $("select-all").checked ? selected.add(s.id) : selected.delete(s.id);
  renderSessions();
};
$("export").onclick = guard(async () => {
  busy = true; updateSelection(); status("Reading a consistent database snapshot and exporting locally…");
  try {
    const report = await job("/api/export", { sessions: [...selected], include_children: config.include_children, include_reasoning: config.include_reasoning });
    status(`Exported ${report.sessions.length} sessions locally. Nothing uploaded.\n${report.warnings.length} mapping note(s). Files: ${config.state}/exports/${report.id}`);
  } finally { busy = false; updateSelection(); await loadExports(); }
});
function showConfiguration(value) {
  config = value;
  $("database").textContent = config.database;
  $("export-folder").textContent = `${config.state.replace(/\/$/, "")}/exports`;
  $("destination").textContent = config.destination || "Not configured · export only";
  if (config.destination && !config.upload_available) $("destination").textContent += " · complete Connection settings to enable uploads";
  $("connection-url").value = config.url || "";
  $("connection-resource").value = config.auth_resource || "";
  $("include-children").checked = config.include_children;
  $("reasoning").checked = config.include_reasoning;
  $("auth-mode").textContent = config.auth_mode === "entra" ? "Microsoft Entra · Azure credentials" : "Legacy static bearer";
}
function setSettingsOpen(open) {
  showConfiguration(config);
  $("connection-test-result").hidden = true;
  $("connection-settings").hidden = !open;
  $("sessions-panel").hidden = open;
  $("exports-panel").hidden = open;
  status("");
  $("toggle-settings").setAttribute("aria-expanded", String(open));
  (open ? $("connection-url") : $("toggle-settings")).focus();
}
$("toggle-settings").onclick = () => setSettingsOpen($("connection-settings").hidden);
$("close-settings").onclick = () => setSettingsOpen(false);
$("connection-settings").onkeydown = (event) => {
  if (event.key === "Escape") {
    event.preventDefault();
    setSettingsOpen(false);
  }
};
for (const id of ["connection-url", "connection-resource"]) {
  $(id).addEventListener("input", () => { $("connection-test-result").hidden = true; });
}
$("test-connection").onclick = async () => {
  if (busy) return;
  const url = $("connection-url").value.trim();
  const authResource = $("connection-resource").value.trim();
  const result = $("connection-test-result");
  busy = true;
  updateSelection();
  result.hidden = false;
  result.textContent = "Testing…";
  try {
    await loadExports();
    const checked = await job("/api/test-connection", { url, auth_resource: authResource });
    if ($("connection-url").value.trim() !== url || $("connection-resource").value.trim() !== authResource) return;
    result.replaceChildren(node("strong", checked.success ? "Connection verified" : "Connection not fully verified"),
      node("div", `Server: ${checked.server.message}`),
      node("div", `Authentication: ${checked.authentication.message}`),
      node("small", "No session data sent. Upload permissions not tested.", "muted"));
  } catch (error) {
    result.textContent = error.message;
  } finally {
    busy = false;
    updateSelection();
    await loadExports().catch(error => status(error.message, true));
  }
};
$("connection-form").onsubmit = (event) => {
  event.preventDefault();
  return guard(async () => {
    if (busy) return;
    $("save-settings").disabled = true;
    try {
      showConfiguration(await api("/api/config", { url: $("connection-url").value.trim(), auth_resource: $("connection-resource").value.trim(),
        include_children: $("include-children").checked, include_reasoning: $("reasoning").checked }));
      await loadExports();
      status("Settings saved.");
    } finally { $("save-settings").disabled = false; }
  })();
};
(async () => {
  try {
    showConfiguration(await api("/api/config"));
    await Promise.all([loadSessions(), loadExports()]);
  } catch (e) { status(e.message, true); }
})();
