/**
 * Email Extraction — manage extraction profiles and view results.
 * Registered as a document-section page in the UI.
 */

import { showToast } from "./ui.js";

const API = "/api/email-extraction";

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
let profiles = [];
let activeProfileId = null;
let resultsData = null;

// ── Init ─────────────────────────────────────────────────────────
export async function initEmailExtraction(container) {
  container.innerHTML = `
    <div class="ee-layout">
      <div class="ee-sidebar">
        <div class="ee-sidebar-header">
          <h3>Extraction Profiles</h3>
          <button class="btn btn-sm btn-primary" id="ee-btn-new">+ New</button>
        </div>
        <div id="ee-profile-list" class="ee-profile-list"></div>
      </div>
      <div class="ee-main" id="ee-main">
        <div class="ee-placeholder">Select a profile or create a new one</div>
      </div>
    </div>
    <div id="ee-modal-overlay" class="ee-modal-overlay" style="display:none"></div>
  `;

  await loadProfiles();
  bindEvents(container);
}

// ── API ──────────────────────────────────────────────────────────
async function loadProfiles() {
  try {
    const data = await apiFetch(`${API}/profiles`);
    profiles = data.profiles || [];
    renderProfileList();
  } catch (e) {
    showToast("Failed to load profiles", "error");
  }
}

async function saveProfile(profile) {
  const isNew = !profile.id;
  const url = isNew ? `${API}/profiles` : `${API}/profiles/${profile.id}`;
  const method = isNew ? "POST" : "PUT";
  const res = await apiFetch(url, {
    method,
    body: JSON.stringify(profile),
  });
  await loadProfiles();
  return res;
}

async function deleteProfile(id) {
  await apiFetch(`${API}/profiles/${id}`, { method: "DELETE" });
  if (activeProfileId === id) {
    activeProfileId = null;
    document.getElementById("ee-main").innerHTML =
      '<div class="ee-placeholder">Select a profile or create a new one</div>';
  }
  await loadProfiles();
}

async function runExtraction(id) {
  const main = document.getElementById("ee-main");
  main.innerHTML = '<div class="ee-loading">Running extraction...</div>';
  try {
    const result = await apiFetch(`${API}/profiles/${id}/run`, { method: "POST" });
    await loadProfiles();
    await showResults(id);
    showToast(`Extracted ${result.rows.length} rows in ${(result.elapsed_ms / 1000).toFixed(1)}s`, "success");
  } catch (e) {
    showToast(`Extraction failed: ${e.message}`, "error");
    main.innerHTML = '<div class="ee-placeholder">Extraction failed. Check the profile settings.</div>';
  }
}

async function showResults(id) {
  activeProfileId = id;
  try {
    const data = await apiFetch(`${API}/profiles/${id}/results`);
    resultsData = data;
    renderResults(data, id);
  } catch (e) {
    showToast("Failed to load results", "error");
  }
}

// ── Render ───────────────────────────────────────────────────────
function renderProfileList() {
  const list = document.getElementById("ee-profile-list");
  if (!list) return;

  if (!profiles.length) {
    list.innerHTML = '<div class="ee-empty">No profiles yet. Click "+ New" to create one.</div>';
    return;
  }

  list.innerHTML = profiles
    .map(
      (p) => `
    <div class="ee-profile-item ${p.id === activeProfileId ? "active" : ""}"
         data-id="${p.id}">
      <div class="ee-profile-name">${esc(p.name)}</div>
      <div class="ee-profile-meta">
        ${p.enabled ? "✓" : "✗"} ${p.folder}
        ${p.last_run_at ? " · " + fmtDate(p.last_run_at) : ""}
      </div>
      <div class="ee-profile-actions">
        <button class="btn-icon ee-btn-run" title="Run now" data-action="run" data-id="${p.id}">▶</button>
        <button class="btn-icon ee-btn-edit" title="Edit" data-action="edit" data-id="${p.id}">✎</button>
        <button class="btn-icon ee-btn-delete" title="Delete" data-action="delete" data-id="${p.id}">✕</button>
      </div>
    </div>`
    )
    .join("");
}

