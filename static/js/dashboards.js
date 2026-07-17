/**
 * Dashboards — user-defined blocks that digest email/calendar/etc. data
 * through an LLM prompt into a table, on a refresh schedule.
 */

import { showToast } from "./ui.js";

const API = "/api/dashboards";

async function apiFetch(url, opts = {}) {
  const res = await fetch(url, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || detail;
    } catch {}
    throw new Error(detail);
  }
  return res.json();
}

// ── State ────────────────────────────────────────────────────────
let dashboards = [];
let activeDashboardId = null;
let blocks = [];
let sourceRegistry = [];
let modelItems = null;

// ── Init ─────────────────────────────────────────────────────────
export async function initDashboards(container) {
  container.innerHTML = `
    <div class="dash-layout">
      <div class="dash-sidebar">
        <div class="dash-sidebar-header">
          <h3>Dashboards</h3>
          <button class="btn btn-sm btn-primary" id="dash-btn-new">+ New</button>
        </div>
        <div id="dash-list" class="dash-list"></div>
      </div>
      <div class="dash-main" id="dash-main">
        <div class="dash-placeholder">Select a dashboard or create a new one</div>
      </div>
    </div>
    <div id="dash-modal-overlay" class="dash-modal-overlay" style="display:none"></div>
  `;

  await loadSourceRegistry();
  await loadDashboards();
  bindEvents(container);
}

// ── API: dashboards ─────────────────────────────────────────────
async function loadSourceRegistry() {
  try {
    const data = await apiFetch(`${API}/meta/sources`);
    sourceRegistry = data.sources || [];
  } catch (e) {
    sourceRegistry = [];
  }
}

async function loadDashboards() {
  try {
    const data = await apiFetch(API);
    dashboards = data.dashboards || [];
    renderDashboardList();
  } catch (e) {
    showToast("Failed to load dashboards", "error");
  }
}

async function createDashboard(name) {
  const d = await apiFetch(API, { method: "POST", body: JSON.stringify({ name }) });
  await loadDashboards();
  return d;
}

async function deleteDashboard(id) {
  await apiFetch(`${API}/${id}`, { method: "DELETE" });
  if (activeDashboardId === id) {
    activeDashboardId = null;
    blocks = [];
    document.getElementById("dash-main").innerHTML =
      '<div class="dash-placeholder">Select a dashboard or create a new one</div>';
  }
  await loadDashboards();
}

async function selectDashboard(id) {
  activeDashboardId = id;
  renderDashboardList();
  await loadBlocks(id);
}

// ── API: blocks ──────────────────────────────────────────────────
async function loadBlocks(dashboardId) {
  const main = document.getElementById("dash-main");
  main.innerHTML = '<div class="dash-loading">Loading…</div>';
  try {
    const data = await apiFetch(`${API}/${dashboardId}/blocks`);
    blocks = data.blocks || [];
    renderBlocks();
  } catch (e) {
    showToast("Failed to load blocks", "error");
  }
}

async function saveBlock(block) {
  const isNew = !block.id;
  const url = isNew ? `${API}/${activeDashboardId}/blocks` : `${API}/blocks/${block.id}`;
  const method = isNew ? "POST" : "PUT";
  await apiFetch(url, { method, body: JSON.stringify(block) });
  await loadBlocks(activeDashboardId);
}

async function deleteBlock(id) {
  await apiFetch(`${API}/blocks/${id}`, { method: "DELETE" });
  await loadBlocks(activeDashboardId);
}

async function runBlock(id) {
  const card = document.querySelector(`.dash-block[data-id="${id}"]`);
  if (card) card.classList.add("dash-block-running");
  try {
    await apiFetch(`${API}/blocks/${id}/run`, { method: "POST" });
    await loadBlocks(activeDashboardId);
    showToast("Block refreshed", "success");
  } catch (e) {
    showToast(`Run failed: ${e.message}`, "error");
    if (card) card.classList.remove("dash-block-running");
  }
}

