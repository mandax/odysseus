"""Email Extraction Profiles ☴ prompt-based email data extraction into tables."""

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from core.database import SessionLocal, EmailExtractionProfile, Document
from src.auth_helpers import get_current_user

logger = logging.getLogger(__name__)


# ── Pydantic schemas ──────────────────────────────────────────────────

class ExtractionProfileCreate(BaseModel):
    name: str
    prompt: str
    folder: str = "INBOX"
    account_id: Optional[str] = None
    search_filter: Optional[str] = None
    max_emails: int = Field(default=50, ge=1, le=500)
    schedule: Optional[str] = None
    enabled: bool = True


class ExtractionProfileUpdate(BaseModel):
    name: Optional[str] = None
    prompt: Optional[str] = None
    folder: Optional[str] = None
    account_id: Optional[str] = None
    search_filter: Optional[str] = None
    max_emails: Optional[int] = Field(default=None, ge=1, le=500)
    schedule: Optional[str] = None
    enabled: Optional[bool] = None


class ExtractionRunResult(BaseModel):
    rows: list[dict]
    columns: list[str]
    summary: str
    doc_id: Optional[str] = None
    elapsed_ms: int


# ── Helpers ──────────── ───────────────────────────────────────────────────

def _owner(request: Request) -> str:
    return get_current_user(request) or ""


