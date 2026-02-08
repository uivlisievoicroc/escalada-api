"""
Public API endpoints for spectators (read-only access).

Architecture:
- Token-based authentication: No credentials required, anyone on LAN can get spectator JWT
- 24h token TTL: Clients proactively refresh when <1h remaining (see PublicHub.tsx)
- Role enforcement: All endpoints require token with role="spectator" in JWT claims
- Read-only access: Spectators can view state but cannot send commands

Endpoints:
1. POST /api/public/token
   - Issue spectator JWT (no credentials required)
   - Returns: {access_token, token_type, expires_in}
   - Used by: PublicHub on mount + proactive refresh

2. GET /api/public/boxes?token=...
   - List initiated boxes (only boxes where initiated=True)
   - Returns: {boxes: [{boxId, label, initiated, timerState, currentClimber, categorie}]}
   - Used by: PublicHub dropdown (polled every 30s)

3. GET /api/public/officials?token=...
   - Get global competition officials (chief judge, event director, chief routesetter)
   - Returns: {judgeChief, competitionDirector, chiefRoutesetter}
   - Used by: CompetitionOfficials component

4. WS /api/public/ws/{box_id}?token=...
   - WebSocket for live state updates per box
   - Sends: STATE_SNAPSHOT (initial + on changes), PING (every 30s)
   - Accepts: PONG (heartbeat response), REQUEST_STATE (manual refresh)
   - Blocks: All command types (INIT_ROUTE, START_TIMER, etc.)
   - Used by: PublicLiveClimbing, PublicRankings components

Security:
- Token validation: decode_token() checks JWT signature + expiry + role="spectator"
- Command blocking: WebSocket ignores all commands, only accepts PONG/REQUEST_STATE
- Rate limiting: Inherited from main app (see escalada/rate_limit.py)
- No box assignment: Spectators can view all initiated boxes (role="spectator", assigned_boxes=[])

Broadcasting:
- State changes trigger broadcast to public_box_channels[box_id]
- Separate channel registry from private WS (see escalada/api/live.py:box_channels)
- Dead connection cleanup: WebSocket send failures automatically remove from registry
- Heartbeat: 30s PING, 90s timeout if no PONG received

Integration:
- State source: escalada.api.live.state_map (shared in-memory state)
- Snapshot builder: escalada.api.live._build_snapshot() (same format as private WS)
- Officials storage: escalada.api.live.get_competition_officials() (global metadata)

Token Lifecycle:
1. Client calls POST /token → receives JWT with 24h TTL
2. Client stores token + expiry in localStorage
3. Client uses token for all subsequent API calls (query param: ?token=...)
4. Backend validates token on every request (_decode_spectator_token)
5. If 401, client clears localStorage + fetches new token (retry once)
6. Client proactively refreshes when <1h remaining (avoids 401 during active use)
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

# FastAPI router: all endpoints prefixed with /api/public (see escalada/main.py)
router = APIRouter(prefix="/public", tags=["public"])

# Spectator token TTL: 24 hours (1440 minutes)
# Client proactively refreshes when <1h remaining to avoid 401 during active use
SPECTATOR_TOKEN_TTL_MINUTES = 24 * 60


# Response model for POST /api/public/token
class SpectatorTokenResponse(BaseModel):
    access_token: str  # JWT with role="spectator", 24h TTL
    token_type: str = "bearer"  # OAuth2 standard (used in Authorization header if needed)
    expires_in: int = SPECTATOR_TOKEN_TTL_MINUTES * 60  # TTL in seconds (86400 = 24h)


# Response model for individual box in GET /api/public/boxes
class PublicBoxInfo(BaseModel):
    boxId: int  # Numeric box identifier (e.g. 1, 2, 3)
    label: str  # Display name (e.g. "Seniori M", "Junioare F")
    initiated: bool  # Always True (only initiated boxes returned by endpoint)
    timerState: str | None = None  # "idle" | "running" | "paused" (None if not started)
    currentClimber: str | None = None  # Name of climber currently climbing (None if between climbers)
    categorie: str | None = None  # Category name (same as label, kept for backward compatibility)


# Response model for GET /api/public/boxes (array wrapper)
class PublicBoxesResponse(BaseModel):
    boxes: List[PublicBoxInfo]  # Sorted by boxId ascending


# Response model for GET /api/public/officials (global competition metadata)
class PublicCompetitionOfficialsResponse(BaseModel):
    judgeChief: str = ""  # Chief Judge name (empty if not set)
    competitionDirector: str = ""  # Event Director name (empty if not set)
    chiefRoutesetter: str = ""  # Chief Routesetter name (empty if not set)


def _decode_spectator_token(token: str) -> Dict[str, Any]:
    """Decode and validate spectator JWT token.
    
    Validation Steps:
    1. Verify JWT signature (decode_token checks SECRET_KEY)
    2. Check expiry timestamp (decode_token raises if expired)
    3. Verify role="spectator" in claims (reject admin/judge tokens)
    
    Error Cases:
    - Token expired: decode_token() raises → 401 invalid_token
    - Token signature invalid: decode_token() raises → 401 invalid_token
    - Role mismatch (admin/judge token): 403 spectator_token_required
    - Token missing: caller should check before calling this (401 token_required)
    
    Args:
        token: JWT string from query param or WS query_params
    
    Returns:
        Dict with JWT claims: {sub: username, role: "spectator", assigned_boxes: [], exp: timestamp}
    
    Raises:
        HTTPException: 401 if token invalid/expired, 403 if role mismatch
    """
    try:
        # decode_token() from escalada.auth.service verifies signature + expiry
        claims = decode_token(token)
        
        # Spectator tokens must have role="spectator" (reject admin/judge tokens)
        if claims.get("role") != "spectator":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="spectator_token_required",  # Client should get spectator token via POST /token
            )
        return claims
    except HTTPException:
        # Re-raise HTTPException as-is (403 from role check above)
        raise
    except Exception:
        # Any other error (expired, invalid signature, decode failure) → 401
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid_token",
        )


@router.post("/token", response_model=SpectatorTokenResponse)
async def get_spectator_token() -> SpectatorTokenResponse:
    """Issue a spectator JWT token with 24h TTL.
    
    Authentication: NONE (public endpoint, no credentials required)
    
    Design Rationale:
    - Anyone on LAN can become a spectator (no password barrier for public viewing)
    - Token still required (prevents unauthorized access from outside LAN if exposed)
    - 24h TTL balances convenience (no frequent re-auth) vs security (token eventually expires)
    
    Token Claims:
    - username: "spectator" (generic, not tied to individual)
    - role: "spectator" (grants read-only access to public endpoints)
    - assigned_boxes: [] (can view all initiated boxes, not restricted to specific ones)
    - exp: now + 24h (JWT expiry timestamp)
    
    Client Usage:
    1. Call POST /api/public/token on app mount (PublicHub.tsx)
    2. Store token + expiry in localStorage
    3. Use token for all subsequent API calls (query param: ?token=...)
    4. Proactively refresh when <1h remaining (avoids 401 during active use)
    
    Security Considerations:
    - Rate limiting: Inherited from main app (see escalada/rate_limit.py)
    - No CORS restrictions: API accessible only on LAN (not exposed to internet)
    - Token rotation: Client can fetch new token anytime (old tokens remain valid until expiry)
    
    Returns:
        SpectatorTokenResponse: {access_token: JWT, token_type: "bearer", expires_in: 86400}
    """
    # create_access_token() from escalada.auth.service signs JWT with SECRET_KEY
    token = create_access_token(
        username="spectator",  # Generic username (not tied to individual spectator)
        role="spectator",  # Role for authorization checks in _decode_spectator_token()
        assigned_boxes=[],  # No box restrictions (can view all initiated boxes)
        expires_minutes=SPECTATOR_TOKEN_TTL_MINUTES,  # 24h TTL
    )
    return SpectatorTokenResponse(
        access_token=token,  # JWT string
        expires_in=SPECTATOR_TOKEN_TTL_MINUTES * 60,  # TTL in seconds (for client expiry tracking)
    )


@router.get("/boxes", response_model=PublicBoxesResponse)
async def get_public_boxes(token: str | None = None) -> PublicBoxesResponse:
    """Get list of initiated boxes for spectator dropdown.
    
    Purpose:
    - Populate PublicHub Live Climbing dropdown with active categories
    - Show current climber + timer status for each box (helps spectators choose which to watch)
    
    Filtering:
    - Only returns boxes where initiated=True (hides uninitiated boxes from spectators)
    - All initiated boxes visible (no per-box access control for spectators)
    
    Polling:
    - Client calls this every 30 seconds (see PublicHub.tsx useEffect)
    - New initiated boxes appear automatically in dropdown
    - Timer state + current climber updated in real-time via polling
    
    Authentication:
    - Requires spectator token in query param: GET /api/public/boxes?token=...
    - Token validated via _decode_spectator_token() (checks role="spectator")
    - 401 if token missing/invalid/expired
    
    Response Shape:
    - {boxes: [{boxId, label, initiated, timerState, currentClimber, categorie}]}
    - Sorted by boxId ascending (consistent ordering across requests)
    
    Performance:
    - Acquires init_lock (shared with command processing in live.py)
    - Snapshot of state_map taken inside lock (prevents race conditions)
    - Lock held briefly (list comprehension + sort outside lock)
    
    Args:
        token: Spectator JWT from query param (required)
    
    Returns:
        PublicBoxesResponse: Array of initiated boxes sorted by boxId
    
    Raises:
        HTTPException: 401 if token missing/invalid, 403 if role mismatch
    """
    # Token validation: 401 if missing, 403 if role mismatch
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="token_required",
        )
    _decode_spectator_token(token)  # Validates JWT signature + expiry + role="spectator"

    # Import here to avoid circular import (escalada.api.live imports escalada.api.public for broadcasting)
    from escalada.api.live import init_lock, state_map

    # Snapshot state_map inside lock (prevents race with command processing)
    async with init_lock:
        items = list(state_map.items())  # [(box_id, state_dict), ...]

    # Filter for initiated boxes only (spectators shouldn't see uninitiated boxes)
    boxes: List[PublicBoxInfo] = []
    for box_id, state in items:
        if state.get("initiated", False):  # Only include boxes where INIT_ROUTE was called
            boxes.append(
                PublicBoxInfo(
                    boxId=box_id,
                    label=state.get("categorie") or f"Box {box_id}",  # Fallback to "Box N" if categorie missing
                    initiated=True,  # Always True in this branch (but included for schema consistency)
                    timerState=state.get("timerState"),  # "idle" | "running" | "paused" | None
                    currentClimber=state.get("currentClimber"),  # Current climber name or None
                    categorie=state.get("categorie"),  # Category name (duplicate of label for backward compat)
                )
            )

    # Sort by boxId for consistent ordering (dropdown shows same order every time)
    boxes.sort(key=lambda b: b.boxId)
    return PublicBoxesResponse(boxes=boxes)


@router.get("/officials", response_model=PublicCompetitionOfficialsResponse)
async def get_public_officials(token: str | None = None) -> PublicCompetitionOfficialsResponse:
    """Get global competition officials (chief judge, event director, chief routesetter).
    
    Purpose:
    - Display competition officials on CompetitionOfficials page (via PublicHub button)
    - Global metadata (not per-box), set by admin via ControlPanel
    
    Data Source:
    - escalada.api.live.get_competition_officials() returns global dict
    - Data stored in-memory (no persistence to JSON currently)
    - Empty strings returned if not set by admin
    
    Authentication:
    - Requires spectator token (same as other public endpoints)
    - Token validated via _decode_spectator_token()
    
    Response Shape:
    - {judgeChief: str, competitionDirector: str, chiefRoutesetter: str}
    - All fields default to empty string if not set
    
    Args:
        token: Spectator JWT from query param (required)
    
    Returns:
        PublicCompetitionOfficialsResponse: Officials names (empty strings if not set)
    
    Raises:
        HTTPException: 401 if token missing/invalid, 403 if role mismatch
    """
    # Token validation
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="token_required",
        )
    _decode_spectator_token(token)  # Validates role="spectator"
    
    # Import live module to access global officials data
    from escalada.api import live as live_module

    # Fetch officials dict from live module (in-memory storage)
    data = live_module.get_competition_officials()
    return PublicCompetitionOfficialsResponse(
        judgeChief=data.get("judgeChief") or "",  # Empty string if not set
        competitionDirector=data.get("competitionDirector") or "",
        chiefRoutesetter=data.get("chiefRoutesetter") or "",
    )


# Public WebSocket channel registry: {box_id: {WebSocket, WebSocket, ...}}
# Separate from private channels (escalada.api.live.box_channels) to avoid interference
# Structure: Dict[box_id, set of WebSocket connections] for O(1) lookup and add/remove
public_box_channels: Dict[int, set[WebSocket]] = {}

# Lock for thread-safe access to public_box_channels (async context)
public_box_channels_lock = asyncio.Lock()


async def broadcast_to_public_box(box_id: int, payload: dict) -> None:
    """Broadcast state update to all public spectators watching a specific box.
    
    Called From:
    - escalada.api.live.process_command() after state change (see live.py)
    - Commands that change state: INIT_ROUTE, START_TIMER, STOP_TIMER, SUBMIT_SCORE, etc.
    
    Flow:
    1. Acquire lock → snapshot WebSocket set (prevents race with connect/disconnect)
    2. Release lock → iterate over sockets outside lock (reduces lock contention)
    3. Send payload to each WebSocket (JSON.dumps with ensure_ascii=False for Romanian chars)
    4. Collect dead connections (send_text raised exception)
    5. Acquire lock again → remove dead connections from registry
    
    Payload Shape:
    - Same as private WS: {type: "STATE_SNAPSHOT", boxId, sessionId, state: {...}}
    - Built by escalada.api.live._build_snapshot() (shared with private WS)
    
    Error Handling:
    - send_text() exceptions: Log at debug level, add to dead list
    - Dead connections: Automatically removed from registry (no explicit close needed)
    
    Performance:
    - Lock held twice: once for snapshot, once for cleanup (minimal duration)
    - Broadcast outside lock: parallel sends (asyncio may interleave)
    - Dead cleanup deferred: doesn't block live connections
    
    Args:
        box_id: Box identifier to broadcast to
        payload: State snapshot dict (will be JSON-serialized)
    """
    # Snapshot WebSocket set inside lock (prevents concurrent modification)
    async with public_box_channels_lock:
        sockets = list(public_box_channels.get(box_id, set()))  # Convert set to list for iteration

    # Broadcast to all sockets (outside lock to reduce contention)
    dead = []  # Track failed sends for cleanup
    for ws in sockets:
        try:
            # Send JSON payload to WebSocket (ensure_ascii=False preserves Romanian chars)
            await ws.send_text(json.dumps(payload, ensure_ascii=False))
        except Exception as e:
            # Connection closed or network error (log at debug level, not error)
            logger.debug(f"Public box broadcast error: {e}")
            dead.append(ws)  # Mark for removal from registry

    # Remove dead connections from registry (if any failures)
    if dead:
        async with public_box_channels_lock:
            channel = public_box_channels.get(box_id, set())
            for ws in dead:
                channel.discard(ws)  # Safe even if already removed


async def _heartbeat(ws: WebSocket, box_id: int, last_pong: dict) -> None:
    """WebSocket heartbeat loop: Send PING every 30s, close if no PONG within 90s.
    
    Purpose:
    - Detect dead connections early (client closed tab, network failure, etc.)
    - Free resources by closing stale WebSockets (prevents memory leaks)
    - Standard WebSocket keepalive pattern (PING/PONG handshake)
    
    Flow:
    1. Sleep 30s (interval between PINGs)
    2. Send {type: "PING"} to client
    3. Check last_pong timestamp (updated by main loop when client sends PONG)
    4. If >90s since last PONG, consider connection dead → break loop
    5. Main loop catches break → closes WebSocket and removes from registry
    
    Timeout Logic:
    - 30s PING interval: Balance between responsiveness and network overhead
    - 90s PONG timeout: Allows up to 3 missed PINGs (network hiccups, slow clients)
    - If client doesn't respond within 90s, assume connection dead
    
    Client Handling:
    - Client must reply with {type: "PONG"} when receiving PING
    - See PublicLiveClimbing.tsx, PublicRankings.tsx for PONG logic
    - If client doesn't implement PONG, connection closed after 90s
    
    Cancellation:
    - Task cancelled by main loop on disconnect (finally block)
    - CancelledError caught and suppressed (normal shutdown, not an error)
    
    Args:
        ws: WebSocket connection to monitor
        box_id: Box identifier (for logging only)
        last_pong: Mutable dict with "ts" key (shared with main loop)
    
    Raises:
        asyncio.CancelledError: When task cancelled by main loop (caught and suppressed)
    """
    try:
        while True:
            # Wait 30 seconds between PINGs
            await asyncio.sleep(30)
            
            try:
                # Send PING to client (expects PONG response)
                await ws.send_text(json.dumps({"type": "PING"}))
            except Exception:
                # Send failed (connection closed) → exit loop
                break
            
            # Check if client responded to recent PINGs
            now = asyncio.get_event_loop().time()  # Current timestamp (monotonic)
            if now - last_pong["ts"] > 90:  # No PONG in 90s (3 missed PINGs)
                logger.warning(f"Public WS box {box_id}: no PONG in 90s, closing")
                break  # Exit loop → main loop closes WebSocket
                
    except asyncio.CancelledError:
        # Task cancelled by main loop (normal shutdown on disconnect)
        pass  # Suppress error, let finally block in main loop handle cleanup


@router.websocket("/ws/{box_id}")
async def public_box_websocket(ws: WebSocket, box_id: int):
    """Public WebSocket endpoint for spectators watching a specific box.
    
    Purpose:
    - Real-time state updates for PublicLiveClimbing and PublicRankings components
    - Read-only access: Spectators receive STATE_SNAPSHOT but cannot send commands
    - Per-box subscription: Each WebSocket watches one box (client opens multiple WS for multiple boxes)
    
    Authentication:
    - Requires spectator token in query param: WS /api/public/ws/{box_id}?token=...
    - Token validated before accept() (rejects invalid/expired/non-spectator tokens)
    - Connection closed with code 4401 if auth fails (custom code for token errors)
    
    Message Types Sent (Server → Client):
    1. STATE_SNAPSHOT: Full box state (sent on connect + after every state change)
       - Shape: {type: "STATE_SNAPSHOT", boxId, sessionId, state: {...}}
       - Same format as private WS (escalada.api.live.box_channels)
    2. PING: Heartbeat every 30s (client must reply with PONG)
       - Shape: {type: "PING"}
       - Used to detect dead connections (90s timeout if no PONG)
    
    Message Types Accepted (Client → Server):
    1. PONG: Heartbeat response (updates last_pong timestamp)
       - Shape: {type: "PONG"}
       - Required to keep connection alive (must respond within 90s)
    2. REQUEST_STATE: Manual refresh (sends STATE_SNAPSHOT immediately)
       - Shape: {type: "REQUEST_STATE"}
       - Used when client detects stale state or network glitch
    
    Message Types Blocked:
    - All commands: INIT_ROUTE, START_TIMER, STOP_TIMER, SUBMIT_SCORE, etc.
    - Logged at debug level and ignored (no error response)
    - Spectators cannot modify state (read-only access)
    
    Connection Lifecycle:
    1. Client connects with token in query param
    2. Server validates token (close with 4401 if invalid)
    3. Server accepts connection → adds to public_box_channels[box_id]
    4. Server sends initial STATE_SNAPSHOT
    5. Server starts heartbeat task (PING every 30s)
    6. Client receives state updates whenever box state changes
    7. Client sends PONG to keep connection alive
    8. On disconnect/timeout: Remove from registry, cancel heartbeat task, close WS
    
    Broadcasting:
    - State changes trigger broadcast_to_public_box() (called from escalada.api.live)
    - All spectators watching same box receive same STATE_SNAPSHOT simultaneously
    - Dead connections removed automatically on broadcast failure
    
    Error Handling:
    - Token validation errors: Close with 4401 + reason (token_required, invalid_token)
    - Receive timeout (180s): Close connection (client stopped sending PONG)
    - Heartbeat timeout (90s no PONG): Close connection (dead client detection)
    - JSON decode errors: Log + ignore message (continue receiving)
    
    Performance:
    - One WebSocket per spectator per box (can be 100+ connections for popular boxes)
    - Heartbeat overhead: 1 PING per connection per 30s (~3.3% bandwidth for PING/PONG)
    - Broadcast: O(n) where n = spectators watching box (no fan-out optimization)
    
    Args:
        ws: WebSocket connection from client
        box_id: Box identifier from URL path (e.g. /api/public/ws/1)
    
    Query Params:
        token: Spectator JWT (required, validated before accept)
    
    Closes With:
        4401: Token missing, invalid, expired, or role mismatch
        Normal: Client disconnected, heartbeat timeout, or receive timeout
    """
    # Extract peer IP for logging (None if unavailable)
    peer = ws.client.host if ws.client else None
    
    # Extract token from query params (e.g. ?token=eyJhbGc...)
    token = ws.query_params.get("token")

    # Reject connection if token missing (close before accept to save resources)
    if not token:
        logger.warning("Public WS denied: token_required box=%s ip=%s", box_id, peer)
        await ws.close(code=4401, reason="token_required")  # Custom code for auth errors
        return

    # Validate token (signature, expiry, role="spectator")
    try:
        claims = _decode_spectator_token(token)  # Raises HTTPException if invalid
    except HTTPException as exc:
        # Token invalid, expired, or role mismatch → reject connection
        logger.warning(
            "Public WS denied: invalid_token box=%s ip=%s detail=%s",
            box_id,
            peer,
            exc.detail,
        )
        await ws.close(code=4401, reason=exc.detail or "invalid_token")
        return

    # Token valid → accept WebSocket connection
    await ws.accept()

    # Add WebSocket to channel registry (for broadcast_to_public_box)
    async with public_box_channels_lock:
        # setdefault: create set if box_id not in dict, then add WebSocket
        public_box_channels.setdefault(box_id, set()).add(ws)
        subscriber_count = len(public_box_channels[box_id])  # Count spectators watching this box

    logger.info(f"Public spectator connected to box {box_id}, total: {subscriber_count}")

    # Send initial state snapshot (client sees current state immediately on connect)
    # targets={ws}: Send only to this WebSocket (not broadcast to all spectators)
    await _send_public_box_snapshot(box_id, targets={ws})

    # Start heartbeat task (PING every 30s, close if no PONG within 90s)
    # last_pong: Mutable dict shared between heartbeat task and main loop
    # Initialize with current timestamp (connection just established)
    last_pong = {"ts": asyncio.get_event_loop().time()}
    heartbeat_task = asyncio.create_task(_heartbeat(ws, box_id, last_pong))

    # Main receive loop: Process messages from client
    try:
        while True:
            try:
                # Wait for message from client (180s timeout = 3x heartbeat interval)
                # Timeout prevents hung connections from accumulating
                data = await asyncio.wait_for(ws.receive_text(), timeout=180)
            except asyncio.TimeoutError:
                # No message in 180s → assume connection dead
                logger.warning(f"Public WS timeout for box {box_id}")
                break  # Exit loop → cleanup in finally block
            except Exception as e:
                # Receive error (connection closed, network error)
                logger.warning(f"Public WS receive error for box {box_id}: {e}")
                break

            # Parse JSON message from client
            try:
                msg = json.loads(data) if isinstance(data, str) else data
                if isinstance(msg, dict):
                    msg_type = msg.get("type")

                    # Handle PONG: Update last_pong timestamp (keeps connection alive)
                    if msg_type == "PONG":
                        last_pong["ts"] = asyncio.get_event_loop().time()  # Reset heartbeat timer
                        continue  # No response needed

                    # Handle REQUEST_STATE: Send fresh state snapshot
                    # Used when client detects stale state or network glitch
                    if msg_type == "REQUEST_STATE":
                        await _send_public_box_snapshot(box_id, targets={ws})  # Send only to this client
                        continue

                    # Block all other message types (commands, unknown types)
                    # Commands like INIT_ROUTE, START_TIMER, etc. are silently ignored
                    # Logged at debug level (not error, expected behavior for read-only access)
                    logger.debug(
                        f"Public WS box {box_id}: ignored message type {msg_type}"
                    )

            except json.JSONDecodeError:
                # Invalid JSON from client (malformed message)
                # Log and continue receiving (don't close connection for parse errors)
                logger.debug(f"Invalid JSON from public WS box {box_id}")
                continue

    except Exception as e:
        # Unexpected error in main loop (not timeout, not receive error)
        logger.error(f"Public WS error for box {box_id}: {e}")
    finally:
        # Cleanup: Always executed (normal disconnect, timeout, error)
        
        # 1. Cancel heartbeat task (stop sending PINGs)
        heartbeat_task.cancel()
        try:
            await heartbeat_task  # Wait for task to acknowledge cancellation
        except asyncio.CancelledError:
            pass  # Expected when cancelling, suppress error

        # 2. Remove WebSocket from channel registry (no more broadcasts to this client)
        async with public_box_channels_lock:
            public_box_channels.get(box_id, set()).discard(ws)  # Safe even if already removed
            remaining = len(public_box_channels.get(box_id, set()))  # Count remaining spectators

        logger.info(f"Public spectator disconnected from box {box_id}, remaining: {remaining}")

        # 3. Close WebSocket connection (if not already closed)
        try:
            await ws.close()  # Graceful close (may fail if already closed)
        except Exception:
            pass  # Suppress errors (connection may be already closed by client)


async def _send_public_box_snapshot(
    box_id: int, targets: set[WebSocket] | None = None
) -> None:
    """Send state snapshot for a specific box to public spectators.
    
    Purpose:
    - Initial snapshot on WebSocket connect (targets={ws})
    - Manual refresh when client sends REQUEST_STATE (targets={ws})
    - Broadcast to all spectators after state change (targets=None → broadcast_to_public_box)
    
    Snapshot Format:
    - Same as private WS: {type: "STATE_SNAPSHOT", boxId, sessionId, state: {...}}
    - Built by escalada.api.live._build_snapshot() (shared function)
    - Contains full box state: competitors, routes, timer, scores, etc.
    
    Target Modes:
    - targets={ws}: Send only to specific WebSocket (single recipient)
      - Used on connect: new spectator gets current state immediately
      - Used on REQUEST_STATE: single client refreshes state
    - targets=None: Broadcast to all spectators watching box (via broadcast_to_public_box)
      - Used after state changes: all spectators get update simultaneously
    
    Error Handling:
    - Box not found: Return early (no error, box may be deleted)
    - Send failures: Log at debug level (connection may be closing)
    - Dead connections: Not removed here (removed by broadcast_to_public_box)
    
    Thread Safety:
    - Acquires init_lock to snapshot state (prevents race with command processing)
    - Lock released before sending (reduces contention)
    
    Args:
        box_id: Box identifier to send snapshot for
        targets: Set of specific WebSockets (or None to broadcast to all)
    """
    # Import here to avoid circular import (live.py imports public.py for broadcasting)
    from escalada.api.live import _build_snapshot, init_lock, state_map

    # Snapshot state inside lock (prevents race with command processing in live.py)
    async with init_lock:
        state = state_map.get(box_id)  # None if box not found

    # Box not found or deleted → return early
    if not state:
        return

    # Build snapshot payload (same format as private WS for consistency)
    # _build_snapshot() from live.py: {type: "STATE_SNAPSHOT", boxId, sessionId, state: {...}}
    payload = _build_snapshot(box_id, state)

    # Send to specific targets or broadcast to all spectators
    if targets:
        # Single recipient mode: Send only to specified WebSockets
        for ws in list(targets):  # Convert to list for safe iteration
            try:
                await ws.send_text(json.dumps(payload, ensure_ascii=False))  # ensure_ascii=False for Romanian chars
            except Exception as e:
                # Send failed (connection closing, network error)
                logger.debug(f"Failed to send public snapshot: {e}")  # Debug level (not error, may be normal disconnect)
                # Note: Dead connection not removed here (handled by broadcast_to_public_box)
    else:
        # Broadcast mode: Send to all spectators watching this box
        await broadcast_to_public_box(box_id, payload)  # Handles dead connection cleanup
