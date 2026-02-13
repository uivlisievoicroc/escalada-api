"""
Official export helpers (Excel/PDF + ZIP bundle).

This module generates a self-contained "official results" archive for a single box/category
based on a state snapshot (as produced by the live runtime). The output is intended for:
- jury/officials export at the end of a category
- offline sharing (ZIP containing XLSX/PDF + metadata JSON)

The export format:
- `overall.xlsx` + `overall.pdf`: overall ranking across routes
- `route_{n}.xlsx` + `route_{n}.pdf`: per-route ranking sheets
- `metadata.json`: source fields (boxId/category/routesCount/export timestamp + clubs)

Notes:
- Times are normalized via `_to_seconds` and formatted with `_format_time` for display only.
- The route-level ranking uses "dense" ranks for equal scores, and derives a points value
  as the average of the tied positions (used by the overall sheet builder).
"""

# -------------------- Standard library imports --------------------
import io
import json
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

# -------------------- Third-party imports --------------------
import pandas as pd

# -------------------- Local application imports --------------------
from escalada.api.save_ranking import (
    RankingIn,
    _build_overall_df,
    _df_to_pdf,
    _format_time,
    _to_seconds,
)
from escalada.api.ranking_time_tiebreak import resolve_rankings_with_time_tiebreak


def safe_zip_component(val: str) -> str:
    """Sanitize an arbitrary string so it is safe to use as a ZIP path component."""
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "_", (val or "").strip())
    cleaned = cleaned.strip("._-")
    return cleaned or "export"


def _route_count_from_snapshot(snapshot: dict[str, Any]) -> int:
    """
    Best-effort route count resolution from a snapshot.

    Priority:
    - explicit `routesCount` (preferred, newer)
    - legacy alias `routes_count`
    - infer from the length of the per-athlete `scores` arrays
    """
    explicit = snapshot.get("routesCount") or snapshot.get("routes_count")
    if isinstance(explicit, int) and explicit > 0:
        return explicit
    scores = snapshot.get("scores") or {}
    try:
        return max((len(v or []) for v in scores.values()), default=0)
    except Exception:
        return 0


def _normalize_times(raw_times: dict[str, list[Any]] | None) -> dict[str, list[int | None]]:
    """
    Convert the raw `times` payload (seconds/ms/strings) into seconds (ints) or None.

    The downstream ranking builders operate on numeric seconds; formatting happens later
    when we render to PDF/Excel.
    """
    times = raw_times or {}
    normalized: dict[str, list[int | None]] = {}
    for name, arr in times.items():
        normalized[name] = [_to_seconds(t) for t in (arr or [])]
    return normalized


def _normalize_clubs(raw_clubs: dict[str, Any] | None) -> dict[str, str]:
    """Normalize club mapping: drop empty keys/values and coerce non-strings to strings."""
    clubs = raw_clubs or {}
    normalized: dict[str, str] = {}
    for name, club in clubs.items():
        if not isinstance(name, str) or not name.strip():
            continue
        if club is None:
            continue
        if not isinstance(club, str):
            club = str(club)
        club = club.strip()
        if not club:
            continue
        normalized[name] = club
    return normalized


