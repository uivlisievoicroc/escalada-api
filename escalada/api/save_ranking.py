# escalada/api/save_ranking.py
"""
Admin-only "save rankings" endpoint and shared export helpers.

This module renders a category's results to disk under `escalada/clasamente/<categorie>/`:
- `overall.xlsx` / `overall.pdf`
- `route_{n}.xlsx` / `route_{n}.pdf` (one per route)

It also exposes helper functions (`_build_overall_df`, `_df_to_pdf`, `_to_seconds`, etc.) that
are reused by other export features (e.g. official ZIP exports).

Important:
- The `use_time_tiebreak` flag is currently **display-only** (adds a Time column). Ranking is
  based on score; ties are handled by assigning the average of the tied positions.
"""

# -------------------- Standard library imports --------------------
import math
import os
from pathlib import Path

# -------------------- Third-party imports --------------------
import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)
from escalada.api.ranking_time_tiebreak import resolve_rankings_with_time_tiebreak

# -------------------- Font setup (PDF rendering) --------------------
# We prefer a Unicode-capable TTF (DejaVuSans) so Romanian diacritics render correctly.
# If the font cannot be found/registered, we fall back to Helvetica (may not render all diacritics).
DEFAULT_FONT = "Helvetica"
try:
    # Try to find and register DejaVuSans
    font_paths = [
        "DejaVuSans.ttf",  # Current directory
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",  # Linux
        "/System/Library/Fonts/Supplemental/DejaVuSans.ttf",  # macOS
        "C:\\Windows\\Fonts\\DejaVuSans.ttf",  # Windows
        "/Library/Fonts/DejaVuSans.ttf",  # macOS user fonts
    ]
    for path in font_paths:
        if os.path.exists(path):
            pdfmetrics.registerFont(TTFont("DejaVuSans", path))
            DEFAULT_FONT = "DejaVuSans"
            break

    # If DejaVuSans not found, use Helvetica (limited diacritics)
    # Note: Helvetica may not render all Romanian diacritics correctly
except Exception as e:
    import logging

    logging.warning(
        f"Could not register DejaVuSans font: {e}. Using Helvetica (limited diacritic support)."
    )
    DEFAULT_FONT = "Helvetica"

from escalada.auth.deps import require_role

router = APIRouter()

def _safe_category_dir(category: str) -> Path:
    """
    Build a safe category directory under `escalada/clasamente`.
    Preserves diacritics/spaces, but blocks path traversal and separators.
    """
    cat = (category or "").strip()
    if not cat or cat in {".", ".."}:
        raise HTTPException(status_code=400, detail="invalid_categorie")
    if "/" in cat or "\\" in cat or ".." in cat:
        raise HTTPException(status_code=400, detail="invalid_categorie")

    base_dir = Path("escalada/clasamente").resolve()
    candidate = (base_dir / cat).resolve()
    if base_dir not in candidate.parents:
        raise HTTPException(status_code=400, detail="invalid_categorie")
    return candidate


class RankingIn(BaseModel):
    """Payload used to generate XLSX/PDF exports for a single category."""
    categorie: str
    route_count: int
    # Score matrix indexed by athlete name and route index:
    # { "Name": [scoreR1, scoreR2, ...] }
    scores: dict[str, list[float]]
    clubs: dict[str, str] = {}
    include_clubs: bool = False
    times: dict[str, list[float | None]] | None = None
    # Legacy flag: controls Time column display only (no time-based tie-breaking).
    use_time_tiebreak: bool = False
    # Current active route index (1-based), used for top-3 route-context tiebreak.
    route_index: int | None = None
    holds_counts: list[int] | None = None
    active_holds_count: int | None = None
    # Optional box id to make tie fingerprints deterministic across boxes.
    box_id: int | None = None
    # Persisted manual tie decision state.
    time_tiebreak_resolved_decision: str | None = None
    time_tiebreak_resolved_fingerprint: str | None = None
    time_tiebreak_preference: str | None = None
    time_tiebreak_decisions: dict[str, str] | None = None
    prev_rounds_tiebreak_resolved_decision: str | None = None
    prev_rounds_tiebreak_resolved_fingerprint: str | None = None
    prev_rounds_tiebreak_preference: str | None = None
    prev_rounds_tiebreak_decisions: dict[str, str] | None = None
    prev_rounds_tiebreak_orders: dict[str, list[str]] | None = None
    prev_rounds_tiebreak_ranks_by_fingerprint: dict[str, dict[str, int]] | None = None
    prev_rounds_tiebreak_lineage_ranks_by_key: dict[str, dict[str, int]] | None = None
    prev_rounds_tiebreak_resolved_ranks_by_name: dict[str, int] | None = None


