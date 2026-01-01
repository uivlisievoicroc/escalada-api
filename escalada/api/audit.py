from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from escalada.auth.deps import require_role
from escalada.db.database import get_session
from escalada.db.models import Event

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
    session: AsyncSession = Depends(get_session),
    claims=Depends(require_role(["admin"])),
):
    """
    Admin-only audit log stream (most recent first).
    Use for post-mortem debugging: who sent what command, when, for which box.
    """
    query = select(Event).order_by(Event.created_at.desc()).limit(limit)
    if box_id is not None:
        query = query.where(Event.box_id == box_id)

    result = await session.execute(query)
    events = result.scalars().all()

    out: list[AuditEventOut] = []
    for ev in events:
        created_at = ev.created_at
        if isinstance(created_at, datetime):
            created_str = created_at.isoformat()
        else:
            created_str = str(created_at)
        out.append(
            AuditEventOut(
                id=str(ev.id),
                createdAt=created_str,
                competitionId=ev.competition_id,
                boxId=ev.box_id,
                action=ev.action,
                actionId=ev.action_id,
                boxVersion=ev.box_version or 0,
                sessionId=ev.session_id,
                actorUsername=ev.actor_username,
                actorRole=ev.actor_role,
                actorIp=ev.actor_ip,
                actorUserAgent=ev.actor_user_agent,
                payload=ev.payload if include_payload else None,
            )
        )
    return out

