import os
from pathlib import Path
from typing import Dict, List

import pandas as pd
from fastapi import APIRouter, HTTPException

router = APIRouter()


@router.get("/podium/{category}", response_model=List[Dict[str, str]])
async def get_podium(category: str):
    """
    Returnează primii 3 clasați pentru categoria specificată,
    citind fișierul Excel generat anterior.
    """
    # Sanitize category to prevent path traversal
    safe_category = os.path.basename(category)
    if not safe_category or safe_category != category:
        raise HTTPException(status_code=400, detail="Invalid category name")

    excel_path = Path("escalada/clasamente") / safe_category / "overall.xlsx"
    if not excel_path.exists():
        raise HTTPException(
            status_code=404, detail="Clasament inexistent pentru categoria specificată."
        )
    try:
        df = pd.read_excel(excel_path)
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Eroare la citirea fișierului Excel: {e}"
        )
    # Presupunem că DataFrame-ul are coloana "Nume" și este deja sortat după tipărirea cu Rank
    top3 = df.head(3)
    colors = ["#ffd700", "#c0c0c0", "#cd7f32"]  # aur, argint, bronz
    result = []
    for idx, row in enumerate(top3.itertuples()):
        name = getattr(row, "Nume", None) or getattr(row, "Name", None)
        if name is None:
            raise HTTPException(
                status_code=500,
                detail="Excel file is missing required 'Nume' or 'Name' column",
            )
        result.append({"name": name, "color": colors[idx]})
    return result
