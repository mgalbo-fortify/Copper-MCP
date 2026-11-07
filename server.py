"""
Copper CRM MCP server for a team (stopgap until Copper's official MCP ships).

Runs two ways:
  - Hosted for the whole team (HTTP, secret URL):  MCP_TRANSPORT=http python server.py
  - Locally for one person in Claude Desktop:      python server.py

Always required:
  COPPER_API_KEY     Copper API key (Settings > Integrations > API Keys in Copper)
  COPPER_USER_EMAIL  Email of the Copper user who owns that key

Optional:
  TEAM_TIMEZONE      IANA time zone for due dates and reminders, e.g. America/New_York (default UTC)

Required when hosted (MCP_TRANSPORT=http):
  MCP_SECRET         Long random string (32+ letters/numbers). The server is only reachable at
                     /mcp/<MCP_SECRET>, so that URL works like a password. Change it to revoke access.

Deliberately not included: deleting records. A leaked link should not be able to wipe the CRM.
"""

import os
from datetime import datetime, time, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo

import httpx
from fastmcp import FastMCP

BASE_URL = "https://api.copper.com/developer_api/v1"
HOSTED = os.getenv("MCP_TRANSPORT", "stdio") == "http"
TZ = ZoneInfo(os.getenv("TEAM_TIMEZONE", "UTC"))

RecordType = Literal["people", "companies", "opportunities", "leads", "projects", "tasks"]
EntityType = Literal["person", "company", "opportunity", "lead", "project", "task"]
PLURAL = {"person": "people", "company": "companies", "opportunity": "opportunities",
          "lead": "leads", "project": "projects", "task": "tasks"}

if HOSTED:
    SECRET = os.getenv("MCP_SECRET", "")
    if len(SECRET) < 32 or not SECRET.replace("-", "").replace("_", "").isalnum():
        raise SystemExit("Set MCP_SECRET to a random string of at least 32 letters/numbers before hosting.")

mcp = FastMCP(
    "copper-crm",
    instructions=(
        "Tools for the team's Copper CRM. Look up IDs before writing: list_users for assignees, "
        "list_pipelines for pipeline/stage IDs, get_reference_data for custom fields, sources, contact "
        "types and loss reasons. Dates are YYYY-MM-DD; times are YYYY-MM-DD HH:MM in the team's time zone. "
        "Confirm with the user before bulk changes."
    ),
)


@mcp.custom_route("/health", methods=["GET", "HEAD"])
async def health(request):
    """Public keep-alive endpoint for uptime pingers. Reveals nothing and touches no Copper data."""
    from starlette.responses import PlainTextResponse
    return PlainTextResponse("ok")


# ---------- Helpers ----------

def _headers() -> dict[str, str]:
    return {
        "X-PW-AccessToken": os.environ["COPPER_API_KEY"],
        "X-PW-Application": "developer_api",
        "X-PW-UserEmail": os.environ["COPPER_USER_EMAIL"],
        "Content-Type": "application/json",
    }


async def _copper(method: str, path: str, body: dict | None = None) -> Any:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.request(method, f"{BASE_URL}{path}", headers=_headers(), json=body)
        if r.status_code >= 400:
            return {"error": r.status_code, "detail": r.text[:500]}
        return r.json() if r.content else {"ok": True}