@router.post("/save_ranking")
def save_ranking(payload: RankingIn, claims=Depends(require_role(["admin"]))):
    """
    Persist category rankings to disk (XLSX + PDF).

    Output path:
      `escalada/clasamente/<categorie>/`

    Files:
    - overall.xlsx / overall.pdf: overall ranking across routes (geometric mean of rank-points)
    - route_{n}.xlsx / route_{n}.pdf: per-route ranking with tie-handling and points column
    """
    cat_dir = _safe_category_dir(payload.categorie)
    cat_dir.mkdir(parents=True, exist_ok=True)
    raw_times = payload.times or {}
    # Normalize all times to seconds (int) or None so rendering is consistent.
    times = {name: [_to_seconds(t) for t in arr] for name, arr in raw_times.items()}
    # Legacy flag: controls time column display only (no tie-breaking).
    use_time = payload.use_time_tiebreak
    active_route_index = payload.route_index or payload.route_count
    derived_holds_count = payload.active_holds_count
    if derived_holds_count is None and isinstance(payload.holds_counts, list):
        idx = max(0, int(active_route_index) - 1)
        if idx < len(payload.holds_counts):
            candidate = payload.holds_counts[idx]
            if isinstance(candidate, int):
                derived_holds_count = candidate
    tiebreak_context = resolve_rankings_with_time_tiebreak(
        scores=payload.scores,
        times=times,
        route_count=payload.route_count,
        active_route_index=active_route_index,
        box_id=payload.box_id,
        time_criterion_enabled=bool(use_time),
        active_holds_count=derived_holds_count,
        prev_resolved_decisions=payload.prev_rounds_tiebreak_decisions,
        prev_orders_by_fingerprint=payload.prev_rounds_tiebreak_orders,
        prev_ranks_by_fingerprint=payload.prev_rounds_tiebreak_ranks_by_fingerprint,
        prev_lineage_ranks_by_key=payload.prev_rounds_tiebreak_lineage_ranks_by_key,
        prev_resolved_fingerprint=payload.prev_rounds_tiebreak_resolved_fingerprint,
        prev_resolved_decision=payload.prev_rounds_tiebreak_resolved_decision,
        prev_resolved_ranks_by_name=payload.prev_rounds_tiebreak_resolved_ranks_by_name,
        resolved_decisions=payload.time_tiebreak_decisions,
        resolved_fingerprint=payload.time_tiebreak_resolved_fingerprint,
        resolved_decision=payload.time_tiebreak_resolved_decision,
    )
    overall_rank_override = {
        row["name"]: int(row["rank"]) for row in tiebreak_context["overall_rows"]
    }
    overall_tb_time = {
        row["name"]: bool(row.get("tb_time")) for row in tiebreak_context["overall_rows"]
    }
    overall_tb_prev = {
        row["name"]: bool(row.get("tb_prev")) for row in tiebreak_context["overall_rows"]
    }
    active_route_rank_override = {
        row["name"]: int(row["rank"]) for row in tiebreak_context["route_rows"]
    }
    active_route_tb_time = {
        row["name"]: bool(row.get("tb_time")) for row in tiebreak_context["route_rows"]
    }
    active_route_tb_prev = {
        row["name"]: bool(row.get("tb_prev")) for row in tiebreak_context["route_rows"]
    }

    def time_for(name: str, idx: int):
        arr = times.get(name, [])
        return _to_seconds(arr[idx]) if idx < len(arr) else None

    # ---------- excel + pdf TOTAL ----------
    overall_df = _build_overall_df(
        payload,
        times,
        rank_override=overall_rank_override,
        tb_time_flags=overall_tb_time,
        tb_prev_flags=overall_tb_prev,
    )
    xlsx_tot = cat_dir / "overall.xlsx"
    pdf_tot = cat_dir / "overall.pdf"
    overall_df.to_excel(xlsx_tot, index=False)
    _df_to_pdf(overall_df, pdf_tot, title=f"{payload.categorie} – Overall")
    saved_paths = [xlsx_tot, pdf_tot]

    # ---------- excel + pdf BY‑ROUTE ----------
    scores = payload.scores
    for r in range(payload.route_count):
        # 1) collect (name, raw score, time) for route r
        route_list = [
            (name, arr[r] if r < len(arr) else None, time_for(name, r))
            for name, arr in scores.items()
        ]
        # 2) sort by score desc (None -> last), then name asc for stable output
        route_list_sorted = sorted(
            route_list,
            key=lambda x: (
                -x[1] if x[1] is not None else math.inf,
                x[0].lower(),
            ),
        )

        # 3) compute per-route ranking points with tie-handling:
        # ties share the average of the tied positions (e.g. tie for 2nd/3rd => 2.5 points).
        points = {}
        pos = 1
        i = 0
        while i < len(route_list_sorted):
            same_score = [
                route_list_sorted[j]
                for j in range(i, len(route_list_sorted))
                if route_list_sorted[j][1] == route_list_sorted[i][1]
            ]
            first = pos
            last = pos + len(same_score) - 1
            avg_rank = (first + last) / 2
            for name, _, _ in same_score:
                points[name] = avg_rank
            pos += len(same_score)
            i += len(same_score)

        # 4) build a "Rank" column with ties (same score -> same rank number)
        ranks = []
        prev_score = None
        prev_rank = 0
        for idx, (_, score, tm) in enumerate(route_list_sorted, start=1):
            if score == prev_score:
                rank = prev_rank
            else:
                rank = idx
            ranks.append(rank)
            prev_score = score
            prev_rank = rank

        is_active_route = (r + 1) == int(active_route_index)
        if is_active_route:
            route_list_sorted = sorted(
                route_list_sorted,
                key=lambda item: (
                    active_route_rank_override.get(item[0], 10**9),
                    item[0].lower(),
                ),
            )
            ranks = [
                active_route_rank_override.get(name, ranks[idx])
                for idx, (name, _, _) in enumerate(route_list_sorted)
            ]

        df_route = pd.DataFrame(
            [
                {
                    "Rank": ranks[i],
                    "Name": name,
                    "Club": payload.clubs.get(name, ""),
                    "Score": score,
                    **({"Time": _format_time(tm)} if use_time else {}),
                    **(
                        {
                            "TB Time": "TB Time"
                            if is_active_route and active_route_tb_time.get(name)
                            else ""
                        }
                    ),
                    **(
                        {
                            "TB Prev": "TB Prev"
                            if is_active_route and active_route_tb_prev.get(name)
                            else ""
                        }
                    ),
                    "Points": points.get(name),
                }
                for i, (name, score, tm) in enumerate(route_list_sorted)
            ]
        )

        # 5) save Excel and PDF for this route
        xlsx_route = cat_dir / f"route_{r+1}.xlsx"
        pdf_route = cat_dir / f"route_{r+1}.pdf"
        df_route.to_excel(xlsx_route, index=False)
        _df_to_pdf(df_route, pdf_route, title=f"{payload.categorie} – Route {r+1}")
        saved_paths.extend([xlsx_route, pdf_route])

    return {
        "status": "ok",
        "saved": [str(p) for p in saved_paths],
        "time_tiebreak_fingerprint": tiebreak_context.get("fingerprint"),
        "time_tiebreak_has_eligible_tie": tiebreak_context.get("has_eligible_tie"),
        "time_tiebreak_is_resolved": tiebreak_context.get("is_resolved"),
    }


