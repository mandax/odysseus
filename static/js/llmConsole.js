/**
 * LLM Console — a right-docked side panel showing live LLM API traffic.
 *
 * Connects to /api/llm-console/stream (SSE) and renders every outgoing chat and
 * background call as a row: model, endpoint, status, duration. Click a row to
 * expand the request messages and the raw response/error body — handy for
 * seeing exactly what a provider returned (e.g. a 401 auth body).
 */

import uiModule from './ui.js';

const API = '/api/llm-console';

let _open = false;
let _es = null;              // EventSource
let _escHandler = null;
let _seen = new Set();       // exchange ids already rendered
let _autoScroll = true;

export function isLlmConsoleOpen() { return _open; }

export function openLlmConsole() {
  if (_open) return;
  _open = true;
  _seen = new Set();
  _autoScroll = true;

  const panel = document.createElement('div');
  panel.className = 'llm-console-panel';
  panel.id = 'llm-console-panel';
  panel.innerHTML = `
    <div class="llm-console-header">
      <span class="llm-console-title">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:6px"><rect x="2" y="4" width="20" height="16" rx="2"/><path d="M7 9l3 3-3 3"/><line x1="13" y1="15" x2="17" y2="15"/></svg>
        LLM Console
      </span>
      <span class="llm-console-status" id="llm-console-status" title="Live connection">●</span>
      <span style="flex:1"></span>
      <button class="memory-toolbar-btn" id="llm-console-clear" title="Clear the log">Clear</button>
      <button class="close-btn" id="llm-console-close" aria-label="Close LLM console">✖</button>
    </div>
    <div class="llm-console-body" id="llm-console-body">
      <div class="llm-console-empty" id="llm-console-empty">Waiting for LLM traffic…</div>
    </div>
    <div class="llm-console-foot">
      <label class="llm-console-follow"><input type="checkbox" id="llm-console-follow" checked> Follow</label>
      <span class="llm-console-hint">chat &amp; background calls · newest at bottom</span>
    </div>
  `;
  document.body.appendChild(panel);

  document.getElementById('llm-console-close').addEventListener('click', closeLlmConsole);
  document.getElementById('llm-console-clear').addEventListener('click', clearConsole);
  const follow = document.getElementById('llm-console-follow');
  follow.addEventListener('change', () => { _autoScroll = follow.checked; if (_autoScroll) _scrollToEnd(); });

  const body = document.getElementById('llm-console-body');
  body.addEventListener('scroll', () => {
    const nearEnd = body.scrollHeight - body.scrollTop - body.clientHeight < 40;
    if (nearEnd !== _autoScroll) {
      _autoScroll = nearEnd;
      follow.checked = nearEnd;
    }
  });

  _escHandler = (e) => { if (e.key === 'Escape') closeLlmConsole(); };
  document.addEventListener('keydown', _escHandler);

  _connect();
}

export function closeLlmConsole() {
  if (!_open) return;
  _open = false;
  _disconnect();
  const panel = document.getElementById('llm-console-panel');
  if (panel) panel.remove();
  if (_escHandler) { document.removeEventListener('keydown', _escHandler); _escHandler = null; }
}

function _setStatus(state) {
  const el = document.getElementById('llm-console-status');
  if (!el) return;
  el.classList.remove('live', 'down');
  el.classList.add(state === 'live' ? 'live' : 'down');
  el.title = state === 'live' ? 'Live' : 'Reconnecting…';
}

function _connect() {
  _disconnect();
  try {
    _es = new EventSource(`${API}/stream`);
  } catch (e) {
    _setStatus('down');
    return;
  }
  _es.onopen = () => _setStatus('live');
  _es.onerror = () => _setStatus('down'); // EventSource auto-reconnects
  _es.onmessage = (ev) => {
    if (!ev.data) return;
    let ex;
    try { ex = JSON.parse(ev.data); } catch { return; }
    _addExchange(ex);
  };
}

function _disconnect() {
  if (_es) { try { _es.close(); } catch {} _es = null; }
}

async function clearConsole() {
  try { await fetch(`${API}/clear`, { method: 'POST', credentials: 'same-origin' }); } catch {}
  _seen = new Set();
  const body = document.getElementById('llm-console-body');
  if (body) body.innerHTML = '<div class="llm-console-empty" id="llm-console-empty">Cleared. Waiting for LLM traffic…</div>';
}

