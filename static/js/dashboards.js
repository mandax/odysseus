/**
 * Dashboards — user-defined blocks that digest email/calendar/etc. data
 * through an LLM prompt into a table, on a refresh schedule.
 *
 * Follows the Tasks-modal pattern: one floating window whose body swaps
 * between a grid view and a block-editor view (no stacked modals).
 */

import uiModule from './ui.js';
import { makeWindowDraggable } from './windowDrag.js';

const API = '/api/dashboards';

async function apiFetch(url, opts = {}) {
  const res = await fetch(url, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
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
let _open = false;
let _escHandler = null;
let _view = 'grid';            // 'grid' | 'editor'
let dashboards = [];
let activeDashboardId = null;
let blocks = [];
let sourceRegistry = [];
let modelItems = null;

export function isDashboardsOpen() { return _open; }

// ── Window lifecycle ────────────────────────────────────────────
export function openDashboards() {
  if (_open) return;
  _open = true;
  _view = 'grid';

  const modal = document.createElement('div');
  modal.className = 'modal';
  modal.id = 'dashboards-modal';
  modal.innerHTML = `
    <div class="modal-content dashboards-modal-content">
      <div class="modal-header">
        <h4 style="margin:0;margin-right:auto;"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:6px"><rect x="3" y="3" width="18" height="18" rx="2"/><line x1="3" y1="9" x2="21" y2="9"/><line x1="9" y1="21" x2="9" y2="9"/></svg>Dashboards</h4>
        <button class="close-btn" id="dashboards-close" aria-label="Close dashboards">✖</button>
      </div>
      <div class="modal-body"></div>
    </div>
  `;
  document.body.appendChild(modal);

  const content = modal.querySelector('.modal-content');
  const header = modal.querySelector('.modal-header');
  if (content && header) makeWindowDraggable(modal, { content, header });

  document.getElementById('dashboards-close').addEventListener('click', closeDashboards);
  modal.addEventListener('click', (e) => {
    if (uiModule.isTouchInsideModal()) return;
    if (e.target === modal) closeDashboards();
  });
  _escHandler = (e) => {
    if (e.key !== 'Escape') return;
    if (_view === 'editor') { renderGrid(); return; }
    closeDashboards();
  };
  document.addEventListener('keydown', _escHandler);

  _load();
}

export function closeDashboards() {
  if (!_open) return;
  _open = false;
  const modal = document.getElementById('dashboards-modal');
  if (modal) {
    const content = modal.querySelector('.modal-content');
    if (content) {
      content.classList.add('modal-closing');
      content.addEventListener('animationend', () => modal.remove(), { once: true });
      setTimeout(() => { if (modal.parentElement) modal.remove(); }, 250);
    } else {
      modal.remove();
    }
  }
  if (_escHandler) {
    document.removeEventListener('keydown', _escHandler);
    _escHandler = null;
  }
}

function _body() { return document.querySelector('#dashboards-modal .modal-body'); }

async function _load() {
  const body = _body();
  if (body) body.innerHTML = '<div class="dash-tile-empty">Loading…</div>';
  await loadSourceRegistry();
  await loadDashboards();
}

// ── Data ─────────────────────────────────────────────────────────
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
    if (!activeDashboardId || !dashboards.find((d) => d.id === activeDashboardId)) {
      activeDashboardId = dashboards.length ? dashboards[0].id : null;
    }
    if (activeDashboardId) {
      await loadBlocks(activeDashboardId, { silent: true });
    } else {
      blocks = [];
    }
    renderGrid();
  } catch (e) {
    uiModule.showToast('Failed to load dashboards', 'error');
  }
}

async function loadBlocks(dashboardId, { silent } = {}) {
  try {
    const data = await apiFetch(`${API}/${dashboardId}/blocks`);
    blocks = data.blocks || [];
    if (!silent) renderGrid();
  } catch (e) {
    uiModule.showToast('Failed to load widgets', 'error');
  }
}

