"""Settings as JSON for the AI agent (docs/agent-api.md): read and change the app's settings through /api/settings.
The registry, the checks and the side effects are in app/services/settings_api.py; opened by the bearer token
(auth.valid_bearer) or the dashboard login like every /api/ route."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.services import settings_api, timeline

router = APIRouter()


@router.get("/api/settings")
async def api_settings(section: str | None = None):
    """Every setting the agent may read, with its type, limits and default; `section`: one settings page only."""
    if section is not None and section not in settings_api.sections():
        raise HTTPException(404, "unknown section")
    return {"settings": settings_api.listing(section)}


@router.get("/api/settings/{key}")
async def api_setting(key: str):
    found = settings_api.get(key)
    if found is None:
        raise HTTPException(404, "unknown setting")
    return found


def _brief(changed: dict[str, Any]) -> dict[str, Any]:
    """What the timeline keeps of a change: the new value, but only the length of a free text (rules are long)."""
    return {k: f"<{len(v)} characters>" if isinstance(v, str) and len(v) > 80 else v for k, v in changed.items()}


@router.patch("/api/settings")
async def api_settings_patch(request: Request):
    """{"values": {key: value, …}}: all values are checked first; one bad value refuses the whole request (400,
    {"errors": {key: message}}) and nothing is stored. Answers the settings that changed."""
    try:
        body = await request.json()
    except ValueError:
        body = None
    values = body.get("values") if isinstance(body, dict) else None
    if not isinstance(values, dict):
        return JSONResponse({"errors": {"values": 'send {"values": {"<key>": <value>, …}}'}}, status_code=400)
    try:
        result = await settings_api.patch(values)
    except settings_api.ValidationErrors as e:
        return JSONResponse({"errors": e.errors}, status_code=400)
    if result["changed"]:         # the timeline's middleware records POST/PUT/DELETE only
        timeline.action("PATCH /api/settings", "/api/settings", payload={"changed": _brief(result["changed"])})
    return result
