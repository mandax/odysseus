"""Dashboards — user-defined blocks that digest email/calendar/etc. data
through an LLM prompt into a table, on a refresh schedule.

Each block owns its own ScheduledTask (task_type="action",
action="run_dashboard_block", prompt=block_id) rather than going through the
generic Tasks modal — a block's identity IS its schedule, so there's no
sensible way to create one without picking a block first. See
src/task_action_policy.py:INTERNAL_TASK_ACTIONS for how it's hidden from the
generic action list.
"""

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from core.database import SessionLocal, Dashboard, DashboardBlock, ScheduledTask
from src.auth_helpers import get_current_user
from src.dashboard_sources import SOURCE_REGISTRY, gather_block_data

logger = logging.getLogger(__name__)

_REFRESH_CRON = {
    "hourly": "0 * * * *",
    "daily": "0 6 * * *",
    "weekly": "0 6 * * 1",
}

# Fixed dashboard grid. Blocks are placed by top-left cell (grid_x, grid_y)
# and span grid_w x grid_h cells. Kept in sync with the CSS grid in
# static/js/dashboards.js.
GRID_COLS = 12
GRID_ROWS = 12
DEFAULT_BLOCK_W = 4
DEFAULT_BLOCK_H = 4

# In-flight "Run now" tasks, keyed by block id, so the UI's Cancel button has
# something to cancel. Scheduled (cron) runs aren't tracked here — there's no
# UI affordance to cancel those, and letting a background refresh finish is
# the safer default.
_RUNNING_RUNS: dict[str, asyncio.Task] = {}

# "Run now" is synchronous, so its LLM budget has to fit inside the global
# request budget (app.py REQUEST_HARD_TIMEOUT, default 60s). Otherwise a slow
# run returns a bare 504 at the middleware while the server keeps working on an
# abandoned request; sized just under it, the caller gets a real "LLM call timed
# out" instead. Scheduled (cron) runs don't go through HTTP and aren't capped here.
_REQUEST_BUDGET = float(os.getenv("REQUEST_HARD_TIMEOUT", "60"))
_RUN_LLM_TIMEOUT = max(20.0, _REQUEST_BUDGET - 10)


# ── Pydantic schemas ────────────────────────────────────────────────────

class DashboardCreate(BaseModel):
    name: str


class DashboardUpdate(BaseModel):
    name: str | None = None
    sort_order: int | None = None


class BlockCreate(BaseModel):
    title: str
    prompt: str
    sources: list[str] = Field(default_factory=list)
    source_config: dict = Field(default_factory=dict)
    model_endpoint_id: str | None = None
    model_endpoint_url: str | None = None
    model: str | None = None
    refresh_interval: str = "manual"


class BlockLayoutItem(BaseModel):
    id: str
    grid_x: int = Field(ge=0, le=GRID_COLS - 1)
    grid_y: int = Field(ge=0, le=GRID_ROWS - 1)
    grid_w: int = Field(ge=1, le=GRID_COLS)
    grid_h: int = Field(ge=1, le=GRID_ROWS)


class LayoutUpdate(BaseModel):
    blocks: list[BlockLayoutItem]


class BlockUpdate(BaseModel):
    title: str | None = None
    prompt: str | None = None
    sources: list[str] | None = None
    source_config: dict | None = None
    model_endpoint_id: str | None = None
    model_endpoint_url: str | None = None
    model: str | None = None
    refresh_interval: str | None = None


# ── Helpers ──────────────────────────────────────────────────────────────

def _owner(request: Request) -> str:
    return get_current_user(request) or ""


def _dashboard_to_dict(d: Dashboard) -> dict:
    return {
        "id": d.id,
        "name": d.name,
        "sort_order": d.sort_order,
        "created_at": d.created_at.isoformat() if d.created_at else None,
    }