# ------- helpers -------
def _build_overall_df(
    p: RankingIn,
    normalized_times: dict[str, list[int | None]] | None = None,
    rank_override: dict[str, int] | None = None,
    tb_time_flags: dict[str, bool] | None = None,
    tb_prev_flags: dict[str, bool] | None = None,
) -> pd.DataFrame:
    """
    Build the overall ranking DataFrame.

    Algorithm (matches frontend):
    - For each route: compute "rank points" per athlete (average-of-positions for ties)
    - For each athlete: compute geometric mean of rank points across routes
    - Sort ascending by total (lower is better), then by name for stability
    - Compute a "Rank" column with ties on equal totals
    """
    from math import prod

    scores = p.scores
    times = normalized_times if normalized_times is not None else (p.times or {})
    # Legacy flag: controls time column display only (no tie-breaking).
    use_time = p.use_time_tiebreak
    rows_data = []
    n = p.route_count
    n_comp = len(scores)

    for name, arr in scores.items():
        # Compute per-route "rank points" exactly like the frontend does.
        rp: list[float | None] = [None] * n
        for r in range(n):
            scored = []
            for nume, sc in scores.items():
                if r < len(sc) and sc[r] is not None:
                    t_val = None
                    t_arr = times.get(nume, [])
                    if r < len(t_arr):
                        t_val = t_arr[r]
                    scored.append((nume, sc[r], t_val))
            scored.sort(key=lambda x: (-x[1], x[0].lower()))

            i = 0
            pos = 1
            while i < len(scored):
                current = scored[i]
                same = [current]
                while (
                    i + len(same) < len(scored)
                    and scored[i][1] == scored[i + len(same)][1]
                ):
                    same.append(scored[i + len(same)])
                avg = (pos + pos + len(same) - 1) / 2
                for nume, _, _ in same:
                    if nume == name:
                        rp[r] = avg
                pos += len(same)
                i += len(same)

        # Fill missing routes (no score) with a penalty worse than last place.
        filled = [v if v is not None else n_comp + 1 for v in rp]
        while len(filled) < n:
            filled.append(n_comp + 1)

        # Geometric mean keeps totals comparable across different route counts.
        total = round(prod(filled) ** (1 / n), 3)
        club = p.clubs.get(name, "")
        row: list[str | float | None] = [name, club]
        time_row = times.get(name, [])
        for idx in range(n):
            row.append(arr[idx] if idx < len(arr) else None)
            if use_time:
                row.append(_format_time(time_row[idx] if idx < len(time_row) else None))
        row.append(total)
        rows_data.append((name, row))

    cols = ["Nume", "Club"]
    for i in range(n):
        cols.append(f"Score R{i+1}")
        if use_time:
            cols.append(f"Time R{i+1}")
    cols.append("Total")
    if rank_override:
        rows_data.sort(
            key=lambda item: (
                rank_override.get(item[0], 10**9),
                item[1][-1],
                item[0].lower(),
            )
        )
        data = [row for _, row in rows_data]
        df = pd.DataFrame(data, columns=cols)
        ranks = [rank_override.get(name, idx + 1) for idx, (name, _) in enumerate(rows_data)]
    else:
        data = [row for _, row in rows_data]
        df = pd.DataFrame(data, columns=cols)
        df.sort_values(["Total", "Nume"], inplace=True)
        # Insert a human-readable rank column with ties for identical totals.
        ranks = []
        prev_total = None
        prev_rank = 0
        for idx, total in enumerate(df["Total"], start=1):
            rank = prev_rank if total == prev_total else idx
            ranks.append(rank)
            prev_total = total
            prev_rank = rank
    if tb_time_flags:
        tb_values = []
        if rank_override:
            for name, _ in rows_data:
                tb_values.append("TB Time" if tb_time_flags.get(name) else "")
        else:
            for _, row in df.iterrows():
                name = str(row["Nume"])
                tb_values.append("TB Time" if tb_time_flags.get(name) else "")
        if any(tb_values):
            df["TB Time"] = tb_values
    if tb_prev_flags:
        prev_values = []
        if rank_override:
            for name, _ in rows_data:
                prev_values.append("TB Prev" if tb_prev_flags.get(name) else "")
        else:
            for _, row in df.iterrows():
                name = str(row["Nume"])
                prev_values.append("TB Prev" if tb_prev_flags.get(name) else "")
        if any(prev_values):
            df["TB Prev"] = prev_values
    df.insert(0, "Rank", ranks)
    return df