function renderResults(data, profileId) {
  const main = document.getElementById("ee-main");
  if (!main) return;

  const profile = profiles.find((p) => p.id === profileId);
  const name = profile ? profile.name : "Results";
  const { rows, columns, summary, last_run_at } = data;

  if (!rows.length && !columns.length) {
    main.innerHTML = `
      <div class="ee-result-header">
        <h3>${esc(name)}</h3>
        <p class="ee-no-results">No results yet. Run the extraction to populate data.</p>
        <button class="btn btn-primary" id="ee-btn-run-now">▶ Run Extraction</button>
      </div>`;
    return;
  }

  let tableHtml = "";
  if (columns.length) {
    tableHtml += "<thead><tr>";
    for (const c of columns) tableHtml += `<th>${esc(c)}</th>`;
    tableHtml += "</tr></thead><tbody>";
    for (const row of rows) {
      tableHtml += "<tr>";
      for (const c of columns) tableHtml += `<td>${esc(String(row[c] ?? ""))}</td>`;
      tableHtml += "</tr>";
    }
    tableHtml += "</tbody>";
  }

  main.innerHTML = `
    <div class="ee-result-header">
      <h3>${esc(name)}</h3>
      <div class="ee-result-actions">
        <button class="btn btn-sm" id="ee-btn-run-now">▶ Run</button>
        <button class="btn btn-sm" id="ee-btn-csv">⬇ CSV</button>
        <button class="btn btn-sm" id="ee-btn-edit-profile">✎ Edit</button>
      </div>
      ${summary ? `<p class="ee-summary">${esc(summary)}</p>` : ""}
      ${last_run_at ? `<p class="ee-meta">Last run: ${fmtDate(last_run_at)}</p>` : ""}
    </div>
    <div class="ee-table-wrap">
      <table class="ee-table">${tableHtml}</table>
    </div>`;
}

// ── Modal (create/edit) ──────────────────────────────────────────
function showModal(profile = null) {
  const isEdit = !!profile;
  const overlay = document.getElementById("ee-modal-overlay");
  if (!overlay) return;

  overlay.innerHTML = `
    <div class="ee-modal">
      <h3>${isEdit ? "Edit Profile" : "New Extraction Profile"}</h3>
      <form id="ee-form">
        <label>Name <input name="name" value="${esc(profile?.name || "")}" required></label>
        <label>Prompt (what to extract)
          <textarea name="prompt" rows="6" required>${esc(profile?.prompt || "")}</textarea>
        </label>
        <label>Folder <input name="folder" value="${esc(profile?.folder || "INBOX")}"></label>
        <label>IMAP Search Filter <input name="search_filter" value="${esc(profile?.search_filter || "")}" placeholder="e.g. UNSEEN, FROM '@example.com', SINCE 01-Jan-2024"></label>
        <label>Max Emails <input name="max_emails" type="number" min="1" max="500" value="${profile?.max_emails || 50}"></label>
        <label class="ee-checkbox"><input name="enabled" type="checkbox" ${profile && !profile.enabled ? "" : "checked"}> Enabled</label>
        <input type="hidden" name="id" value="${profile?.id || ""}">
        <div class="ee-modal-buttons">
          <button type="submit" class="btn btn-primary">Save</button>
          <button type="button" class="btn" id="ee-modal-cancel">Cancel</button>
        </div>
      </form>
    </div>`;
  overlay.style.display = "flex";

  overlay.querySelector("#ee-modal-cancel").onclick = () => {
    overlay.style.display = "none";
  };
  overlay.querySelector("#ee-form").onsubmit = async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const data = {
      name: fd.get("name"),
      prompt: fd.get("prompt"),
      folder: fd.get("folder") || "INBOX",
      search_filter: fd.get("search_filter") || null,
      max_emails: parseInt(fd.get("max_emails")) || 50,
      enabled: fd.get("enabled") === "on",
    };
    if (fd.get("id")) data.id = fd.get("id");
    await saveProfile(data);
    overlay.style.display = "none";
    showToast(isEdit ? "Profile updated" : "Profile created", "success");
  };
}