def _profile_to_dict(p: EmailExtractionProfile) -> dict:
    return {
        "id": p.id,
        "owner": p.owner,
        "name": p.name,
        "prompt": p.prompt,
        "folder": p.folder,
        "account_id": p.account_id,
        "search_filter": p.search_filter,
        "max_emails": p.max_emails,
        "schedule": p.schedule,
        "enabled": p.enabled,
        "last_run_at": p.last_run_at.isoformat() if p.last_run_at else None,
        "last_result_doc_id": p.last_result_doc_id,
        "last_summary": p.last_summary,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


# ── Router setup ──────────────────────────────────────────────────────────────────



# -- Table helpers ----------------------------------------------------------

def _parse_table_from_markdown(md: str):
    """Parse a markdown table into list of dicts. Returns (rows, columns)."""
    lines = md.strip().split("\n")
    header_idx = None
    for i, line in enumerate(lines):
        if line.startswith("|") and "---" not in line:
            header_idx = i
            break
    if header_idx is None:
        return [], []

    if header_idx + 1 >= len(lines):
        return [], []
    sep = lines[header_idx + 1]
    if not all(c in "|-: " for c in sep.strip()):
        return [], []

    columns = [c.strip() for c in lines[header_idx].split("|")[1:-1]]
    rows = []
    for line in lines[header_idx + 2:]:
        line = line.strip()
        if not line.startswith("|"):
            break
        cells = [c.strip() for c in line.split("|")[1:-1]]
        if len(cells) != len(columns):
            continue
        row = {}
        for j, col in enumerate(columns):
            row[col] = cells[j] if j < len(cells) else ""
        rows.append(row)
    return rows, columns


def _rows_to_csv(rows: list, columns: list) -> str:
    """Convert rows+columns to CSV string."""
    import csv, io
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buf.getvalue()


def setup_email_extraction_routes():
    router = APIRouter(prefix="/api/email-extraction", tags=["email-extraction"])

    @router.get("/profiles")
    async def list_profiles(request: Request):
        """List all extraction profiles for the current user."""
        owner = _owner(request)
        db = SessionLocal()
        try:
            q = db.query(EmailExtractionProfile)
            if owner:
                q = q.filter(EmailExtractionProfile.owner == owner)
            profiles = q.order_by(EmailExtractionProfile.name.asc()).all()
            return {"profiles": [_profile_to_dict(p) for p in profiles]}
        finally:
            db.close()

    @router.post("/profiles")
    async def create_profile(payload: ExtractionProfileCreate, request: Request):
        """Create a new extraction profile."""
        owner = _owner(request)
        db = SessionLocal()
        try:
            profile = EmailExtractionProfile(
                id=uuid.uuid4().hex[:12],
                owner=owner,
                name=payload.name.strip(),
                prompt=payload.prompt.strip(),
                folder=payload.folder or "INBOX",
                account_id=payload.account_id,
                search_filter=payload.search_filter,
                max_emails=payload.max_emails,
                schedule=payload.schedule,
                enabled=payload.enabled,
            )
            db.add(profile)
            db.commit()
            db.refresh(profile)
            return _profile_to_dict(profile)
        finally:
            db.close()

    @router.put("/profiles/{profile_id}")
    async def update_profile(profile_id: str, payload: ExtractionProfileUpdate, request: Request):
        """Update an extraction profile."""
        owner = _owner(request)
        db = SessionLocal()
        try:
            profile = db.query(EmailExtractionProfile).filter(
                EmailExtractionProfile.id == profile_id,
                EmailExtractionProfile.owner == owner,
            ).first()
            if not profile:
                raise HTTPException(status_code=404, detail="Profile not found")

            if payload.name is not None:
                profile.name = payload.name.strip()
            if payload.prompt is not None:
                profile.prompt = payload.prompt.strip()
            if payload.folder is not None:
                profile.folder = payload.folder
            if payload.account_id is not None:
                profile.account_id = payload.account_id
            if payload.search_filter is not None:
                profile.search_filter = payload.search_filter
            if payload.max_emails is not None:
                profile.max_emails = payload.max_emails
            if payload.schedule is not None:
                profile.schedule = payload.schedule
            if payload.enabled is not None:
                profile.enabled = payload.enabled

            db.commit()
            db.refresh(profile)
            return _profile_to_dict(profile)
        finally:
            db.close()

    @router.delete("/profiles/{profile_id}")
    async def delete_profile(profile_id: str, request: Request):
        """Delete an extraction profile."""
        owner = _owner(request)
        db = SessionLocal()
        try:
            profile = db.query(EmailExtractionProfile).filter(
                EmailExtractionProfile.id == profile_id,
                EmailExtractionProfile.owner == owner,
            ).first()
            if not profile:
                raise HTTPException(status_code=404, detail="Profile not found")
            db.delete(profile)
            db.commit()
            return {"ok": True}
        finally:
            db.close()

    @router.post("/profiles/{profile_id}/run")
    async def run_extraction(profile_id: str, request: Request):
        """Run extraction now for a profile. Returns table rows + columns."""
        owner = _owner(request)
        db = SessionLocal()
        try:
            profile = db.query(EmailExtractionProfile).filter(
                EmailExtractionProfile.id == profile_id,
                EmailExtractionProfile.owner == owner,
            ).first()
            if not profile:
                raise HTTPException(status_code=404, detail="Profile not found")
        finally:
            db.close()

        import time as _time
        t0 = _time.monotonic()

        try:
            result = await _run_extraction_for_profile(profile)
        except Exception as e:
            logger.error(f"Extraction run failed for profile {profile_id}: {e}")
            raise HTTPException(status_code=500, detail=str(e))

        elapsed_ms = int((_time.monotonic() - t0) * 1000)

        # Save result as a document
        doc = await _save_extraction_document(profile, result, owner)
        if doc:
            db = SessionLocal()
            try:
                p = db.query(EmailExtractionProfile).filter(
                    EmailExtractionProfile.id == profile_id,
                ).first()
                if p:
                    p.last_run_at = datetime.now(timezone.utc).replace(tzinfo=None)
                    p.last_result_doc_id = doc.id
                    p.last_summary = result.get("summary", "")
                    db.commit()
            finally:
                db.close()

        return ExtractionRunResult(
            rows=result.get("rows", []),
            columns=result.get("columns", []),
            summary=result.get("summary", ""),
            doc_id=doc.id if doc else None,
            elapsed_ms=elapsed_ms,
        )

    @router.get("/profiles/{profile_id}/results")
    async def get_results(profile_id: str, request: Request):
        """Get the last extraction results for a profile."""
        owner = _owner(request)
        db = SessionLocal()
        try:
            profile = db.query(EmailExtractionProfile).filter(
                EmailExtractionProfile.id == profile_id,
                EmailExtractionProfile.owner == owner,
            ).first()
            if not profile:
                raise HTTPException(status_code=404, detail="Profile not found")

            if not profile.last_result_doc_id:
                return {"rows": [], "columns": [], "summary": None}

            doc = db.query(Document).filter(
                Document.id == profile.last_result_doc_id,
            ).first()
            if not doc:
                return {"rows": [], "columns": [], "summary": profile.last_summary}

            content = doc.current_content or ""
            rows, columns = _parse_table_from_markdown(content)
            return {
                "rows": rows,
                "columns": columns,
                "summary": profile.last_summary,
                "doc_id": doc.id,
                "doc_name": doc.title,
                "last_run_at": profile.last_run_at.isoformat() if profile.last_run_at else None,
            }
        finally:
            db.close()

    @router.get("/profiles/{profile_id}/results.csv")
    async def download_csv(profile_id: str, request: Request):
        """Download last extraction results as CSV."""
        from fastapi.responses import Response

        owner = _owner(request)
        db = SessionLocal()
        try:
            profile = db.query(EmailExtractionProfile).filter(
                EmailExtractionProfile.id == profile_id,
                EmailExtractionProfile.owner == owner,
            ).first()
            if not profile:
                raise HTTPException(status_code=404, detail="Profile not found")

            if not profile.last_result_doc_id:
                raise HTTPException(status_code=404, detail="No results yet")

            doc = db.query(Document).filter(
                Document.id == profile.last_result_doc_id,
            ).first()
            if not doc:
                raise HTTPException(status_code=404, detail="Result document not found")

            rows, columns = _parse_table_from_markdown(doc.current_content or "")
            csv_content = _rows_to_csv(rows, columns)

            safe_name = profile.name.replace(" ", "_").replace("/", "_")
            return Response(
                content=csv_content,
                media_type="text/csv",
                headers={
                    "Content-Disposition": f'attachment; filename="{safe_name}.csv"',
                },
            )
        finally:
            db.close()

    return router


# ── Extraction engine ──────── ────────────────────────────────────────────

async def _run_extraction_for_profile(profile) -> dict:
    """Core extraction: fetch emails, call LLM, parse structured result."""
    import asyncio
    import email as _email_mod

    from routes.email_helpers import _imap_connect, _extract_text, _decode_header

    account_id = profile.account_id
    owner = profile.owner or ""

    try:
        conn = _imap_connect(account_id, owner=owner)
    except Exception as e:
        raise RuntimeError(f"IMAP connection failed: {e}")

    try:
        folder = profile.folder or "INBOX"
        status, _ = conn.select(folder, readonly=True)
        if status != "OK":
            raise RuntimeError(f"Cannot open folder: {folder}")

        search_criteria = profile.search_filter or "ALL"
        status, data = conn.search(None, search_criteria)
        if status != "OK":
            raise RuntimeError(f"Search failed: {search_criteria}")

        uids = data[0].split()
        if not uids:
            return {"rows": [], "columns": [], "summary": "No emails matched the filter."}

        uids = uids[-profile.max_emails:]
        emails = []

        for uid in uids:
            try:
                status, msg_data = conn.fetch(uid, "(RFC822)")
                if status != "OK":
                    continue
                raw = msg_data[0][1]
                msg = _email_mod.message_from_bytes(raw)

                subject = _decode_header(msg.get("Subject", ""))
                from_addr = _decode_header(msg.get("From", ""))
                date_str = msg.get("Date", "")
                body = _extract_text(msg)[:4000]

                emails.append({
                    "uid": uid.decode() if isinstance(uid, bytes) else str(uid),
                    "subject": subject,
                    "from": from_addr,
                    "date": date_str,
                    "body": body,
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

    if not emails:
        return {"rows": [], "columns": [], "summary": "Could not parse any emails."}

    # 2. Call LLM
    from src.task_endpoint import task_llm_call_async as _llm

    emails_json = json.dumps(emails, ensure_ascii=False, indent=2)
    system_prompt = f"""You are a data extraction assistant. The user has provided a list of emails in JSON format.
Extract the requested data from each email. Return ONLY valid JSON — no markdown, no explanation.

{profile.prompt}

Return format:
{{
  "columns": ["Column1", "Column2", ...],
  "rows": [
    {{"Column1": "value", "Column2": "value", ...}},
    ...
  ],
  "summary": "One-line summary of what was extracted"
}}"""

    user_message = f"Emails to extract from:\n{emails_json}"

    try:
        response = await asyncio.wait_for(
            _llm(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                owner=owner or None,
                temperature=0.1,
                max_tokens=8000,
            ),
            timeout=120,
        )
    except asyncio.TimeoutError:
        raise RuntimeError("LLM call timed out (120s)")
    except Exception as e:
        raise RuntimeError(f"LLM call failed: {e}")

    # 3. Parse LLM response
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

    return {
        "rows": rows,
        "columns": columns,
        "summary": summary,
    }


async def _save_extraction_document(profile, result: dict, owner: str):
    """Save extraction results as a Document for viewing in the UI."""
    rows = result.get("rows", [])
    columns = result.get("columns", [])
    summary = result.get("summary", "")

    if not rows:
        return None

    md = f"# {profile.name}\n\n"
    md += f"*{summary}*\n\n"
    md += f"**Source:** {profile.folder}"
    if profile.search_filter:
        md += f" (filter: `{profile.search_filter}`)"
    md += f" | **Emails scanned:** up to {profile.max_emails}\n\n"

    if columns:
        md += "| " + " | ".join(columns) + " |\n"
        md += "| " + " | ".join(["---"] * len(columns)) + " |\n"
        for row in rows:
            vals = [str(row.get(c, "")) for c in columns]
            md += "| " + " | ".join(vals) + " |\n"

    db = SessionLocal()
    try:
        doc = Document(
            id=uuid.uuid4().hex[:12],
            owner=owner,
            title=f"Extraction: {profile.name}",
            language="markdown",
            current_content=md,
            version_count=1,
            is_active=True,
        )
        db.add(doc)
        db.commit()
        db.refresh(doc)
        return doc
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Router setup