async function createDashboard() {
  const name = prompt('Dashboard name:');
  if (!name || !name.trim()) return;
  const d = await apiFetch(API, { method: 'POST', body: JSON.stringify({ name: name.trim() }) });
  activeDashboardId = d.id;
  await loadDashboards();
}

async function deleteDashboard(id) {
  if (!confirm('Delete this dashboard and all its widgets?')) return;
  await apiFetch(`${API}/${id}`, { method: 'DELETE' });
  if (activeDashboardId === id) activeDashboardId = null;
  await loadDashboards();
}

async function saveBlock(block) {
  const isNew = !block.id;
  const url = isNew ? `${API}/${activeDashboardId}/blocks` : `${API}/blocks/${block.id}`;
  await apiFetch(url, { method: isNew ? 'POST' : 'PUT', body: JSON.stringify(block) });
  await loadBlocks(activeDashboardId, { silent: true });
}

async function deleteBlock(id) {
  if (!confirm('Delete this widget?')) return;
  await apiFetch(`${API}/blocks/${id}`, { method: 'DELETE' });
  await loadBlocks(activeDashboardId);
}

async function runBlock(id) {
  const tile = document.querySelector(`.dash-tile[data-id="${id}"]`);
  if (tile) _showRunOverlay(tile, id);
  try {
    await apiFetch(`${API}/blocks/${id}/run`, { method: 'POST' });
    await loadBlocks(activeDashboardId);
    uiModule.showToast('Widget refreshed', { duration: 1500 });
  } catch (e) {
    const cancelled = /cancel/i.test(e.message || '');
    uiModule.showToast(cancelled ? 'Run cancelled' : `Run failed: ${e.message}`, cancelled ? { duration: 1500 } : 'error');
    await loadBlocks(activeDashboardId);
  }
}

async function cancelBlockRun(id) {
  try {
    await apiFetch(`${API}/blocks/${id}/cancel`, { method: 'POST' });
  } catch (e) {
    // The run may have already finished — the next loadBlocks() reconciles.
  }
}

function _showRunOverlay(tile, id) {
  if (tile.querySelector('.dash-tile-run-overlay')) return;
  const overlay = document.createElement('div');
  overlay.className = 'dash-tile-run-overlay';
  overlay.innerHTML = `<span>Running…</span><button class="memory-toolbar-btn danger" data-cancel-run>Cancel</button>`;
  overlay.querySelector('[data-cancel-run]').addEventListener('click', (e) => {
    e.stopPropagation();
    overlay.querySelector('span').textContent = 'Cancelling…';
    cancelBlockRun(id);
  });
  tile.appendChild(overlay);
}

// ── Grid view ────────────────────────────────────────────────────
function renderGrid() {
  _view = 'grid';
  const body = _body();
  if (!body) return;

  const dashOptions = dashboards
    .map((d) => `<option value="${esc(d.id)}" ${d.id === activeDashboardId ? 'selected' : ''}>${esc(d.name)}</option>`)
    .join('');

  body.innerHTML = `
    <div class="admin-card" style="flex:1;display:flex;flex-direction:column;overflow:hidden;gap:6px;">
      <div class="dash-toolbar">
        ${dashboards.length
          ? `<select id="dash-dashboard-select" class="task-form-input dash-dashboard-select" title="Switch dashboard">${dashOptions}</select>
             <button class="memory-toolbar-btn" id="dash-delete-dashboard" title="Delete this dashboard">Delete</button>`
          : ''}
        <button class="memory-toolbar-btn" id="dash-new-dashboard">+ Dashboard</button>
        <span style="flex:1"></span>
        <button class="memory-toolbar-btn active" id="dash-add-block" ${activeDashboardId ? '' : 'disabled'}>+ Add Widget</button>
      </div>
      <p class="memory-desc">Each widget pairs an LLM prompt with data sources (email, calendar, …) and refreshes on its own schedule.</p>
      <div class="dash-grid" id="dash-grid"></div>
    </div>
  `;

  body.querySelector('#dash-dashboard-select')?.addEventListener('change', async (e) => {
    activeDashboardId = e.target.value;
    await loadBlocks(activeDashboardId);
  });
  body.querySelector('#dash-new-dashboard')?.addEventListener('click', createDashboard);
  body.querySelector('#dash-delete-dashboard')?.addEventListener('click', () => {
    if (activeDashboardId) deleteDashboard(activeDashboardId);
  });
  body.querySelector('#dash-add-block')?.addEventListener('click', () => renderEditor(null));

  _renderTiles();
}

