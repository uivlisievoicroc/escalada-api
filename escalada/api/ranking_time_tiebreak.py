"""
API adapter over the core Lead ranking engine.

This module keeps backwards-compatible response keys used by API/UI/export layers,
while delegating ranking/tie-break semantics to `escalada-core`.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from escalada_core import Athlete, LeadResult, TieBreakDecision, TieContext, compute_lead_ranking


def _coerce_time_seconds(val: Any) -> float | None:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        if not math.isfinite(val):
            return None
        return float(val)
    if isinstance(val, str):
        raw = val.strip()
        if not raw:
            return None
        if ":" in raw:
            parts = raw.split(":")
            if len(parts) == 2:
                try:
                    return float(int(parts[0]) * 60 + int(parts[1]))
                except Exception:
                    return None
        try:
            parsed = float(raw)
            if not math.isfinite(parsed):
                return None
            return parsed
        except Exception:
            return None
    return None


def _sanitize_scores(
    scores: dict[str, list[float | None | int]] | None,
) -> dict[str, list[float | None]]:
    out: dict[str, list[float | None]] = {}
    for name, arr in (scores or {}).items():
        if not isinstance(name, str) or not name.strip():
            continue
        if not isinstance(arr, list):
            continue
        clean: list[float | None] = []
        for value in arr:
            if isinstance(value, bool):
                clean.append(None)
                continue
            if isinstance(value, (int, float)) and math.isfinite(value):
                clean.append(float(value))
            else:
                clean.append(None)
        out[name] = clean
    return out


def _sanitize_times(
    times: dict[str, list[int | float | str | None]] | None,
) -> dict[str, list[float | None]]:
    out: dict[str, list[float | None]] = {}
    for name, arr in (times or {}).items():
        if not isinstance(name, str) or not name.strip():
            continue
        if not isinstance(arr, list):
            continue
        out[name] = [_coerce_time_seconds(v) for v in arr]
    return out


def _normalize_resolved_decisions(
    *,
    resolved_decisions: dict[str, str] | None,
    resolved_fingerprint: str | None,
    resolved_decision: str | None,
) -> dict[str, str]:
    normalized: dict[str, str] = {}
    if isinstance(resolved_decisions, dict):
        for fp, decision in resolved_decisions.items():
            if not isinstance(fp, str) or not fp.strip():
                continue
            if decision not in {"yes", "no"}:
                continue
            normalized[fp.strip()] = decision
    if (
        isinstance(resolved_fingerprint, str)
        and resolved_fingerprint.strip()
        and resolved_decision in {"yes", "no"}
    ):
        normalized[resolved_fingerprint.strip()] = resolved_decision
    return normalized


def _normalize_order_map(orders: dict[str, Any] | None) -> dict[str, list[str]]:
    normalized: dict[str, list[str]] = {}
    if not isinstance(orders, dict):
        return normalized
    for fp, value in orders.items():
        if not isinstance(fp, str) or not fp.strip():
            continue
        if not isinstance(value, list):
            continue
        seen: set[str] = set()
        out: list[str] = []
        for item in value:
            if not isinstance(item, str):
                continue
            name = item.strip()
            if not name or name in seen:
                continue
            seen.add(name)
            out.append(name)
        if out:
            normalized[fp.strip()] = out
    return normalized


def _normalize_ranks_map(
    ranks: dict[str, Any] | None,
) -> dict[str, dict[str, int]]:
    normalized: dict[str, dict[str, int]] = {}
    if not isinstance(ranks, dict):
        return normalized
    for fp, value in ranks.items():
        if not isinstance(fp, str) or not fp.strip() or not isinstance(value, dict):
            continue
        out: dict[str, int] = {}
        for raw_name, raw_rank in value.items():
            if not isinstance(raw_name, str):
                continue
            name = raw_name.strip()
            if not name:
                continue
            if isinstance(raw_rank, bool) or not isinstance(raw_rank, int) or raw_rank <= 0:
                continue
            out[name] = int(raw_rank)
        if out:
            normalized[fp.strip()] = out
    return normalized


def _order_to_ranks(
    order: list[str] | None,
    member_ids: list[str],
) -> dict[str, int] | None:
    if not isinstance(order, list) or not order:
        return None
    available = list(member_ids)
    available_set = set(available)
    clean_order: list[str] = []
    seen: set[str] = set()
    for raw in order:
        if not isinstance(raw, str):
            continue
        name = raw.strip()
        if not name or name in seen or name not in available_set:
            continue
        seen.add(name)
        clean_order.append(name)
    if not clean_order:
        return None
    if len(available) >= 3 and len(clean_order) != len(available):
        return None
    if len(available) == 2 and len(clean_order) == 1:
        winner = clean_order[0]
        other = [nm for nm in available if nm != winner]
        clean_order = [winner, *other]
    else:
        leftovers = [nm for nm in sorted(available, key=str.lower) if nm not in set(clean_order)]
        clean_order = [*clean_order, *leftovers]
    return {name: idx + 1 for idx, name in enumerate(clean_order)}


def _result_key(result: LeadResult) -> tuple[int, int, int]:
    return (
        1 if result.topped else 0,
        int(result.hold),
        1 if (result.plus and not result.topped) else 0,
    )


def _score_to_lead_result(
    *,
    score: float | None,
    time_seconds: float | None,
    active_holds_count: int | None,
) -> LeadResult:
    if score is None:
        return LeadResult(topped=False, hold=-1, plus=False, time_seconds=time_seconds)
    safe_score = float(score)
    hold_cap = int(active_holds_count) if isinstance(active_holds_count, int) and active_holds_count > 0 else None
    if hold_cap is not None and safe_score >= float(hold_cap):
        return LeadResult(topped=True, hold=hold_cap, plus=False, time_seconds=time_seconds)
    hold = int(math.floor(max(safe_score, 0.0)))
    frac = safe_score - float(hold)
    plus = frac > 1e-9
    return LeadResult(topped=False, hold=hold, plus=plus, time_seconds=time_seconds)


def _event_global_fingerprint(
    *,
    box_id: int | None,
    route_index: int,
    tie_groups: list[dict[str, Any]],
) -> str | None:
    if not tie_groups:
        return None
    payload = {
        "boxId": box_id,
        "routeIndex": route_index,
        "ties": tie_groups,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"tb3:{hashlib.sha1(raw.encode('utf-8')).hexdigest()}"


class _StateBackedResolver:
    def __init__(
        self,
        *,
        prev_decisions: dict[str, str],
        prev_orders: dict[str, list[str]],
        prev_ranks: dict[str, dict[str, int]],
        prev_lineage_ranks: dict[str, dict[str, int]],
        time_decisions: dict[str, str],
        global_fingerprint: str | None,
        event_prev_fingerprint: str | None,
        event_prev_decision: str | None,
        event_prev_order: list[str] | None,
        event_prev_ranks: dict[str, int] | None,
        event_prev_lineage_key: str | None,
        event_time_fingerprint: str | None,
        event_time_decision: str | None,
    ):
        self.prev_decisions = prev_decisions
        self.prev_orders = prev_orders
        self.prev_ranks = prev_ranks
        self.prev_lineage_ranks = prev_lineage_ranks
        self.time_decisions = time_decisions
        self.global_fingerprint = global_fingerprint
        self.event_prev_fingerprint = event_prev_fingerprint
        self.event_prev_decision = event_prev_decision
        self.event_prev_order = event_prev_order
        self.event_prev_ranks = event_prev_ranks
        self.event_prev_lineage_key = event_prev_lineage_key
        self.event_time_fingerprint = event_time_fingerprint
        self.event_time_decision = event_time_decision
        self._prev_fp_by_signature: dict[tuple[tuple[str, ...], int, int], str] = {}

    def _matches_event_scope(self, event_fp: str | None, ctx_fp: str) -> bool:
        if not isinstance(event_fp, str) or not event_fp:
            return False
        if event_fp == ctx_fp:
            return True
        if self.global_fingerprint and event_fp == self.global_fingerprint:
            return True
        return False

    def resolve(self, group: list[Athlete], context: TieContext) -> TieBreakDecision:
        signature = (
            tuple(sorted(a.id for a in group)),
            int(context.rank_start),
            int(context.rank_end),
        )
        if context.stage == "previous_rounds":
            self._prev_fp_by_signature[signature] = context.fingerprint
            member_ids = {a.id for a in group}
            lineage_key = (
                context.lineage_key.strip()
                if isinstance(context.lineage_key, str) and context.lineage_key.strip()
                else None
            )
            lineage_ranks: dict[str, int] = {}
            if lineage_key:
                lineage_ranks = {
                    athlete_id: int(rank)
                    for athlete_id, rank in (self.prev_lineage_ranks.get(lineage_key) or {}).items()
                    if athlete_id in member_ids
                }
            if (
                not lineage_ranks
                and lineage_key
                and isinstance(self.event_prev_lineage_key, str)
                and self.event_prev_lineage_key == lineage_key
            ):
                lineage_ranks = {
                    athlete_id: int(rank)
                    for athlete_id, rank in (self.event_prev_ranks or {}).items()
                    if athlete_id in member_ids
                }
            decision = self.prev_decisions.get(context.fingerprint)
            if decision not in {"yes", "no"} and self._matches_event_scope(
                self.event_prev_fingerprint, context.fingerprint
            ):
                decision = self.event_prev_decision
            if decision not in {"yes", "no"} and lineage_ranks:
                decision = "yes"
            if decision not in {"yes", "no"}:
                return TieBreakDecision(choice="pending")
            if decision == "no":
                return TieBreakDecision(choice="no")
            merged_ranks: dict[str, int] = dict(lineage_ranks)
            fp_ranks = self.prev_ranks.get(context.fingerprint)
            if isinstance(fp_ranks, dict):
                merged_ranks.update(
                    {
                        athlete_id: int(rank)
                        for athlete_id, rank in fp_ranks.items()
                        if athlete_id in member_ids
                    }
                )
            if not merged_ranks:
                order_ranks = _order_to_ranks(
                    self.prev_orders.get(context.fingerprint), [a.id for a in group]
                )
                if isinstance(order_ranks, dict):
                    merged_ranks.update(order_ranks)
            if not merged_ranks and self._matches_event_scope(
                self.event_prev_fingerprint, context.fingerprint
            ):
                order_ranks = _order_to_ranks(self.event_prev_order, [a.id for a in group])
                if isinstance(order_ranks, dict):
                    merged_ranks.update(order_ranks)
            if (
                isinstance(self.event_prev_ranks, dict)
                and self._matches_event_scope(self.event_prev_fingerprint, context.fingerprint)
            ):
                merged_ranks.update(
                    {
                        athlete_id: int(rank)
                        for athlete_id, rank in self.event_prev_ranks.items()
                        if athlete_id in member_ids
                    }
                )
            return TieBreakDecision(choice="yes", previous_ranks_by_athlete=merged_ranks or {})

        decision = self.time_decisions.get(context.fingerprint)
        if decision not in {"yes", "no"}:
            legacy_prev_fp = self._prev_fp_by_signature.get(signature)
            if legacy_prev_fp:
                decision = self.time_decisions.get(legacy_prev_fp)
        if decision not in {"yes", "no"} and self._matches_event_scope(
            self.event_time_fingerprint, context.fingerprint
        ):
            decision = self.event_time_decision
        if decision not in {"yes", "no"}:
            legacy_prev_fp = self._prev_fp_by_signature.get(signature)
            if self._matches_event_scope(self.event_time_fingerprint, legacy_prev_fp or ""):
                decision = self.event_time_decision
        if decision not in {"yes", "no"}:
            return TieBreakDecision(choice="pending")
        return TieBreakDecision(choice=decision)


def resolve_rankings_with_time_tiebreak(
    *,
    scores: dict[str, list[float | None | int]] | None,
    times: dict[str, list[int | float | str | None]] | None,
    route_count: int,
    active_route_index: int,
    box_id: int | None,
    time_criterion_enabled: bool,
    active_holds_count: int | None = None,
    prev_resolved_decisions: dict[str, str] | None = None,
    prev_orders_by_fingerprint: dict[str, list[str]] | None = None,
    prev_ranks_by_fingerprint: dict[str, dict[str, int]] | None = None,
    prev_lineage_ranks_by_key: dict[str, dict[str, int]] | None = None,
    prev_resolved_fingerprint: str | None = None,
    prev_resolved_decision: str | None = None,
    prev_resolved_order: list[str] | None = None,
    prev_resolved_ranks_by_name: dict[str, int] | None = None,
    prev_resolved_lineage_key: str | None = None,
    resolved_decisions: dict[str, str] | None = None,
    resolved_fingerprint: str | None = None,
    resolved_decision: str | None = None,
) -> dict[str, Any]:
    normalized_scores = _sanitize_scores(scores)
    normalized_times = _sanitize_times(times)
    active_route_norm = max(1, int(active_route_index or 1))
    route_offset = active_route_norm - 1

    athlete_ids = sorted(
        set(normalized_scores.keys()) | set(normalized_times.keys()),
        key=lambda name: name.lower(),
    )
    athletes = [Athlete(id=name, name=name) for name in athlete_ids]

    results: dict[str, LeadResult] = {}
    for athlete_id in athlete_ids:
        score_arr = normalized_scores.get(athlete_id, [])
        time_arr = normalized_times.get(athlete_id, [])
        score = score_arr[route_offset] if route_offset < len(score_arr) else None
        time_value = time_arr[route_offset] if route_offset < len(time_arr) else None
        results[athlete_id] = _score_to_lead_result(
            score=score,
            time_seconds=time_value,
            active_holds_count=active_holds_count,
        )

    # Build baseline tie groups for has_eligible_tie and event-level fingerprint compatibility.
    baseline_sorted = sorted(
        athletes,
        key=lambda athlete: (
            -_result_key(results[athlete.id])[0],
            -_result_key(results[athlete.id])[1],
            -_result_key(results[athlete.id])[2],
            athlete.name.lower(),
            athlete.id,
        ),
    )
    tie_groups: list[dict[str, Any]] = []
    pos = 1
    i = 0
    while i < len(baseline_sorted):
        athlete = baseline_sorted[i]
        k = _result_key(results[athlete.id])
        j = i + 1
        while j < len(baseline_sorted) and _result_key(results[baseline_sorted[j].id]) == k:
            j += 1
        size = j - i
        if size > 1:
            members = baseline_sorted[i:j]
            tie_groups.append(
                {
                    "rank_start": pos,
                    "rank_end": pos + size - 1,
                    "affects_podium": pos <= 3,
                    "members": [
                        {
                            "name": m.name,
                            "topped": bool(results[m.id].topped),
                            "hold": int(results[m.id].hold),
                            "plus": bool(results[m.id].plus),
                            "time": results[m.id].time_seconds,
                        }
                        for m in members
                    ],
                }
            )
        pos += size
        i = j
    has_eligible_tie = bool(tie_groups)
    event_fp = _event_global_fingerprint(
        box_id=box_id,
        route_index=active_route_norm,
        tie_groups=tie_groups,
    )

    if not has_eligible_tie:
        rows = []
        for idx, athlete in enumerate(baseline_sorted):
            result = results[athlete.id]
            score_hint = float(result.hold) + (
                0.0 if result.topped else (0.1 if result.plus else 0.0)
            )
            rows.append(
                {
                    "name": athlete.name,
                    "rank": idx + 1,
                    "total": score_hint,
                    "score": score_hint,
                    "time": result.time_seconds,
                    "tb_time": False,
                    "tb_prev": False,
                    "raw_scores": normalized_scores.get(athlete.id, []),
                    "raw_times": normalized_times.get(athlete.id, []),
                }
            )
        return {
            "overall_rows": rows,
            "route_rows": rows,
            "lead_ranking_rows": rows,
            "lead_tie_events": [],
            "lead_ranking_resolved": True,
            "eligible_groups": [],
            "prev_resolved_decisions": {},
            "prev_orders_by_fingerprint": {},
            "prev_ranks_by_fingerprint": {},
            "resolved_decisions": {},
            "fingerprint": None,
            "has_eligible_tie": False,
            "is_resolved": True,
            "errors": [],
        }

    normalized_prev_decisions = _normalize_resolved_decisions(
        resolved_decisions=prev_resolved_decisions,
        resolved_fingerprint=prev_resolved_fingerprint,
        resolved_decision=prev_resolved_decision,
    )
    normalized_prev_orders = _normalize_order_map(prev_orders_by_fingerprint)
    if (
        isinstance(prev_resolved_fingerprint, str)
        and prev_resolved_fingerprint.strip()
        and isinstance(prev_resolved_order, list)
    ):
        normalized_prev_orders[prev_resolved_fingerprint.strip()] = [
            item.strip()
            for item in prev_resolved_order
            if isinstance(item, str) and item.strip()
        ]
    normalized_prev_ranks = _normalize_ranks_map(prev_ranks_by_fingerprint)
    normalized_prev_lineage_ranks = _normalize_ranks_map(prev_lineage_ranks_by_key)
    event_prev_ranks = None
    if isinstance(prev_resolved_ranks_by_name, dict):
        event_prev_ranks = {
            str(name).strip(): int(rank)
            for name, rank in prev_resolved_ranks_by_name.items()
            if isinstance(name, str)
            and name.strip()
            and isinstance(rank, int)
            and not isinstance(rank, bool)
            and rank > 0
        }
    normalized_time_decisions = _normalize_resolved_decisions(
        resolved_decisions=resolved_decisions,
        resolved_fingerprint=resolved_fingerprint,
        resolved_decision=resolved_decision,
    )

    if not bool(time_criterion_enabled):
        rows: list[dict[str, Any]] = []
        rank = 1
        i = 0
        while i < len(baseline_sorted):
            athlete = baseline_sorted[i]
            current_key = _result_key(results[athlete.id])
            j = i + 1
            while j < len(baseline_sorted) and _result_key(results[baseline_sorted[j].id]) == current_key:
                j += 1
            for k in range(i, j):
                athlete_k = baseline_sorted[k]
                result_k = results[athlete_k.id]
                score_hint = float(result_k.hold) + (
                    0.0 if result_k.topped else (0.1 if result_k.plus else 0.0)
                )
                rows.append(
                    {
                        "name": athlete_k.name,
                        "rank": rank,
                        "total": score_hint,
                        "score": score_hint,
                        "time": result_k.time_seconds,
                        "tb_time": False,
                        "tb_prev": False,
                        "raw_scores": normalized_scores.get(athlete_k.id, []),
                        "raw_times": normalized_times.get(athlete_k.id, []),
                    }
                )
            rank += j - i
            i = j
        route_rows = [
            {
                "name": row["name"],
                "rank": row["rank"],
                "score": row["score"],
                "time": row["time"],
                "tb_time": False,
                "tb_prev": False,
            }
            for row in rows
        ]
        return {
            "overall_rows": rows,
            "route_rows": route_rows,
            "lead_ranking_rows": rows,
            "lead_tie_events": [],
            "lead_ranking_resolved": True,
            "eligible_groups": [],
            "prev_resolved_decisions": normalized_prev_decisions,
            "prev_orders_by_fingerprint": normalized_prev_orders,
            "prev_ranks_by_fingerprint": normalized_prev_ranks,
            "resolved_decisions": normalized_time_decisions,
            "fingerprint": event_fp,
            "has_eligible_tie": has_eligible_tie,
            "is_resolved": True,
            "errors": [],
        }

    resolver = _StateBackedResolver(
        prev_decisions=normalized_prev_decisions,
        prev_orders=normalized_prev_orders,
        prev_ranks=normalized_prev_ranks,
        prev_lineage_ranks=normalized_prev_lineage_ranks,
        time_decisions=normalized_time_decisions,
        global_fingerprint=event_fp,
        event_prev_fingerprint=prev_resolved_fingerprint.strip()
        if isinstance(prev_resolved_fingerprint, str)
        else None,
        event_prev_decision=prev_resolved_decision
        if prev_resolved_decision in {"yes", "no"}
        else None,
        event_prev_order=prev_resolved_order if isinstance(prev_resolved_order, list) else None,
        event_prev_ranks=event_prev_ranks,
        event_prev_lineage_key=prev_resolved_lineage_key.strip()
        if isinstance(prev_resolved_lineage_key, str) and prev_resolved_lineage_key.strip()
        else None,
        event_time_fingerprint=resolved_fingerprint.strip()
        if isinstance(resolved_fingerprint, str)
        else None,
        event_time_decision=resolved_decision if resolved_decision in {"yes", "no"} else None,
    )

    core_result = compute_lead_ranking(
        athletes=athletes,
        results=results,
        tie_break_resolver=resolver if bool(time_criterion_enabled) else None,
        podium_places=3,
        round_name=f"Final|route:{active_route_norm}",
    )

    row_by_id = {row.athlete_id: row for row in core_result.rows}
    ordered_ids = [row.athlete_id for row in core_result.rows]

    overall_rows: list[dict[str, Any]] = []
    for athlete_id in ordered_ids:
        row = row_by_id[athlete_id]
        overall_rows.append(
            {
                "name": row.athlete_name,
                "rank": int(row.rank),
                "total": float(row.score_hint),
                "score": float(row.score_hint),
                "time": row.time_seconds,
                "tb_time": bool(row.tb_time),
                "tb_prev": bool(row.tb_prev),
                "raw_scores": normalized_scores.get(athlete_id, []),
                "raw_times": normalized_times.get(athlete_id, []),
            }
        )

    route_rows = [
        {
            "name": row["name"],
            "rank": row["rank"],
            "score": row["score"],
            "time": row["time"],
            "tb_time": row["tb_time"],
            "tb_prev": row["tb_prev"],
        }
        for row in overall_rows
    ]

    eligible_groups: list[dict[str, Any]] = []
    for event in core_result.tie_events:
        members = [
            {
                "name": member.athlete_name,
                "time": member.time_seconds,
                "value": float(member.score_hint),
            }
            for member in event.members
        ]
        prev_decision = (
            normalized_prev_decisions.get(event.fingerprint)
            if event.stage == "previous_rounds"
            else None
        )
        time_decision = (
            normalized_time_decisions.get(event.fingerprint)
            if event.stage == "time"
            else None
        )
        known_prev = {
            name: int(rank)
            for name, rank in (event.known_prev_ranks_by_athlete or {}).items()
            if isinstance(name, str) and isinstance(rank, int) and rank > 0
        }
        if not known_prev:
            known_prev = {
                name: int(rank)
                for name, rank in (normalized_prev_ranks.get(event.fingerprint) or {}).items()
                if isinstance(name, str) and isinstance(rank, int) and rank > 0
            }
        eligible_groups.append(
            {
                "context": "overall",
                "rank": int(event.rank_start),
                "value": float(event.members[0].score_hint) if event.members else None,
                "members": members,
                "fingerprint": event.fingerprint,
                "stage": event.stage,
                "affects_podium": bool(event.affects_podium),
                "status": event.status,
                "detail": event.detail,
                "prev_rounds_decision": prev_decision if prev_decision in {"yes", "no"} else None,
                "prev_rounds_order": normalized_prev_orders.get(event.fingerprint),
                "prev_rounds_ranks_by_name": known_prev or None,
                "lineage_key": event.lineage_key,
                "known_prev_ranks_by_name": known_prev or {},
                "missing_prev_rounds_members": list(event.missing_prev_rounds_athlete_ids or []),
                "requires_prev_rounds_input": bool(event.requires_prev_rounds_input),
                "time_decision": time_decision if time_decision in {"yes", "no"} else None,
                "resolved_decision": time_decision if time_decision in {"yes", "no"} else None,
                "resolution_kind": "time" if event.stage == "time" else "previous_rounds",
                "is_resolved": bool(event.status == "resolved"),
            }
        )

    return {
        "overall_rows": overall_rows,
        "route_rows": route_rows,
        "lead_ranking_rows": overall_rows,
        "lead_tie_events": eligible_groups,
        "lead_ranking_resolved": bool(core_result.is_resolved),
        "eligible_groups": eligible_groups,
        "prev_resolved_decisions": normalized_prev_decisions,
        "prev_orders_by_fingerprint": normalized_prev_orders,
        "prev_ranks_by_fingerprint": normalized_prev_ranks,
        "prev_lineage_ranks_by_key": normalized_prev_lineage_ranks,
        "resolved_decisions": normalized_time_decisions,
        "fingerprint": event_fp,
        "has_eligible_tie": has_eligible_tie,
        "is_resolved": bool(core_result.is_resolved),
        "errors": list(core_result.errors),
    }
