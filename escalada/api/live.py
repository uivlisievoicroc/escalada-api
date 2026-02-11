# escalada/api/live.py
"""
Live contest API (state + WebSockets).

This module is the "authoritative runtime" for contest state per `box_id`:
- POST `/api/cmd`: apply commands (timer/progress/scoring/init/reset) with validation + rate limiting
- WS `/api/ws/{box_id}`: authenticated real-time stream for ControlPanel/ContestPage/JudgePage
- GET `/api/state/{box_id}`: on-demand state snapshot for hydration/recovery and headless flows
- Public snapshot/WS: read-only, aggregated updates for spectators

Key design points:
- In-memory state is protected with a per-box asyncio.Lock (`state_locks`)
- Box initialization is protected with a global lock (`init_lock`) to avoid races
- Audit logging is append-only and includes actor metadata via a ContextVar (`current_actor`)
"""

# -------------------- Standard library imports --------------------
import asyncio
import json
import logging
import os
import time
import uuid

# -------------------- Typing/context helpers --------------------
# ContextVar is used to attach request actor info to state mutations for audit logging.
from contextvars import ContextVar
from typing import Any
from typing import Dict

# -------------------- Third-party imports --------------------
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from starlette.websockets import WebSocket

# -------------------- Local application imports --------------------
# Rate limiting is applied per box + per command type to keep the server responsive during events.
from escalada.rate_limit import check_rate_limit
# Core command validation + state transition logic (shared with other services).
from escalada_core import (
    ValidatedCmd,
    ValidationError,
    apply_command,
    default_state,
    parse_timer_preset,
    validate_session_and_version,
)
from escalada.auth.deps import (
    require_box_access,
    require_view_access,
    require_view_box_access,
)
from escalada.auth.service import decode_token
from escalada.storage.json_store import (
    append_audit_event,
    build_audit_event,
    clear_box_state_files,
    ensure_storage_dirs,
    load_box_states,
    load_competition_officials,
    save_competition_officials,
    save_box_state,
)

logger = logging.getLogger(__name__)

# -------------------- In-memory contest state (authoritative at runtime) --------------------
# `state_map` stores the per-box state dict; it is persisted to JSON on each state-changing command.
state_map: Dict[int, dict] = {}
# Per-box lock to serialize command application and state persistence for a given box id.
state_locks: Dict[int, asyncio.Lock] = {}  # Lock per boxId
# Global init lock protects creation/registration of per-box locks and first state initialization.
init_lock = asyncio.Lock()  # Protects state_map and state_locks initialization
# Actor metadata for audit log entries (set in request handlers).
current_actor: ContextVar[dict[str, Any] | None] = ContextVar("current_actor", default=None)

router = APIRouter()
# Active authenticated WS subscribers per box id.
channels: dict[int, set[WebSocket]] = {}
channels_lock = asyncio.Lock()  # Protects concurrent access to channels dict
# Public spectators (unauthenticated, aggregated stream).
public_channels: set[WebSocket] = set()
public_channels_lock = asyncio.Lock()

# Validation toggle:
# - True in normal operation (ValidatedCmd + stale/session checks + rate limiting)
# - Tests may disable it for backwards compatibility / focused unit scenarios
VALIDATION_ENABLED = True
# Global competition officials (not box-specific). Loaded at startup and included in snapshots.
competition_officials: dict[str, str] = {"judgeChief": "", "competitionDirector": "", "chiefRoutesetter": ""}


def get_competition_officials() -> dict[str, str]:
    """Return a defensive copy of the global competition officials."""
    return dict(competition_officials)


def set_competition_officials(
    *, judge_chief: str, competition_director: str, chief_routesetter: str
) -> dict[str, str]:
    """
    Update global officials (judge chief + director + chief routesetter).

    This is global to the entire event (not per-box/per-route) and is persisted to JSON so
    ContestPage/Public views can show it consistently even after restarts.
    """
    global competition_officials
    competition_officials = {
        "judgeChief": (judge_chief or "").strip(),
        "competitionDirector": (competition_director or "").strip(),
        "chiefRoutesetter": (chief_routesetter or "").strip(),
    }
    try:
        save_competition_officials(
            competition_officials["judgeChief"],
            competition_officials["competitionDirector"],
            competition_officials["chiefRoutesetter"],
        )
    except Exception as exc:
        logger.error("Failed to persist competition officials: %s", exc, exc_info=True)
    return dict(competition_officials)


def _server_side_timer_enabled() -> bool:
    """
    Server is authoritative for the countdown by default.

    Set SERVER_SIDE_TIMER=0/false/no to opt-out (legacy client-driven TIMER_SYNC).
    """
    value = os.getenv("SERVER_SIDE_TIMER", "").strip().lower()
    if value in {"0", "false", "no", "n", "off"}:
        return False
    return True


