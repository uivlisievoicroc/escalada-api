import csv
import io
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from escalada.auth.deps import require_role
from escalada.api import live
from escalada.db.database import AsyncSessionLocal
from escalada.db import repositories as repos
from escalada.api.save_ranking import _build_overall_df, _format_time

router = APIRouter()


async def _fetch_box_snapshot(session, box_id: int) -> Dict[str, Any] | None:
    box_repo = repos.BoxRepository(session)
    box = await box_repo.get_by_id(box_id)
    if not box:
        # fallback pe state-ul din memorie
        state = live.state_map.get(box_id) or live._default_state()
        scores = state.get("scores", {})
        times = state.get("times", {})
        return {
            "boxId": box_id,
            "competitionId": None,
            "initiated": state.get("initiated", False),
            "holdsCount": state.get("holdsCount", 0),
            "routeIndex": state.get("routeIndex", 1),
            "currentClimber": state.get("currentClimber", ""),
            "started": state.get("started", False),
            "timerState": state.get("timerState", "idle"),
            "holdCount": state.get("holdCount", 0.0),
            "competitors": state.get("competitors", []),
            "categorie": state.get("categorie", ""),
            "registeredTime": state.get("lastRegisteredTime"),
            "remaining": state.get("remaining"),
            "timeCriterionEnabled": state.get("timeCriterionEnabled"),
            "timerPreset": state.get("timerPreset"),
            "timerPresetSec": state.get("timerPresetSec"),
            "sessionId": state.get("sessionId"),
            "boxVersion": state.get("boxVersion", 0),
            "competitorsAll": [],
            "scores": scores,
            "times": times,
        }

    # Build snapshot similar to /api/state
    state = box.state or {}
    snapshot = {
        "boxId": box.id,
        "competitionId": box.competition_id,
        "initiated": state.get("initiated", False),
        "holdsCount": state.get("holdsCount", 0),
        "routeIndex": state.get("routeIndex", 1),
        "currentClimber": state.get("currentClimber", ""),
        "started": state.get("started", False),
        "timerState": state.get("timerState", "idle"),
        "holdCount": state.get("holdCount", 0.0),
        "competitors": state.get("competitors", []),
        "categorie": state.get("categorie", ""),
        "registeredTime": state.get("lastRegisteredTime"),
        "remaining": state.get("remaining"),
        "timeCriterionEnabled": state.get("timeCriterionEnabled"),
        "timerPreset": state.get("timerPreset"),
        "timerPresetSec": state.get("timerPresetSec"),
        "sessionId": box.session_id,
        "boxVersion": box.box_version or 0,
    }

    # Load competitors assigned to this box (if any)
    comp_repo = repos.CompetitorRepository(session)
    competitors = await comp_repo.list_by_competition(box.competition_id)
    snapshot["competitorsAll"] = [
        {
            "id": c.id,
            "name": c.name,
            "category": c.category,
            "bib": c.bib,
            "boxId": c.box_id,
        }
        for c in competitors
    ]

    # Minimal “scores” placeholder (extend as needed)
    snapshot["scores"] = state.get("scores", {})
    snapshot["times"] = state.get("times", {})

    # If DB competitors missing but state has them, include minimal list
    if not snapshot.get("competitorsAll"):
        comps_state = state.get("competitors") or []
        snapshot["competitorsAll"] = []
        for idx, comp in enumerate(comps_state):
            if not isinstance(comp, dict):
                continue
            snapshot["competitorsAll"].append(
                {
                    "id": None,
                    "name": comp.get("nume") or comp.get("name") or f"comp_{idx}",
                    "category": comp.get("categorie") or comp.get("category"),
                    "bib": comp.get("bib"),
                    "boxId": box.id if box else box_id,
                }
            )

    # Ranking (overall) computed from state scores if available
    try:
        scores = snapshot.get("scores") or {}
        times = snapshot.get("times") or {}
        route_count = state.get("routesCount") or state.get("routes_count") or 0
        if scores and route_count:
            df_overall = _build_overall_df(
                type(
                    "Payload",
                    (),
                    {
                        "scores": scores,
                        "route_count": route_count,
                        "clubs": {},
                        "include_clubs": False,
                        "times": times,
                        "use_time_tiebreak": bool(state.get("timeCriterionEnabled")),
                    },
                ),
                times,
            )
            snapshot["ranking"] = df_overall.to_dict(orient="records")
        else:
            snapshot["ranking"] = []
    except Exception:
        snapshot["ranking"] = []

    return snapshot