def _block_to_dict(b: DashboardBlock) -> dict:
    return {
        "id": b.id,
        "dashboard_id": b.dashboard_id,
        "title": b.title,
        "prompt": b.prompt,
        "sources": b.sources or [],
        "source_config": b.source_config or {},
        "model_endpoint_id": b.model_endpoint_id,
        "model_endpoint_url": b.model_endpoint_url,
        "model": b.model,
        "refresh_interval": b.refresh_interval,
        "task_id": b.task_id,
        "sort_order": b.sort_order,
        "grid_x": b.grid_x if b.grid_x is not None else 0,
        "grid_y": b.grid_y if b.grid_y is not None else 0,
        "grid_w": b.grid_w or DEFAULT_BLOCK_W,
        "grid_h": b.grid_h or DEFAULT_BLOCK_H,
        "last_run_at": b.last_run_at.isoformat() if b.last_run_at else None,
        "last_columns": b.last_columns or [],
        "last_rows": b.last_rows or [],
        "last_summary": b.last_summary,
    }


# ── Router ───────────────────────────────────────────────────────────────

def setup_dashboard_routes():
    router = APIRouter(prefix="/api/dashboards", tags=["dashboards"])

    @router.get("/meta/sources")
    async def list_sources(request: Request):
        """Describe available block data sources for the block editor UI."""
        return {
            "sources": [
                {"id": sid, "label": entry["label"], "config_schema": entry["config_schema"]}
                for sid, entry in SOURCE_REGISTRY.items()
            ]
        }

    @router.get("")
    async def list_dashboards(request: Request):
        owner = _owner(request)
        db = SessionLocal()
        try:
            dashboards = (
                db.query(Dashboard)
                .filter(Dashboard.owner == owner)
                .order_by(Dashboard.sort_order.asc(), Dashboard.created_at.asc())
                .all()
            )
            return {"dashboards": [_dashboard_to_dict(d) for d in dashboards]}
        finally:
            db.close()

    @router.post("")
    async def create_dashboard(payload: DashboardCreate, request: Request):
        owner = _owner(request)
        db = SessionLocal()
        try:
            d = Dashboard(id=uuid.uuid4().hex[:12], owner=owner, name=payload.name.strip() or "Untitled Dashboard")
            db.add(d)
            db.commit()
            db.refresh(d)
            return _dashboard_to_dict(d)
        finally:
            db.close()

    @router.put("/{dashboard_id}")
    async def update_dashboard(dashboard_id: str, payload: DashboardUpdate, request: Request):
        owner = _owner(request)
        db = SessionLocal()
        try:
            d = db.query(Dashboard).filter(Dashboard.id == dashboard_id, Dashboard.owner == owner).first()
            if not d:
                raise HTTPException(404, "Dashboard not found")
            if payload.name is not None:
                d.name = payload.name.strip() or d.name
            if payload.sort_order is not None:
                d.sort_order = payload.sort_order
            db.commit()
            db.refresh(d)
            return _dashboard_to_dict(d)
        finally:
            db.close()

    @router.delete("/{dashboard_id}")
    async def delete_dashboard(dashboard_id: str, request: Request):
        owner = _owner(request)
        db = SessionLocal()
        try:
            d = db.query(Dashboard).filter(Dashboard.id == dashboard_id, Dashboard.owner == owner).first()
            if not d:
                raise HTTPException(404, "Dashboard not found")
            for b in list(d.blocks):
                _delete_block_task(db, b)
            db.delete(d)
            db.commit()
            return {"ok": True}
        finally:
            db.close()

    @router.get("/{dashboard_id}/blocks")
    async def list_blocks(dashboard_id: str, request: Request):
        owner = _owner(request)
        db = SessionLocal()
        try:
            d = db.query(Dashboard).filter(Dashboard.id == dashboard_id, Dashboard.owner == owner).first()
            if not d:
                raise HTTPException(404, "Dashboard not found")
            blocks = (
                db.query(DashboardBlock)
                .filter(DashboardBlock.dashboard_id == dashboard_id)
                .order_by(DashboardBlock.sort_order.asc(), DashboardBlock.created_at.asc())
                .all()
            )
            return {"blocks": [_block_to_dict(b) for b in blocks]}
        finally:
            db.close()

    @router.put("/{dashboard_id}/layout")
    async def update_layout(dashboard_id: str, payload: LayoutUpdate, request: Request):
        """Persist grid positions after a drag/resize. Only touches blocks that
        belong to this dashboard + owner; unknown ids are ignored."""
        owner = _owner(request)
        db = SessionLocal()
        try:
            d = db.query(Dashboard).filter(Dashboard.id == dashboard_id, Dashboard.owner == owner).first()
            if not d:
                raise HTTPException(404, "Dashboard not found")
            by_id = {
                b.id: b
                for b in db.query(DashboardBlock).filter(DashboardBlock.dashboard_id == dashboard_id).all()
            }
            for item in payload.blocks:
                b = by_id.get(item.id)
                if not b:
                    continue
                # Clamp spans so x+w / y+h stay on the grid.
                b.grid_w = min(item.grid_w, GRID_COLS)
                b.grid_h = min(item.grid_h, GRID_ROWS)
                b.grid_x = min(item.grid_x, GRID_COLS - b.grid_w)
                b.grid_y = min(item.grid_y, GRID_ROWS - b.grid_h)
            db.commit()
            return {"ok": True}
        finally:
            db.close()

    @router.post("/{dashboard_id}/blocks")
    async def create_block(dashboard_id: str, payload: BlockCreate, request: Request):
        owner = _owner(request)
        db = SessionLocal()
        try:
            d = db.query(Dashboard).filter(Dashboard.id == dashboard_id, Dashboard.owner == owner).first()
            if not d:
                raise HTTPException(404, "Dashboard not found")
            existing = db.query(DashboardBlock).filter(DashboardBlock.dashboard_id == dashboard_id).all()
            gx, gy = _first_free_slot(existing, DEFAULT_BLOCK_W, DEFAULT_BLOCK_H)
            block = DashboardBlock(
                id=uuid.uuid4().hex[:12],
                dashboard_id=dashboard_id,
                owner=owner,
                title=payload.title.strip(),
                prompt=payload.prompt.strip(),
                sources=payload.sources,
                source_config=payload.source_config,
                model_endpoint_id=payload.model_endpoint_id or None,
                model_endpoint_url=payload.model_endpoint_url or None,
                model=payload.model or None,
                refresh_interval=payload.refresh_interval or "manual",
                grid_x=gx,
                grid_y=gy,
                grid_w=DEFAULT_BLOCK_W,
                grid_h=DEFAULT_BLOCK_H,
            )
            db.add(block)
            db.commit()
            db.refresh(block)
            _sync_block_schedule(db, block)
            db.commit()
            db.refresh(block)
            return _block_to_dict(block)
        finally:
            db.close()

    @router.put("/blocks/{block_id}")
    async def update_block(block_id: str, payload: BlockUpdate, request: Request):
        owner = _owner(request)
        db = SessionLocal()
        try:
            block = db.query(DashboardBlock).filter(DashboardBlock.id == block_id, DashboardBlock.owner == owner).first()
            if not block:
                raise HTTPException(404, "Block not found")

            if payload.title is not None:
                block.title = payload.title.strip()
            if payload.prompt is not None:
                block.prompt = payload.prompt.strip()
            if payload.sources is not None:
                block.sources = payload.sources
            if payload.source_config is not None:
                block.source_config = payload.source_config
            if payload.model_endpoint_id is not None:
                block.model_endpoint_id = payload.model_endpoint_id or None
            if payload.model_endpoint_url is not None:
                block.model_endpoint_url = payload.model_endpoint_url or None
            if payload.model is not None:
                block.model = payload.model or None
            if payload.refresh_interval is not None:
                block.refresh_interval = payload.refresh_interval

            db.commit()
            db.refresh(block)
            _sync_block_schedule(db, block)
            db.commit()
            db.refresh(block)
            return _block_to_dict(block)
        finally:
            db.close()

    @router.delete("/blocks/{block_id}")
    async def delete_block(block_id: str, request: Request):
        owner = _owner(request)
        db = SessionLocal()
        try:
            block = db.query(DashboardBlock).filter(DashboardBlock.id == block_id, DashboardBlock.owner == owner).first()
            if not block:
                raise HTTPException(404, "Block not found")
            _delete_block_task(db, block)
            db.delete(block)
            db.commit()
            return {"ok": True}
        finally:
            db.close()

    @router.post("/blocks/{block_id}/run")
    async def run_block_now(block_id: str, request: Request):
        owner = _owner(request)
        db = SessionLocal()
        try:
            block = db.query(DashboardBlock).filter(DashboardBlock.id == block_id, DashboardBlock.owner == owner).first()
            if not block:
                raise HTTPException(404, "Block not found")
        finally:
            db.close()

        logger.info(f"Dashboard widget run started: '{block.title}' ({block_id}), sources={block.sources or []}")
        task = asyncio.ensure_future(run_block(block))
        _RUNNING_RUNS[block_id] = task
        try:
            result = await task
        except asyncio.CancelledError:
            logger.info(f"Dashboard widget run cancelled: '{block.title}' ({block_id})")
            raise HTTPException(409, "Run cancelled")
        except Exception as e:
            logger.error(f"Dashboard widget run failed: '{block.title}' ({block_id}): {e}")
            raise HTTPException(500, str(e))
        finally:
            if _RUNNING_RUNS.get(block_id) is task:
                _RUNNING_RUNS.pop(block_id, None)
        logger.info(
            f"Dashboard widget run finished: '{block.title}' ({block_id}) — "
            f"{len(result.get('rows', []))} rows, {len(result.get('columns', []))} columns"
        )

        db = SessionLocal()
        try:
            b = db.query(DashboardBlock).filter(DashboardBlock.id == block_id).first()
            if b:
                b.last_run_at = datetime.now(timezone.utc).replace(tzinfo=None)
                b.last_columns = result.get("columns", [])
                b.last_rows = result.get("rows", [])
                b.last_summary = result.get("summary", "")
                db.commit()
                db.refresh(b)
                return _block_to_dict(b)
            raise HTTPException(404, "Block not found")
        finally:
            db.close()

    @router.post("/blocks/{block_id}/cancel")
    async def cancel_block_run(block_id: str, request: Request):
        """Cancel an in-flight 'Run now' for this block, if one is running."""
        owner = _owner(request)
        db = SessionLocal()
        try:
            block = db.query(DashboardBlock).filter(DashboardBlock.id == block_id, DashboardBlock.owner == owner).first()
            if not block:
                raise HTTPException(404, "Block not found")
        finally:
            db.close()

        task = _RUNNING_RUNS.get(block_id)
        if not task or task.done():
            raise HTTPException(404, "No run in progress")
        task.cancel()
        return {"ok": True}

    @router.get("/blocks/{block_id}/download")
    async def download_block(block_id: str, request: Request):
        from fastapi.responses import Response

        owner = _owner(request)
        db = SessionLocal()
        try:
            block = db.query(DashboardBlock).filter(DashboardBlock.id == block_id, DashboardBlock.owner == owner).first()
            if not block:
                raise HTTPException(404, "Block not found")

            columns = block.last_columns or []
            rows = block.last_rows or []
            safe_name = (block.title or "block").replace(" ", "_").replace("/", "_")

            if columns:
                import csv, io
                from src.dashboard_cells import cell_to_text
                buf = io.StringIO()
                writer = csv.DictWriter(buf, fieldnames=columns)
                writer.writeheader()
                for row in rows:
                    writer.writerow({c: cell_to_text(row.get(c, "")) for c in columns})
                return Response(
                    content=buf.getvalue(),
                    media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{safe_name}.csv"'},
                )

            return Response(
                content=block.last_summary or "",
                media_type="text/plain",
                headers={"Content-Disposition": f'attachment; filename="{safe_name}.txt"'},
            )
        finally:
            db.close()

    return router