def _now_ms() -> int:
    return int(time.time() * 1000)


def _compute_remaining(state: dict, now_ms: int) -> float | None:
    # Remaining time is derived from (in priority order):
    # - `timerEndsAtMs` (server-side authoritative countdown while running)
    # - `timerRemainingSec` (persisted remaining seconds while paused/idle)
    # - legacy `remaining` (older clients)
    # - `timerPresetSec` (fallback to full preset)
    ends_at = state.get("timerEndsAtMs")
    if isinstance(ends_at, (int, float)):
        remaining = (float(ends_at) - now_ms) / 1000.0
        return max(0.0, remaining)

    remaining = state.get("timerRemainingSec")
    if isinstance(remaining, (int, float)):
        return max(0.0, float(remaining))

    legacy = state.get("remaining")
    if isinstance(legacy, (int, float)):
        return max(0.0, float(legacy))

    preset = state.get("timerPresetSec")
    if isinstance(preset, (int, float)):
        return max(0.0, float(preset))

    return None


def _apply_server_side_timer(state: dict, cmd_payload: dict, now_ms: int) -> None:
    # Server-side timer implementation:
    # - Uses `timerEndsAtMs` while running for monotonic, stutter-free countdown
    # - Stores `timerRemainingSec` when paused/stopped so resume continues from the correct value
    # - Treats client TIMER_SYNC as best-effort for legacy flows and never allows "time extension"
    cmd_type = cmd_payload.get("type")

    if cmd_type == "INIT_ROUTE":
        preset = state.get("timerPresetSec")
        if preset is None:
            preset = parse_timer_preset(cmd_payload.get("timerPreset"))
        state["timerRemainingSec"] = float(preset) if isinstance(preset, (int, float)) else None
        state["timerEndsAtMs"] = None
        return

    if cmd_type == "SET_TIMER_PRESET":
        preset = state.get("timerPresetSec")
        if preset is None:
            preset = parse_timer_preset(cmd_payload.get("timerPreset"))
        # Do not disrupt an active/pending competitor; apply preset when idle only.
        if state.get("timerState") in {"running", "paused"}:
            return
        state["timerRemainingSec"] = float(preset) if isinstance(preset, (int, float)) else None
        state["timerEndsAtMs"] = None
        return

    if cmd_type == "RESET_PARTIAL":
        # RESET_PARTIAL allows selective reset of timer/progress/competitors.
        # Sync timer state: reset remaining time to preset when timer is reset.
        # Note: escalada-core handles timerState/holdCount/marked changes; this syncs derived fields.
        if cmd_payload.get("resetTimer") or cmd_payload.get("unmarkAll"):
            preset = state.get("timerPresetSec")
            state["timerRemainingSec"] = float(preset) if isinstance(preset, (int, float)) else None
            state["timerEndsAtMs"] = None
        return

    if cmd_type in {"START_TIMER", "RESUME_TIMER"}:
        remaining = _compute_remaining(state, now_ms)
        if remaining is None:
            state["timerEndsAtMs"] = None
            return
        state["timerRemainingSec"] = float(remaining)
        state["timerEndsAtMs"] = int(now_ms + remaining * 1000.0)
        return

    if cmd_type == "STOP_TIMER":
        ends_at = state.get("timerEndsAtMs")
        remaining = None
        if isinstance(ends_at, (int, float)):
            remaining = max(0.0, (float(ends_at) - now_ms) / 1000.0)
        elif isinstance(state.get("timerRemainingSec"), (int, float)):
            remaining = float(state.get("timerRemainingSec"))
        elif isinstance(cmd_payload.get("remaining"), (int, float)):
            remaining = float(cmd_payload.get("remaining"))
        state["timerRemainingSec"] = remaining
        state["timerEndsAtMs"] = None
        return

    if cmd_type == "TIMER_SYNC":
        # When server-side timer is enabled, TIMER_SYNC is treated as best-effort/legacy.
        # Never let a client "extend" a running timer (UI stalls/spam-clicks used to do that).
        if state.get("timerState") == "running":
            return
        if isinstance(cmd_payload.get("remaining"), (int, float)):
            remaining = float(cmd_payload.get("remaining"))
            state["timerRemainingSec"] = remaining
            state["timerEndsAtMs"] = None
        return

    if cmd_type in {"SUBMIT_SCORE", "RESET_BOX"}:
        preset = state.get("timerPresetSec")
        state["timerRemainingSec"] = float(preset) if isinstance(preset, (int, float)) else None
        state["timerEndsAtMs"] = None
        return

