"""
Public API endpoints for spectators (read-only).
Token-based access with 24h TTL.
"""

import asyncio
import json
import logging
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from starlette.websockets import WebSocket

from escalada.auth.service import create_access_token, decode_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/public", tags=["public"])

# TTL for spectator tokens: 24 hours
SPECTATOR_TOKEN_TTL_MINUTES = 24 * 60


class SpectatorTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int = SPECTATOR_TOKEN_TTL_MINUTES * 60  # seconds


class PublicBoxInfo(BaseModel):
    boxId: int
    label: str
    initiated: bool
    timerState: str | None = None
    currentClimber: str | None = None
    categorie: str | None = None


class PublicBoxesResponse(BaseModel):
    boxes: List[PublicBoxInfo]


class PublicCompetitionOfficialsResponse(BaseModel):
    judgeChief: str = ""
    competitionDirector: str = ""
    chiefRoutesetter: str = ""


def _decode_spectator_token(token: str) -> Dict[str, Any]:
    """Decode and validate spectator token."""
    try:
        claims = decode_token(token)
        if claims.get("role") != "spectator":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="spectator_token_required",
            )
        return claims
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid_token",
        )


@router.post("/token", response_model=SpectatorTokenResponse)
async def get_spectator_token() -> SpectatorTokenResponse:
    """
    Issue a spectator token with 24h TTL.
    No credentials required - anyone on the LAN can get a spectator token.
    """
    token = create_access_token(
        username="spectator",
        role="spectator",
        assigned_boxes=[],
        expires_minutes=SPECTATOR_TOKEN_TTL_MINUTES,
    )
    return SpectatorTokenResponse(
        access_token=token,
        expires_in=SPECTATOR_TOKEN_TTL_MINUTES * 60,
    )


@router.get("/boxes", response_model=PublicBoxesResponse)
async def get_public_boxes(token: str | None = None) -> PublicBoxesResponse:
    """
    Get list of initiated boxes for the dropdown.
    Requires spectator token in query param.
    """
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="token_required",
        )
    _decode_spectator_token(token)

    # Import here to avoid circular imports
    from escalada.api.live import init_lock, state_map

    async with init_lock:
        items = list(state_map.items())

    boxes: List[PublicBoxInfo] = []
    for box_id, state in items:
        if state.get("initiated", False):
            boxes.append(
                PublicBoxInfo(
                    boxId=box_id,
                    label=state.get("categorie") or f"Box {box_id}",
                    initiated=True,
                    timerState=state.get("timerState"),
                    currentClimber=state.get("currentClimber"),
                    categorie=state.get("categorie"),
                )
            )

    # Sort by boxId for consistent ordering
    boxes.sort(key=lambda b: b.boxId)
    return PublicBoxesResponse(boxes=boxes)


@router.get("/officials", response_model=PublicCompetitionOfficialsResponse)
async def get_public_officials(token: str | None = None) -> PublicCompetitionOfficialsResponse:
    """Get global competition officials (spectator token required)."""
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="token_required",
        )
    _decode_spectator_token(token)
    from escalada.api import live as live_module

    data = live_module.get_competition_officials()
    return PublicCompetitionOfficialsResponse(
        judgeChief=data.get("judgeChief") or "",
        competitionDirector=data.get("competitionDirector") or "",
        chiefRoutesetter=data.get("chiefRoutesetter") or "",
    )


# Channel registry for authenticated public WS connections per box
public_box_channels: Dict[int, set[WebSocket]] = {}
public_box_channels_lock = asyncio.Lock()


async def broadcast_to_public_box(box_id: int, payload: dict) -> None:
    """Broadcast state update to all public spectators watching a specific box."""
    async with public_box_channels_lock:
        sockets = list(public_box_channels.get(box_id, set()))

    dead = []
    for ws in sockets:
        try:
            await ws.send_text(json.dumps(payload, ensure_ascii=False))
        except Exception as e:
            logger.debug(f"Public box broadcast error: {e}")
            dead.append(ws)

    if dead:
        async with public_box_channels_lock:
            channel = public_box_channels.get(box_id, set())
            for ws in dead:
                channel.discard(ws)


