import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from escalada.api import live
from escalada.api.backup import collect_snapshots, latest_backup_file, write_backup_file
from escalada.auth.deps import require_role

router = APIRouter(prefix="/ops", tags=["ops"])


class DrillRequest(BaseModel):
    box_ids: list[int] | None = None
    write_backup_file: bool = False


@router.get("/status")
async def ops_status(claims=Depends(require_role(["admin"]))):
    """Operational status for contest-day checks (JSON-only)."""

    backup_dir = Path(os.getenv("BACKUP_DIR", "backups"))
    last = latest_backup_file(backup_dir)
    last_mtime = datetime.fromtimestamp(last.stat().st_mtime, tz=timezone.utc) if last else None
    age_sec = (
        (datetime.now(timezone.utc) - last_mtime).total_seconds() if last_mtime else None
    )

    return {
        "serverTimeUtc": datetime.now(timezone.utc).isoformat(),
        "db": {"status": "disabled", "storage": "json"},
        "counts": {
            "competitions": 0,
            "boxes": len(live.state_map),
            "events": 0,
            "lastEventAt": None,
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
    """Force a backup write to disk (same format as periodic backups)."""

    backup_dir = Path(os.getenv("BACKUP_DIR", "backups"))
    snaps = await collect_snapshots()
    path = await write_backup_file(backup_dir, snaps)
    return {"status": "ok", "filename": path.name, "snapshots": len(snaps)}


@router.post("/drill/backup_restore")
async def drill_backup_restore(payload: DrillRequest, claims=Depends(require_role(["admin"]))):
    """Postgres-only drill removed in JSON-only build."""

    raise HTTPException(status_code=501, detail="backup_drill_not_supported_json")