function _renderTiles() {
  const grid = document.getElementById('dash-grid');
  if (!grid) return;

  if (!activeDashboardId) {
    grid.innerHTML = '<div class="dash-tile-empty">Create a dashboard to get started.</div>';
    return;
  }
  if (!blocks.length) {
    grid.innerHTML = '<div class="dash-tile-empty">No widgets yet — click “+ Add Widget”.</div>';
    return;
  }

  grid.innerHTML = blocks.map(renderTile).join('');
  grid.querySelectorAll('[data-action]').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const id = btn.dataset.id;
      switch (btn.dataset.action) {
        case 'run': runBlock(id); break;
        case 'edit': renderEditor(blocks.find((b) => b.id === id)); break;
        case 'delete': deleteBlock(id); break;
        case 'download': window.open(`${API}/blocks/${id}/download`, '_blank'); break;
      }
    });
  });
  _installGridInteractions(grid);
}

// ── 12x12 grid drag + resize ─────────────────────────────────────
const GRID_COLS = 12;
const GRID_ROWS = 12;

function _blockById(id) { return blocks.find((b) => b.id === id); }

function _collides(x, y, w, h, exceptId) {
  for (const b of blocks) {
    if (b.id === exceptId) continue;
    const bx = b.grid_x ?? 0, by = b.grid_y ?? 0, bw = b.grid_w || 4, bh = b.grid_h || 4;
    if (x < bx + bw && x + w > bx && y < by + bh && y + h > by) return true;
  }
  return false;
}

function _gridMetrics(grid) {
  const rect = grid.getBoundingClientRect();
  const cs = getComputedStyle(grid);
  const padL = parseFloat(cs.paddingLeft) || 0;
  const padT = parseFloat(cs.paddingTop) || 0;
  const padR = parseFloat(cs.paddingRight) || 0;
  const padB = parseFloat(cs.paddingBottom) || 0;
  const gap = parseFloat(cs.columnGap || cs.gap) || 0;
  const rowGap = parseFloat(cs.rowGap || cs.gap) || 0;
  const innerW = rect.width - padL - padR;
  const innerH = rect.height - padT - padB;
  const stepX = (innerW + gap) / GRID_COLS;
  const stepY = (innerH + rowGap) / GRID_ROWS;
  return { rect, padL, padT, stepX, stepY };
}

function _persistLayout() {
  if (!activeDashboardId) return;
  const payload = { blocks: blocks.map((b) => ({
    id: b.id, grid_x: b.grid_x ?? 0, grid_y: b.grid_y ?? 0, grid_w: b.grid_w || 4, grid_h: b.grid_h || 4,
  })) };
  apiFetch(`${API}/${activeDashboardId}/layout`, { method: 'PUT', body: JSON.stringify(payload) })
    .catch(() => uiModule.showToast('Failed to save layout', 'error'));
}

function _makePlaceholder(grid) {
  const ph = document.createElement('div');
  ph.className = 'dash-grid-placeholder';
  grid.appendChild(ph);
  return ph;
}

function _placeEl(el, x, y, w, h) {
  el.style.gridColumn = `${x + 1} / span ${w}`;
  el.style.gridRow = `${y + 1} / span ${h}`;
}