// ── Events ───────────────────────────────────────────────────────
function bindEvents(container) {
  container.addEventListener("click", async (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;

    // Profile list actions
    const action = btn.dataset.action;
    const id = btn.dataset.id;
    if (action === "run" && id) {
      await runExtraction(id);
    } else if (action === "edit" && id) {
      const p = profiles.find((x) => x.id === id);
      showModal(p);
    } else if (action === "delete" && id) {
      if (confirm("Delete this profile?")) await deleteProfile(id);
    }

    // New button
    if (btn.id === "ee-btn-new") showModal();

    // Run / CSV / Edit in results view
    if (btn.id === "ee-btn-run-now" && activeProfileId) {
      await runExtraction(activeProfileId);
    }
    if (btn.id === "ee-btn-csv" && activeProfileId) {
      window.open(`${API}/profiles/${activeProfileId}/results.csv`, "_blank");
    }
    if (btn.id === "ee-btn-edit-profile" && activeProfileId) {
      const p = profiles.find((x) => x.id === activeProfileId);
      showModal(p);
    }
  });

  // Click profile item to show results
  container.addEventListener("click", (e) => {
    const item = e.target.closest(".ee-profile-item");
    if (item) {
      const id = item.dataset.id;
      if (id) showResults(id);
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
.ee-layout { display: flex; gap: 16px; height: 100%; }
.ee-sidebar { width: 280px; flex-shrink: 0; border-right: 1px solid var(--border, #ddd); overflow-y: auto; padding-right: 8px; }
.ee-sidebar-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
.ee-sidebar-header h3 { margin: 0; }
.ee-profile-list { display: flex; flex-direction: column; gap: 4px; }
.ee-profile-item { padding: 8px; border-radius: 6px; cursor: pointer; border: 1px solid transparent; }
.ee-profile-item:hover { background: var(--hover, #f5f5f5); }
.ee-profile-item.active { border-color: var(--primary, #4a90d9); background: var(--active-bg, #e8f0fe); }
.ee-profile-name { font-weight: 600; }
.ee-profile-meta { font-size: 0.8em; color: var(--muted, #888); }
.ee-profile-actions { display: flex; gap: 4px; margin-top: 4px; }
.ee-profile-actions .btn-icon { background: none; border: none; cursor: pointer; font-size: 1.1em; padding: 2px 6px; border-radius: 4px; }
.ee-profile-actions .btn-icon:hover { background: var(--hover, #eee); }
.ee-main { flex: 1; overflow-y: auto; }
.ee-placeholder { color: var(--muted, #999); text-align: center; padding-top: 60px; }
.ee-loading { text-align: center; padding-top: 60px; color: var(--muted, #999); }
.ee-empty { color: var(--muted, #999); padding: 12px; font-style: italic; }
.ee-result-header { margin-bottom: 16px; }
.ee-result-header h3 { margin: 0 0 8px 0; }
.ee-result-actions { display: flex; gap: 8px; margin-bottom: 8px; }
.ee-summary { color: var(--muted, #666); font-style: italic; }
.ee-meta { font-size: 0.8em; color: var(--muted, #999); }
.ee-table-wrap { overflow-x: auto; }
.ee-table { width: 100%; border-collapse: collapse; }
.ee-table th, .ee-table td { border: 1px solid var(--border, #ddd); padding: 6px 10px; text-align: left; font-size: 0.9em; }
.ee-table th { background: var(--th-bg, #f5f5f5); font-weight: 600; }
.ee-table tr:hover { background: var(--hover, #fafafa); }
.ee-no-results { color: var(--muted, #999); }
.ee-modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.4); display: flex; align-items: center; justify-content: center; z-index: 1000; }
.ee-modal { background: var(--bg, #fff); border-radius: 8px; padding: 24px; width: 560px; max-width: 90vw; max-height: 85vh; overflow-y: auto; }
.ee-modal h3 { margin: 0 0 16px 0; }
.ee-modal label { display: block; margin-bottom: 12px; font-weight: 500; }
.ee-modal input, .ee-modal textarea { width: 100%; padding: 8px; border: 1px solid var(--border, #ddd); border-radius: 4px; font-size: 0.9em; }
.ee-modal textarea { font-family: monospace; resize: vertical; }
.ee-checkbox { display: flex !important; align-items: center; gap: 8px; }
.ee-checkbox input { width: auto; }
.ee-modal-buttons { display: flex; gap: 8px; justify-content: flex-end; margin-top: 16px; }
`;

if (typeof document !== "undefined") {
  const style = document.createElement("style");
  style.textContent = CSS;
  style.id = "ee-styles";
  document.head.appendChild(style);
}

// ── Auto-wire sidebar entry ─────────────────────────────────────
if (typeof document !== "undefined") {
  document.addEventListener("DOMContentLoaded", () => {
    const btn = document.getElementById("email-extraction-section-title");
    if (!btn) return;
    btn.addEventListener("click", async () => {
      const { initEmailExtraction } = await import("./emailExtraction.js");
      // Find or create a content panel
      let panel = document.getElementById("ee-content-panel");
      if (!panel) {
        panel = document.createElement("div");
        panel.id = "ee-content-panel";
        panel.style.cssText = "position:fixed;inset:0;z-index:900;background:var(--bg,#fff);overflow-y:auto;padding:24px;display:none";
        // Close button
        const close = document.createElement("button");
        close.textContent = "×";
        close.style.cssText = "position:absolute;top:12px;right:16px;background:none;border:none;font-size:24px;cursor:pointer;color:var(--muted,#999)";
        close.addEventListener("click", () => { panel.style.display = "none"; });
        panel.appendChild(close);
        // Content wrapper
        const wrap = document.createElement("div");
        wrap.id = "ee-content-wrap";
        panel.appendChild(wrap);
        document.body.appendChild(panel);
      }
      panel.style.display = "block";
      initEmailExtraction(document.getElementById("ee-content-wrap"));
    });
  });
}