@router.get("/backup/box/{box_id}")
async def backup_box(box_id: int, claims=Depends(require_role(["admin"]))):
    """Return JSON snapshot for a single box."""
    async with AsyncSessionLocal() as session:
        snap = await _fetch_box_snapshot(session, box_id)
        if not snap:
            raise HTTPException(status_code=404, detail="box_not_found")
        return {"status": "ok", "snapshot": snap}


@router.get("/backup/full")
async def backup_full(claims=Depends(require_role(["admin"]))):
    """Return JSON snapshots for all boxes across competitions."""
    async with AsyncSessionLocal() as session:
        snapshots = await collect_snapshots(session)
        return {"status": "ok", "snapshots": snapshots}


@router.get("/backup/last")
async def backup_last(download: bool = False, claims=Depends(require_role(["admin"]))):
    """Return metadata for last backup or download it."""
    out_dir = Path(os.getenv("BACKUP_DIR", "backups"))
    last_file = latest_backup_file(out_dir)
    if not last_file:
        raise HTTPException(status_code=404, detail="backup_not_found")

    if download:
        return FileResponse(
            last_file,
            media_type="application/json",
            filename=last_file.name,
        )

    mtime = datetime.fromtimestamp(last_file.stat().st_mtime, tz=timezone.utc)
    return {"status": "ok", "filename": last_file.name, "timestamp": mtime.isoformat()}


@router.get("/export/box/{box_id}")
async def export_box_csv(box_id: int, claims=Depends(require_role(["admin"]))):
    """Export current state to CSV (lightweight per-box export)."""
    async with AsyncSessionLocal() as session:
        snap = await _fetch_box_snapshot(session, box_id)
        if not snap:
            raise HTTPException(status_code=404, detail="box_not_found")

        output = io.StringIO()
        writer = csv.writer(output)

        writer.writerow(["boxId", snap["boxId"]])
        writer.writerow(["competitionId", snap["competitionId"]])
        writer.writerow(["categorie", snap.get("categorie", "")])
        writer.writerow(["boxVersion", snap.get("boxVersion", 0)])
        writer.writerow([])

        writer.writerow(["Current state"])
        writer.writerow(["holdCount", snap.get("holdCount", 0)])
        writer.writerow(["started", snap.get("started", False)])
        writer.writerow(["timerState", snap.get("timerState", "idle")])
        writer.writerow(["currentClimber", snap.get("currentClimber", "")])
        writer.writerow(["remaining", snap.get("remaining")])
        writer.writerow([])

        writer.writerow(["Competitors (all)"])
        writer.writerow(["id", "name", "category", "bib", "boxId"])
        for c in snap.get("competitorsAll", []):
            writer.writerow([
                c.get("id"),
                c.get("name"),
                c.get("category"),
                c.get("bib"),
                c.get("boxId"),
            ])

        # Scores per competitor (if available)
        scores = snap.get("scores") or {}
        times = snap.get("times") or {}
        if scores:
            writer.writerow([])
            writer.writerow(["Scores (per competitor)"])
            max_routes = max((len(v or []) for v in scores.values()), default=0)
            headers = ["Name"] + [f"Route {i+1}" for i in range(max_routes)]
            use_time = bool(snap.get("timeCriterionEnabled"))
            if use_time:
                for i in range(max_routes):
                    headers.append(f"Time {i+1}")
            writer.writerow(headers)
            for name, arr in scores.items():
                row = [name]
                arr = arr or []
                for i in range(max_routes):
                    row.append(arr[i] if i < len(arr) else "")
                if use_time:
                    t_arr = times.get(name, [])
                    for i in range(max_routes):
                        row.append(_format_time(t_arr[i]) if i < len(t_arr) else "")
                writer.writerow(row)

        # Ranking section if available
        ranking = snap.get("ranking") or []
        if ranking:
            writer.writerow([])
            writer.writerow(["Ranking (overall)"])
            headers = ranking[0].keys()
            writer.writerow(headers)
            for row in ranking:
                writer.writerow([row.get(h, "") for h in headers])

        csv_bytes = output.getvalue().encode("utf-8")
        return Response(
            content=csv_bytes,
            media_type="text/csv",
            headers={
                "Content-Disposition": f"attachment; filename=box_{box_id}_export.csv"
            },
        )