async def _heartbeat(ws: WebSocket, box_id: int, last_pong: dict) -> None:
    """Send PING every 30s and check for PONG response."""
    try:
        while True:
            await asyncio.sleep(30)
            try:
                await ws.send_text(json.dumps({"type": "PING"}))
            except Exception:
                break
            # Check if client responded to last PING
            now = asyncio.get_event_loop().time()
            if now - last_pong["ts"] > 90:
                logger.warning(f"Public WS box {box_id}: no PONG in 90s, closing")
                break
    except asyncio.CancelledError:
        pass


@router.websocket("/ws/{box_id}")
async def public_box_websocket(ws: WebSocket, box_id: int):
    """
    Public WebSocket for spectators watching a specific box.
    Requires spectator token in query param.
    Read-only: only PONG and REQUEST_STATE are accepted.
    """
    peer = ws.client.host if ws.client else None
    token = ws.query_params.get("token")

    if not token:
        logger.warning("Public WS denied: token_required box=%s ip=%s", box_id, peer)
        await ws.close(code=4401, reason="token_required")
        return

    try:
        claims = _decode_spectator_token(token)
    except HTTPException as exc:
        logger.warning(
            "Public WS denied: invalid_token box=%s ip=%s detail=%s",
            box_id,
            peer,
            exc.detail,
        )
        await ws.close(code=4401, reason=exc.detail or "invalid_token")
        return

    await ws.accept()

    # Add to channel
    async with public_box_channels_lock:
        public_box_channels.setdefault(box_id, set()).add(ws)
        subscriber_count = len(public_box_channels[box_id])

    logger.info(f"Public spectator connected to box {box_id}, total: {subscriber_count}")

    # Send initial state snapshot
    await _send_public_box_snapshot(box_id, targets={ws})

    # Start heartbeat
    last_pong = {"ts": asyncio.get_event_loop().time()}
    heartbeat_task = asyncio.create_task(_heartbeat(ws, box_id, last_pong))

    try:
        while True:
            try:
                data = await asyncio.wait_for(ws.receive_text(), timeout=180)
            except asyncio.TimeoutError:
                logger.warning(f"Public WS timeout for box {box_id}")
                break
            except Exception as e:
                logger.warning(f"Public WS receive error for box {box_id}: {e}")
                break

            try:
                msg = json.loads(data) if isinstance(data, str) else data
                if isinstance(msg, dict):
                    msg_type = msg.get("type")

                    # Only accept PONG and REQUEST_STATE - no commands
                    if msg_type == "PONG":
                        last_pong["ts"] = asyncio.get_event_loop().time()
                        continue

                    if msg_type == "REQUEST_STATE":
                        await _send_public_box_snapshot(box_id, targets={ws})
                        continue

                    # Log and ignore any other message types (commands blocked)
                    logger.debug(
                        f"Public WS box {box_id}: ignored message type {msg_type}"
                    )

            except json.JSONDecodeError:
                logger.debug(f"Invalid JSON from public WS box {box_id}")
                continue

    except Exception as e:
        logger.error(f"Public WS error for box {box_id}: {e}")
    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass

        async with public_box_channels_lock:
            public_box_channels.get(box_id, set()).discard(ws)
            remaining = len(public_box_channels.get(box_id, set()))

        logger.info(f"Public spectator disconnected from box {box_id}, remaining: {remaining}")

        try:
            await ws.close()
        except Exception:
            pass


async def _send_public_box_snapshot(
    box_id: int, targets: set[WebSocket] | None = None
) -> None:
    """Send state snapshot for a specific box to public spectators."""
    # Import here to avoid circular imports
    from escalada.api.live import _build_snapshot, init_lock, state_map

    async with init_lock:
        state = state_map.get(box_id)

    if not state:
        return

    # Build snapshot (same format as private WS for consistency)
    payload = _build_snapshot(box_id, state)

    if targets:
        for ws in list(targets):
            try:
                await ws.send_text(json.dumps(payload, ensure_ascii=False))
            except Exception as e:
                logger.debug(f"Failed to send public snapshot: {e}")
    else:
        await broadcast_to_public_box(box_id, payload)