function _installGridInteractions(grid) {
  grid.querySelectorAll('.dash-tile').forEach((tile) => {
    const id = tile.dataset.id;
    const header = tile.querySelector('.dash-tile-header');
    const handle = tile.querySelector('.dash-tile-resize');
    if (header) header.addEventListener('pointerdown', (e) => _startDrag(e, grid, tile, id));
    if (handle) handle.addEventListener('pointerdown', (e) => _startResize(e, grid, tile, id));
  });
}

function _startDrag(e, grid, tile, id) {
  if (e.button != null && e.button !== 0) return;
  if (e.target.closest('[data-action]')) return; // let action buttons work
  const block = _blockById(id);
  if (!block) return;
  e.preventDefault();

  const m = _gridMetrics(grid);
  const tileRect = tile.getBoundingClientRect();
  const grabX = e.clientX - tileRect.left;
  const grabY = e.clientY - tileRect.top;
  const w = block.grid_w || 4, h = block.grid_h || 4;

  let targetX = block.grid_x ?? 0, targetY = block.grid_y ?? 0;
  const ph = _makePlaceholder(grid);
  _placeEl(ph, targetX, targetY, w, h);

  // Lift the tile out to follow the cursor.
  tile.classList.add('dash-dragging');
  tile.style.position = 'fixed';
  tile.style.width = `${tileRect.width}px`;
  tile.style.height = `${tileRect.height}px`;
  tile.style.left = `${tileRect.left}px`;
  tile.style.top = `${tileRect.top}px`;

  const onMove = (ev) => {
    tile.style.left = `${ev.clientX - grabX}px`;
    tile.style.top = `${ev.clientY - grabY}px`;
    const cx = Math.round((ev.clientX - grabX - m.rect.left - m.padL) / m.stepX);
    const cy = Math.round((ev.clientY - grabY - m.rect.top - m.padT) / m.stepY);
    const nx = Math.max(0, Math.min(cx, GRID_COLS - w));
    const ny = Math.max(0, Math.min(cy, GRID_ROWS - h));
    if (!_collides(nx, ny, w, h, id)) { targetX = nx; targetY = ny; _placeEl(ph, targetX, targetY, w, h); }
  };
  const onUp = () => {
    document.removeEventListener('pointermove', onMove);
    document.removeEventListener('pointerup', onUp);
    ph.remove();
    tile.classList.remove('dash-dragging');
    tile.style.position = tile.style.width = tile.style.height = tile.style.left = tile.style.top = '';
    const changed = block.grid_x !== targetX || block.grid_y !== targetY;
    block.grid_x = targetX; block.grid_y = targetY;
    _placeEl(tile, targetX, targetY, w, h);
    if (changed) _persistLayout();
  };
  document.addEventListener('pointermove', onMove);
  document.addEventListener('pointerup', onUp);
}

function _startResize(e, grid, tile, id) {
  if (e.button != null && e.button !== 0) return;
  const block = _blockById(id);
  if (!block) return;
  e.preventDefault();
  e.stopPropagation();

  const m = _gridMetrics(grid);
  const x = block.grid_x ?? 0, y = block.grid_y ?? 0;
  const startX = e.clientX, startY = e.clientY;
  const startW = block.grid_w || 4, startH = block.grid_h || 4;

  let targetW = startW, targetH = startH;
  const ph = _makePlaceholder(grid);
  _placeEl(ph, x, y, targetW, targetH);
  tile.classList.add('dash-resizing');

  const onMove = (ev) => {
    const dw = Math.round((ev.clientX - startX) / m.stepX);
    const dh = Math.round((ev.clientY - startY) / m.stepY);
    const nw = Math.max(1, Math.min(startW + dw, GRID_COLS - x));
    const nh = Math.max(1, Math.min(startH + dh, GRID_ROWS - y));
    if (!_collides(x, y, nw, nh, id)) {
      targetW = nw; targetH = nh;
      _placeEl(ph, x, y, targetW, targetH);
      _placeEl(tile, x, y, targetW, targetH);
    }
  };
  const onUp = () => {
    document.removeEventListener('pointermove', onMove);
    document.removeEventListener('pointerup', onUp);
    ph.remove();
    tile.classList.remove('dash-resizing');
    const changed = block.grid_w !== targetW || block.grid_h !== targetH;
    block.grid_w = targetW; block.grid_h = targetH;
    _placeEl(tile, x, y, targetW, targetH);
    if (changed) _persistLayout();
  };
  document.addEventListener('pointermove', onMove);
  document.addEventListener('pointerup', onUp);
}

