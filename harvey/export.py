"""Prospect-list export — the list itself is the product.

Produces a CSV that drops straight into Instantly, Smartlead, or any
sequencer (email / first_name / last_name / company_name / personalization
columns), plus Harvey's own quality columns (score, email_status, signals)
so a human can eyeball what they're getting.
"""

import csv
import io
import json
import logging

from harvey.models.campaign import Campaign
from harvey.state import StateManager

logger = logging.getLogger("harvey.export")

# Column order chosen so the file imports cleanly into cold-email tools
# without remapping; extra columns are simply available as custom variables.
CSV_COLUMNS = [
    "email", "first_name", "last_name", "company_name", "title",
    "website", "linkedin_url", "industry", "personalization",
    "email_status", "score", "seniority", "source", "status", "created_at",
]

DEFAULT_EMAIL_STATUSES = ["verified", "risky"]


async def collect_prospects(
    state: StateManager,
    email_statuses: list[str] | None = None,
    min_score: int = 0,
    statuses: list[str] | None = None,
    include_all: bool = False,
) -> list[dict]:
    """Fetch prospects matching the filters as export-ready dicts.

    Default view is the *deliverable* list (verified + risky emails).
    ``include_all`` disables every filter for a full dump.
    """
    import aiosqlite

    where = []
    params: list = []
    if not include_all:
        wanted = email_statuses if email_statuses is not None else DEFAULT_EMAIL_STATUSES
        if wanted:
            placeholders = ",".join("?" for _ in wanted)
            where.append(f"email != '' AND email_status IN ({placeholders})")
            params.extend(wanted)
        if min_score > 0:
            where.append("score >= ?")
            params.append(min_score)
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            where.append(f"status IN ({placeholders})")
            params.extend(statuses)

    sql = "SELECT * FROM prospects"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY score DESC, created_at DESC"

    async with aiosqlite.connect(state.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cursor:
            rows = [dict(r) for r in await cursor.fetchall()]

    out = []
    for r in rows:
        out.append({
            "email": r.get("email", ""),
            "first_name": r.get("first_name", ""),
            "last_name": r.get("last_name", ""),
            "company_name": r.get("company", ""),
            "title": r.get("title", ""),
            "website": r.get("source_url", ""),
            "linkedin_url": r.get("linkedin_url", ""),
            "industry": r.get("industry", ""),
            "personalization": r.get("personalization_notes", ""),
            "email_status": r.get("email_status", ""),
            "score": r.get("score", 0),
            "seniority": r.get("seniority", ""),
            "source": r.get("source", ""),
            "status": r.get("status", ""),
            "created_at": r.get("created_at", ""),
        })
    return out


def to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buf.getvalue()


async def export_prospects_csv(
    state: StateManager,
    out_path: str | None = None,
    email_statuses: list[str] | None = None,
    min_score: int = 0,
    statuses: list[str] | None = None,
    include_all: bool = False,
) -> tuple[int, str]:
    """Export prospects to CSV. Returns (row_count, csv_text).

    Writes to ``out_path`` when given; caller handles printing/serving.
    """
    rows = await collect_prospects(
        state,
        email_statuses=email_statuses,
        min_score=min_score,
        statuses=statuses,
        include_all=include_all,
    )
    text = to_csv(rows)
    if out_path:
        with open(out_path, "w", newline="") as f:
            f.write(text)
        logger.info(f"Exported {len(rows)} prospects to {out_path}")
    return len(rows), text


# ── Lead export (JSON) — for bridging Harvey into another CRM ──────────

LEAD_EXPORT_VERSION = 1


def _render_template(text: str, prospect: dict) -> str:
    """Fill {{merge}} variables of a campaign-level template step."""
    values = {
        "first_name": prospect.get("first_name", ""),
        "last_name": prospect.get("last_name", ""),
        "company": prospect.get("company", ""),
        "title": prospect.get("title", ""),
        "personalization": prospect.get("personalization_notes", ""),
    }
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value or "")
        text = text.replace("{{ " + key + " }}", value or "")
    return text.strip()


