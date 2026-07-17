# Email Data Extraction Feature - Design Document

**Date:** 2026-07-17
**Author:** Anderson (via Odysseus agent)
**Target repo:** mandax/odysseus (fork), branch dev

---

## 1. Summary

A feature that lets users define **extraction profiles** — each a named LLM prompt that scans incoming email and extracts structured data points into a **live document table**. Every profile produces its own page (one document per profile), rendered as a sortable/filterable HTML table with CSV download. Profiles are saved as user settings and can run on a schedule or on demand.

---

## 2. Idiomatic Architecture

Odysseus is a **FastAPI + vanilla JS SPA** with no build step. The pattern to follow is:

| Layer | Pattern | Precedent |
|-------|---------|-----------|
| **Route** | routes/email_extraction_routes.py, APIRouter returned by setup_...() | routes/document_routes.py |
| **Models** | Single new DB table EmailExtractionProfile | core/database.py ScheduledTask style |
| **LLM** | src/ai_interaction.py dispatch_ai_tool() OpenAI-compatible | Existing email summarization |
| **Scheduling** | ScheduledTask with task_type=action | src/task_scheduler.py |
| **Frontend** | Vanilla ES6 module static/js/emailExtraction.js | static/js/emailLibrary.js |
| **Settings** | JSON blob stored via new DB table | routes/assistant_routes.py |
| **Documents** | One Document row per profile, updated in-place | Existing document CRUD |


### Why NOT a separate "page" system

Odysseus has no page router — everything lives in one SPA shell with the sidebar. The Documents system already provides exactly what we need: a rendered markdown table view, CSV export logic, and API CRUD. Each extraction profile maps 1:1 to a Document.

---

## 3. Data Model

### 3.1 New table: email_extraction_profiles

```python
class EmailExtractionProfile(TimestampMixin, Base):
    __tablename__ = "email_extraction_profiles"

    id              = Column(String, primary_key=True, index=True)
    owner           = Column(String, nullable=True, index=True)
    name            = Column(String, nullable=False)
    prompt          = Column(Text, nullable=False)
    output_doc_id   = Column(String, ForeignKey("documents.id", ondelete="SET NULL"), nullable=True)
    
    # Scanning config
    folder          = Column(String, default="INBOX")
    filter_query    = Column(Text, nullable=True)
    max_emails_per_run = Column(Integer, default=20)
    mark_processed   = Column(Boolean, default=False)
    
    # Tracking
    last_uid_scanned = Column(String, nullable=True)
    last_run        = Column(DateTime, nullable=True)
    run_count       = Column(Integer, default=0)
    status          = Column(String, default="active")
    
    # Schedule linkage
    task_id         = Column(String, ForeignKey("scheduled_tasks.id", ondelete="SET NULL"), nullable=True)
    
    # Output format
    output_fields   = Column(Text, nullable=True)
```

### 3.2 CSF fields array

When the user configures the prompt, they also specify the expected output fields (column names). The LLM is instructed to return a JSON array of objects, each with those keys. Example:

```json
{"output_fields": ["Date", "Vendor", "Amount", "Due Date", "Invoice #"]}
```

### 3.3 Document format (stored content)

Each profile's output document stores a **markdown table** as its current_content:

```markdown
# Invoice Tracker

*Last scan: 2026-07-17 10:30 | 47 emails processed | 3 new entries*

| Date | Vendor | Amount | Due Date | Invoice # | Source Email |
|------|--------|--------|----------|------------|-------------|
| 2026-07-15 | AWS | 1234.50 | 2026-08-15 | INV-001 | Re: July billing |
| 2026-07-14 | GCP | 567.00 | 2026-08-14 | INV-002 | Cloud invoice |
```

The document renderer (static/js/document.js) already renders markdown tables. CSF download can be derived from the same data.



---

## 4. API Design

### 4.1 Profile CRUD

All under prefix /api/email-extraction:

| Method | Path | Purpose |
|--------|------|---------|
| GET | /api/email-extraction/profiles | List user profiles |
| POST | /api/email-extraction/profiles | Create a new profile |
| GET | /api/email-extraction/profiles/{id} | Get single profile |
| PUT | /api/email-extraction/profiles/{id} | Update profile |
| DELETE | /api/email-extraction/profiles/{id} | Delete profile + linked doc |
| POST | /api/email-extraction/profiles/{id}/run | Run extraction now |
| POST | /api/email-extraction/profiles/{id}/schedule | Create/update linked task |
| GET | /api/email-extraction/profiles/{id}/results | Get output doc contents |
| GET | /api/email-extraction/profiles/{id}/csv | Download results as CSV |

### 4.2 Extraction flow (POST .../run)

1. Load profile from DB
2. Call email_helpers IMAP functions to fetch emails
3. Batch emails into chunks of 5-10 to stay under context limits
4. Call LLM via llm_call_async() with the user extraction prompt + email text
5. Parse LLM response as JSON array, validate against output_fields
6. Merge new entries into the document markdown table
7. Update last_uid_scanned
8. Return count of new entries extracted

### 4.3 Prompt template sent to LLM

SYSTEM: You extract structured data from emails. Return ONLY a JSON array of objects.
No explanations, no markdown wrapper. Each object must have exactly these keys: {fields}
If an email contains no relevant data, omit it from the array.
If a field value is not found, use null.

USER: {user_prompt}

Emails to process:
{email_texts}

---

## 5. Frontend Design

### 5.1 New JS module: static/js/emailExtraction.js

Follows the pattern of static/js/emailLibrary.js — a module that manages its own state
and mounts UI into the main content area.

Key functions:
- renderProfileList() — sidebar listing of all profiles
- renderProfileEditor(profileId?) — create/edit form modal
- renderResultsTable(profileId) — document rendered as HTML table
- downloadCSV(profileId) — triggers CSV endpoint
- runExtractionNow(profileId) — POST to run, show spinner, refresh

### 5.2 Settings UI (profile editor)

A modal or inline form with:
- Name — text input
- Prompt — textarea with placeholder
- Output Fields — tag-style or comma-separated text
- Folder — dropdown (INBOX, Archive, etc.)
- Max emails per run — number input (default 20)
- Schedule — reuse existing task schedule picker
- Save & Run Now buttons

### 5.3 Table view

Rendered via the existing document markdown renderer. The document.js renderDocument()
already handles markdown tables. We add a "Download CSV" button to extraction document
toolbars.

### 5.4 Sidebar integration

Add a new section "Email Extraction" under the existing "Email" sidebar group.
Hook into static/js/section-management.js to register the new section.



---

## 6. Scheduling Integration

### 6.1 New built-in action: run_email_extraction

Register in src/task_scheduler.py alongside existing actions (tidy_sessions, summarize_emails, etc.):

```python
if action == "run_email_extraction":
    from routes.email_extraction_routes import run_extraction_for_profile
    await run_extraction_for_profile(profile_id=task.metadata_json.get("profile_id"))
```

### 6.2 Task creation flow

When a user creates a schedule for a profile:
1. Create a ScheduledTask with task_type="action", action="run_email_extraction"
2. Store the profile_id in the task metadata
3. Link back: store task_id on the EmailExtractionProfile
4. The task scheduler handles the rest

---

## 7. File-by-File Implementation Plan

| # | File | Action | Lines (est.) |
|---|------|--------|-------------|
| 1 | core/database.py | Add EmailExtractionProfile model | ~40 |
| 2 | routes/email_extraction_routes.py | NEW: all route handlers | ~300 |
| 3 | src/task_scheduler.py | Add run_email_extraction action handler | ~30 |
| 4 | static/js/emailExtraction.js | NEW: frontend module | ~350 |
| 5 | static/js/section-management.js | Register new sidebar section | ~15 |
| 6 | static/js/document.js | Add CSV download button for extraction docs | ~25 |
| 7 | app.py | Import + include_router | ~5 |
| 8 | routes/task_routes.py | Add to allowed actions list | ~3 |