# ── Grid placement ───────────────────────────────────────────────────────

def _first_free_slot(existing, w: int, h: int) -> tuple[int, int]:
    """Find the top-left cell of the first free w×h area on the 12×12 grid,
    scanning row-major. Falls back to (0, 0) if the grid is full."""
    occupied = set()
    for b in existing:
        bx = b.grid_x if b.grid_x is not None else 0
        by = b.grid_y if b.grid_y is not None else 0
        bw = b.grid_w or DEFAULT_BLOCK_W
        bh = b.grid_h or DEFAULT_BLOCK_H
        for yy in range(by, min(by + bh, GRID_ROWS)):
            for xx in range(bx, min(bx + bw, GRID_COLS)):
                occupied.add((xx, yy))

    for y in range(GRID_ROWS - h + 1):
        for x in range(GRID_COLS - w + 1):
            if all((x + dx, y + dy) not in occupied
                   for dy in range(h) for dx in range(w)):
                return x, y
    return 0, 0


# ── Scheduling ───────────────────────────────────────────────────────────

def _sync_block_schedule(db, block: DashboardBlock):
    """Create/update/remove the ScheduledTask backing this block's refresh_interval."""
    interval = block.refresh_interval or "manual"

    if interval == "manual":
        _delete_block_task(db, block)
        return

    from src.task_scheduler import compute_next_run
    cron = _REFRESH_CRON.get(interval)
    if not cron:
        return

    next_run = compute_next_run("cron", None, cron_expression=cron)

    if block.task_id:
        task = db.query(ScheduledTask).filter(ScheduledTask.id == block.task_id).first()
        if task:
            task.schedule = "cron"
            task.cron_expression = cron
            task.next_run = next_run
            task.status = "active"
            return

    task = ScheduledTask(
        id=uuid.uuid4().hex,
        owner=block.owner,
        name=f"Dashboard: {block.title}",
        task_type="action",
        action="run_dashboard_block",
        prompt=block.id,
        schedule="cron",
        cron_expression=cron,
        trigger_type="schedule",
        next_run=next_run,
        status="active",
        notifications_enabled=False,
    )
    db.add(task)
    db.flush()
    block.task_id = task.id