def _build_route_df(
    *,
    scores: dict[str, list[float]],
    times: dict[str, list[int | None]],
    clubs: dict[str, str],
    route_index: int,
    use_time_tiebreak: bool,
    rank_override: dict[str, int] | None = None,
    tb_time_flags: dict[str, bool] | None = None,
    tb_prev_flags: dict[str, bool] | None = None,
) -> pd.DataFrame:
    """
    Build a per-route ranking DataFrame.

    Ranking is score-descending, then name ascending for stable output. We compute:
    - `Rank`: 1..N with ties (same score => same rank number)
    - `Points`: average of the tied positions (e.g., tie for 2nd/3rd => (2+3)/2 = 2.5)
    - `Time`: formatted time column (optional; currently display-only)
    """
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
            x[0].lower(),
        ),
    )

    # Build a rank column with ties. Example:
    # scores: [10, 9, 9, 8] => ranks: [1, 2, 2, 4]
    ranks: list[int] = []
    prev_score: float | None = None
    prev_rank = 0
    for idx, (_, score, tm) in enumerate(route_entries_sorted, start=1):
        if score == prev_score:
            rank = prev_rank
        else:
            rank = idx
        ranks.append(rank)
        prev_score = score
        prev_rank = rank

    # Points are used by the overall builder. We assign the "average placing" for tied scores
    # so the overall sheet can aggregate fairly across routes.
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
            same.append((name_j, score_j, time_j))
            j += 1
        first = pos
        last = pos + len(same) - 1
        avg_rank = (first + last) / 2
        for nm, _, _ in same:
            points[nm] = avg_rank
        pos += len(same)
        i += len(same)

    if rank_override:
        route_entries_sorted = sorted(
            route_entries_sorted,
            key=lambda x: (rank_override.get(x[0], 10**9), x[0].lower()),
        )
        ranks = [rank_override.get(name, idx + 1) for idx, (name, _, _) in enumerate(route_entries_sorted)]

    rows = []
    for idx, (name, score, tm) in enumerate(route_entries_sorted):
        # Include clubs when available; keep schema stable even when clubs are unknown.
        row: dict[str, Any] = {
            "Rank": ranks[idx],
            "Name": name,
            "Club": clubs.get(name, ""),
            "Score": score,
            "Points": points.get(name),
        }
        if use_time_tiebreak:
            # Legacy flag: currently display-only (no time-based tie-breaking here).
            row["Time"] = _format_time(tm)
        if tb_time_flags:
            row["TB Time"] = "TB Time" if tb_time_flags.get(name) else ""
        if tb_prev_flags:
            row["TB Prev"] = "TB Prev" if tb_prev_flags.get(name) else ""
        rows.append(row)
    df = pd.DataFrame(rows)
    if "TB Time" in df.columns and not any(bool(v) for v in df["TB Time"].tolist()):
        df.drop(columns=["TB Time"], inplace=True)
    if "TB Prev" in df.columns and not any(bool(v) for v in df["TB Prev"].tolist()):
        df.drop(columns=["TB Prev"], inplace=True)
    return df