function _iconBtn(action, id, title, svg) {
  return `<button class="memory-item-btn" data-action="${action}" data-id="${id}" title="${title}">${svg}</button>`;
}

function renderTile(b) {
  const pills = (b.sources || [])
    .map((sid) => sourceRegistry.find((s) => s.id === sid)?.label || sid)
    .map((l) => `<span class="dash-source-pill">${esc(l)}</span>`)
    .join('');

  let tableBody;
  if (b.last_columns && b.last_columns.length) {
    const thead = `<thead><tr>${b.last_columns.map((c) => `<th>${esc(c)}</th>`).join('')}</tr></thead>`;
    const tbody = `<tbody>${(b.last_rows || [])
      .map((row) => `<tr>${b.last_columns.map((c) => { const v = String(row[c] ?? ''); return `<td title="${esc(v)}">${esc(v)}</td>`; }).join('')}</tr>`)
      .join('')}</tbody>`;
    tableBody = `<div class="dash-tile-table-wrap"><table class="dash-tile-table">${thead}${tbody}</table></div>`;
  } else {
    tableBody = `<div class="dash-tile-empty">${b.last_run_at ? 'No rows extracted.' : 'Not run yet.'}</div>`;
  }

  const meta = pills + (b.last_run_at
    ? `<span class="dash-tile-updated">updated ${fmtDate(b.last_run_at)}</span>`
    : `<span class="dash-tile-updated">refresh: ${esc(b.refresh_interval)}</span>`);

  const x = (b.grid_x ?? 0) + 1, y = (b.grid_y ?? 0) + 1;
  const w = b.grid_w || 4, h = b.grid_h || 4;
  const place = `grid-column:${x} / span ${w};grid-row:${y} / span ${h};`;

  return `
    <div class="dash-tile" data-id="${b.id}" style="${place}">
      <div class="dash-tile-header">
        <div style="min-width:0;">
          <div class="dash-tile-title">${esc(b.title)}</div>
          <div class="dash-tile-meta">${meta}</div>
        </div>
        <div class="dash-tile-actions">
          ${_iconBtn('run', b.id, 'Run now', '<svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor" stroke="none"><polygon points="6 4 20 12 6 20 6 4"/></svg>')}
          ${_iconBtn('download', b.id, 'Download', '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>')}
          ${_iconBtn('edit', b.id, 'Edit', '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.12 2.12 0 0 1 3 3L12 15l-4 1 1-4z"/></svg>')}
          ${_iconBtn('delete', b.id, 'Delete', '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/></svg>')}
        </div>
      </div>
      ${b.last_summary ? `<div class="dash-tile-summary">${esc(b.last_summary)}</div>` : ''}
      ${tableBody}
      <div class="dash-tile-resize" title="Resize"></div>
    </div>`;
}