async def preload_states_from_json() -> int:
    """
    Load persisted box states + global officials into memory (JSON-only).

    By default we start clean on each launch to avoid accidental state carry-over between events.
    To resume previous state, set `RESET_BOXES_ON_START=0/false/no`.
    """
    ensure_storage_dirs()

    # Default behavior: start clean on every launch.
    # Opt-out (resume previous contest state) by setting RESET_BOXES_ON_START=0/false/no.
    reset_env = os.getenv("RESET_BOXES_ON_START", "").strip().lower()
    should_reset = reset_env not in {"0", "false", "no", "n", "off"}
    if should_reset:
        removed = clear_box_state_files()
        logger.warning(
            "Starting clean: deleted %s box state files (set RESET_BOXES_ON_START=0 to keep state)",
            removed,
        )
    states = load_box_states()
    try:
        global competition_officials
        competition_officials = load_competition_officials()
    except Exception as exc:
        logger.warning("Failed to preload competition officials: %s", exc)
    loaded = 0
    async with init_lock:
        for box_id, state in states.items():
            state_map[box_id] = state
            state_locks[box_id] = state_locks.get(box_id) or asyncio.Lock()
            loaded += 1
    if loaded:
        logger.info(f"Preloaded {loaded} box states from JSON")
    return loaded

async def preload_states() -> int:
    return await preload_states_from_json()


async def get_all_states_snapshot() -> Dict[int, dict]:
    """Return a thread-safe shallow copy of all states for backup/export operations."""
    async with init_lock:
        return {box_id: dict(state) for box_id, state in state_map.items()}


class Cmd(BaseModel):
    """
    Legacy command schema accepted by `/api/cmd`.

    Notes:
    - When `VALIDATION_ENABLED` is True, this payload is normalized into `ValidatedCmd`
      (stricter typing + sanitization + required field enforcement).
    - When validation is disabled (test/back-compat mode), we allow the legacy shape as-is.
    """

    boxId: int
    # Command type drives which optional fields must be present.
    type: str  # START_TIMER, STOP_TIMER, RESUME_TIMER, PROGRESS_UPDATE, REQUEST_ACTIVE_COMPETITOR, SUBMIT_SCORE, INIT_ROUTE, REQUEST_STATE

    # ---- generic optional fields ----
    # for PROGRESS_UPDATE
    delta: float | None = None

    # for SUBMIT_SCORE
    score: float | None = None
    competitor: str | None = None
    registeredTime: float | None = None
    competitorIdx: int | None = None
    # alias for competitorIdx (0-based)
    idx: int | None = None

    # for INIT_ROUTE
    routeIndex: int | None = None
    holdsCount: int | None = None
    routesCount: int | None = None
    holdsCounts: list[int] | None = None
    competitors: list[dict] | None = None
    categorie: str | None = None
    timerPreset: str | None = None

    # for SET_TIME_CRITERION
    timeCriterionEnabled: bool | None = None

    # for TIMER_SYNC
    remaining: float | None = None

    # legacy alias for registeredTime
    time: float | None = None

    # Session token for state bleed prevention
    sessionId: str | None = None

    # Box version for stale command detection (TASK 2.6)
    boxVersion: int | None = None

def _get_actor_from_request_and_claims(
    request: Request | None, claims: dict | None
) -> dict[str, Any] | None:
    """
    Build an audit "actor" object from auth claims + request metadata.

    This is stored in a ContextVar (`current_actor`) so lower-level helpers (_persist_state)
    can include actor info without threading request objects through every call.
    """
    if not claims or not isinstance(claims, dict):
        return None
    username = claims.get("sub")
    role = claims.get("role")
    if not username and not role:
        return None
    ip = None
    user_agent = None
    if request is not None:
        ip = request.client.host if request.client else None
        user_agent = request.headers.get("user-agent")
    return {"username": username, "role": role, "ip": ip, "user_agent": user_agent}