def build_official_results_zip(snapshot: dict[str, Any]) -> bytes:
    """
    Build a ZIP bundle with "official" results for one box snapshot:
      - overall.xlsx / overall.pdf
      - route_{n}.xlsx / route_{n}.pdf
      - metadata.json
    """
    # Use category name for folder naming; fall back to box id for robustness.
    categorie = snapshot.get("categorie") or f"box_{snapshot.get('boxId')}"
    folder = safe_zip_component(str(categorie))
    exported_at = datetime.now(timezone.utc).isoformat()

    # We expect a `scores` mapping: {athleteName: [route1Score, route2Score, ...]}.
    scores = snapshot.get("scores") or {}
    if not isinstance(scores, dict) or not scores:
        raise ValueError("missing_scores")

    # Times are optional; normalize to seconds so renderers can format them consistently.
    raw_times = snapshot.get("times") or {}
    times = _normalize_times(raw_times if isinstance(raw_times, dict) else {})

    # Clubs are optional; prefer explicit snapshot field but also support the competitors list.
    clubs = _normalize_clubs(snapshot.get("clubs") if isinstance(snapshot.get("clubs"), dict) else {})
    if not clubs:
        competitors = snapshot.get("competitors") or []
        if isinstance(competitors, list):
            for comp in competitors:
                if not isinstance(comp, dict):
                    continue
                name = comp.get("nume") or comp.get("name")
                club = comp.get("club")
                if (
                    isinstance(name, str)
                    and name.strip()
                    and isinstance(club, str)
                    and club.strip()
                ):
                    clubs[name] = club.strip()

    route_count = _route_count_from_snapshot(snapshot)
    if route_count <= 0:
        raise ValueError("missing_routes_count")

    # Legacy flag: controls time column display only (no tie-breaking).
    use_time = bool(snapshot.get("timeCriterionEnabled"))

    # `RankingIn` is shared with other exports; we reuse the same overall builder for consistency.
    payload = RankingIn(
        categorie=str(categorie),
        route_count=int(route_count),
        scores=scores,
        times=times,
        use_time_tiebreak=use_time,
        route_index=int(snapshot.get("routeIndex") or route_count),
        holds_counts=snapshot.get("holdsCounts")
        if isinstance(snapshot.get("holdsCounts"), list)
        else None,
        active_holds_count=snapshot.get("holdsCount")
        if isinstance(snapshot.get("holdsCount"), int)
        else None,
        box_id=snapshot.get("boxId"),
        time_tiebreak_resolved_decision=snapshot.get("timeTiebreakResolvedDecision"),
        time_tiebreak_resolved_fingerprint=snapshot.get("timeTiebreakResolvedFingerprint"),
        time_tiebreak_preference=snapshot.get("timeTiebreakPreference"),
        time_tiebreak_decisions=snapshot.get("timeTiebreakDecisions"),
        prev_rounds_tiebreak_resolved_decision=snapshot.get("prevRoundsTiebreakResolvedDecision"),
        prev_rounds_tiebreak_resolved_fingerprint=snapshot.get("prevRoundsTiebreakResolvedFingerprint"),
        prev_rounds_tiebreak_preference=snapshot.get("prevRoundsTiebreakPreference"),
        prev_rounds_tiebreak_decisions=snapshot.get("prevRoundsTiebreakDecisions"),
        prev_rounds_tiebreak_orders=snapshot.get("prevRoundsTiebreakOrders"),
        prev_rounds_tiebreak_ranks_by_fingerprint=snapshot.get("prevRoundsTiebreakRanks"),
        prev_rounds_tiebreak_lineage_ranks_by_key=snapshot.get("prevRoundsTiebreakLineageRanks"),
        clubs=clubs,
        include_clubs=bool(clubs),
    )

    tiebreak_context = resolve_rankings_with_time_tiebreak(
        scores=scores,
        times=times,
        route_count=route_count,
        active_route_index=int(snapshot.get("routeIndex") or route_count),
        box_id=snapshot.get("boxId"),
        time_criterion_enabled=use_time,
        active_holds_count=snapshot.get("holdsCount")
        if isinstance(snapshot.get("holdsCount"), int)
        else None,
        prev_resolved_decisions=snapshot.get("prevRoundsTiebreakDecisions"),
        prev_orders_by_fingerprint=snapshot.get("prevRoundsTiebreakOrders"),
        prev_ranks_by_fingerprint=snapshot.get("prevRoundsTiebreakRanks"),
        prev_lineage_ranks_by_key=snapshot.get("prevRoundsTiebreakLineageRanks"),
        prev_resolved_fingerprint=snapshot.get("prevRoundsTiebreakResolvedFingerprint"),
        prev_resolved_decision=snapshot.get("prevRoundsTiebreakResolvedDecision"),
        resolved_decisions=snapshot.get("timeTiebreakDecisions"),
        resolved_fingerprint=snapshot.get("timeTiebreakResolvedFingerprint"),
        resolved_decision=snapshot.get("timeTiebreakResolvedDecision"),
    )
    overall_rank_override = {
        row["name"]: int(row["rank"]) for row in tiebreak_context["overall_rows"]
    }
    overall_tb_flags = {
        row["name"]: bool(row.get("tb_time")) for row in tiebreak_context["overall_rows"]
    }
    overall_tb_prev_flags = {
        row["name"]: bool(row.get("tb_prev")) for row in tiebreak_context["overall_rows"]
    }
    active_route_rank_override = {
        row["name"]: int(row["rank"]) for row in tiebreak_context["route_rows"]
    }
    active_route_tb_flags = {
        row["name"]: bool(row.get("tb_time")) for row in tiebreak_context["route_rows"]
    }
    active_route_tb_prev_flags = {
        row["name"]: bool(row.get("tb_prev")) for row in tiebreak_context["route_rows"]
    }

    with TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)

        # Overall files (XLSX + PDF)
        overall_df = _build_overall_df(
            payload,
            times,
            rank_override=overall_rank_override,
            tb_time_flags=overall_tb_flags,
            tb_prev_flags=overall_tb_prev_flags,
        )
        overall_xlsx = tmp_dir / "overall.xlsx"
        overall_pdf = tmp_dir / "overall.pdf"
        overall_df.to_excel(overall_xlsx, index=False)
        _df_to_pdf(overall_df, overall_pdf, title=f"{categorie} – Overall")

        file_paths: list[Path] = [overall_xlsx, overall_pdf]

        # Per-route files (XLSX + PDF)
        for r in range(route_count):
            is_active_route = (r + 1) == int(snapshot.get("routeIndex") or route_count)
            df_route = _build_route_df(
                scores=scores,
                times=times,
                clubs=clubs,
                route_index=r,
                use_time_tiebreak=use_time,
                rank_override=active_route_rank_override if is_active_route else None,
                tb_time_flags=active_route_tb_flags if is_active_route else None,
                tb_prev_flags=active_route_tb_prev_flags if is_active_route else None,
            )
            xlsx_route = tmp_dir / f"route_{r+1}.xlsx"
            pdf_route = tmp_dir / f"route_{r+1}.pdf"
            df_route.to_excel(xlsx_route, index=False)
            _df_to_pdf(df_route, pdf_route, title=f"{categorie} – Route {r+1}")
            file_paths.extend([xlsx_route, pdf_route])

        # Include metadata for traceability/debugging (no personal data beyond names/clubs).
        metadata = {
            "boxId": snapshot.get("boxId"),
            "competitionId": snapshot.get("competitionId"),
            "categorie": snapshot.get("categorie"),
            "routesCount": route_count,
            "timeCriterionEnabled": bool(snapshot.get("timeCriterionEnabled")),
            "timeTiebreakPreference": snapshot.get("timeTiebreakPreference"),
            "timeTiebreakDecisions": snapshot.get("timeTiebreakDecisions") or {},
            "timeTiebreakResolvedFingerprint": snapshot.get("timeTiebreakResolvedFingerprint"),
            "timeTiebreakResolvedDecision": snapshot.get("timeTiebreakResolvedDecision"),
            "prevRoundsTiebreakPreference": snapshot.get("prevRoundsTiebreakPreference"),
            "prevRoundsTiebreakDecisions": snapshot.get("prevRoundsTiebreakDecisions") or {},
            "prevRoundsTiebreakOrders": snapshot.get("prevRoundsTiebreakOrders") or {},
            "prevRoundsTiebreakRanks": snapshot.get("prevRoundsTiebreakRanks") or {},
            "prevRoundsTiebreakLineageRanks": snapshot.get("prevRoundsTiebreakLineageRanks")
            or {},
            "prevRoundsTiebreakResolvedFingerprint": snapshot.get("prevRoundsTiebreakResolvedFingerprint"),
            "prevRoundsTiebreakResolvedDecision": snapshot.get("prevRoundsTiebreakResolvedDecision"),
            "timeTiebreakCurrentFingerprint": tiebreak_context.get("fingerprint"),
            "timeTiebreakHasEligibleTie": tiebreak_context.get("has_eligible_tie"),
            "timeTiebreakIsResolved": tiebreak_context.get("is_resolved"),
            "timeTiebreakEligibleGroups": tiebreak_context.get("eligible_groups") or [],
            "clubs": clubs,
            "exportedAt": exported_at,
        }
        metadata_path = tmp_dir / "metadata.json"
        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        file_paths.append(metadata_path)

        # Package everything into a ZIP buffer for streaming as a single response.
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for p in file_paths:
                zf.write(p, arcname=f"{folder}/{p.name}")
        return buf.getvalue()