def _delete_block_task(db, block: DashboardBlock):
    if not block.task_id:
        return
    task = db.query(ScheduledTask).filter(ScheduledTask.id == block.task_id).first()
    if task:
        db.delete(task)
    block.task_id = None


# ── Extraction engine ────────────────────────────────────────────────────

def _resolve_block_candidates(block: DashboardBlock, owner: str) -> list:
    """Resolve the per-widget model override to LLM candidates *with credentials*.

    Prefers ``model_endpoint_id`` (resolves the endpoint's stored API key via
    resolve_endpoint_by_id — earlier the run sent no key and remote providers
    401'd). Falls back to a legacy ``model_endpoint_url`` (sent keyless, best
    effort). Returns [] to signal "use the shared background-task chain".
    """
    owner_arg = owner or None
    if block.model_endpoint_id:
        from src.endpoint_resolver import resolve_endpoint_by_id
        resolved = resolve_endpoint_by_id(block.model_endpoint_id, block.model, owner=owner_arg)
        if resolved:
            url, model, headers = resolved
            return [(url, model, headers or {})]
        logger.warning(
            f"Dashboard widget '{block.title}' ({block.id}): configured endpoint "
            f"'{block.model_endpoint_id}' could not be resolved; falling back."
        )
    if block.model_endpoint_url and block.model:
        # Legacy rows stored only a URL (no id). Resolve the matching endpoint's
        # credentials by URL so the call still authenticates — dispatching the
        # bare URL keyless is what made remote providers (DeepSeek) 401.
        from src.endpoint_resolver import resolve_endpoint_by_url
        resolved = resolve_endpoint_by_url(block.model_endpoint_url, block.model, owner=owner_arg)
        if resolved:
            url, model, headers = resolved
            return [(url, model, headers or {})]
        logger.warning(
            f"Dashboard widget '{block.title}' ({block.id}): no endpoint matched "
            f"legacy URL '{block.model_endpoint_url}'; dispatching without credentials."
        )
        return [(block.model_endpoint_url, block.model, {})]
    return []