@router.post("/cmd")
async def cmd(cmd: Cmd, request: Request = None, claims=Depends(require_box_access)):
    """
    Handle competition commands with validation and rate limiting

    Validates:
    - Input format and types
    - Box ID range
    - Required fields for each command type
    - Competitor name safety
    - Timer preset format

    Rate Limits:
    - 60 requests/minute per box (global)
    - 10 requests/second per box
    - Per-command-type limits (e.g., PROGRESS_UPDATE: 120/min)
    """

    # Attach actor metadata (username/role/ip/user-agent) to this request so persistence/audit
    # helpers can record "who did what" without passing Request everywhere.
    actor_token = current_actor.set(
        _get_actor_from_request_and_claims(
            request,
            claims if isinstance(claims, dict) else None,
        )
    )
    try:
        # ==================== VALIDATION ====================
        # Map legacy "time" field to registeredTime when provided
        if cmd.registeredTime is None and cmd.time is not None:
            cmd.registeredTime = cmd.time

        # ==================== VALIDATION ====================
        try:
            if VALIDATION_ENABLED:
                # Build dict with only non-None values
                cmd_data = {k: v for k, v in cmd.model_dump().items() if v is not None}
                if "time" in cmd_data and "registeredTime" not in cmd_data:
                    cmd_data["registeredTime"] = cmd_data.pop("time")
                # Validate and sanitize input
                validated_cmd = ValidatedCmd(**cmd_data)
            else:
                # Validation disabled - use cmd as is
                validated_cmd = cmd
        except Exception as e:
            logger.warning(f"Command validation failed for box {cmd.boxId}: {e}")
            raise HTTPException(status_code=400, detail=f"Invalid command: {str(e)}")

        # Use validated command downstream (normalization + stricter schema)
        cmd = validated_cmd

        # ==================== RATE LIMITING ====================
        # Skip rate limiting in test mode (when VALIDATION_ENABLED is False)
        if VALIDATION_ENABLED:
            is_allowed, reason = check_rate_limit(cmd.boxId, cmd.type)
            if not is_allowed:
                logger.warning(f"Rate limit exceeded for box {cmd.boxId}: {reason}")
                raise HTTPException(status_code=429, detail=reason)

        # ==================== SANITIZATION ====================
        # Validation already checks for SQL injection/XSS in ValidatedCmd
        # No additional sanitization needed - preserve original input including diacritics

        print(f"Backend received cmd: {cmd}")

        # ==================== ATOMIC LOCK + STATE INITIALIZATION ====================
        # CRITICAL: Create the per-box lock under `init_lock`, then hold that per-box lock for the
        # entire request (including first-time `_ensure_state()` initialization). Without this,
        # two concurrent requests for a new box can interleave and cause double-init / lost updates.
        # Get or create the box-specific lock under global init_lock protection
        async with init_lock:
            if cmd.boxId not in state_locks:
                state_locks[cmd.boxId] = asyncio.Lock()
            lock = state_locks[cmd.boxId]

        # Lock state access for this boxId
        async with lock:
            # Initialize state INSIDE the lock (no window for race condition)
            sm = await _ensure_state(cmd.boxId)

            # ==================== SESSION & VERSION VALIDATION ====================
            # Enforce session/version only when validation is enabled (test-mode bypass)
            if VALIDATION_ENABLED:
                validation_error: ValidationError | None = validate_session_and_version(
                    sm,
                    cmd.model_dump(),
                    require_session=cmd.type != "INIT_ROUTE",
                )
                if validation_error:
                    if validation_error.status_code:
                        logger.warning(
                            f"Command {cmd.type} for box {cmd.boxId} missing sessionId"
                        )
                        raise HTTPException(
                            status_code=validation_error.status_code,
                            detail=validation_error.message,
                        )
                    if validation_error.kind:
                        logger.warning(
                            f"Command {cmd.type} for box {cmd.boxId} rejected: {validation_error.kind}"
                        )
                        return {"status": "ignored", "reason": validation_error.kind}

            # Handle request-state early (transport-only)
            if cmd.type == "REQUEST_STATE":
                await _send_state_snapshot(cmd.boxId)
                return {"status": "ok"}

            # NOTE: For RESET_PARTIAL, we must forward the checkbox flags even if the upstream
            # Cmd/ValidatedCmd schema doesn't include them (Pydantic would silently drop extras).
            # We read them from the raw request body and merge into the dict we pass to `apply_command`.
            cmd_dict = cmd.model_dump()
            if cmd.type == "RESET_PARTIAL" and request is not None:
                try:
                    raw = await request.json()
                    if isinstance(raw, dict):
                        for k in ("resetTimer", "clearProgress", "unmarkAll"):
                            if k in raw and isinstance(raw.get(k), bool):
                                cmd_dict[k] = raw.get(k)
                except Exception:
                    pass

            outcome = apply_command(sm, cmd_dict)
            cmd_payload = outcome.cmd_payload
            if _server_side_timer_enabled():
                _apply_server_side_timer(sm, cmd_payload, _now_ms())

            # Persist snapshot + audit log for state-changing commands
            persist_result = "ok"
            if VALIDATION_ENABLED:
                persist_result = await _persist_state(cmd.boxId, sm, cmd.type, cmd_payload)
                if persist_result == "stale":
                    return {"status": "ignored", "reason": "stale_version"}

            # Broadcast command echo to all active WebSockets for this box
            await _broadcast_to_box(cmd.boxId, cmd_payload)

            # Send authoritative snapshot for real-time clients when needed
            if outcome.snapshot_required:
                await _send_state_snapshot(cmd.boxId)

            public_update = _public_update_type(cmd.type)
            if public_update:
                await _broadcast_public_box_update(cmd.boxId, public_update)

        return {"status": "ok"}
    finally:
        try:
            current_actor.reset(actor_token)
        except Exception:
            pass

