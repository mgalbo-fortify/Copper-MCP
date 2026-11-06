"""
Copper CRM MCP server for a team (stopgap until Copper's official MCP ships).

Runs two ways:
  - Hosted for the whole team (HTTP, secret URL):  MCP_TRANSPORT=http python server.py
  - Locally for one person in Claude Desktop:      python server.py

Always required:
  COPPER_API_KEY     Copper API key (Settings > Integrations > API Keys in Copper)
  COPPER_USER_EMAIL  Email of the Copper user who owns that key

Required when hosted (MCP_TRANSPORT=http):
  MCP_SECRET         Long random string (32+ characters). The server is only reachable at
                     /mcp/<MCP_SECRET>, so that URL works like a password. Change it to revoke access.
"""

import os
from typing import Any, Literal

import httpx
from fastmcp import FastMCP

BASE_URL = "https://api.copper.com/developer_api/v1"
RecordType = Literal["people", "companies", "opportunities", "leads"]
HOSTED = os.getenv("MCP_TRANSPORT", "stdio") == "http"

if HOSTED:
    SECRET = os.getenv("MCP_SECRET", "")
    if len(SECRET) < 32 or not SECRET.replace("-", "").replace("_", "").isalnum():
        raise SystemExit("Set MCP_SECRET to a random string of at least 32 letters/numbers before hosting.")

mcp = FastMCP("copper-crm")


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
        return r.json()


def _slim(records: list[dict]) -> list[dict]:
    """Keep the fields Claude actually needs so responses stay small."""
    keep = {
        "id", "name", "title", "company_name", "emails", "phone_numbers",
        "status", "monetary_value", "pipeline_id", "pipeline_stage_id",
        "close_date", "assignee_id", "tags", "date_created", "date_modified",
        "customer_source_id", "details",
    }
    return [{k: v for k, v in rec.items() if k in keep and v not in (None, [], "")} for rec in records]


# ---------- Read tools ----------

@mcp.tool()
async def search_records(
    record_type: RecordType,
    name: str | None = None,
    email: str | None = None,
    tags: list[str] | None = None,
    page_size: int = 25,
    page_number: int = 1,
) -> Any:
    """Search Copper people, companies, opportunities, or leads by name, email, or tags."""
    body: dict[str, Any] = {"page_size": min(page_size, 200), "page_number": page_number}
    if name:
        body["name"] = name
    if email and record_type in ("people", "leads"):
        body["emails"] = [email]
    if tags:
        body["tags"] = tags
    result = await _copper("POST", f"/{record_type}/search", body)
    return _slim(result) if isinstance(result, list) else result


@mcp.tool()
async def get_record(record_type: RecordType, record_id: int) -> Any:
    """Get the full details of one Copper record by its ID."""
    return await _copper("GET", f"/{record_type}/{record_id}")


@mcp.tool()
async def search_opportunities_by_status(
    status: Literal["Open", "Won", "Lost", "Abandoned"] = "Open",
    pipeline_id: int | None = None,
    page_size: int = 50,
) -> Any:
    """List opportunities filtered by status and (optionally) pipeline."""
    body = {"page_size": min(page_size, 200), "status": status}
    if pipeline_id:
        body["pipeline_ids"] = [pipeline_id]
    result = await _copper("POST", "/opportunities/search", body)
    return _slim(result) if isinstance(result, list) else result


@mcp.tool()
async def list_pipelines() -> Any:
    """List sales pipelines and their stages (needed to interpret pipeline/stage IDs)."""
    return await _copper("GET", "/pipelines")


@mcp.tool()
async def list_activities(record_type: Literal["person", "company", "opportunity", "lead"], record_id: int) -> Any:
    """List recent activities (notes, calls, emails) logged against a record."""
    return await _copper("POST", f"/{_plural(record_type)}/{record_id}/activities", {})


def _plural(singular: str) -> str:
    return {"person": "people", "company": "companies", "opportunity": "opportunities", "lead": "leads"}[singular]


# ---------- Write tools ----------

@mcp.tool()
async def create_lead(name: str, email: str | None = None, company_name: str | None = None,
                      title: str | None = None, tags: list[str] | None = None) -> Any:
    """Create a new lead in Copper."""
    body: dict[str, Any] = {"name": name, "details": "Created via Claude"}
    if email:
        body["email"] = {"email": email, "category": "work"}
    if company_name:
        body["company_name"] = company_name
    if title:
        body["title"] = title
    if tags:
        body["tags"] = tags
    return await _copper("POST", "/leads", body)


@mcp.tool()
async def update_record(record_type: RecordType, record_id: int, fields: dict[str, Any]) -> Any:
    """Update fields on a Copper record, e.g. {"tags": ["webinar-nov"]} or {"pipeline_stage_id": 123}."""
    return await _copper("PUT", f"/{record_type}/{record_id}", fields)


@mcp.tool()
async def add_note(record_type: Literal["person", "company", "opportunity", "lead"], record_id: int, note: str) -> Any:
    """Log a note on a Copper record."""
    types = await _copper("GET", "/activity_types")
    note_type = next((t for t in types.get("user", []) if t.get("name") == "Note"), None) if isinstance(types, dict) else None
    if not note_type:
        return {"error": "Could not find the 'Note' activity type in this Copper account."}
    body = {
        "parent": {"type": record_type, "id": record_id},
        "type": {"category": "user", "id": note_type["id"]},
        "details": f"{note}\n\n(Logged via Claude)",
    }
    return await _copper("POST", "/activities", body)


if __name__ == "__main__":
    if HOSTED:
        mcp.run(transport="http", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), path=f"/mcp/{SECRET}")
    else:
        mcp.run()
