"""LLM Console — live view of outgoing LLM API traffic.

Streams the exchanges captured in src/llm_console.py (request + response/error
for every chat and background call) to a frontend side panel over SSE. The
buffer is global (all traffic on the instance), so the endpoints are admin-only.
"""

import asyncio
import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from core.middleware import require_admin
from src import llm_console

logger = logging.getLogger(__name__)


def setup_llm_console_routes():
    router = APIRouter(prefix="/api/llm-console", tags=["llm-console"])

    @router.get("/recent")
    async def recent(request: Request):
        require_admin(request)
        return {"exchanges": llm_console.snapshot()}

    @router.post("/clear")
    async def clear(request: Request):
        require_admin(request)
        llm_console.clear()
        return {"ok": True}

    @router.get("/stream")
    async def stream(request: Request):
        require_admin(request)
        queue = llm_console.subscribe()

        async def gen():
            try:
                # Replay recent history so a freshly-opened console isn't empty.
                for ex in llm_console.snapshot():
                    yield f"data: {json.dumps(ex)}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        ex = await asyncio.wait_for(queue.get(), timeout=15)
                        yield f"data: {json.dumps(ex)}\n\n"
                    except asyncio.TimeoutError:
                        # Heartbeat comment keeps the connection (and proxies) alive.
                        yield ": keepalive\n\n"
            finally:
                llm_console.unsubscribe(queue)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router
