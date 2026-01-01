import io
import json
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pandas as pd

from escalada.api.save_ranking import (
    RankingIn,
    _build_overall_df,
    _df_to_pdf,
    _format_time,
    _to_seconds,
)


def safe_zip_component(val: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "_", (val or "").strip())
    cleaned = cleaned.strip("._-")
    return cleaned or "export"


def _route_count_from_snapshot(snapshot: dict[str, Any]) -> int:
    explicit = snapshot.get("routesCount") or snapshot.get("routes_count")
    if isinstance(explicit, int) and explicit > 0:
        return explicit
    scores = snapshot.get("scores") or {}
    try:
        return max((len(v or []) for v in scores.values()), default=0)
    except Exception:
        return 0


def _normalize_times(raw_times: dict[str, list[Any]] | None) -> dict[str, list[int | None]]:
    times = raw_times or {}
    normalized: dict[str, list[int | None]] = {}
    for name, arr in times.items():
        normalized[name] = [_to_seconds(t) for t in (arr or [])]
    return normalized


def _build_route_df(
    *,
    scores: dict[str, list[float]],
    times: dict[str, list[int | None]],
    route_index: int,
    use_time_tiebreak: bool,
) -> pd.DataFrame:
    route_entries: list[tuple[str, float | None, int | None]] = []
    for name, arr in (scores or {}).items():
        score = arr[route_index] if route_index < len(arr or []) else None
        t_arr = times.get(name, [])
        tm = t_arr[route_index] if route_index < len(t_arr or []) else None
        route_entries.append((name, score, tm))

    route_entries_sorted = sorted(
        route_entries,
        key=lambda x: (
            -x[1] if x[1] is not None else float("inf"),
            (x[2] if (use_time_tiebreak and x[2] is not None) else float("inf")),
            x[0].lower(),
        ),
    )

    ranks: list[int] = []
    prev_score: float | None = None
    prev_time: int | None = None
    prev_rank = 0
    for idx, (_, score, tm) in enumerate(route_entries_sorted, start=1):
        if score == prev_score and ((not use_time_tiebreak) or tm == prev_time):
            rank = prev_rank
        else:
            rank = idx
        ranks.append(rank)
        prev_score = score
        prev_time = tm
        prev_rank = rank

    points: dict[str, float] = {}
    pos = 1
    i = 0
    while i < len(route_entries_sorted):
        _, score_i, time_i = route_entries_sorted[i]
        same: list[tuple[str, float | None, int | None]] = []
        j = i
        while j < len(route_entries_sorted):
            name_j, score_j, time_j = route_entries_sorted[j]
            if score_j != score_i:
                break
            if use_time_tiebreak and time_j != time_i:
                break
            same.append((name_j, score_j, time_j))
            j += 1
        first = pos
        last = pos + len(same) - 1
        avg_rank = (first + last) / 2
        for nm, _, _ in same:
            points[nm] = avg_rank
        pos += len(same)
        i += len(same)

    rows = []
    for idx, (name, score, tm) in enumerate(route_entries_sorted):
        row: dict[str, Any] = {
            "Rank": ranks[idx],
            "Name": name,
            "Score": score,
            "Points": points.get(name),
        }
        if use_time_tiebreak:
            row["Time"] = _format_time(tm)
        rows.append(row)
    return pd.DataFrame(rows)


def build_official_results_zip(snapshot: dict[str, Any]) -> bytes:
    """
    Build a ZIP bundle with "official" results for one box snapshot:
      - overall.xlsx / overall.pdf
      - route_{n}.xlsx / route_{n}.pdf
      - metadata.json
    """
    categorie = snapshot.get("categorie") or f"box_{snapshot.get('boxId')}"
    folder = safe_zip_component(str(categorie))
    exported_at = datetime.now(timezone.utc).isoformat()

    scores = snapshot.get("scores") or {}
    if not isinstance(scores, dict) or not scores:
        raise ValueError("missing_scores")

    raw_times = snapshot.get("times") or {}
    times = _normalize_times(raw_times if isinstance(raw_times, dict) else {})

    route_count = _route_count_from_snapshot(snapshot)
    if route_count <= 0:
        raise ValueError("missing_routes_count")

    use_time = bool(snapshot.get("timeCriterionEnabled"))

    payload = RankingIn(
        categorie=str(categorie),
        route_count=int(route_count),
        scores=scores,
        times=times,
        use_time_tiebreak=use_time,
        clubs={},
        include_clubs=False,
    )

    with TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)

        overall_df = _build_overall_df(payload, times)
        overall_xlsx = tmp_dir / "overall.xlsx"
        overall_pdf = tmp_dir / "overall.pdf"
        overall_df.to_excel(overall_xlsx, index=False)
        _df_to_pdf(overall_df, overall_pdf, title=f"{categorie} – Overall")

        file_paths: list[Path] = [overall_xlsx, overall_pdf]

        for r in range(route_count):
            df_route = _build_route_df(
                scores=scores,
                times=times,
                route_index=r,
                use_time_tiebreak=use_time,
            )
            xlsx_route = tmp_dir / f"route_{r+1}.xlsx"
            pdf_route = tmp_dir / f"route_{r+1}.pdf"
            df_route.to_excel(xlsx_route, index=False)
            _df_to_pdf(df_route, pdf_route, title=f"{categorie} – Route {r+1}")
            file_paths.extend([xlsx_route, pdf_route])

        metadata = {
            "boxId": snapshot.get("boxId"),
            "competitionId": snapshot.get("competitionId"),
            "categorie": snapshot.get("categorie"),
            "routesCount": route_count,
            "timeCriterionEnabled": bool(snapshot.get("timeCriterionEnabled")),
            "exportedAt": exported_at,
        }
        metadata_path = tmp_dir / "metadata.json"
        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        file_paths.append(metadata_path)

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for p in file_paths:
                zf.write(p, arcname=f"{folder}/{p.name}")
        return buf.getvalue()

