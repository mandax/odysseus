"""In-memory tap for live LLM API traffic, feeding the frontend LLM Console.

Every non-streaming call (llm_call_async — dashboards, tasks, utilities) and
streaming chat (stream_llm) records one "exchange" here: the outgoing request
(model, endpoint, messages) and the response (status + body, or error). A small
ring buffer keeps recent history; an asyncio pub/sub pushes new exchanges to any
connected console (SSE).

Secrets never enter here: only the target URL is stored (no Authorization
header / API key), and payloads are truncated. This is a debug view of the
operator's own traffic, so message content is kept (truncated), not redacted.
"""

import asyncio
import itertools
import logging
from collections import deque
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Ring buffer of recent exchanges + live subscribers. Recording is always on
# (appending to a bounded deque is negligible); subscribers only exist while a
# console panel is open.
_BUFFER: "deque[dict]" = deque(maxlen=300)
_SUBSCRIBERS: "list[asyncio.Queue]" = []
_ids = itertools.count(1)

# Truncation caps keep the buffer and the wire small.
_MAX_CONTENT = 4000        # per request message
_MAX_MESSAGES = 12         # messages kept per request
_MAX_RESPONSE = 16000      # response/body text
_MAX_STREAM_RAW = 262144   # bytes of raw stream chunks accumulated before parse


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _truncate(s: Optional[str], limit: int) -> str:
    if not s:
        return ""
    s = str(s)
    return s if len(s) <= limit else s[:limit] + f"\n…[truncated {len(s) - limit} chars]"


def _summarize_messages(messages: Optional[List[Dict]]) -> List[Dict]:
    out: List[Dict] = []
    for m in (messages or [])[:_MAX_MESSAGES]:
        try:
            content = m.get("content")
            if isinstance(content, list):
                # Multimodal content parts -> join text parts, note the rest.
                parts = []
                for p in content:
                    if isinstance(p, dict) and p.get("type") in (None, "text"):
                        parts.append(str(p.get("text") or ""))
                    else:
                        parts.append(f"[{(p or {}).get('type', 'part')}]")
                content = " ".join(parts)
            out.append({"role": m.get("role", "?"), "content": _truncate(content, _MAX_CONTENT)})
        except Exception:
            out.append({"role": "?", "content": "[unreadable]"})
    extra = max(0, len(messages or []) - _MAX_MESSAGES)
    if extra:
        out.append({"role": "…", "content": f"[+{extra} more message(s)]"})
    return out


def _publish(exchange: dict) -> None:
    _BUFFER.append(exchange)
    for q in list(_SUBSCRIBERS):
        try:
            q.put_nowait(exchange)
        except Exception:
            # Slow/full consumer — drop rather than block the LLM path.
            pass


def record_exchange(
    *,
    kind: str,
    url: str,
    model: str,
    workload: str,
    messages: Optional[List[Dict]],
    status: Optional[int] = None,
    ok: bool = False,
    response: Optional[str] = None,
    error: Optional[str] = None,
    duration_ms: Optional[int] = None,
) -> None:
    """Append one completed exchange and fan it out to live consoles. Never raises."""
    try:
        exchange = {
            "id": next(_ids),
            "ts": _now_iso(),
            "kind": kind,
            "url": url or "",
            "model": model or "",
            "workload": workload or "",
            "status": status,
            "ok": bool(ok),
            "response": _truncate(response, _MAX_RESPONSE),
            "error": _truncate(error, 2000) if error else None,
            "request": _summarize_messages(messages),
            "duration_ms": duration_ms,
        }
        _publish(exchange)
    except Exception as e:  # pragma: no cover - telemetry must never break calls
        logger.debug(f"llm_console.record_exchange failed: {e}")


# ── Streaming helpers (stream_llm wraps its generator with these) ──────────

def begin_stream(*, url: str, model: str, workload: str, messages: Optional[List[Dict]]) -> Optional[dict]:
    """Start a streaming record. Returns an opaque handle passed to observe/finish."""
    try:
        return {"url": url, "model": model, "workload": workload,
                "messages": messages, "raw": [], "raw_len": 0, "start": _loop_time()}
    except Exception:
        return None


def observe_chunk(handle: Optional[dict], chunk: Any) -> None:
    """Cheaply accumulate raw SSE chunks (bounded); parsing is deferred to finish."""
    if not handle:
        return
    try:
        if handle["raw_len"] >= _MAX_STREAM_RAW:
            return
        s = chunk if isinstance(chunk, str) else str(chunk)
        handle["raw"].append(s)
        handle["raw_len"] += len(s)
    except Exception:
        pass


def finish_stream(handle: Optional[dict], *, error: Optional[str] = None) -> None:
    """Reconstruct the streamed text from accumulated chunks and record it."""
    if not handle:
        return
    try:
        text, status, ok = _reconstruct_stream("".join(handle["raw"]))
        if error:
            ok = False
        dur = None
        try:
            dur = int((_loop_time() - handle["start"]) * 1000)
        except Exception:
            pass
        record_exchange(
            kind="stream",
            url=handle["url"],
            model=handle["model"],
            workload=handle["workload"],
            messages=handle["messages"],
            status=status,
            ok=ok and not error,
            response=text,
            error=error,
            duration_ms=dur,
        )
    except Exception as e:  # pragma: no cover
        logger.debug(f"llm_console.finish_stream failed: {e}")


def _reconstruct_stream(raw: str) -> tuple[str, Optional[int], bool]:
    """Best-effort assembly of streamed assistant text from raw SSE lines.

    Handles OpenAI-compatible `choices[].delta.content` (DeepSeek, vLLM, OpenAI,
    …) and Anthropic `content_block_delta`. Unknown formats yield empty text but
    the request is still recorded. Returns (text, status, ok)."""
    import json
    parts: List[str] = []
    status: Optional[int] = 200
    ok = True
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            obj = json.loads(data)
        except Exception:
            continue
        try:
            if obj.get("error") or (obj.get("status") and obj.get("text")):
                status = int(obj.get("status") or 502)
                ok = False
                parts.append(str(obj.get("text") or obj.get("error") or ""))
                continue
            choices = obj.get("choices")
            if isinstance(choices, list) and choices:
                delta = choices[0].get("delta") or {}
                piece = delta.get("content") or delta.get("reasoning_content") or ""
                if piece:
                    parts.append(piece)
                continue
            # Anthropic-style
            if obj.get("type") == "content_block_delta":
                piece = (obj.get("delta") or {}).get("text") or ""
                if piece:
                    parts.append(piece)
        except Exception:
            continue
    return _truncate("".join(parts), _MAX_RESPONSE), status, ok


def _loop_time() -> float:
    try:
        return asyncio.get_event_loop().time()
    except Exception:
        import time
        return time.time()


# ── Console API (used by routes/llm_console_routes.py) ─────────────────────

def snapshot() -> List[dict]:
    """Return recent exchanges, oldest first."""
    return list(_BUFFER)


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    _SUBSCRIBERS.append(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    try:
        _SUBSCRIBERS.remove(q)
    except ValueError:
        pass


def clear() -> None:
    _BUFFER.clear()
