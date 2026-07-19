"""Sanitization for dashboard-widget table cells.

Widget rows come from an LLM digesting untrusted source data (emails, crawled
pages, …), so a cell may be plain text *or* a rich object carrying sanitized
HTML and/or action buttons (CTAs). Everything here is defense-in-depth: the
frontend renders `html` via innerHTML and dispatches `actions` by type, so this
module is the trust boundary. It must never let script, event handlers, or
javascript:/data: URLs through, and it caps every string.

Cell shapes accepted from the model:
  "plain string"
  {"text": "...", "html": "<b>rich</b>", "actions": [ <action>, ... ]}

Action shapes (type-allowlisted):
  {"type": "url",           "label": "...", "href": "https://…"}
  {"type": "chat",          "label": "...", "prompt": "…"}
  {"type": "email_compose", "label": "...", "to": "a@b.c", "subject": "…"}
  {"type": "email_open",    "label": "...", "uid": "123", "folder": "INBOX"}
  {"type": "navigate",      "label": "...", "target": "calendar"}
"""

from typing import Any

import nh3

# Rich-text tags allowed inside a cell's `html`. Intentionally small: inline
# formatting + links only, no block/media/structural tags, no class/style/id
# (which could bleed into or restyle the app), no script/iframe/etc.
_CELL_TAGS = {"a", "b", "strong", "i", "em", "u", "s", "span", "br",
              "small", "code", "mark", "sub", "sup"}
_CELL_ATTRS = {"a": {"href", "title"}}
_URL_SCHEMES = {"http", "https", "mailto"}

# CTA action types the frontend knows how to dispatch, and the Odysseus tools a
# `navigate` action may open. Kept in lockstep with dashboards.js runCellAction.
_ACTION_TYPES = {"url", "chat", "email_compose", "email_open", "navigate"}
_NAV_TARGETS = {"calendar", "tasks", "dashboards", "email", "memory", "notes",
                "compare", "cookbook", "research", "gallery", "library"}

# How a {"date": …} cell renders on the frontend (localized). "date" is default.
_DATE_FORMATS = {"date", "datetime", "time", "relative"}

# Per-field length caps — a widget cell is a compact affordance, not a payload.
_CAP = {
    "label": 80, "href": 2000, "prompt": 4000, "to": 320,
    "subject": 300, "uid": 200, "folder": 80, "target": 40,
    "text": 4000, "html": 8000,
}


def _s(value: Any, cap: int) -> str:
    if value is None:
        return ""
    s = str(value)
    return s[:cap]


def _clean_html(raw: Any) -> str:
    """Allowlist-sanitize rich cell HTML; force safe, isolated external links."""
    html = _s(raw, _CAP["html"])
    if not html:
        return ""
    return nh3.clean(
        html,
        tags=_CELL_TAGS,
        attributes=_CELL_ATTRS,
        url_schemes=_URL_SCHEMES,
        link_rel="noopener noreferrer nofollow",
    )


def _valid_url(href: str) -> bool:
    low = href.strip().lower()
    return low.startswith(("http://", "https://", "mailto:"))


def _sanitize_action(action: Any) -> dict | None:
    """Return a validated action dict, or None to drop it."""
    if not isinstance(action, dict):
        return None
    a_type = str(action.get("type") or "").strip()
    if a_type not in _ACTION_TYPES:
        return None
    label = _s(action.get("label"), _CAP["label"]).strip()

    if a_type == "url":
        href = _s(action.get("href"), _CAP["href"]).strip()
        if not _valid_url(href):
            return None
        return {"type": "url", "label": label or href, "href": href}

    if a_type == "chat":
        prompt = _s(action.get("prompt"), _CAP["prompt"])
        if not prompt.strip():
            return None
        return {"type": "chat", "label": label or "Ask Odysseus", "prompt": prompt}

    if a_type == "email_compose":
        out = {"type": "email_compose", "label": label or "Compose"}
        to = _s(action.get("to"), _CAP["to"]).strip()
        subject = _s(action.get("subject"), _CAP["subject"])
        if to:
            out["to"] = to
        if subject.strip():
            out["subject"] = subject
        return out

    if a_type == "email_open":
        uid = _s(action.get("uid"), _CAP["uid"]).strip()
        if not uid:
            return None
        out = {"type": "email_open", "label": label or "Open email", "uid": uid}
        folder = _s(action.get("folder"), _CAP["folder"]).strip()
        if folder:
            out["folder"] = folder
        return out

    if a_type == "navigate":
        target = _s(action.get("target"), _CAP["target"]).strip().lower()
        if target not in _NAV_TARGETS:
            return None
        return {"type": "navigate", "label": label or target.title(), "target": target}

    return None


def sanitize_cell(value: Any) -> Any:
    """Sanitize one cell. Returns a plain string, or a {text?,html?,actions?} dict."""
    if isinstance(value, dict):
        out: dict = {}
        date = value.get("date")
        if date is not None and str(date).strip():
            # Keep the raw value (frontend parses + localizes); just cap it and
            # allowlist the format. Invalid dates fall back to raw on render.
            out["date"] = _s(date, 64).strip()
            fmt = str(value.get("format") or "date").strip().lower()
            out["format"] = fmt if fmt in _DATE_FORMATS else "date"
        text = value.get("text")
        if text is not None:
            out["text"] = _s(text, _CAP["text"])
        html = value.get("html")
        if html is not None:
            cleaned = _clean_html(html)
            if cleaned:
                out["html"] = cleaned
        actions = value.get("actions")
        if isinstance(actions, list):
            cleaned_actions = [a for a in (_sanitize_action(x) for x in actions[:12]) if a]
            if cleaned_actions:
                out["actions"] = cleaned_actions
        # An object with nothing renderable collapses to empty text.
        if not out:
            return ""
        return out
    if isinstance(value, (str, int, float, bool)):
        return _s(value, _CAP["text"])
    if value is None:
        return ""
    return _s(value, _CAP["text"])


def sanitize_rows(columns: list, rows: list) -> list:
    """Sanitize every cell of every row (only for the declared columns)."""
    cols = columns or []
    clean: list = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        clean.append({c: sanitize_cell(row.get(c)) for c in cols})
    return clean


def cell_to_text(value: Any) -> str:
    """Flatten a (possibly rich) cell to plain text for CSV export / summaries."""
    if isinstance(value, dict):
        if value.get("date"):
            return str(value["date"])
        if value.get("text"):
            return str(value["text"])
        if value.get("html"):
            # Strip tags for a text rendering.
            return nh3.clean(str(value["html"]), tags=set(), attributes={})
        actions = value.get("actions") or []
        parts = []
        for a in actions:
            if not isinstance(a, dict):
                continue
            if a.get("type") == "url" and a.get("href"):
                parts.append(f"{a.get('label') or ''} <{a['href']}>".strip())
            elif a.get("label"):
                parts.append(str(a["label"]))
        return "; ".join(parts)
    if value is None:
        return ""
    return str(value)