// ── Model picker data ─────────────────────────────────────────────
async function loadModels() {
  if (modelItems) return modelItems;
  try {
    const res = await fetch('/api/models?background=false', { credentials: 'same-origin' });
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
    const cat = item.category === 'local' ? 'local' : 'api';
    const ep = item.endpoint_name || 'Unknown';
    const offline = item.offline ? ' (offline)' : '';
    // Endpoints expose curated models (`models`) plus everything else in
    // `models_extra`. Remote providers like DeepSeek often surface their
    // actual model IDs only in `models_extra`, so include both — matching
    // how models.js builds the main picker.
    const curated = item.models || [];
    const curatedDisplay = item.models_display || curated;
    const extra = item.models_extra || [];
    const extraDisplay = item.models_extra_display || extra;
    curated.forEach((mid, i) => {
      groups[cat].push({ url: item.url, mid, label: `${ep} — ${curatedDisplay[i] || mid}${offline}` });
    });
    extra.forEach((mid, i) => {
      groups[cat].push({ url: item.url, mid, label: `${ep} — ${extraDisplay[i] || mid}${offline}` });
    });
  }
  const optGroup = (label, list) => {
    if (!list.length) return '';
    const opts = list.map((m) => {
      const val = `${m.url}|||${m.mid}`;
      const sel = m.url === selectedUrl && m.mid === selectedModel ? 'selected' : '';
      return `<option value="${esc(val)}" ${sel}>${esc(m.label)}</option>`;
    }).join('');
    return `<optgroup label="${label}">${opts}</optgroup>`;
  };
  return `<option value="">Use background-task default</option>${optGroup('Remote / API', groups.api)}${optGroup('Local', groups.local)}`;
}

