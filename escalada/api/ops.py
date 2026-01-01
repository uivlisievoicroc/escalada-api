import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select

from escalada.api.backup import collect_snapshots, restore_snapshots, write_backup_file, latest_backup_file
from escalada.auth.deps import require_role
from escalada.db.database import AsyncSessionLocal
from escalada.db.health import health_check_db
from escalada.db.models import Box, Competition, Event


router = APIRouter(prefix="/ops", tags=["ops"])


class DrillRequest(BaseModel):
    box_ids: list[int] | None = None
    write_backup_file: bool = False


@router.get("/status")
async def ops_status(claims=Depends(require_role(["admin"]))):
    """
    Operational status for contest-day checks:
    - DB health + counts
    - last backup file age (disk)
    """
    backup_dir = Path(os.getenv("BACKUP_DIR", "backups"))
    last = latest_backup_file(backup_dir)
    last_mtime = (
        datetime.fromtimestamp(last.stat().st_mtime, tz=timezone.utc) if last else None
    )
    age_sec = (
        (datetime.now(timezone.utc) - last_mtime).total_seconds() if last_mtime else None
    )

    async with AsyncSessionLocal() as session:
        db_health = await health_check_db(session)
        comps_count = await session.scalar(select(func.count(Competition.id)))
        boxes_count = await session.scalar(select(func.count(Box.id)))
        events_count = await session.scalar(select(func.count(Event.id)))
        last_event = await session.scalar(select(func.max(Event.created_at)))

    return {
        "serverTimeUtc": datetime.now(timezone.utc).isoformat(),
        "db": db_health,
        "counts": {
            "competitions": comps_count or 0,
            "boxes": boxes_count or 0,
            "events": events_count or 0,
            "lastEventAt": last_event.isoformat() if last_event else None,
        },
        "backup": {
            "dir": str(backup_dir),
            "lastFile": last.name if last else None,
            "lastTimestampUtc": last_mtime.isoformat() if last_mtime else None,
            "ageSeconds": age_sec,
        },
    }


@router.post("/backup/now")
async def backup_now(claims=Depends(require_role(["admin"]))):
    """
    Force a backup write to disk (same format as periodic backups).
    """
    backup_dir = Path(os.getenv("BACKUP_DIR", "backups"))
    async with AsyncSessionLocal() as session:
        snaps = await collect_snapshots(session)
    path = await write_backup_file(backup_dir, snaps)
    return {"status": "ok", "filename": path.name, "snapshots": len(snaps)}


@router.post("/drill/backup_restore")
async def drill_backup_restore(payload: DrillRequest, claims=Depends(require_role(["admin"]))):
    """
    Non-destructive drill:
      - collect snapshots from DB
      - optionally write backup file to disk
      - run restore logic in a SAVEPOINT and rollback (no DB changes, no in-memory changes)
    """
    backup_dir = Path(os.getenv("BACKUP_DIR", "backups"))
    async with AsyncSessionLocal() as session:
        snaps = await collect_snapshots(session)

        backup_filename = None
        if payload.write_backup_file:
            path = await write_backup_file(backup_dir, snaps)
            backup_filename = path.name

        nested = await session.begin_nested()
        try:
            restored, conflicts = await restore_snapshots(
                session,
                snaps,
                box_ids=payload.box_ids,
                hydrate_memory=False,
                broadcast_time_criterion=False,
                bump_sequences=False,
            )
            # Ensure all SQL flushes happen inside the SAVEPOINT
            await session.flush()
        finally:
            await nested.rollback()

    if conflicts:
        raise HTTPException(
            status_code=409,
            detail={
                "drill_conflicts": conflicts,
                "restored": restored,
                "backupFile": backup_filename,
            },
        )

    return {
        "status": "ok",
        "snapshots": len(snaps),
        "restored": len(restored),
        "backupFile": backup_filename,
    }

