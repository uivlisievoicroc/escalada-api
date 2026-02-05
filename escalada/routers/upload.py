from io import BytesIO
from zipfile import BadZipFile

import openpyxl
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

router = APIRouter(tags=["upload"], prefix="/admin")
from escalada.auth.deps import require_role
from escalada.api import live as live_module


@router.post("/upload")
async def upload_listbox(
    category: str = Form(...),
    routesCount: str = Form(...),
    holdsCounts: str = Form(...),
    file: UploadFile = File(...),
    include_clubs: str = Form(default="true"),
    claims=Depends(require_role(["admin"])),
):
    """Upload competition data from Excel file."""
    # verific MIME
    if file.content_type not in (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel",
    ):
        raise HTTPException(status_code=400, detail="Tip fișier neacceptat")

    data = await file.read()
    try:
        wb = openpyxl.load_workbook(filename=BytesIO(data), read_only=True)
    except BadZipFile:
        raise HTTPException(
            status_code=400, detail="Fișierul încărcat nu este un .xlsx valid"
        )

    try:
        ws = wb.active
        if ws is None:
            raise HTTPException(
                status_code=400, detail="Fișierul Excel nu conține nicio foaie"
            )

        competitors = []
        # presupunem că prima linie sunt anteturi: Nume, Club
        for row in ws.iter_rows(min_row=2, values_only=True):
            nume, club = row[:2]
            if nume and club:
                competitors.append({"nume": str(nume), "club": str(club)})
    finally:
        wb.close()

    # Parse holdsCounts from JSON string
    import json

    try:
        holds_counts_list = json.loads(holdsCounts)
    except:
        holds_counts_list = []

    # Return the complete listbox object so frontend can add it immediately
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
    judgeChief: str = ""
    competitionDirector: str = ""
    chiefRoutesetter: str = ""


@router.get("/competition_officials")
async def get_competition_officials(claims=Depends(require_role(["admin"]))):
    return {"status": "ok", **live_module.get_competition_officials()}


@router.post("/competition_officials")
async def set_competition_officials(
    payload: CompetitionOfficialsPayload, claims=Depends(require_role(["admin"]))
):
    officials = live_module.set_competition_officials(
        judge_chief=payload.judgeChief,
        competition_director=payload.competitionDirector,
        chief_routesetter=payload.chiefRoutesetter,
    )
    return {"status": "ok", **officials}