async def run_block(block: DashboardBlock) -> dict:
    """Gather each source's data, ask the LLM to extract a table per the block's prompt."""
    owner = block.owner or ""
    gathered = await gather_block_data(owner, block.sources or [], block.source_config or {})

    # A source that blew up (bad IMAP filter, dead connection) must surface as a
    # real error. Feeding {"error": ...} to the LLM just gets it paraphrased into
    # a "summary", hiding the cause. Only hard-fail when nothing usable came back.
    errors = [f"{sid}: {v['error']}" for sid, v in gathered.items()
              if isinstance(v, dict) and v.get("error")]
    usable = any(isinstance(v, list) and v for v in gathered.values())
    if errors and not usable:
        raise RuntimeError("; ".join(errors))

    if not any(gathered.values()):
        return {"rows": [], "columns": [], "summary": "No data from the selected sources."}

    system_prompt = f"""You are a data digest assistant. The user has provided data from one or more sources
(email, calendar, ...) as JSON, keyed by source name. Extract the requested data per the
instructions below. Return ONLY valid JSON — no markdown, no explanation.

{block.prompt}

Return format:
{{
  "columns": ["Column1", "Column2", ...],
  "rows": [
    {{"Column1": "value", "Column2": "value", ...}},
    ...
  ],
  "summary": "One-line summary of what was extracted"
}}

A cell value is normally a plain string. ONLY IF the user's instructions above
ask for links, buttons/actions (CTAs), or formatted dates may a cell instead be
an object:
{{
  "text": "shown when there are no actions",
  "date": "2026-07-22T10:00:00",  "format": "date",  // date|datetime|time|relative — renders localized
  "html": "<b>inline</b> rich text or an <a href=\\"https://…\\">external link</a>",
  "actions": [
    {{"type": "url",           "label": "Open",      "href": "https://…"}},
    {{"type": "chat",          "label": "Summarize", "prompt": "Summarize invoice X"}},
    {{"type": "email_compose", "label": "Reply",     "to": "a@b.com", "subject": "Re: …"}},
    {{"type": "email_open",    "label": "View",      "uid": "<the email's uid from the source data>"}},
    {{"type": "navigate",      "label": "Calendar",  "target": "calendar"}}
  ]
}}
Rules: use objects only where a CTA or date formatting is requested — keep
other cells plain strings. For dates, prefer {{"date": "<ISO 8601>", "format":
"date"}} so they render localized. `html` allows only inline tags (a, b, i,
span, br, code, …). `url`
hrefs must be http(s)/mailto. `navigate` target is one of: calendar, tasks,
dashboards, email, memory, notes, compare, cookbook, research, gallery,
library. For `email_open`, use a uid present in the source data — never invent
one."""

    user_message = f"Source data:\n{json.dumps(gathered, ensure_ascii=False, indent=2, default=str)}"

    candidates = _resolve_block_candidates(block, owner)
    if candidates:
        from src.llm_core import llm_call_async_with_fallback
        llm_call = lambda **kw: llm_call_async_with_fallback(candidates, **kw)
    else:
        from src.task_endpoint import task_llm_call_async
        llm_call = lambda **kw: task_llm_call_async(owner=owner or None, **kw)

    try:
        response = await asyncio.wait_for(
            llm_call(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                temperature=0.1,
                max_tokens=8000,
            ),
            timeout=_RUN_LLM_TIMEOUT,
        )
    except asyncio.TimeoutError:
        raise RuntimeError(f"LLM call timed out ({_RUN_LLM_TIMEOUT:.0f}s)")
    except Exception as e:
        raise RuntimeError(f"LLM call failed: {e}")

    text = (response or "").strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        import re
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            try:
                parsed = json.loads(m.group(0))
            except json.JSONDecodeError:
                raise RuntimeError(f"Could not parse LLM response as JSON. Raw: {text[:500]}")
        else:
            raise RuntimeError(f"LLM response was not JSON. Raw: {text[:500]}")

    columns = parsed.get("columns", [])
    rows = parsed.get("rows", [])
    summary = parsed.get("summary", f"Extracted {len(rows)} rows with {len(columns)} columns")

    # Cells may carry LLM-authored HTML/CTAs built from untrusted source data —
    # sanitize before it's stored and rendered (see src/dashboard_cells.py).
    from src.dashboard_cells import sanitize_rows
    rows = sanitize_rows(columns, rows)

    return {"rows": rows, "columns": columns, "summary": summary}


async def run_block_by_id(block_id: str, owner: str) -> DashboardBlock:
    """Run a block by id (used by the scheduled-task builtin action) and persist the result."""
    db = SessionLocal()
    try:
        block = db.query(DashboardBlock).filter(
            DashboardBlock.id == block_id,
            DashboardBlock.owner == owner,
        ).first()
        if not block:
            raise ValueError(f"no dashboard widget found for id '{block_id}'")
    finally:
        db.close()

    result = await run_block(block)

    db = SessionLocal()
    try:
        b = db.query(DashboardBlock).filter(DashboardBlock.id == block_id).first()
        if b:
            b.last_run_at = datetime.now(timezone.utc).replace(tzinfo=None)
            b.last_columns = result.get("columns", [])
            b.last_rows = result.get("rows", [])
            b.last_summary = result.get("summary", "")
            db.commit()
            db.refresh(b)
        return b
    finally:
        db.close()
