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


_IMAP_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _clean_value(value) -> str:
    """Strip control chars (CRLF would break/inject into the IMAP command)."""
    import re
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(value or "")).strip()


def _imap_date(value) -> str | None:
    """Normalize a date to IMAP's dd-Mon-yyyy.

    Accepts the HTML date input's YYYY-MM-DD as well as an already-IMAP
    dd-Mon-yyyy. Returns None for anything unparseable, so a malformed date is
    dropped rather than corrupting the whole SEARCH command.
    """
    import re
    v = _clean_value(value)
    if not v:
        return None
    m = re.fullmatch(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})", v)
    if m and m.group(2).title() in _IMAP_MONTHS:
        return f"{int(m.group(1)):02d}-{m.group(2).title()}-{m.group(3)}"
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", v)
    if m:
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= month <= 12 and 1 <= day <= 31:
            return f"{day:02d}-{_IMAP_MONTHS[month - 1]}-{year}"
    return None


def _truthy(value) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def build_imap_search(config: dict) -> str:
    """Compile the widget's structured email filters into a valid IMAP SEARCH.

    Users kept hitting IMAP's syntax rules by hand — single quotes aren't string
    delimiters, criteria are space-separated (not comma), dates are dd-Mon-yyyy.
    So the UI collects intent as plain fields and this builds the command, which
    removes that whole class of error. `search_filter` remains an advanced
    override for anything these fields can't express.
    """
    config = config or {}
    raw = _clean_value(config.get("search_filter"))
    if raw:
        return normalize_imap_search(raw)

    parts: list[str] = []
    if _truthy(config.get("unread_only")):
        parts.append("UNSEEN")
    for key, imap_key in (("from_contains", "FROM"),
                          ("to_contains", "TO"),
                          ("subject_contains", "SUBJECT")):
        value = _clean_value(config.get(key))
        if value:
            parts.append(f"{imap_key} {_imap_quote(value)}")
    since = _imap_date(config.get("since"))
    if since:
        parts.append(f"SINCE {since}")
    # IMAP BEFORE is exclusive of the given date.
    until = _imap_date(config.get("until"))
    if until:
        parts.append(f"BEFORE {until}")

    return " ".join(parts) or "ALL"


def normalize_imap_search(raw: str) -> str:
    """Rewrite a user-entered IMAP SEARCH filter into valid IMAP syntax.

    IMAP strings must be double-quoted (RFC 3501) — single quotes are not string
    delimiters, so a filter like ``TO '@example.com'`` is sent verbatim and the
    server fails to parse it ("expected valid digit for number"). Rewrite every
    single- or double-quoted run as a properly escaped double-quoted string and
    pass bare atoms, keywords, and parens through untouched.
    """
    # Strip control characters first. A stray CR/LF (easy to paste in) breaks the
    # command mid-line — servers report "expected CR" — and would otherwise let a
    # filter inject a second IMAP command. Replace with a space so tokens don't fuse.
    import re
    s = re.sub(r"[\x00-\x1f\x7f]", " ", raw or "").strip()
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
        token = s[start:i]
        # IMAP ANDs criteria by whitespace — there is no comma separator. Users
        # reasonably write "SINCE 01-Jan-2019, TO ..." (the old placeholder read
        # like a comma-separated list), which makes servers fail at the comma
        # with "expected CR". Drop a *trailing* comma only, so message sets that
        # legitimately contain commas (UID 1,3,5) still work.
        token = token.rstrip(",")
        if token:
            out.append((token, False))

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
    search_filter = build_imap_search(config)
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

        # repr() so hidden whitespace/control chars in a bad filter are visible.
        logger.info("dashboard email search in %r: %r", folder, search_filter)
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
            # options_url populates a datalist of the account's real IMAP folders —
            # searching INBOX for mail you *sent* is an easy, silent mistake.
            {"key": "folder", "label": "Folder", "type": "text", "default": "INBOX",
             "options_url": "/api/email/folders", "options_key": "folders",
             "hint": "Mail you sent lives in Sent, not INBOX"},
            # Structured filters — compiled to valid IMAP by build_imap_search()
            # so nobody has to hand-write IMAP syntax.
            {"key": "from_contains", "label": "From contains", "type": "text",
             "placeholder": "e.g. @vendor.com"},
            {"key": "to_contains", "label": "To contains", "type": "text",
             "placeholder": "e.g. @accountants.com"},
            {"key": "subject_contains", "label": "Subject contains", "type": "text",
             "placeholder": "e.g. invoice"},
            {"key": "since", "label": "Since", "type": "date"},
            {"key": "until", "label": "Until", "type": "date"},
            {"key": "unread_only", "label": "Unread only", "type": "checkbox"},
            {"key": "max_emails", "label": "Max emails", "type": "number", "default": 50},
            {"key": "search_filter", "label": "Advanced filter", "type": "text",
             "placeholder": 'raw IMAP — e.g. HEADER X-Label invoice',
             "hint": "Optional. Overrides every field above."},
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