class RestoreRequest(BaseModel):
    snapshots: List[Dict[str, Any]]
    box_ids: List[int] | None = None


@router.post("/restore")
async def restore_backup(payload: RestoreRequest, claims=Depends(require_role(["admin"]))):
    """
    Restore snapshots. Policy: accept if incoming boxVersion > current OR same version with matching sessionId.
    Otherwise raise conflict.
    """
    conflicts = []
    restored = []
    async with AsyncSessionLocal() as session:
        box_repo = repos.BoxRepository(session)
        comp_repo = repos.CompetitionRepository(session)
        comp = await comp_repo.get_by_name("Restored Default")
        if not comp:
            comp = await comp_repo.create(name="Restored Default")
        for snap in payload.snapshots:
            box_id = snap.get("boxId")
            if payload.box_ids and box_id not in payload.box_ids:
                continue
            if box_id is None:
                continue
            desired_version = snap.get("boxVersion", 0)
            session_id = snap.get("sessionId")

            # Clone snapshot into state and drop only non-state extras
            state = snap.copy()
            state.pop("ranking", None)
            state["scores"] = snap.get("scores") or {}
            state["times"] = snap.get("times") or {}

            box = await box_repo.get_by_id(box_id)
            current_version = box.box_version if box else -1
            current_session = box.session_id if box else None

            if box and desired_version < current_version:
                conflicts.append({"boxId": box_id, "reason": "lower_version"})
                continue
            if box and desired_version == current_version and session_id and current_session and session_id != current_session:
                conflicts.append({"boxId": box_id, "reason": "session_conflict"})
                continue

            if not box:
                # create box with provided state
                new_box = await box_repo.create(
                    competition_id=comp.id,
                    name=f"Restored Box {box_id}",
                    route_index=state.get("routeIndex", 1) or 1,
                    routes_count=state.get("routesCount", 1) or 1,
                    holds_count=state.get("holdsCount", 0) or 0,
                )
                await session.flush()
                box = new_box

            box.state = state
            box.box_version = desired_version
            box.session_id = session_id or box.session_id
            await session.flush()

            # sync in-memory snapshot
            async with live.init_lock:
                live.state_map[box_id] = state
                live.state_map[box_id]["sessionId"] = box.session_id
                live.state_map[box_id]["boxVersion"] = box.box_version
            restored.append(box_id)

        await session.commit()

    if conflicts:
        raise HTTPException(
            status_code=409,
            detail={"restore_conflict": conflicts, "restored": restored},
        )
    return {"status": "ok", "restored": restored}


async def collect_snapshots(session) -> List[Dict[str, Any]]:
    """Collect snapshots for all boxes from DB + in-memory."""
    box_repo = repos.BoxRepository(session)
    comp_repo = repos.CompetitionRepository(session)
    competitions = await comp_repo.list_all()
    snapshots: List[Dict[str, Any]] = []
    for comp in competitions:
        boxes = await box_repo.list_by_competition(comp.id)
        for box in boxes:
            snap = await _fetch_box_snapshot(session, box.id)
            if snap:
                snapshots.append(snap)

    # Include și box-urile doar în memorie
    for box_id in live.state_map.keys():
        if any(s.get("boxId") == box_id for s in snapshots):
            continue
        snap = await _fetch_box_snapshot(session, box_id)
        if snap:
            snapshots.append(snap)
    return snapshots


async def write_backup_file(output_dir: Path, snapshots: List[Dict[str, Any]]) -> Path:
    """Persist snapshots to a JSON file on disk."""
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    path = output_dir / f"backup_{ts}.json"
    path.write_text(json.dumps({"snapshots": snapshots}, ensure_ascii=False, indent=2))
    return path


def latest_backup_file(output_dir: Path) -> Path | None:
    files = sorted(output_dir.glob("backup_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None