// ── Render: dashboard list ──────────────────────────────────────
function renderDashboardList() {
  const list = document.getElementById("dash-list");
  if (!list) return;

  if (!dashboards.length) {
    list.innerHTML = '<div class="dash-empty">No dashboards yet. Click "+ New" to create one.</div>';
    return;
  }

  list.innerHTML = dashboards
    .map(
      (d) => `
    <div class="dash-list-item ${d.id === activeDashboardId ? "active" : ""}" data-id="${d.id}">
      <div class="dash-list-name">${esc(d.name)}</div>
      <button class="btn-icon dash-btn-delete" title="Delete" data-action="delete-dashboard" data-id="${d.id}">✕</button>
    </div>`
    )
    .join("");
}

// ── Render: blocks ───────────────────────────────────────────────
function renderBlocks() {
  const main = document.getElementById("dash-main");
  if (!main) return;

  const dashboard = dashboards.find((d) => d.id === activeDashboardId);
  const header = `
    <div class="dash-main-header">
      <h3>${esc(dashboard ? dashboard.name : "Dashboard")}</h3>
      <button class="btn btn-sm btn-primary" id="dash-btn-add-block">+ Add Block</button>
    </div>`;

  if (!blocks.length) {
    main.innerHTML = `${header}<div class="dash-placeholder">No blocks yet. Add one to start digesting data.</div>`;
    return;
  }

  main.innerHTML = `${header}<div class="dash-blocks">${blocks.map(renderBlockCard).join("")}</div>`;
}

function renderBlockCard(b) {
  let tableHtml = "";
  if (b.last_columns && b.last_columns.length) {
    tableHtml += "<thead><tr>";
    for (const c of b.last_columns) tableHtml += `<th>${esc(c)}</th>`;
    tableHtml += "</tr></thead><tbody>";
    for (const row of b.last_rows || []) {
      tableHtml += "<tr>";
      for (const c of b.last_columns) tableHtml += `<td>${esc(String(row[c] ?? ""))}</td>`;
      tableHtml += "</tr>";
    }
    tableHtml += "</tbody>";
  }

  const sourceLabels = (b.sources || [])
    .map((sid) => sourceRegistry.find((s) => s.id === sid)?.label || sid)
    .join(", ");

  return `
    <div class="dash-block" data-id="${b.id}">
      <div class="dash-block-header">
        <div>
          <div class="dash-block-title">${esc(b.title)}</div>
          <div class="dash-block-meta">
            ${esc(sourceLabels || "No sources")} · refresh: ${esc(b.refresh_interval)}
            ${b.last_run_at ? " · last run " + fmtDate(b.last_run_at) : ""}
          </div>
        </div>
        <div class="dash-block-actions">
          <button class="btn-icon" title="Run now" data-action="run-block" data-id="${b.id}">▶</button>
          <button class="btn-icon" title="Download" data-action="download-block" data-id="${b.id}">⬇</button>
          <button class="btn-icon" title="Edit" data-action="edit-block" data-id="${b.id}">✎</button>
          <button class="btn-icon" title="Delete" data-action="delete-block" data-id="${b.id}">✕</button>
        </div>
      </div>
      ${b.last_summary ? `<p class="dash-block-summary">${esc(b.last_summary)}</p>` : ""}
      ${tableHtml ? `<div class="dash-table-wrap"><table class="dash-table">${tableHtml}</table></div>` : `<p class="dash-no-results">Not run yet.</p>`}
      <div class="dash-block-spinner">Running…</div>
    </div>`;
}

// ── Model picker data ─────────────────────────────────────────────
async function loadModels() {
  if (modelItems) return modelItems;
  try {
    const res = await fetch("/api/models?background=false", { credentials: "same-origin" });
    const data = await res.json();
    modelItems = data.items || [];
  } catch (e) {
    modelItems = [];
  }
  return modelItems;
}