async def collect_leads(
    state: StateManager,
    since: str = "",
    email_statuses: list[str] | None = None,
    include_all: bool = False,
    require_draft: bool = False,
    limit: int = 0,
) -> list[dict]:
    """Prospects + their company, observations and step-1 draft, as plain dicts.

    A stable, documented shape so a bridge never has to read the SQLite
    schema. ``since`` is an ISO timestamp compared with the prospect's
    created_at/updated_at. The draft is the per-prospect personalized email
    when one was written, else campaign step 1 rendered with the prospect's
    merge variables (``draft.source`` says which).
    """
    import aiosqlite

    where, params = [], []
    if not include_all:
        wanted = email_statuses if email_statuses is not None else DEFAULT_EMAIL_STATUSES
        if wanted:
            where.append(
                "email != '' AND email_status IN (%s)" % ",".join("?" for _ in wanted)
            )
            params.extend(wanted)
    if since:
        where.append("(created_at >= ? OR updated_at >= ?)")
        params.extend([since, since])
    sql = "SELECT * FROM prospects"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at ASC"

    async with aiosqlite.connect(state.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cursor:
            rows = [dict(r) for r in await cursor.fetchall()]
        # campaign id -> step-1 template, for the fallback draft
        templates: dict[str, dict] = {}
        async with db.execute("SELECT id, sequence_json, prospect_ids_json FROM campaigns") as cursor:
            for c in await cursor.fetchall():
                steps = Campaign.sequence_from_json(c["sequence_json"])
                first = next((st for st in steps if st.step == 1), None)
                if not first:
                    continue
                try:
                    pids = json.loads(c["prospect_ids_json"] or "[]")
                except (TypeError, ValueError):
                    pids = []
                for pid in pids:
                    templates[pid] = {"campaign_id": c["id"], "subject": first.subject, "body": first.body}

    out: list[dict] = []
    for r in rows:
        draft = None
        stored = await state.get_draft(r["id"], 1)
        if stored and stored.get("subject") and stored.get("body"):
            draft = {
                "step": 1, "source": "personalized",
                "subject": stored["subject"], "body": stored["body"],
                "campaign_id": stored.get("campaign_id", ""),
            }
        elif r["id"] in templates:
            t = templates[r["id"]]
            draft = {
                "step": 1, "source": "template",
                "subject": _render_template(t["subject"], r),
                "body": _render_template(t["body"], r),
                "campaign_id": t["campaign_id"],
            }
        if require_draft and not draft:
            continue

        company = None
        observations: list[dict] = []
        if r.get("company_id"):
            c = await state.get_company(r["company_id"])
            if c:
                company = {
                    "id": c.id, "name": c.name, "domain": c.domain,
                    "website": c.website, "description": c.description,
                    "industry": c.industry, "company_size": c.company_size,
                    "location": c.location, "phone": c.phone,
                    "source": c.source, "source_url": c.source_url,
                    "tech_stack": list(c.tech_stack), "signals": list(c.signals),
                }
            for o in await state.get_observations(company_id=r["company_id"], latest_only=True, limit=200):
                observations.append({
                    "signal_code": o.get("signal_code", ""),
                    "value_num": o.get("value_num"),
                    "value_text": o.get("value_text", ""),
                    "confidence": o.get("confidence"),
                    "evidence_url": o.get("evidence_url", ""),
                    "observed_at": o.get("observed_at", ""),
                })

        out.append({
            "prospect_id": r["id"],
            "company_id": r.get("company_id", ""),
            "first_name": r.get("first_name", ""),
            "last_name": r.get("last_name", ""),
            "title": r.get("title", ""),
            "seniority": r.get("seniority", ""),
            "email": r.get("email", ""),
            "email_status": r.get("email_status", ""),
            "email_verified": bool(r.get("email_verified")),
            "phone": r.get("phone", ""),
            "linkedin_url": r.get("linkedin_url", ""),
            "score": r.get("score", 0),
            "personalization_notes": r.get("personalization_notes", ""),
            "prospect_status": r.get("status", ""),
            "source": r.get("source", ""),
            "source_url": r.get("source_url", ""),
            "company_name": r.get("company", ""),
            "industry": r.get("industry", ""),
            "created_at": r.get("created_at", ""),
            "updated_at": r.get("updated_at", ""),
            "company": company,
            "observations": observations,
            "draft": draft,
        })
        if limit and len(out) >= limit:
            break
    return out


def leads_to_json(leads: list[dict]) -> str:
    return json.dumps(
        {"version": LEAD_EXPORT_VERSION, "count": len(leads), "leads": leads},
        indent=2, default=str, ensure_ascii=False,
    )