def _build_by_route_df(p: RankingIn) -> pd.DataFrame:
    """Build a long-form table (Route/Name/Score/Time) from a RankingIn payload."""
    rows = []
    n = p.route_count
    times = p.times or {}
    for r in range(n):
        for name, arr in p.scores.items():
            score = arr[r] if r < len(arr) else None
            t_arr = times.get(name, [])
            tm = t_arr[r] if r < len(t_arr) else None
            rows.append(
                {"Route": r + 1, "Name": name, "Score": score, "Time": _format_time(tm)}
            )
    return pd.DataFrame(rows)


def _format_time(val) -> str | None:
    """Format a time value as 'mm:ss' (or None if missing/invalid)."""
    sec = _to_seconds(val)
    if sec is None:
        return None
    m = sec // 60
    s = sec % 60
    return f"{m:02d}:{s:02d}"


def _to_seconds(val) -> int | None:
    """
    Normalize various time representations into integer seconds.

    Accepts:
    - numeric seconds (int/float)
    - 'mm:ss' strings
    - numeric strings (e.g. '120', '120.0')
    """
    if val is None:
        return None
    # accept already-numeric
    if isinstance(val, (int, float)):
        if math.isnan(val):
            return None
        return int(val)
    # accept "mm:ss"
    if isinstance(val, str) and ":" in val:
        try:
            parts = val.split(":")
            if len(parts) == 2:
                m, s = parts
                return int(m) * 60 + int(s)
        except Exception:
            return None
    # accept numeric strings
    try:
        return int(float(val))
    except Exception:
        return None