function _addExchange(ex) {
  if (!ex || ex.id == null || _seen.has(ex.id)) return;
  _seen.add(ex.id);
  const body = document.getElementById('llm-console-body');
  if (!body) return;
  const empty = document.getElementById('llm-console-empty');
  if (empty) empty.remove();

  body.appendChild(_renderRow(ex));
  // Cap DOM rows so a long session doesn't grow unbounded.
  while (body.children.length > 400) body.removeChild(body.firstChild);
  if (_autoScroll) _scrollToEnd();
}

function _renderRow(ex) {
  const row = document.createElement('div');
  const failed = ex.error || (ex.status && ex.status >= 400) || (!ex.ok && ex.status != null);
  row.className = 'llm-console-row' + (failed ? ' failed' : '');

  const statusLabel = ex.error ? 'ERR' : (ex.status != null ? String(ex.status) : (ex.ok ? 'ok' : '—'));
  const dur = ex.duration_ms != null ? `${ex.duration_ms} ms` : '';
  const host = _host(ex.url);

  const head = document.createElement('div');
  head.className = 'llm-console-row-head';
  head.innerHTML = `
    <span class="llm-console-time">${_time(ex.ts)}</span>
    <span class="llm-console-badge ${ex.kind === 'stream' ? 'stream' : 'call'}">${ex.kind === 'stream' ? 'chat' : (ex.workload || 'call')}</span>
    <span class="llm-console-model" title="${esc(ex.model)} @ ${esc(ex.url)}">${esc(ex.model || '—')}</span>
    <span class="llm-console-host">${esc(host)}</span>
    <span class="llm-console-status-code ${failed ? 'bad' : 'good'}">${esc(statusLabel)}</span>
    <span class="llm-console-dur">${esc(dur)}</span>
  `;
  row.appendChild(head);

  const detail = document.createElement('div');
  detail.className = 'llm-console-detail';
  detail.style.display = 'none';
  detail.appendChild(_renderDetail(ex));
  row.appendChild(detail);

  head.addEventListener('click', () => {
    const showing = detail.style.display !== 'none';
    detail.style.display = showing ? 'none' : 'block';
    row.classList.toggle('expanded', !showing);
  });
  return row;
}

function _renderDetail(ex) {
  const wrap = document.createElement('div');
  const req = (ex.request || []).map((m) =>
    `<div class="llm-console-msg"><span class="llm-console-role">${esc(m.role)}</span><pre>${esc(m.content)}</pre></div>`
  ).join('');
  const respLabel = ex.error ? 'Error' : 'Response';
  const respBody = ex.error || ex.response || '(empty)';
  wrap.innerHTML = `
    <div class="llm-console-detail-sec">
      <div class="llm-console-detail-label">Endpoint</div>
      <pre class="llm-console-endpoint">${esc(ex.url || '—')}</pre>
    </div>
    <div class="llm-console-detail-sec">
      <div class="llm-console-detail-label">Request (${(ex.request || []).length} msg)</div>
      ${req || '<div class="llm-console-msg"><pre>(none)</pre></div>'}
    </div>
    <div class="llm-console-detail-sec">
      <div class="llm-console-detail-label">${respLabel}</div>
      <pre class="llm-console-resp ${ex.error ? 'err' : ''}">${esc(respBody)}</pre>
    </div>
  `;
  return wrap;
}

function _scrollToEnd() {
  const body = document.getElementById('llm-console-body');
  if (body) body.scrollTop = body.scrollHeight;
}

// ── Helpers ─────────────────────────────────────────────────────────
function _host(url) {
  if (!url) return '';
  try { return new URL(url).host; } catch { return String(url).replace(/^https?:\/\//, '').split('/')[0]; }
}
function _time(iso) {
  if (!iso) return '';
  try { return new Date(iso).toLocaleTimeString(); } catch { return ''; }
}
function esc(s) {
  if (s == null) return '';
  const d = document.createElement('div');
  d.textContent = String(s);
  return d.innerHTML;
}

const llmConsoleModule = { openLlmConsole, closeLlmConsole, isLlmConsoleOpen };
export default llmConsoleModule;
window.llmConsoleModule = llmConsoleModule;
