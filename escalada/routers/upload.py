"""
Admin upload routes.

This router currently provides:
- `/api/admin/upload`: Parse an uploaded Excel listbox and return a listbox object for the UI
- `/api/admin/competition_officials`: Get/set global officials (chief judge/director/chief routesetter)

Notes:
- The upload endpoint is intentionally "stateless": it does not mutate live contest state directly.
  The frontend uses the returned listbox to populate UI and then initiates boxes via `/api/cmd`.
- Requests are admin-only (enforced via `require_role(["admin"])`).
"""

# -------------------- Standard library imports --------------------
import json
from io import BytesIO
from zipfile import BadZipFile

# -------------------- Third-party imports --------------------
import openpyxl
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

# -------------------- Local application imports --------------------
from escalada.auth.deps import require_role
from escalada.api import live as live_module

# Router is mounted under `/api/admin` (see escalada/main.py).
router = APIRouter(tags=["upload"], prefix="/admin")


@router.post("/upload")
async def upload_listbox(
    category: str = Form(...),
    routesCount: str = Form(...),
    holdsCounts: str = Form(...),
    file: UploadFile = File(...),
    include_clubs: str = Form(default="true"),
    claims=Depends(require_role(["admin"])),
):
    """
    Upload competition data from an Excel file.

    Expected format (active sheet):
    - Row 1: headers
    - Row 2..N: competitors, with first two columns: [Name, Club]

    Returns a listbox object for the frontend to use immediately.
    """
    # Basic MIME check to reject obviously wrong uploads (client-side validation is not enough).
    if file.content_type not in (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel",
    ):
        raise HTTPException(status_code=400, detail="Tip fișier neacceptat")

    # Load the workbook into memory. openpyxl expects a file-like object.
    data = await file.read()
    try:
        wb = openpyxl.load_workbook(filename=BytesIO(data), read_only=True)
    except BadZipFile:
        raise HTTPException(
            status_code=400, detail="Fișierul încărcat nu este un .xlsx valid"
        )

    try:
        # We read the active sheet and assume row 1 contains headers.
        ws = wb.active
        if ws is None:
            raise HTTPException(
                status_code=400, detail="Fișierul Excel nu conține nicio foaie"
            )

        competitors = []
        # Convention: row 1 is headers, rows 2..N are competitor data: [Name, Club, ...].
        for row in ws.iter_rows(min_row=2, values_only=True):
            nume, club = row[:2]
            if nume and club:
                competitors.append({"nume": str(nume), "club": str(club)})
    finally:
        wb.close()

    # Parse holdsCounts from JSON string (Form fields are strings).
    try:
        holds_counts_list = json.loads(holdsCounts)
    except Exception:
        holds_counts_list = []

    # Return the complete listbox object so the frontend can add it immediately.
    # `routesCount` is a form string; normalize to int for downstream logic.
    # `include_clubs` is currently accepted for UI compatibility (not embedded into the listbox here).
    new_listbox = {
        "categorie": category,
        "concurenti": competitors,
        "routesCount": int(routesCount),
        "holdsCounts": holds_counts_list,
        "routeIndex": 1,
        "holdsCount": holds_counts_list[0] if holds_counts_list else 0,
        "initiated": False,
        "timerPreset": "05:00",
    }

    return {
        "status": "success",
        "message": "Listbox uploaded successfully",
        "listbox": new_listbox,
    }


class CompetitionOfficialsPayload(BaseModel):
    """Payload for setting global officials (applies to the entire event, not per box)."""
    judgeChief: str = ""
    competitionDirector: str = ""
    chiefRoutesetter: str = ""


@router.get("/competition_officials")
async def get_competition_officials(claims=Depends(require_role(["admin"]))):
    """Return persisted global officials (admin-only)."""
    return {"status": "ok", **live_module.get_competition_officials()}


@router.post("/competition_officials")
async def set_competition_officials(
    payload: CompetitionOfficialsPayload, claims=Depends(require_role(["admin"]))
):
    """Persist global officials (admin-only)."""
    officials = live_module.set_competition_officials(
        judge_chief=payload.judgeChief,
        competition_director=payload.competitionDirector,
        chief_routesetter=payload.chiefRoutesetter,
    )
    return {"status": "ok", **officials}