def _df_to_pdf(df: pd.DataFrame, pdf_path: Path, title="Ranking"):
    """Render a DataFrame as a simple landscape-A4 PDF table."""
    # Create document with margins and landscape A4
    doc = SimpleDocTemplate(
        str(pdf_path),
        pagesize=landscape(A4),
        leftMargin=36,
        rightMargin=36,
        topMargin=36,
        bottomMargin=36,
    )

    # Styles for title
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "TitleStyle",
        parent=styles["Heading1"],
        alignment=1,  # center
        fontSize=18,
        fontName=DEFAULT_FONT,
        spaceAfter=12,
    )

    # Build table data
    data = [df.columns.tolist()] + df.astype(str).values.tolist()

    # Create table
    table = Table(data, hAlign="CENTER")
    # Table styling
    tbl_style = TableStyle(
        [
            ("FONTNAME", (0, 0), (-1, 0), DEFAULT_FONT),
            ("FONTNAME", (0, 1), (-1, -1), DEFAULT_FONT),
            ("FONTSIZE", (0, 0), (-1, 0), 12),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#4F81BD")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
        ]
    )
    # Alternate row background colors
    for i in range(1, len(data)):
        bg_color = colors.whitesmoke if i % 2 == 0 else colors.lightgrey
        tbl_style.add("BACKGROUND", (0, i), (-1, i), bg_color)

    table.setStyle(tbl_style)

    # Build document elements
    elements = []
    elements.append(Paragraph(title, title_style))
    elements.append(Spacer(1, 12))
    elements.append(table)

    # Generate PDF
    doc.build(elements)