function modelOptionsHtml(items, selectedUrl, selectedModel) {
  const groups = { local: [], api: [] };
  for (const item of items) {
    const cat = item.category === "local" ? "local" : "api";
    const displayNames = item.models_display || item.models || [];
    (item.models || []).forEach((mid, i) => {
      groups[cat].push({
        url: item.url,
        mid,
        label: `${item.endpoint_name || "Unknown"} — ${displayNames[i] || mid}`,
      });
    });
  }
  const optGroup = (label, list) => {
    if (!list.length) return "";
    const opts = list
      .map((m) => {
        const val = `${m.url}|||${m.mid}`;
        const sel = m.url === selectedUrl && m.mid === selectedModel ? "selected" : "";
        return `<option value="${esc(val)}" ${sel}>${esc(m.label)}</option>`;
      })
      .join("");
    return `<optgroup label="${label}">${opts}</optgroup>`;
  };
  return (
    `<option value="">Use background-task default</option>` +
    optGroup("Remote / API", groups.api) +
    optGroup("Local", groups.local)
  );
}

// ── Modal: block editor ────────────────────────────────────────────
async function showBlockModal(block = null) {
  const isEdit = !!block;
  const overlay = document.getElementById("dash-modal-overlay");
  if (!overlay) return;

  const items = await loadModels();
  const sourcesConf = block?.source_config || {};
  const activeSources = new Set(block?.sources || []);

  const sourceFieldsHtml = sourceRegistry
    .map((s) => {
      const checked = activeSources.has(s.id) ? "checked" : "";
      const cfg = sourcesConf[s.id] || {};
      const fields = (s.config_schema || [])
        .map((f) => {
          const val = cfg[f.key] ?? f.default ?? "";
          return `<label class="dash-subfield">${esc(f.label)}
            <input type="${f.type === "number" ? "number" : "text"}" data-source="${s.id}" data-key="${f.key}"
                   value="${esc(val)}" placeholder="${esc(f.placeholder || "")}">
          </label>`;
        })
        .join("");
      return `
        <div class="dash-source-block">
          <label class="dash-checkbox"><input type="checkbox" class="dash-source-toggle" data-source-id="${s.id}" ${checked}> ${esc(s.label)}</label>
          <div class="dash-source-fields" ${checked ? "" : 'style="display:none"'}>${fields}</div>
        </div>`;
    })
    .join("");

  overlay.innerHTML = `
    <div class="dash-modal">
      <h3>${isEdit ? "Edit Block" : "New Block"}</h3>
      <form id="dash-block-form">
        <label>Title <input name="title" value="${esc(block?.title || "")}" required></label>
        <label>Prompt (what to extract, and in what format)
          <textarea name="prompt" rows="6" required placeholder="e.g. List every invoice mentioned, with columns Date, Vendor, Amount, Due Date.">${esc(block?.prompt || "")}</textarea>
        </label>

        <label>Data sources</label>
        <div id="dash-sources">${sourceFieldsHtml}</div>

        <label>Model <span style="opacity:0.5;font-weight:normal;font-size:10px;">(optional — overrides the default background-task model)</span>
          <select name="model_select" id="dash-model-select">${modelOptionsHtml(items, block?.model_endpoint_url, block?.model)}</select>
        </label>

        <label>Refresh
          <select name="refresh_interval">
            ${["manual", "hourly", "daily", "weekly"]
              .map((v) => `<option value="${v}" ${block?.refresh_interval === v ? "selected" : ""}>${v[0].toUpperCase() + v.slice(1)}</option>`)
              .join("")}
          </select>
        </label>

        <input type="hidden" name="id" value="${block?.id || ""}">
        <div class="dash-modal-buttons">
          <button type="submit" class="btn btn-primary">Save</button>
          <button type="button" class="btn" id="dash-modal-cancel">Cancel</button>
        </div>
      </form>
    </div>`;
  overlay.style.display = "flex";

  overlay.querySelector("#dash-modal-cancel").onclick = () => {
    overlay.style.display = "none";
  };

  overlay.querySelectorAll(".dash-source-toggle").forEach((cb) => {
    cb.addEventListener("change", () => {
      const fields = cb.closest(".dash-source-block").querySelector(".dash-source-fields");
      fields.style.display = cb.checked ? "" : "none";
    });
  });

  overlay.querySelector("#dash-block-form").onsubmit = async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);

    const sources = [];
    const source_config = {};
    overlay.querySelectorAll(".dash-source-toggle").forEach((cb) => {
      if (!cb.checked) return;
      const sid = cb.dataset.sourceId;
      sources.push(sid);
      const cfg = {};
      overlay.querySelectorAll(`[data-source="${sid}"]`).forEach((input) => {
        if (input.value !== "") cfg[input.dataset.key] = input.value;
      });
      source_config[sid] = cfg;
    });

    const modelVal = fd.get("model_select") || "";
    const [model_endpoint_url, model] = modelVal ? modelVal.split("|||") : [null, null];

    const data = {
      title: fd.get("title"),
      prompt: fd.get("prompt"),
      sources,
      source_config,
      model_endpoint_url,
      model,
      refresh_interval: fd.get("refresh_interval") || "manual",
    };
    if (fd.get("id")) data.id = fd.get("id");

    await saveBlock(data);
    overlay.style.display = "none";
    showToast(isEdit ? "Block updated" : "Block created", "success");
  };
}