async def _heartbeat(ws: WebSocket, box_id: int, last_pong: dict[str, float]) -> None:
    """Send PING every 30s; close if no PONG for 60s."""
    heartbeat_interval = 30
    heartbeat_timeout = 60

    while True:
        try:
            await asyncio.sleep(heartbeat_interval)
            now = asyncio.get_event_loop().time()

            # Check timeout
            if now - (last_pong.get("ts") or 0.0) > heartbeat_timeout:
                logger.warning(f"Heartbeat timeout for box {box_id}, closing")
                try:
                    await ws.close(code=1000)
                except Exception:
                    pass
                break

            # Send PING
            await ws.send_text(
                json.dumps({"type": "PING", "timestamp": now}, ensure_ascii=False)
            )
        except Exception as e:
            logger.debug(f"Heartbeat error for box {box_id}: {e}")
            break

async def _broadcast_to_box(box_id: int, payload: dict) -> None:
    """Safely broadcast JSON payload to all subscribers on a box.
    Removes dead connections automatically.
    Disconnects slow clients (timeout 5s) to prevent blocking.
    """
    # Get snapshot of current subscribers
    async with channels_lock:
        sockets = list(channels.get(box_id) or set())

    dead = []
    message = json.dumps(payload, ensure_ascii=False)
    for ws in sockets:
        try:
            # Add timeout to prevent slow clients from blocking broadcast
            await asyncio.wait_for(ws.send_text(message), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning(f"WebSocket send timeout for box {box_id}, disconnecting slow client")
            dead.append(ws)
            try:
                await ws.close(code=1008, reason="Send timeout")
            except Exception:
                pass
        except Exception as e:
            logger.debug(f"Broadcast error to box {box_id}: {e}")
            dead.append(ws)

    # Clean up dead connections
    if dead:
        async with channels_lock:
            for ws in dead:
                channels.get(box_id, set()).discard(ws)

def _public_preparing_climber(state: dict) -> str:
    """
    Best-effort "on deck" climber for public views.

    Public snapshots intentionally do not include the full competitor list, but the UI still
    wants a lightweight "next up" label. We derive it from the internal competitor list by:
    - finding `currentClimber` in `competitors`
    - returning the first *unmarked* competitor after them
    """
    competitors = state.get("competitors") or []
    if not isinstance(competitors, list):
        return ""

    current = state.get("currentClimber")
    if not isinstance(current, str) or not current:
        return ""

    current_idx = None
    for i, comp in enumerate(competitors):
        if isinstance(comp, dict) and comp.get("nume") == current:
            current_idx = i
            break
    if current_idx is None:
        return ""

    for comp in competitors[current_idx + 1 :]:
        if not isinstance(comp, dict):
            continue
        name = comp.get("nume")
        if not isinstance(name, str) or not name.strip():
            continue
        if comp.get("marked"):
            continue
        return name
    return ""


def _build_public_box_state(box_id: int, state: dict) -> dict:
    """
    Build the read-only state shape sent to the public hub/WS.

    This is a reduced projection of the internal box state:
    - does NOT expose the full competitor list (privacy + payload size)
    - includes enough information for Live Rankings / Live Climbing tiles
    - computes `remaining` from the authoritative server timer when enabled
    """
    routes_count = state.get("routesCount")
    if routes_count is None:
        routes_count = state.get("routeIndex") or 1
    holds_counts = state.get("holdsCounts") or []
    if not isinstance(holds_counts, list):
        holds_counts = []
    remaining = state.get("remaining")
    if _server_side_timer_enabled():
        remaining = _compute_remaining(state, _now_ms())
    return {
        "boxId": box_id,
        "categorie": state.get("categorie", ""),
        "initiated": state.get("initiated", False),
        "routeIndex": state.get("routeIndex", 1),
        "routesCount": routes_count,
        "holdsCount": state.get("holdsCount", 0),
        "holdsCounts": holds_counts,
        "currentClimber": state.get("currentClimber", ""),
        "preparingClimber": (state.get("preparingClimber") or _public_preparing_climber(state)),
        "timerState": state.get("timerState", "idle"),
        "remaining": remaining,
        "timeCriterionEnabled": state.get("timeCriterionEnabled", False),
        "scoresByName": state.get("scores") or {},
        "timesByName": state.get("times") or {},
    }

async def _broadcast_public(payload: dict) -> None:
    """
    Best-effort broadcast to all public spectators.

    Unlike the authenticated per-box channel, we keep this simple:
    - no per-client authorization
    - remove dead sockets on send errors
    """
    async with public_channels_lock:
        sockets = list(public_channels)

    dead = []
    for ws in sockets:
        try:
            await ws.send_text(json.dumps(payload, ensure_ascii=False))
        except Exception as e:
            logger.debug(f"Public broadcast error: {e}")
            dead.append(ws)

    if dead:
        async with public_channels_lock:
            for ws in dead:
                public_channels.discard(ws)

async def _build_public_snapshot_payload() -> dict:
    """Build a full public snapshot of all boxes (used on connect and on refresh)."""
    async with init_lock:
        items = list(state_map.items())
    return {
        "type": "PUBLIC_STATE_SNAPSHOT",
        "boxes": [_build_public_box_state(box_id, state) for box_id, state in items],
    }

async def _send_public_snapshot(targets: set[WebSocket] | None = None) -> None:
    """
    Send a public snapshot either to a specific set of sockets or to everyone.

    `targets` is used on connect/refresh so we don't rebroadcast to all clients unnecessarily.
    """
    payload = await _build_public_snapshot_payload()
    if targets:
        for ws in list(targets):
            try:
                await ws.send_text(json.dumps(payload, ensure_ascii=False))
            except Exception as e:
                logger.debug(f"Failed to send public snapshot: {e}")
    else:
        await _broadcast_public(payload)

async def _broadcast_public_box_update(box_id: int, update_type: str) -> None:
    """
    Broadcast a single-box update to public spectators.

    This is used for incremental updates (timer/progress/scoring) so public clients can keep
    their UI current without requesting full snapshots on every command.
    """
    async with init_lock:
        state = state_map.get(box_id)
    if not state:
        return
    payload = {
        "type": update_type,
        "box": _build_public_box_state(box_id, state),
    }
    await _broadcast_public(payload)

    # Also notify the per-box public feed (separate module) if it is enabled.
    # Imported lazily to avoid circular imports during startup.
    try:
        from escalada.api.public import broadcast_to_public_box, _send_public_box_snapshot
        await _send_public_box_snapshot(box_id)
    except ImportError:
        pass

def _public_update_type(cmd_type: str) -> str | None:
    """Map internal command types to public update event types (or None for no public update)."""
    return {
        "INIT_ROUTE": "BOX_STATUS_UPDATE",
        "RESET_BOX": "BOX_STATUS_UPDATE",
        "RESET_PARTIAL": "BOX_STATUS_UPDATE",
        "START_TIMER": "BOX_FLOW_UPDATE",
        "STOP_TIMER": "BOX_FLOW_UPDATE",
        "RESUME_TIMER": "BOX_FLOW_UPDATE",
        "SET_TIMER_PRESET": "BOX_FLOW_UPDATE",
        "TIMER_SYNC": "BOX_FLOW_UPDATE",
        "REGISTER_TIME": "BOX_FLOW_UPDATE",
        "SUBMIT_SCORE": "BOX_RANKING_UPDATE",
        "SET_TIME_CRITERION": "BOX_RANKING_UPDATE",
    }.get(cmd_type)

def _authorize_ws(box_id: int, claims: dict) -> bool:
    """Return True if claims allow subscription to box_id."""
    role = claims.get("role")
    if role == "admin":
        return True
    boxes = set(claims.get("boxes") or [])
    if role == "judge":
        return int(box_id) in boxes
    if role == "viewer":
        # Allow viewers; if boxes are specified, enforce membership
        return not boxes or int(box_id) in boxes
    return False

@router.websocket("/ws/{box_id}")
async def websocket_endpoint(ws: WebSocket, box_id: int):
    """
    Authenticated per-box WebSocket (ControlPanel / ContestPage / JudgePage).

    Flow:
    - Authenticate token (query param for legacy, then cookie)
    - Authorize access to the requested box_id
    - Add subscriber to the per-box channel
    - Send an initial STATE_SNAPSHOT for hydration
    - Maintain a heartbeat (PING/PONG) and handle REQUEST_STATE refresh messages
    """
    peer = ws.client.host if ws.client else None

    # Try to get token from query params first (backwards compatible), then from cookie
    token = ws.query_params.get("token")
    if not token:
        # Try httpOnly cookie
        token = ws.cookies.get("escalada_token")

    if not token:
        logger.warning("WS connect denied: token_required box=%s ip=%s", box_id, peer)
        await ws.close(code=4401, reason="token_required")
        return

    try:
        claims = decode_token(token)
    except HTTPException as exc:
        logger.warning("WS connect denied: invalid_token box=%s ip=%s detail=%s", box_id, peer, exc.detail)
        await ws.close(code=4401, reason=exc.detail or "invalid_token")
        return

    if not _authorize_ws(box_id, claims):
        logger.warning(
            "WS connect denied: forbidden box=%s ip=%s role=%s boxes=%s",
            box_id,
            peer,
            claims.get("role"),
            claims.get("boxes"),
        )
        await ws.close(code=4403, reason="forbidden_box_or_role")
        return

    await ws.accept()

    # Atomically add to channel so broadcasts see a consistent subscriber set.
    async with channels_lock:
        channels.setdefault(box_id, set()).add(ws)
        subscriber_count = len(channels[box_id])

    logger.info(f"Client connected to box {box_id}, total: {subscriber_count}")
    # Immediately send a snapshot so the client can render without waiting for the next command.
    await _send_state_snapshot(box_id, targets={ws})

    # Start heartbeat task
    last_pong = {"ts": asyncio.get_event_loop().time()}
    heartbeat_task = asyncio.create_task(_heartbeat(ws, box_id, last_pong))

    try:
        while True:
            try:
                # Receive with 180s timeout
                data = await asyncio.wait_for(ws.receive_text(), timeout=180)
            except asyncio.TimeoutError:
                logger.warning(f"WebSocket receive timeout for box {box_id}")
                break
            except Exception as e:
                logger.warning(f"WebSocket receive error for box {box_id}: {e}")
                break

            # Handle lightweight control messages from client (no state mutation over WS here).
            try:
                msg = json.loads(data) if isinstance(data, str) else data
                if isinstance(msg, dict):
                    msg_type = msg.get("type")

                    # Acknowledge PONG
                    if msg_type == "PONG":
                        last_pong["ts"] = asyncio.get_event_loop().time()
                        continue

                    # REQUEST_STATE lets a client recover after missed messages or tab backgrounding.
                    if msg_type == "REQUEST_STATE":
                        requested_box_id = msg.get("boxId", box_id)
                        try:
                            requested_box_id = int(requested_box_id)
                        except Exception:
                            continue

                        if requested_box_id != int(box_id) and not _authorize_ws(requested_box_id, claims):
                            logger.warning(
                                "Forbidden WS REQUEST_STATE: conn_box=%s requested_box=%s role=%s boxes=%s",
                                box_id,
                                requested_box_id,
                                claims.get("role"),
                                claims.get("boxes"),
                            )
                            continue

                        logger.info(
                            f"WebSocket REQUEST_STATE for box {requested_box_id}"
                        )
                        await _send_state_snapshot(requested_box_id, targets={ws})
                        continue

            except json.JSONDecodeError:
                logger.debug(f"Invalid JSON from WS box {box_id}")
                continue

    except Exception as e:
        logger.error(f"WebSocket error for box {box_id}: {e}")
    finally:
        # Cancel heartbeat task
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass

        # Atomically remove from channel
        async with channels_lock:
            channels.get(box_id, set()).discard(ws)
            remaining = len(channels.get(box_id, set()))

        logger.info(f"Client disconnected from box {box_id}, remaining: {remaining}")

        try:
            await ws.close()
        except Exception:
            pass

@router.get("/public/rankings")
async def public_rankings():
    return await _build_public_snapshot_payload()

@router.websocket("/public/ws")
async def public_websocket(ws: WebSocket):
    """
    Public (unauthenticated) WebSocket feed for spectators.

    Clients receive PUBLIC_STATE_SNAPSHOT on connect and can request a refresh with REQUEST_STATE.
    A heartbeat is maintained to detect dead connections.
    """
    await ws.accept()

    async with public_channels_lock:
        public_channels.add(ws)

    await _send_public_snapshot(targets={ws})

    last_pong = {"ts": asyncio.get_event_loop().time()}
    heartbeat_task = asyncio.create_task(_heartbeat(ws, -1, last_pong))

    try:
        while True:
            try:
                data = await asyncio.wait_for(ws.receive_text(), timeout=180)
            except asyncio.TimeoutError:
                logger.warning("Public WebSocket receive timeout")
                break
            except Exception as e:
                logger.warning(f"Public WebSocket receive error: {e}")
                break

            try:
                msg = json.loads(data) if isinstance(data, str) else data
                if isinstance(msg, dict):
                    msg_type = msg.get("type")
                    if msg_type == "PONG":
                        last_pong["ts"] = asyncio.get_event_loop().time()
                        continue
                    if msg_type == "PING":
                        # Some clients send PING; respond with PONG for compatibility.
                        await ws.send_text(
                            json.dumps(
                                {"type": "PONG", "timestamp": msg.get("timestamp")},
                                ensure_ascii=False,
                            )
                        )
                        continue
                    if msg_type == "REQUEST_STATE":
                        # Client-driven refresh: resend the full snapshot.
                        await _send_public_snapshot(targets={ws})
                        continue
            except json.JSONDecodeError:
                logger.debug("Invalid JSON from public WebSocket")
                continue

    except Exception as e:
        logger.error(f"Public WebSocket error: {e}")
    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass

        async with public_channels_lock:
            public_channels.discard(ws)

        try:
            await ws.close()
        except Exception:
            pass

# Route to get state snapshot for a box
from fastapi import HTTPException

@router.get("/state/{box_id}")
async def get_state(box_id: int, claims=Depends(require_view_box_access())):
    """
    Return current contest state for a judge client.
    Create a placeholder state with sessionId if box doesn't exist yet.
    """
    # ==================== ATOMIC STATE ACCESS ====================
    # Use global init_lock to prevent race conditions during state access
    async with init_lock:
        if box_id not in state_locks:
            state_locks[box_id] = asyncio.Lock()

    state = await _ensure_state(box_id)
    return _build_snapshot(box_id, state)

# helpers
def _build_snapshot(box_id: int, state: dict) -> dict:
    """
    Build the full state snapshot sent to authenticated clients.

    Includes internal fields required by ControlPanel/ContestPage/JudgePage:
    - competitors list + current flow fields
    - timer/preset data
    - global competition officials
    """
    remaining = state.get("remaining")
    if _server_side_timer_enabled():
        remaining = _compute_remaining(state, _now_ms())
    officials = get_competition_officials()
    return {
        "type": "STATE_SNAPSHOT",
        "boxId": box_id,
        "initiated": state.get("initiated", False),
        "holdsCount": state.get("holdsCount", 0),
        "routeIndex": state.get("routeIndex", 1),
        "routesCount": state.get("routesCount"),
        "holdsCounts": state.get("holdsCounts"),
        "currentClimber": state.get("currentClimber", ""),
        "preparingClimber": state.get("preparingClimber", ""),
        "started": state.get("started", False),
        "timerState": state.get("timerState", "idle"),
        "holdCount": state.get("holdCount", 0.0),
        "competitors": state.get("competitors", []),
        "categorie": state.get("categorie", ""),
        "registeredTime": state.get("lastRegisteredTime"),
        "remaining": remaining,
        "timeCriterionEnabled": state.get("timeCriterionEnabled", False),
        "timerPreset": state.get("timerPreset"),
        "timerPresetSec": state.get("timerPresetSec"),
        "judgeChief": officials.get("judgeChief", ""),
        "competitionDirector": officials.get("competitionDirector", ""),
        "chiefRoutesetter": officials.get("chiefRoutesetter", ""),
        "sessionId": state.get("sessionId"),  # Include session ID for client validation
        "boxVersion": state.get("boxVersion", 0),
    }

async def _send_state_snapshot(box_id: int, targets: set[WebSocket] | None = None):
    """
    Send a STATE_SNAPSHOT either to a specific set of sockets or to the whole box channel.

    Used on:
    - WS connect (targets={ws})
    - server-driven refreshes when a command requires a full snapshot
    """
    # Ensure state exists and get a copy atomically
    state = await _ensure_state(box_id)
    if state is None:
        return
    payload = _build_snapshot(box_id, state)

    # If targets specified (e.g., on new connection), send only to them
    if targets:
        for ws in list(targets):
            try:
                await ws.send_text(json.dumps(payload, ensure_ascii=False))
            except Exception as e:
                logger.debug(f"Failed to send snapshot to target: {e}")
    else:
        # Otherwise broadcast to all subscribers on this box
        await _broadcast_to_box(box_id, payload)

async def _ensure_state(box_id: int) -> dict:
    """
    Ensure the in-memory state exists for a box (JSON-only).

    This function only handles *initialization* (create-on-first-use) under `init_lock`.
    Callers must still use the per-box lock (`state_locks[box_id]`) for any mutations.
    """
    async with init_lock:
        existing = state_map.get(box_id)
        if existing is not None:
            return existing
        if box_id not in state_locks:
            state_locks[box_id] = asyncio.Lock()
        state = default_state()
        state_map[box_id] = state
        return state

async def _persist_state(box_id: int, state: dict, action: str, payload: dict) -> str:
    """Persist snapshot + audit event (JSON-only)."""
    # Box version is used to prevent stale UI actions. Do not bump for TIMER_SYNC:
    # - TIMER_SYNC can be high-frequency
    # - clients may omit boxVersion for TIMER_SYNC
    # - bumping here causes unrelated commands (e.g. SUBMIT_SCORE) to be rejected as stale
    if action not in {"INIT_ROUTE", "TIMER_SYNC"}:
        state["boxVersion"] = int(state.get("boxVersion", 0) or 0) + 1
    await save_box_state(box_id, state)
    event = build_audit_event(
        action=action,
        payload=payload,
        box_id=box_id,
        state=state,
        actor=current_actor.get(),
    )
    await append_audit_event(event)
    return "ok"

async def _persist_audit_only(action: str, payload: dict) -> None:
    """Persist an audit event that doesn't mutate box state (best-effort, JSON-only)."""
    box_id = payload.get("boxId") if isinstance(payload, dict) else None
    event = build_audit_event(
        action=action,
        payload=payload if isinstance(payload, dict) else {},
        box_id=box_id,
        state=state_map.get(box_id) if box_id is not None else None,
        actor=current_actor.get(),
    )
    await append_audit_event(event)

def _default_state(session_id: str | None = None) -> dict:
    """Backward-compatible alias for tests."""
    return default_state(session_id)

def _parse_timer_preset(preset: str | None) -> int | None:
    """Backward-compatible alias for tests."""
    return parse_timer_preset(preset)