**Total:** ~768 lines across 8 files (5 new, 3 modified)

---

## 8. Database Migration

Odysseus creates tables automatically on startup via Base.metadata.create_all().
Adding the new model to core/database.py is sufficient. For existing deployments:

```python
try:
    with engine.connect() as conn:
        conn.execute(text("SELECT 1 FROM email_extraction_profiles LIMIT 0"))
except:
    EmailExtractionProfile.__table__.create(engine)
```

---

## 9. CSV Download Design

Two approaches:

**A (simpler):** Parse markdown table from Document.current_content, extract rows, write CSV.

**B (more robust):** Store raw JSON alongside the markdown in the document metadata.
This doubles as the CSV source and the LLM response log.

**Recommendation:** Use approach B with a raw_results TEXT field on the document.
This avoids fragile markdown parsing and gives a clean audit trail.

---

## 10. Two Deployment Paths

### Path A: Contribute to upstream Odysseus (PR)

- Fork is already at mandax/odysseus
- Branch off dev, implement per the plan above
- Open PR against upstream
- **Pros:** Everyone benefits, you stay on upstream
- **Cons:** PR review latency, potential design discussion

### Path B: Local-only (skill-based)

Use Odysseus existing skill system to orchestrate extraction without any code changes:

1. Create a skill that:
   - Calls /api/email/search to find new emails
   - Calls /api/email/read/{uid} for each
   - Calls the LLM via chat API with the extraction prompt
   - Appends results to a Document via /api/document/{doc_id} (PATCH)
2. Create a ScheduledTask that runs the skill daily
3. Create a Document that serves as the results table

**Pros:** Zero code, works today, survives upgrades
**Cons:** No dedicated UI, no CSV download button, less polished

---

## 11. Recommended Approach

**Build the local skill version first** (2-3 hours) to validate the extraction prompts
and data flow. This gives you a working prototype without waiting for PR review.

Then **implement the native feature** (8-12 hours) and submit it as a PR.
The skill prototype will inform the API design and help you write better tests.

---

## 12. Risk Assessment

| Risk | Mitigation |
|------|-----------|
| LLM hallucinates fields not in schema | Validate returned JSON against output_fields before inserting |
| Large email batches blow context window | Chunk emails into groups of 5-10; configurable max_emails_per_run |
| Duplicate entries from overlapping scans | Track last_uid_scanned; deduplicate by email message-id |
| Long-running extraction blocks event loop | Run as background task; update status in DB |
| IMAP connection drops mid-scan | Reuse existing _open_imap_connection with timeout/retry logic |

---

## Appendix: Key Source Files During Implementation

| File | What to reference |
|------|------------------|
| routes/email_routes.py | /api/email/search (L2115), /api/email/read/{uid} (L2462) |
| routes/email_helpers.py | _extract_text(), _extract_html(), _get_email_config(), _open_imap_connection() |
| routes/document_routes.py | create_document(), update_document(), patch_document() |
| src/llm_core.py | llm_call_async() for LLM calls |
| src/ai_interaction.py | dispatch_ai_tool() for how tools dispatch to LLM |
| src/task_scheduler.py | How built-in actions are dispatched, TASK_DEFAULT_SHELL_TOOLS pattern |
| routes/task_routes.py | Task CRUD, action validation |
| static/js/emailLibrary.js | Frontend pattern to follow for new module |
| static/js/section-management.js | Sidebar section registration |
| static/js/document.js | Document rendering (table support, CSV export) |
| app.py | Router registration pattern (lines 742-839) |
| core/database.py | Model definitions (Document L283, ScheduledTask L643) |