// ── Events ───────────────────────────────────────────────────────
function bindEvents(container) {
  container.addEventListener("click", async (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;

    if (btn.id === "dash-btn-new") {
      const name = prompt("Dashboard name:");
      if (name && name.trim()) {
        const d = await createDashboard(name.trim());
        await selectDashboard(d.id);
      }
      return;
    }

    if (btn.id === "dash-btn-add-block") {
      showBlockModal();
      return;
    }

    const action = btn.dataset.action;
    const id = btn.dataset.id;

    if (action === "delete-dashboard" && id) {
      e.stopPropagation();
      if (confirm("Delete this dashboard and all its blocks?")) await deleteDashboard(id);
      return;
    }
    if (action === "run-block" && id) {
      await runBlock(id);
      return;
    }
    if (action === "edit-block" && id) {
      showBlockModal(blocks.find((b) => b.id === id));
      return;
    }
    if (action === "delete-block" && id) {
      if (confirm("Delete this block?")) await deleteBlock(id);
      return;
    }
    if (action === "download-block" && id) {
      window.open(`${API}/blocks/${id}/download`, "_blank");
      return;
    }

    const item = e.target.closest(".dash-list-item");
    if (item && !e.target.closest("[data-action]")) {
      await selectDashboard(item.dataset.id);
    }
  });
}

// ── Helpers ──────────────────────────────────────────────────────
function esc(s) {
  if (s == null) return "";
  const d = document.createElement("div");
  d.textContent = String(s);
  return d.innerHTML;
}