// ── Editor view (swaps into the same window body) ─────────────────
async function renderEditor(block) {
  _view = 'editor';
  const isEdit = !!block;
  const body = _body();
  if (!body) return;
  body.innerHTML = '<div class="dash-tile-empty">Loading…</div>';

  const items = await loadModels();
  if (_view !== 'editor') return; // user navigated away while models loaded
  const sourcesConf = block?.source_config || {};
  const activeSources = new Set(block?.sources || []);
  const curRefresh = block?.refresh_interval || 'manual';

  body.innerHTML = `
    <div class="dash-editor admin-card">
      <button class="memory-toolbar-btn dash-editor-back" id="dash-editor-back"><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-1px;margin-right:3px;"><polyline points="15 18 9 12 15 6"/></svg>Back</button>
      <form id="dash-block-form">
        <label class="task-form-label">Title</label>
        <input class="task-form-input" name="title" value="${esc(block?.title || '')}" placeholder="e.g. Invoice tracker" required>

        <label class="task-form-label">Prompt <span style="opacity:0.6;font-weight:normal;">(what to extract, and the columns you want)</span></label>
        <textarea class="task-form-input task-form-textarea" name="prompt" rows="4" required placeholder="e.g. List every invoice, with columns Date, Vendor, Amount, Due Date.">${esc(block?.prompt || '')}</textarea>

        <label class="task-form-label">Data sources</label>
        <div class="dash-source-toggles" id="dash-source-toggles">
          ${sourceRegistry.map((s) => `<button type="button" class="task-toggle-btn ${activeSources.has(s.id) ? 'active' : ''}" data-source-id="${s.id}">${esc(s.label)}</button>`).join('')}
        </div>
        <div id="dash-source-configs"></div>

        <label class="task-form-label">Model <span style="opacity:0.6;font-weight:normal;">(optional — overrides the background-task default)</span></label>
        <select class="task-form-input" name="model_select">${modelOptionsHtml(items, block?.model_endpoint_url, block?.model)}</select>

        <label class="task-form-label">Refresh</label>
        <div class="task-form-toggle" id="dash-refresh-toggle">
          ${['manual', 'hourly', 'daily', 'weekly'].map((v) => `<button type="button" class="task-toggle-btn ${curRefresh === v ? 'active' : ''}" data-val="${v}">${v[0].toUpperCase() + v.slice(1)}</button>`).join('')}
        </div>
        <input type="hidden" name="refresh_interval" value="${curRefresh}">

        <div class="task-form-actions">
          <button type="button" class="memory-toolbar-btn" id="dash-editor-cancel">Cancel</button>
          <button type="submit" class="memory-toolbar-btn active">${isEdit ? 'Save' : 'Create'}</button>
        </div>
      </form>
    </div>
  `;

  const configsEl = body.querySelector('#dash-source-configs');
  const renderSourceConfig = (sourceId) => {
    const entry = sourceRegistry.find((s) => s.id === sourceId);
    if (!entry) return '';
    const cfg = sourcesConf[sourceId] || {};
    const rows = (entry.config_schema || []).map((f) => {
      const val = cfg[f.key] ?? f.default ?? '';
      return `<div class="dash-source-config-row">
        <label>${esc(f.label)}</label>
        <input class="task-form-input" type="${f.type === 'number' ? 'number' : 'text'}" data-source="${sourceId}" data-key="${f.key}" value="${esc(val)}" placeholder="${esc(f.placeholder || '')}">
      </div>`;
    }).join('');
    return `<div class="dash-source-config"><div class="dash-source-config-title">${esc(entry.label)} settings</div>${rows}</div>`;
  };
  const syncConfigs = () => {
    const active = [...body.querySelectorAll('#dash-source-toggles .task-toggle-btn.active')].map((b) => b.dataset.sourceId);
    configsEl.innerHTML = active.map(renderSourceConfig).join('');
  };
  syncConfigs();

  body.querySelectorAll('#dash-source-toggles .task-toggle-btn').forEach((btn) => {
    btn.addEventListener('click', () => { btn.classList.toggle('active'); syncConfigs(); });
  });

  const refreshHidden = body.querySelector('input[name="refresh_interval"]');
  body.querySelector('#dash-refresh-toggle').addEventListener('click', (e) => {
    const btn = e.target.closest('.task-toggle-btn');
    if (!btn) return;
    body.querySelectorAll('#dash-refresh-toggle .task-toggle-btn').forEach((b) => b.classList.toggle('active', b === btn));
    refreshHidden.value = btn.dataset.val;
  });

  body.querySelector('#dash-editor-back').addEventListener('click', renderGrid);
  body.querySelector('#dash-editor-cancel').addEventListener('click', renderGrid);

  body.querySelector('#dash-block-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);

    const sources = [];
    const source_config = {};
    body.querySelectorAll('#dash-source-toggles .task-toggle-btn.active').forEach((btn) => {
      const sid = btn.dataset.sourceId;
      sources.push(sid);
      const cfg = {};
      body.querySelectorAll(`[data-source="${sid}"]`).forEach((input) => {
        if (input.value !== '') cfg[input.dataset.key] = input.value;
      });
      source_config[sid] = cfg;
    });

    const modelVal = fd.get('model_select') || '';
    const [model_endpoint_url, model] = modelVal ? modelVal.split('|||') : [null, null];

    const data = {
      title: fd.get('title'),
      prompt: fd.get('prompt'),
      sources,
      source_config,
      model_endpoint_url,
      model,
      refresh_interval: fd.get('refresh_interval') || 'manual',
    };
    if (block?.id) data.id = block.id;

    try {
      await saveBlock(data);
      uiModule.showToast(isEdit ? 'Widget updated' : 'Widget created', { duration: 1500 });
      renderGrid();
    } catch (err) {
      uiModule.showToast(`Save failed: ${err.message}`, 'error');
    }
  });
}

// ── Helpers ──────────────────────────────────────────────────────
function esc(s) {
  if (s == null) return '';
  const d = document.createElement('div');
  d.textContent = String(s);
  return d.innerHTML;
}

function fmtDate(iso) {
  if (!iso) return '';
  try { return new Date(iso).toLocaleString(); } catch { return iso; }
}

const dashboardsModule = { openDashboards, closeDashboards, isDashboardsOpen };
export default dashboardsModule;
window.dashboardsModule = dashboardsModule;
