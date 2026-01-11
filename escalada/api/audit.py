from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from escalada.auth.deps import require_role
from escalada.storage.json_store import read_latest_events

router = APIRouter()


class AuditEventOut(BaseModel):
    id: str
    createdAt: str
    competitionId: int
    boxId: int | None
    action: str
    actionId: str | None
    boxVersion: int
    sessionId: str | None
    actorUsername: str | None
    actorRole: str | None
    actorIp: str | None
    actorUserAgent: str | None
    payload: dict | None = None


@router.get("/audit/events", response_model=list[AuditEventOut])
async def list_audit_events(
    box_id: int | None = Query(default=None, alias="boxId"),
    limit: int = Query(default=200, ge=1, le=2000),
    include_payload: bool = Query(default=False, alias="includePayload"),
    claims=Depends(require_role(["admin"])),
):
    """Admin-only audit log stream (most recent first)."""

    events = read_latest_events(
        limit=limit,
        include_payload=include_payload,
        box_id=box_id,
    )

    return [
        AuditEventOut(
            id=str(ev.get("id", "")),
            createdAt=str(ev.get("createdAt", "")),
            competitionId=int(ev.get("competitionId", 0) or 0),
            boxId=ev.get("boxId"),
            action=str(ev.get("action", "")),
            actionId=ev.get("actionId"),
            boxVersion=int(ev.get("boxVersion", 0) or 0),
            sessionId=ev.get("sessionId"),
            actorUsername=ev.get("actorUsername"),
            actorRole=ev.get("actorRole"),
            actorIp=ev.get("actorIp"),
            actorUserAgent=ev.get("actorUserAgent"),
            payload=ev.get("payload") if include_payload else None,
        )
        for ev in events
    ]