def _ts(value: str, end_of_day: bool = False) -> int:
    """'2026-11-20' or '2026-11-20 14:30' (team time zone) -> Unix timestamp.
    A bare date means 12:00 local (or 23:59 for the end of a range), so it never slips a day."""
    value = value.strip().replace("T", " ")
    if len(value) == 10:
        d = datetime.strptime(value, "%Y-%m-%d")
        dt = datetime.combine(d.date(), time(23, 59) if end_of_day else time(12, 0), TZ)
    else:
        dt = datetime.strptime(value[:16], "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    return int(dt.timestamp())


DATE_FIELDS = {"due_date", "reminder_date", "completed_date", "date_created", "date_modified",
               "activity_date", "date_last_contacted", "date_stage_changed", "date_lead_created"}


def _readable(obj: Any) -> Any:
    """Turn Copper's Unix timestamps into readable local times for Claude."""
    if isinstance(obj, list):
        return [_readable(x) for x in obj]
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in DATE_FIELDS and isinstance(v, (int, float)) and v > 0:
                out[k] = datetime.fromtimestamp(v, TZ).strftime("%Y-%m-%d %H:%M %Z")
            else:
                out[k] = _readable(v)
        return out
    return obj


SUMMARY_FIELDS = {
    "id", "name", "title", "company_name", "company_id", "emails", "email", "phone_numbers",
    "status", "monetary_value", "pipeline_id", "pipeline_stage_id", "close_date", "priority",
    "assignee_id", "tags", "due_date", "related_resource", "date_created", "date_modified",
    "date_last_contacted", "customer_source_id", "contact_type_id", "win_probability",
}


def _summary(result: Any) -> Any:
    if isinstance(result, list):
        result = [{k: v for k, v in r.items() if k in SUMMARY_FIELDS and v not in (None, [], "")} for r in result]
    return _readable(result)


def _drop_none(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


def _email(email: str | None, key: str = "emails") -> dict:
    if not email:
        return {}
    entry = {"email": email, "category": "work"}
    return {"email": entry} if key == "email" else {"emails": [entry]}


def _phone(phone: str | None) -> dict:
    return {"phone_numbers": [{"number": phone, "category": "work"}]} if phone else {}


def _custom(custom_fields: dict[str, Any] | None) -> dict:
    """{'123': 'value'} -> Copper's custom_fields list. IDs come from get_reference_data('custom_field_definitions')."""
    if not custom_fields:
        return {}
    return {"custom_fields": [{"custom_field_definition_id": int(k), "value": v} for k, v in custom_fields.items()]}


# ---------- Reference data ----------

@mcp.tool()
async def list_users() -> Any:
    """List Copper users (id, name, email). Use for assignee_id on any record or task."""
    result = await _copper("POST", "/users/search", {"page_size": 200})
    if isinstance(result, list):
        return [{"id": u.get("id"), "name": u.get("name"), "email": u.get("email")} for u in result]
    return result


@mcp.tool()
async def list_pipelines() -> Any:
    """List opportunity pipelines and their stages (IDs and names)."""
    return await _copper("GET", "/pipelines")


@mcp.tool()
async def get_reference_data(
    kind: Literal["custom_field_definitions", "customer_sources", "contact_types", "loss_reasons",
                  "activity_types", "tags", "account"],
) -> Any:
    """Look up account settings: custom field IDs and options, lead/customer sources, contact types,
    opportunity loss reasons, activity types (Note, Phone Call, Meeting...), existing tags, account info."""
    return await _copper("GET", f"/{kind}")


# ---------- Search & read ----------

@mcp.tool()
async def search_records(
    record_type: Literal["people", "companies", "opportunities", "leads", "projects"],
    name: str | None = None,
    email: str | None = None,
    tags: list[str] | None = None,
    assignee_ids: list[int] | None = None,
    created_after: str | None = None,
    created_before: str | None = None,
    modified_after: str | None = None,
    modified_before: str | None = None,
    sort_by: str | None = None,
    sort_direction: Literal["asc", "desc"] = "desc",
    extra_filters: dict[str, Any] | None = None,
    page_size: int = 25,
    page_number: int = 1,
) -> Any:
    """Search people, companies, opportunities, leads, or projects. Dates are YYYY-MM-DD.
    sort_by examples: name, date_modified, date_created. extra_filters passes any other Copper search
    filter through unchanged, e.g. {"pipeline_ids": [1]}, {"customer_source_ids": [5]},
    {"contact_type_ids": [2]}, {"city": "Austin"}, {"statuses": ["Open"]} for opportunities.
    Results are a summary; use get_record for full details including custom fields."""
    body: dict[str, Any] = {"page_size": min(page_size, 200), "page_number": page_number,
                            "sort_direction": sort_direction}
    if name:
        body["name"] = name
    if email and record_type in ("people", "leads"):
        body["emails"] = [email]
    if tags:
        body["tags"] = {"option": "ANY", "value": tags}
    if assignee_ids:
        body["assignee_ids"] = assignee_ids
    if created_after:
        body["minimum_created_date"] = _ts(created_after)
    if created_before:
        body["maximum_created_date"] = _ts(created_before, end_of_day=True)
    if modified_after:
        body["minimum_modified_date"] = _ts(modified_after)
    if modified_before:
        body["maximum_modified_date"] = _ts(modified_before, end_of_day=True)
    if sort_by:
        body["sort_by"] = sort_by
    body.update(extra_filters or {})
    return _summary(await _copper("POST", f"/{record_type}/search", body))


@mcp.tool()
async def search_opportunities_by_status(
    status: Literal["Open", "Won", "Lost", "Abandoned"] = "Open",
    pipeline_id: int | None = None,
    close_after: str | None = None,
    close_before: str | None = None,
    page_size: int = 50,
) -> Any:
    """List opportunities by status, optionally within a pipeline and expected close-date range (YYYY-MM-DD)."""
    body: dict[str, Any] = {"page_size": min(page_size, 200), "statuses": [status]}
    if pipeline_id:
        body["pipeline_ids"] = [pipeline_id]
    if close_after:
        body["minimum_close_date"] = _ts(close_after)
    if close_before:
        body["maximum_close_date"] = _ts(close_before, end_of_day=True)
    return _summary(await _copper("POST", "/opportunities/search", body))


@mcp.tool()
async def get_record(record_type: RecordType, record_id: int) -> Any:
    """Get the full details of one record, including custom fields."""
    return _readable(await _copper("GET", f"/{record_type}/{record_id}"))


@mcp.tool()
async def list_related_records(entity: EntityType, record_id: int,
                               related_type: EntityType | None = None) -> Any:
    """List records linked to a record, e.g. the people on an opportunity or the tasks on a company."""
    path = f"/{PLURAL[entity]}/{record_id}/related"
    if related_type:
        path += f"/{PLURAL[related_type]}"
    return await _copper("GET", path)


@mcp.tool()
async def list_activities(entity: Literal["person", "company", "opportunity", "lead", "project"],
                          record_id: int, page_size: int = 50) -> Any:
    """List activities (notes, calls, meetings, emails) logged on a record, newest first."""
    body = {"parent": {"type": entity, "id": record_id}, "page_size": min(page_size, 200)}
    return _readable(await _copper("POST", "/activities/search", body))


# ---------- Create ----------

@mcp.tool()
async def create_person(name: str, email: str | None = None, phone: str | None = None,
                        title: str | None = None, company_id: int | None = None,
                        assignee_id: int | None = None, contact_type_id: int | None = None,
                        tags: list[str] | None = None, details: str | None = None,
                        custom_fields: dict[str, Any] | None = None) -> Any:
    """Create a contact (person). Search first to avoid duplicates. custom_fields is {definition_id: value}."""
    body = _drop_none({"name": name, "title": title, "company_id": company_id, "assignee_id": assignee_id,
                       "contact_type_id": contact_type_id, "tags": tags, "details": details})
    body |= _email(email) | _phone(phone) | _custom(custom_fields)
    return _readable(await _copper("POST", "/people", body))


@mcp.tool()
async def create_company(name: str, email_domain: str | None = None, phone: str | None = None,
                         assignee_id: int | None = None, contact_type_id: int | None = None,
                         tags: list[str] | None = None, details: str | None = None,
                         custom_fields: dict[str, Any] | None = None) -> Any:
    """Create a company. Search first to avoid duplicates."""
    body = _drop_none({"name": name, "email_domain": email_domain, "assignee_id": assignee_id,
                       "contact_type_id": contact_type_id, "tags": tags, "details": details})
    body |= _phone(phone) | _custom(custom_fields)
    return _readable(await _copper("POST", "/companies", body))


@mcp.tool()
async def create_opportunity(name: str, primary_contact_id: int | None = None, company_id: int | None = None,
                             pipeline_id: int | None = None, pipeline_stage_id: int | None = None,
                             monetary_value: float | None = None, close_date: str | None = None,
                             priority: Literal["None", "Low", "Medium", "High"] | None = None,
                             win_probability: int | None = None, assignee_id: int | None = None,
                             customer_source_id: int | None = None, tags: list[str] | None = None,
                             details: str | None = None, custom_fields: dict[str, Any] | None = None) -> Any:
    """Create an opportunity (deal). close_date is YYYY-MM-DD. Get pipeline/stage IDs from list_pipelines."""
    body = _drop_none({"name": name, "primary_contact_id": primary_contact_id, "company_id": company_id,
                       "pipeline_id": pipeline_id, "pipeline_stage_id": pipeline_stage_id,
                       "monetary_value": monetary_value, "priority": priority, "win_probability": win_probability,
                       "assignee_id": assignee_id, "customer_source_id": customer_source_id,
                       "tags": tags, "details": details})
    if close_date:
        body["close_date"] = datetime.strptime(close_date, "%Y-%m-%d").strftime("%m/%d/%Y")
    body |= _custom(custom_fields)
    return _readable(await _copper("POST", "/opportunities", body))


@mcp.tool()
async def create_lead(name: str, email: str | None = None, phone: str | None = None,
                      company_name: str | None = None, title: str | None = None,
                      assignee_id: int | None = None, customer_source_id: int | None = None,
                      monetary_value: float | None = None, tags: list[str] | None = None,
                      details: str | None = None, custom_fields: dict[str, Any] | None = None) -> Any:
    """Create a lead. Use get_reference_data('customer_sources') for customer_source_id (e.g. a campaign source)."""
    body = _drop_none({"name": name, "company_name": company_name, "title": title, "assignee_id": assignee_id,
                       "customer_source_id": customer_source_id, "monetary_value": monetary_value, "tags": tags,
                       "details": details or "Created via Claude"})
    body |= _email(email, key="email") | _phone(phone) | _custom(custom_fields)
    return _readable(await _copper("POST", "/leads", body))


@mcp.tool()
async def create_project(name: str, assignee_id: int | None = None,
                         status: Literal["Open", "Completed"] = "Open", tags: list[str] | None = None,
                         details: str | None = None, custom_fields: dict[str, Any] | None = None) -> Any:
    """Create a project (e.g. a campaign or onboarding). Link records to it with link_records."""
    body = _drop_none({"name": name, "assignee_id": assignee_id, "status": status, "tags": tags, "details": details})
    body |= _custom(custom_fields)
    return _readable(await _copper("POST", "/projects", body))


# ---------- Update ----------

@mcp.tool()
async def update_record(record_type: RecordType, record_id: int, fields: dict[str, Any] | None = None,
                        custom_fields: dict[str, Any] | None = None) -> Any:
    """Update any fields on a record. Examples: {"pipeline_stage_id": 123}, {"assignee_id": 5},
    {"status": "Won"} for an opportunity, {"close_date": "12/31/2026"} (opportunities use MM/DD/YYYY).
    custom_fields is {definition_id: value}. To add or remove tags without replacing them, use update_tags."""
    body = dict(fields or {}) | _custom(custom_fields)
    if not body:
        return {"error": "Nothing to update."}
    return _readable(await _copper("PUT", f"/{record_type}/{record_id}", body))


@mcp.tool()
async def update_tags(record_type: RecordType, record_id: int, add: list[str] | None = None,
                      remove: list[str] | None = None) -> Any:
    """Add and/or remove tags on a record while keeping its other tags."""
    record = await _copper("GET", f"/{record_type}/{record_id}")
    if "error" in record:
        return record
    tags = [t for t in record.get("tags", []) if t not in (remove or [])]
    tags += [t for t in (add or []) if t not in tags]
    result = await _copper("PUT", f"/{record_type}/{record_id}", {"tags": tags})
    return {"id": record_id, "tags": result.get("tags", tags)} if "error" not in result else result


@mcp.tool()
async def link_records(entity: EntityType, record_id: int, related_type: EntityType, related_id: int) -> Any:
    """Link two records, e.g. add a person to an opportunity, or a company to a project."""
    body = {"resource": {"id": related_id, "type": related_type}}
    return await _copper("POST", f"/{PLURAL[entity]}/{record_id}/related", body)


@mcp.tool()
async def convert_lead(lead_id: int, company_name: str | None = None, create_opportunity: bool = False,
                       opportunity_name: str | None = None, pipeline_id: int | None = None,
                       pipeline_stage_id: int | None = None, monetary_value: float | None = None) -> Any:
    """Convert a lead into a person, optionally with a company and a new opportunity."""
    details: dict[str, Any] = {"person": {}}
    if company_name:
        details["company"] = {"name": company_name}
    if create_opportunity:
        details["opportunity"] = _drop_none({"name": opportunity_name or "New opportunity",
                                             "pipeline_id": pipeline_id, "pipeline_stage_id": pipeline_stage_id,
                                             "monetary_value": monetary_value})
    return _readable(await _copper("POST", f"/leads/{lead_id}/convert", {"details": details}))


# ---------- Deals & pipeline ----------

async def _pipelines() -> list[dict]:
    result = await _copper("GET", "/pipelines")
    return result if isinstance(result, list) else []


def _find(items: list[dict], wanted: str | int | None) -> dict | None:
    if wanted is None:
        return None
    for item in items:
        if str(item.get("id")) == str(wanted) or item.get("name", "").strip().lower() == str(wanted).strip().lower():
            return item
    return None


@mcp.tool()
async def update_opportunity(
    opportunity_id: int,
    stage: str | None = None,
    pipeline: str | None = None,
    status: Literal["Open", "Won", "Lost", "Abandoned"] | None = None,
    loss_reason: str | None = None,
    monetary_value: float | None = None,
    close_date: str | None = None,
    win_probability: int | None = None,
    priority: Literal["None", "Low", "Medium", "High"] | None = None,
    assignee_id: int | None = None,
    name: str | None = None,
    primary_contact_id: int | None = None,
    customer_source_id: int | None = None,
    details: str | None = None,
    custom_fields: dict[str, Any] | None = None,
) -> Any:
    """Update a deal. stage and pipeline accept a name ("Proposal Sent") or an ID. Moving to another
    pipeline requires a stage in that pipeline (defaults to its first stage). status Won/Lost/Abandoned
    closes the deal; loss_reason is a name or ID from get_reference_data('loss_reasons').
    close_date is YYYY-MM-DD. Returns the deal before and after so changes can be confirmed."""
    current = await _copper("GET", f"/opportunities/{opportunity_id}")
    if not isinstance(current, dict) or "error" in current:
        return current
    body: dict[str, Any] = _drop_none({
        "status": status, "monetary_value": monetary_value, "win_probability": win_probability,
        "priority": priority, "assignee_id": assignee_id, "name": name,
        "primary_contact_id": primary_contact_id, "customer_source_id": customer_source_id, "details": details,
    })

    if stage is not None or pipeline is not None:
        pipelines = await _pipelines()
        target = _find(pipelines, pipeline) if pipeline is not None else _find(pipelines, current.get("pipeline_id"))
        if not target:
            return {"error": f"Pipeline '{pipeline}' not found.", "pipelines": [p.get("name") for p in pipelines]}
        stages = target.get("stages", [])
        if stage is not None:
            target_stage = _find(stages, stage)
            if not target_stage:
                return {"error": f"Stage '{stage}' not found in pipeline '{target.get('name')}'.",
                        "stages": [s.get("name") for s in stages]}
        elif target.get("id") != current.get("pipeline_id"):
            target_stage = stages[0] if stages else None
        else:
            target_stage = None
        body["pipeline_id"] = target["id"]
        if target_stage:
            body["pipeline_stage_id"] = target_stage["id"]

    if loss_reason is not None:
        reasons = await _copper("GET", "/loss_reasons")
        reason = _find(reasons if isinstance(reasons, list) else [], loss_reason)
        if not reason:
            return {"error": f"Loss reason '{loss_reason}' not found.",
                    "loss_reasons": [r.get("name") for r in reasons] if isinstance(reasons, list) else reasons}
        body["loss_reason_id"] = reason["id"]
    if close_date:
        body["close_date"] = datetime.strptime(close_date, "%Y-%m-%d").strftime("%m/%d/%Y")
    body |= _custom(custom_fields)
    if not body:
        return {"error": "Nothing to update."}

    updated = await _copper("PUT", f"/opportunities/{opportunity_id}", body)
    if not isinstance(updated, dict) or "error" in updated:
        return updated
    watch = ["name", "status", "pipeline_id", "pipeline_stage_id", "monetary_value", "close_date",
             "win_probability", "priority", "assignee_id", "loss_reason_id"]
    return {"before": {k: current.get(k) for k in watch}, "after": {k: updated.get(k) for k in watch}}


@mcp.tool()
async def pipeline_summary(pipeline: str | None = None, assignee_id: int | None = None) -> Any:
    """Overview of open deals by pipeline and stage: count and total value per stage, plus the deals
    in each. pipeline is a name or ID (omit for all pipelines); assignee_id narrows to one owner."""
    pipelines = await _pipelines()
    if pipeline is not None:
        p = _find(pipelines, pipeline)
        if not p:
            return {"error": f"Pipeline '{pipeline}' not found.", "pipelines": [x.get("name") for x in pipelines]}
        pipelines = [p]

    deals: list[dict] = []
    for page in range(1, 11):  # up to 2,000 open deals
        body: dict[str, Any] = {"page_size": 200, "page_number": page, "statuses": ["Open"],
                                "pipeline_ids": [p["id"] for p in pipelines]}
        if assignee_id:
            body["assignee_ids"] = [assignee_id]
        batch = await _copper("POST", "/opportunities/search", body)
        if not isinstance(batch, list):
            return batch
        deals += batch
        if len(batch) < 200:
            break

    summary = []
    for p in pipelines:
        stages_out = []
        for s in p.get("stages", []):
            in_stage = [d for d in deals if d.get("pipeline_stage_id") == s["id"]]
            stages_out.append({
                "stage": s.get("name"), "stage_id": s["id"], "count": len(in_stage),
                "total_value": round(sum(d.get("monetary_value") or 0 for d in in_stage), 2),
                "deals": [{"id": d["id"], "name": d.get("name"), "value": d.get("monetary_value"),
                           "close_date": d.get("close_date"), "assignee_id": d.get("assignee_id")}
                          for d in in_stage],
            })
        summary.append({"pipeline": p.get("name"), "pipeline_id": p["id"],
                         "open_deals": sum(s["count"] for s in stages_out),
                         "total_value": round(sum(s["total_value"] for s in stages_out), 2),
                         "stages": stages_out})
    return summary


# ---------- Activities ----------

@mcp.tool()
async def log_activity(entity: Literal["person", "company", "opportunity", "lead", "project"], record_id: int,
                       details: str, activity_type: str = "Note", activity_date: str | None = None) -> Any:
    """Log a note, phone call, meeting, or other activity on a record. activity_type is a name from
    get_reference_data('activity_types'), e.g. "Note", "Phone Call", "Meeting". activity_date is optional
    (YYYY-MM-DD or YYYY-MM-DD HH:MM); defaults to now."""
    types = await _copper("GET", "/activity_types")
    user_types = types.get("user", []) if isinstance(types, dict) else []
    match = next((t for t in user_types if t.get("name", "").lower() == activity_type.lower()), None)
    if not match:
        return {"error": f"No activity type named '{activity_type}'.",
                "available": [t.get("name") for t in user_types]}
    body: dict[str, Any] = {"parent": {"type": entity, "id": record_id},
                            "type": {"category": "user", "id": match["id"]},
                            "details": f"{details}\n\n(Logged via Claude)"}
    if activity_date:
        body["activity_date"] = _ts(activity_date)
    return _readable(await _copper("POST", "/activities", body))


# ---------- Tasks ----------

@mcp.tool()
async def create_task(name: str, due_date: str | None = None, reminder: str | None = None,
                      assignee_id: int | None = None,
                      related_type: Literal["person", "company", "opportunity", "lead", "project"] | None = None,
                      related_id: int | None = None, priority: Literal["None", "High"] = "None",
                      details: str | None = None, tags: list[str] | None = None,
                      custom_fields: dict[str, Any] | None = None) -> Any:
    """Create a task. due_date: YYYY-MM-DD or YYYY-MM-DD HH:MM. reminder: when Copper should remind the
    assignee, same format. Use list_users for assignee_id (otherwise it goes to the API-key owner) and
    search_records to find the record to link (related_type + related_id)."""
    body: dict[str, Any] = _drop_none({"name": name, "priority": priority, "status": "Open",
                                       "assignee_id": assignee_id, "tags": tags})
    if due_date:
        body["due_date"] = _ts(due_date)
    if reminder:
        body["reminder_date"] = _ts(reminder)
    if related_type and related_id:
        body["related_resource"] = {"type": related_type, "id": related_id}
    body["details"] = f"{details}\n\n(Created via Claude)" if details else "Created via Claude"
    body |= _custom(custom_fields)
    return _readable(await _copper("POST", "/tasks", body))


@mcp.tool()
async def search_tasks(assignee_ids: list[int] | None = None,
                       status: Literal["Open", "Completed"] | None = "Open",
                       due_after: str | None = None, due_before: str | None = None,
                       name: str | None = None, tags: list[str] | None = None,
                       page_size: int = 50, page_number: int = 1) -> Any:
    """Search tasks, sorted by due date. Examples: open tasks for one teammate; tasks due this week
    (due_after/due_before as YYYY-MM-DD); overdue tasks (due_before = yesterday, status Open)."""
    body: dict[str, Any] = {"page_size": min(page_size, 200), "page_number": page_number,
                            "sort_by": "due_date", "sort_direction": "asc"}
    if assignee_ids:
        body["assignee_ids"] = assignee_ids
    if status:
        body["statuses"] = [status]
    if due_after:
        body["minimum_due_date"] = _ts(due_after)
    if due_before:
        body["maximum_due_date"] = _ts(due_before, end_of_day=True)
    if name:
        body["name"] = name
    if tags:
        body["tags"] = {"option": "ANY", "value": tags}
    return _summary(await _copper("POST", "/tasks/search", body))


@mcp.tool()
async def update_task(task_id: int, status: Literal["Open", "Completed"] | None = None,
                      due_date: str | None = None, reminder: str | None = None,
                      assignee_id: int | None = None, name: str | None = None,
                      priority: Literal["None", "High"] | None = None, details: str | None = None,
                      related_type: Literal["person", "company", "opportunity", "lead", "project"] | None = None,
                      related_id: int | None = None) -> Any:
    """Update a task: complete or reopen it, change its due date or reminder (YYYY-MM-DD or
    YYYY-MM-DD HH:MM), reassign, rename, change priority or notes, or link it to a record."""
    body = _drop_none({"status": status, "assignee_id": assignee_id, "name": name,
                       "priority": priority, "details": details})
    if due_date:
        body["due_date"] = _ts(due_date)
    if reminder:
        body["reminder_date"] = _ts(reminder)
    if related_type and related_id:
        body["related_resource"] = {"type": related_type, "id": related_id}
    if not body:
        return {"error": "Nothing to update."}
    return _readable(await _copper("PUT", f"/tasks/{task_id}", body))


if __name__ == "__main__":
    if HOSTED:
        mcp.run(transport="http", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), path=f"/mcp/{SECRET}")
    else:
        mcp.run()
