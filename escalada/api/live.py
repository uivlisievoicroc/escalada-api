# escalada/api/live.py
import asyncio
import json
import logging
import os
import uuid
# state per boxId
from contextvars import ContextVar
from typing import Any
from typing import Dict

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from starlette.websockets import WebSocket

from escalada.rate_limit import check_rate_limit
# Import validation and rate limiting
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
    save_box_state,
)

logger = logging.getLogger(__name__)

state_map: Dict[int, dict] = {}
state_locks: Dict[int, asyncio.Lock] = {}  # Lock per boxId
init_lock = asyncio.Lock()  # Protects state_map and state_locks initialization
current_actor: ContextVar[dict[str, Any] | None] = ContextVar("current_actor", default=None)

router = APIRouter()
channels: dict[int, set[WebSocket]] = {}
channels_lock = asyncio.Lock()  # Protects concurrent access to channels dict
public_channels: set[WebSocket] = set()
public_channels_lock = asyncio.Lock()

# Test mode - disable validation for backward compatibility
VALIDATION_ENABLED = True

async def preload_states_from_json() -> int:
    ensure_storage_dirs()
    if os.getenv("RESET_BOXES_ON_START", "").strip().lower() in {"1", "true", "yes", "y", "on"}:
        removed = clear_box_state_files()
        logger.warning("RESET_BOXES_ON_START enabled: deleted %s box state files", removed)
    states = load_box_states()
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
class Cmd(BaseModel):
    """Legacy Cmd model - use ValidatedCmd for new validation"""

    boxId: int
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

        # ==================== ATOMIC STATE INITIALIZATION ====================
        # CRITICAL FIX: Keep lock acquired across entire initialization to prevent race conditions
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

            outcome = apply_command(sm, cmd.model_dump())
            sm = outcome.state
            cmd_payload = outcome.cmd_payload

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
    """
    # Get snapshot of current subscribers
    async with channels_lock:
        sockets = list(channels.get(box_id) or set())

    dead = []
    for ws in sockets:
        try:
            await ws.send_text(json.dumps(payload, ensure_ascii=False))
        except Exception as e:
            logger.debug(f"Broadcast error to box {box_id}: {e}")
            dead.append(ws)

    # Clean up dead connections
    if dead:
        async with channels_lock:
            for ws in dead:
                channels.get(box_id, set()).discard(ws)

def _public_preparing_climber(state: dict) -> str:
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
    routes_count = state.get("routesCount")
    if routes_count is None:
        routes_count = state.get("routeIndex") or 1
    holds_counts = state.get("holdsCounts") or []
    if not isinstance(holds_counts, list):
        holds_counts = []
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
        "remaining": state.get("remaining"),
        "timeCriterionEnabled": state.get("timeCriterionEnabled", False),
        "scoresByName": state.get("scores") or {},
        "timesByName": state.get("times") or {},
    }

async def _broadcast_public(payload: dict) -> None:
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
    async with init_lock:
        items = list(state_map.items())
    return {
        "type": "PUBLIC_STATE_SNAPSHOT",
        "boxes": [_build_public_box_state(box_id, state) for box_id, state in items],
    }

async def _send_public_snapshot(targets: set[WebSocket] | None = None) -> None:
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
    state = state_map.get(box_id)
    if not state:
        return
    payload = {
        "type": update_type,
        "box": _build_public_box_state(box_id, state),
    }
    await _broadcast_public(payload)

def _public_update_type(cmd_type: str) -> str | None:
    return {
        "INIT_ROUTE": "BOX_STATUS_UPDATE",
        "RESET_BOX": "BOX_STATUS_UPDATE",
        "START_TIMER": "BOX_FLOW_UPDATE",
        "STOP_TIMER": "BOX_FLOW_UPDATE",
        "RESUME_TIMER": "BOX_FLOW_UPDATE",
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
    peer = ws.client.host if ws.client else None
    token = ws.query_params.get("token")
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

    # Atomically add to channel
    async with channels_lock:
        channels.setdefault(box_id, set()).add(ws)
        subscriber_count = len(channels[box_id])

    logger.info(f"Client connected to box {box_id}, total: {subscriber_count}")
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

            # Handle PONG response
            try:
                msg = json.loads(data) if isinstance(data, str) else data
                if isinstance(msg, dict):
                    msg_type = msg.get("type")

                    # Acknowledge PONG
                    if msg_type == "PONG":
                        last_pong["ts"] = asyncio.get_event_loop().time()
                        continue

                    # NEW: Handle REQUEST_STATE command
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
                        await ws.send_text(
                            json.dumps(
                                {"type": "PONG", "timestamp": msg.get("timestamp")},
                                ensure_ascii=False,
                            )
                        )
                        continue
                    if msg_type == "REQUEST_STATE":
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
    # ==================== ATOMIC STATE INITIALIZATION ====================
    # Use global init_lock to prevent race conditions
    async with init_lock:
        if box_id not in state_locks:
            state_locks[box_id] = asyncio.Lock()
    await _ensure_state(box_id)

    state = state_map[box_id]
    return _build_snapshot(box_id, state)

# helpers
def _build_snapshot(box_id: int, state: dict) -> dict:
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
        "remaining": state.get("remaining"),
        "timeCriterionEnabled": state.get("timeCriterionEnabled", False),
        "timerPreset": state.get("timerPreset"),
        "timerPresetSec": state.get("timerPresetSec"),
        "sessionId": state.get("sessionId"),  # Include session ID for client validation
        "boxVersion": state.get("boxVersion", 0),
    }

async def _send_state_snapshot(box_id: int, targets: set[WebSocket] | None = None):
    # Ensure state exists before sending snapshot
    async with init_lock:
        if box_id not in state_locks:
            state_locks[box_id] = asyncio.Lock()
    await _ensure_state(box_id)

    state = state_map.get(box_id)
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
    """Ensure in-memory state exists (JSON-only)."""
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
    if action != "INIT_ROUTE":
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