function fmtDate(iso) {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

// ── CSS injection ────────────────────────────────────────────────
const CSS = `
.dash-layout { display: flex; gap: 16px; height: 100%; }
.dash-sidebar { width: 260px; flex-shrink: 0; border-right: 1px solid var(--border, #ddd); overflow-y: auto; padding-right: 8px; }
.dash-sidebar-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
.dash-sidebar-header h3 { margin: 0; }
.dash-list { display: flex; flex-direction: column; gap: 4px; }
.dash-list-item { display: flex; justify-content: space-between; align-items: center; padding: 8px; border-radius: 6px; cursor: pointer; border: 1px solid transparent; }
.dash-list-item:hover { background: var(--hover, #f5f5f5); }
.dash-list-item.active { border-color: var(--primary, #4a90d9); background: var(--active-bg, #e8f0fe); }
.dash-list-name { font-weight: 600; }
.dash-main { flex: 1; overflow-y: auto; }
.dash-main-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.dash-placeholder { color: var(--muted, #999); text-align: center; padding-top: 60px; }
.dash-loading { text-align: center; padding-top: 60px; color: var(--muted, #999); }
.dash-empty { color: var(--muted, #999); padding: 12px; font-style: italic; }
.dash-blocks { display: flex; flex-direction: column; gap: 16px; }
.dash-block { border: 1px solid var(--border, #ddd); border-radius: 8px; padding: 16px; position: relative; }
.dash-block-header { display: flex; justify-content: space-between; align-items: flex-start; }
.dash-block-title { font-weight: 600; }
.dash-block-meta { font-size: 0.8em; color: var(--muted, #888); margin-top: 2px; }
.dash-block-actions { display: flex; gap: 2px; }
.dash-block-actions .btn-icon { background: none; border: none; cursor: pointer; font-size: 1.05em; padding: 2px 6px; border-radius: 4px; }
.dash-block-actions .btn-icon:hover { background: var(--hover, #eee); }
.dash-block-summary { color: var(--muted, #666); font-style: italic; margin: 8px 0 0; }
.dash-no-results { color: var(--muted, #999); margin: 8px 0 0; }
.dash-table-wrap { overflow-x: auto; margin-top: 10px; }
.dash-table { width: 100%; border-collapse: collapse; }
.dash-table th, .dash-table td { border: 1px solid var(--border, #ddd); padding: 6px 10px; text-align: left; font-size: 0.9em; }
.dash-table th { background: var(--th-bg, #f5f5f5); font-weight: 600; }
.dash-table tr:hover { background: var(--hover, #fafafa); }
.dash-block-spinner { display: none; position: absolute; inset: 0; align-items: center; justify-content: center; background: var(--bg, rgba(255,255,255,0.85)); border-radius: 8px; color: var(--muted, #999); }
.dash-block-running .dash-block-spinner { display: flex; }
.dash-modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.4); display: flex; align-items: center; justify-content: center; z-index: 1000; }
.dash-modal { background: var(--bg, #fff); border-radius: 8px; padding: 24px; width: 600px; max-width: 92vw; max-height: 88vh; overflow-y: auto; }
.dash-modal h3 { margin: 0 0 16px 0; }
.dash-modal label { display: block; margin-bottom: 12px; font-weight: 500; }
.dash-modal input, .dash-modal textarea, .dash-modal select { width: 100%; padding: 8px; border: 1px solid var(--border, #ddd); border-radius: 4px; font-size: 0.9em; box-sizing: border-box; }
.dash-modal textarea { font-family: monospace; resize: vertical; }
.dash-source-block { border: 1px solid var(--border, #ddd); border-radius: 6px; padding: 8px 10px; margin-bottom: 8px; }
.dash-source-fields { margin-top: 8px; display: flex; flex-direction: column; gap: 6px; }
.dash-subfield { font-size: 0.85em; font-weight: normal; margin-bottom: 0; }
.dash-checkbox { display: flex !important; align-items: center; gap: 8px; margin-bottom: 0 !important; }
.dash-checkbox input { width: auto; }
.dash-modal-buttons { display: flex; gap: 8px; justify-content: flex-end; margin-top: 16px; }
`;

if (typeof document !== "undefined") {
  const style = document.createElement("style");
  style.textContent = CSS;
  style.id = "dash-styles";
  document.head.appendChild(style);
}

// ── Auto-wire sidebar entry ─────────────────────────────────────
if (typeof document !== "undefined") {
  document.addEventListener("DOMContentLoaded", () => {
    const btn = document.getElementById("dashboards-section-title");
    if (!btn) return;
    btn.addEventListener("click", async () => {
      let panel = document.getElementById("dash-content-panel");
      if (!panel) {
        panel = document.createElement("div");
        panel.id = "dash-content-panel";
        panel.style.cssText = "position:fixed;inset:0;z-index:900;background:var(--bg,#fff);overflow-y:auto;padding:24px;display:none";
        const close = document.createElement("button");
        close.textContent = "×";
        close.style.cssText = "position:absolute;top:12px;right:16px;background:none;border:none;font-size:24px;cursor:pointer;color:var(--muted,#999)";
        close.addEventListener("click", () => { panel.style.display = "none"; });
        panel.appendChild(close);
        const wrap = document.createElement("div");
        wrap.id = "dash-content-wrap";
        panel.appendChild(wrap);
        document.body.appendChild(panel);
      }
      panel.style.display = "block";
      initDashboards(document.getElementById("dash-content-wrap"));
    });
  });
}
