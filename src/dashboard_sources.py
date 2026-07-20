"""Pluggable data-source registry for dashboard blocks.

A dashboard block picks one or more sources from SOURCE_REGISTRY, each
contributing a list of JSON-able dicts that get handed to the LLM alongside
the block's prompt. To add a new source type (documents, RAG, skills, ...):

  1. Write an async fetch(owner: str, config: dict) -> list[dict].
  2. Register it below with a label and a config_schema describing the
     fields the block editor should render for it.

Nothing else needs to change — routes/dashboard_routes.py and the frontend
both drive off this registry.
"""

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


def _imap_quote(value: str) -> str:
    """Quote a value as an RFC 3501 IMAP string."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def normalize_imap_search(raw: str) -> str:
    """Rewrite a user-entered IMAP SEARCH filter into valid IMAP syntax.

    IMAP strings must be double-quoted (RFC 3501) — single quotes are not string
    delimiters, so a filter like ``TO '@example.com'`` is sent verbatim and the
    server fails to parse it ("expected valid digit for number"). Rewrite every
    single- or double-quoted run as a properly escaped double-quoted string and
    pass bare atoms, keywords, and parens through untouched.
    """
    s = (raw or "").strip()
    if not s:
        return "ALL"

    # (token, is_quoted) — is_quoted marks emitted strings so paren-tightening
    # below never rewrites punctuation that lives *inside* a quoted value.
    out: list[tuple[str, bool]] = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c.isspace():
            i += 1
            continue
        if c in ("'", '"'):
            quote, i, buf = c, i + 1, []
            while i < n:
                ch = s[i]
                if ch == "\\" and i + 1 < n:      # keep escaped char literally
                    buf.append(s[i + 1])
                    i += 2
                    continue
                if ch == quote:
                    i += 1
                    break
                buf.append(ch)
                i += 1
            out.append((_imap_quote("".join(buf)), True))
            continue
        start = i
        while i < n and not s[i].isspace() and s[i] not in ("'", '"'):
            i += 1
        out.append((s[start:i], False))

    # Join with spaces, but keep parens tight: IMAP's grammar is
    # "(" search-key *(SP search-key) ")" — no space before ")".
    res = ""
    for text, is_quoted in out:
        if not res:
            res = text
        elif (not is_quoted and text.startswith(")")) or res.endswith("("):
            res += text
        else:
            res += " " + text
    return res or "ALL"


async def fetch_email(owner: str, config: dict) -> list[dict]:
    """Fetch recent emails matching the block's IMAP filter."""
    import email as _email_mod
    from routes.email_helpers import _imap_connect, _extract_text, _decode_header

    account_id = config.get("account_id") or None
    folder = config.get("folder") or "INBOX"
    search_filter = normalize_imap_search(config.get("search_filter") or "ALL")
    max_emails = int(config.get("max_emails") or 50)

    try:
        conn = _imap_connect(account_id, owner=owner or "")
    except Exception as e:
        raise RuntimeError(f"IMAP connection failed: {e}")

    items: list[dict] = []
    try:
        status, _ = conn.select(folder, readonly=True)
        if status != "OK":
            raise RuntimeError(f"Cannot open folder: {folder}")

        try:
            status, data = conn.search(None, search_filter)
        except Exception as e:
            # imaplib raises on a BAD response — keep the server's own message
            # and show the filter as actually sent so a bad one is debuggable.
            raise RuntimeError(f"IMAP SEARCH rejected filter {search_filter}: {e}")
        if status != "OK":
            raise RuntimeError(f"IMAP SEARCH failed for filter {search_filter}")

        uids = data[0].split()
        if not uids:
            return []
        uids = uids[-max_emails:]

        for uid in uids:
            try:
                status, msg_data = conn.fetch(uid, "(RFC822)")
                if status != "OK":
                    continue
                raw = msg_data[0][1]
                msg = _email_mod.message_from_bytes(raw)
                items.append({
                    "uid": uid.decode() if isinstance(uid, bytes) else str(uid),
                    "subject": _decode_header(msg.get("Subject", "")),
                    "from": _decode_header(msg.get("From", "")),
                    "date": msg.get("Date", ""),
                    "body": _extract_text(msg)[:4000],
                })
            except Exception:
                continue
    finally:
        try:
            conn.close()
        except Exception:
            pass
        try:
            conn.logout()
        except Exception:
            pass

    return items


async def fetch_calendar(owner: str, config: dict) -> list[dict]:
    """Fetch calendar events in a relative date window around now."""
    from core.database import SessionLocal, CalendarEvent, CalendarCal
    from routes.calendar_routes import _expand_rrule, FALLBACK_OWNER
    from sqlalchemy import and_, or_

    # In single-user / auth-off mode the calendar stores rows under
    # FALLBACK_OWNER (see calendar_routes._require_user), while dashboard
    # widgets carry owner="". Resolve the same way the calendar's own
    # read/write paths do, or the widget would never see any events.
    owner = owner or FALLBACK_OWNER

    days_back = int(config.get("days_back") or 0)
    days_forward = int(config.get("days_forward") or 14)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    start = now - timedelta(days=days_back)
    end = now + timedelta(days=days_forward)

    db = SessionLocal()
    try:
        q = db.query(CalendarEvent).join(CalendarCal).filter(
            CalendarEvent.status != "cancelled",
            CalendarCal.owner == owner,
            or_(
                and_(
                    or_(CalendarEvent.rrule == "", CalendarEvent.rrule.is_(None)),
                    CalendarEvent.dtstart < end,
                    CalendarEvent.dtend > start,
                ),
                and_(
                    CalendarEvent.rrule.isnot(None),
                    CalendarEvent.rrule != "",
                    CalendarEvent.dtstart < end,
                ),
            ),
        )
        events = q.order_by(CalendarEvent.dtstart).all()
        occurrences: list[dict] = []
        for ev in events:
            occurrences.extend(_expand_rrule(ev, start, end))
        return [
            {
                "summary": o.get("summary", ""),
                "start": o.get("dtstart", ""),
                "end": o.get("dtend", ""),
                "location": o.get("location", ""),
                "description": o.get("description", ""),
                "calendar": o.get("calendar", ""),
            }
            for o in occurrences
        ]
    finally:
        db.close()


SOURCE_REGISTRY = {
    "email": {
        "label": "Email",
        "fetch": fetch_email,
        "config_schema": [
            {"key": "folder", "label": "Folder", "type": "text", "default": "INBOX"},
            {"key": "search_filter", "label": "IMAP search filter", "type": "text",
             "placeholder": 'e.g. UNSEEN, FROM "@example.com", SINCE 01-Jan-2024'},
            {"key": "max_emails", "label": "Max emails", "type": "number", "default": 50},
        ],
    },
    "calendar": {
        "label": "Calendar",
        "fetch": fetch_calendar,
        "config_schema": [
            {"key": "days_back", "label": "Days back", "type": "number", "default": 0},
            {"key": "days_forward", "label": "Days forward", "type": "number", "default": 14},
        ],
    },
}


async def gather_block_data(owner: str, sources: list, source_config: dict) -> dict:
    """Fetch data for every source a block declares. Returns {source_id: items}."""
    source_config = source_config or {}
    result = {}
    for source_id in sources or []:
        entry = SOURCE_REGISTRY.get(source_id)
        if not entry:
            continue
        try:
            result[source_id] = await entry["fetch"](owner, source_config.get(source_id) or {})
        except Exception as e:
            logger.error("dashboard source '%s' failed: %s", source_id, e)
            result[source_id] = {"error": str(e)}
    return result
