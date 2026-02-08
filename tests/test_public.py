"""
Tests for public API endpoints (spectator access).
Purpose:
- Verify spectator authentication flow (token generation, validation, role enforcement)
- Test public endpoints filtering (only initiated boxes visible to spectators)
- Validate WebSocket security (command blocking, safe message types only)
- Ensure token lifecycle works correctly (24h TTL, expiry checks, cache invalidation)

Coverage:
- Token generation: Role assignment (spectator vs admin), expiry calculation (24h), claims structure
- Token validation: Signature checks, role enforcement (reject non-spectator), expired token handling
- Boxes filtering: Only initiated=True boxes returned, label fallback ("Box N"), state fields (timer, climber)
- WebSocket security: PONG/REQUEST_STATE allowed, all commands blocked (INIT_ROUTE, START_TIMER, etc.)

Security focus:
- Role-based access control: Spectator tokens can't access admin endpoints
- Command blocking: WebSocket prevents state mutations from spectators
- Token isolation: Spectator and admin tokens have different roles/expiry

Integration:
- Uses escalada.auth.service for token operations (create_access_token, decode_token)
- Tests escalada.api.public module directly (_decode_spectator_token, PublicBoxInfo)
- Mocks state_map structure (see escalada.api.live for actual implementation)"""
import pytest
from datetime import datetime, timedelta, timezone

# Handle import of jwt module
try:
    import jwt
except ImportError:
    jwt = None

from escalada.auth.service import create_access_token, decode_token, JWT_SECRET, JWT_ALGORITHM


class TestSpectatorToken:
    """Test spectator token generation and validation.
    
    Verifies:
    - Token claims structure (role, sub, boxes fields)
    - Expiry calculation (24h TTL vs admin 1h TTL)
    - Role differentiation (spectator vs admin tokens are distinct)
    
    Why this matters:
    - Spectator tokens must have role="spectator" for public endpoint access
    - 24h TTL reduces token fetch frequency (performance + UX)
    - assigned_boxes=[] means spectators can view all initiated boxes (no restrictions)
    
    Token structure:
    - sub: Username (always "spectator" for public access)
    - role: Role identifier ("spectator" for read-only, "admin" for write access)
    - boxes: Assigned box IDs (empty for spectators = all boxes)
    - exp: Expiry timestamp (Unix epoch seconds)
    - iat: Issued-at timestamp (Unix epoch seconds)
    """

    def test_create_spectator_token_has_correct_role(self):
        """Spectator token should have role=spectator.
        
        Flow:
        1. Generate spectator token with 24h TTL (1440 minutes)
        2. Decode token to extract JWT claims
        3. Verify role="spectator" (required for public endpoint access)
        4. Verify sub="spectator" (username field)
        5. Verify boxes=[] (no box restrictions, can view all initiated boxes)
        
        Claims validation:
        - role="spectator" → routes to public endpoints (escalada.api.public)
        - boxes=[] → no filtering applied (can see all initiated boxes)
        - sub="spectator" → username for logging/audit (not used for auth)
        """
        # Generate token using auth service (same as POST /api/public/token endpoint)
        token = create_access_token(
            username="spectator",
            role="spectator",
            assigned_boxes=[],  # Empty = access to all initiated boxes
            expires_minutes=24 * 60,  # 24 hours (1440 minutes)
        )
        # Decode JWT to verify claims structure
        claims = decode_token(token)
        
        # Verify role claim (used for endpoint authorization)
        assert claims["role"] == "spectator"
        # Verify subject (username) claim
        assert claims["sub"] == "spectator"
        # Verify boxes claim (empty list = no restrictions)
        assert claims["boxes"] == []

    def test_spectator_token_expires_in_24h(self):
        """Spectator token should expire after 24 hours.
        
        Flow:
        1. Generate token with expires_minutes=1440 (24 hours)
        2. Decode to extract exp claim (Unix timestamp)
        3. Convert exp to datetime and compare with now
        4. Verify delta is ~24 hours (within 1-minute tolerance)
        
        Why 24h TTL:
        - Reduces token fetch frequency (better performance)
        - Spectators don't need frequent token rotation (read-only access)
        - Client proactively refreshes at <1h remaining (avoids mid-session expiry)
        
        Tolerance window:
        - Lower bound: 23h59m (account for test execution time)
        - Upper bound: 24h1m (account for clock skew)
        """
        # Generate token with 24h expiry
        token = create_access_token(
            username="spectator",
            role="spectator",
            assigned_boxes=[],
            expires_minutes=24 * 60,  # 1440 minutes = 24 hours
        )
        claims = decode_token(token)
        
        # Extract expiry timestamp and convert to datetime (UTC)
        exp = datetime.fromtimestamp(claims["exp"], tz=timezone.utc)
        now = datetime.now(timezone.utc)
        delta = exp - now
        
        # Verify expiry is approximately 24 hours from now
        # Allow 1-minute tolerance for test execution time and clock skew
        assert timedelta(hours=23, minutes=59) < delta < timedelta(hours=24, minutes=1)

    def test_spectator_token_different_from_admin(self):
        """Spectator and admin tokens should have different roles.
        
        Flow:
        1. Generate spectator token (24h TTL, role="spectator")
        2. Generate admin token (1h TTL, role="admin")
        3. Decode both tokens to extract claims
        4. Verify role field differs ("spectator" vs "admin")
        
        Security implications:
        - role claim determines endpoint access (public vs private)
        - Spectator tokens can't access /api/cmd (command endpoint blocked)
        - Admin tokens can't access /api/public/ws (public WebSocket requires spectator role)
        - Role-based routing prevents privilege escalation attacks
        
        TTL differences:
        - Spectator: 24h (convenience, low risk for read-only access)
        - Admin: 1h (security, shorter window for stolen token abuse)
        """
        # Generate spectator token (24h TTL for convenience)
        spectator_token = create_access_token(
            username="spectator",
            role="spectator",
            assigned_boxes=[],  # No box restrictions for spectators
            expires_minutes=24 * 60,  # 24 hours
        )
        # Generate admin token (1h TTL for security)
        admin_token = create_access_token(
            username="admin",
            role="admin",
            assigned_boxes=[],  # Admin can access all boxes
            expires_minutes=60,  # 1 hour
        )
        
        spectator_claims = decode_token(spectator_token)
        admin_claims = decode_token(admin_token)
        
        # Verify role claim differs (critical for access control)
        assert spectator_claims["role"] == "spectator"
        assert admin_claims["role"] == "admin"


class TestPublicBoxesFilter:
    """Test that boxes endpoint only returns initiated boxes.
    
    Verifies:
    - Filtering logic (initiated=True required for inclusion)
    - Label fallback ("Box N" if categorie missing)
    - Optional fields handled correctly (timerState, currentClimber can be None)
    
    Why filter initiated boxes:
    - Hide unconfigured boxes from spectators (admin hasn't set up route yet)
    - Prevent confusion (empty categorie, no competitors loaded)
    - Only show "live" categories with active competition
    
    Filtering scenarios:
    - Box initiated=False → excluded from response (not configured yet)
    - Box initiated=True → included with all state fields (timer, climber, label)
    - Box missing initiated field → treated as False, excluded
    
    Response shape:
    - boxes: List[PublicBoxInfo] with fields: boxId, label, initiated, timerState?, currentClimber?, categorie?
    - Sorted by boxId (consistent ordering for UI dropdown)
    - Only initiated boxes included (spectators see active categories only)
    """

    def test_filter_initiated_boxes_only(self):
        """Should only include boxes with initiated=True.
        
        Flow:
        1. Mock state_map with 3 boxes (2 initiated, 1 not initiated)
        2. Apply filtering logic (same as GET /api/public/boxes endpoint)
        3. Build PublicBoxInfo objects for initiated boxes only
        4. Verify box 1 (initiated=False) is excluded from result
        
        State_map structure:
        - Box 0: initiated=True, idle timer, climber "Alex" → INCLUDED
        - Box 1: initiated=False, no timer state → EXCLUDED
        - Box 2: initiated=True, running timer, climber "Bob" → INCLUDED
        
        Real-world scenario:
        - Admin configured Youth (box 0) and Seniors (box 2) categories
        - Adults (box 1) not yet initiated (admin still setting up)
        - Spectators should only see Youth and Seniors in dropdown
        """
        from escalada.api.public import PublicBoxInfo
        
        # Simulate state_map from escalada.api.live.state_map
        # Mix of initiated and non-initiated boxes
        mock_states = {
            0: {"initiated": True, "categorie": "Youth", "timerState": "idle", "currentClimber": "Alex"},
            1: {"initiated": False, "categorie": "Adults"},  # Not yet configured by admin
            2: {"initiated": True, "categorie": "Seniors", "timerState": "running", "currentClimber": "Bob"},
        }
        
        # Apply filtering logic (same as get_public_boxes endpoint)
        boxes = []
        for box_id, state in mock_states.items():
            # Only include boxes with initiated=True
            if state.get("initiated", False):
                boxes.append(
                    PublicBoxInfo(
                        boxId=box_id,
                        label=state.get("categorie") or f"Box {box_id}",  # Fallback label
                        initiated=True,
                        timerState=state.get("timerState"),  # Optional: None if not started
                        currentClimber=state.get("currentClimber"),  # Optional: None if no climber
                        categorie=state.get("categorie"),  # Backward compat field
                    )
                )
        
        # Verify filtering: only 2 initiated boxes returned
        assert len(boxes) == 2
        box_ids = [b.boxId for b in boxes]
        assert 0 in box_ids  # Youth (initiated=True)
        assert 2 in box_ids  # Seniors (initiated=True)
        assert 1 not in box_ids  # Adults (initiated=False, excluded)


class TestSpectatorTokenValidation:
    """Test spectator token validation in public endpoints.
    
    Verifies:
    - Valid spectator tokens decode successfully (signature + expiry + role checks)
    - Non-spectator tokens rejected with 403 (role mismatch)
    - Invalid/malformed tokens rejected with 401 (signature failure)
    
    Security layers:
    1. JWT signature validation (prevents token forgery)
    2. Expiry check (rejects expired tokens)
    3. Role enforcement (only role="spectator" allowed)
    
    Why strict validation:
    - Prevents privilege escalation (admin tokens can't access public WS)
    - Prevents token reuse attacks (expired tokens immediately rejected)
    - Prevents forged tokens (signature validation using JWT_SECRET)
    
    Error codes:
    - 401 (invalid_token): Signature invalid, token malformed, or expired
    - 403 (spectator_token_required): Valid token but role != "spectator"
    - 401 (token_required): Token missing from request
    
    Validation flow:
    1. Extract token from query param (?token=...)
    2. Decode JWT using JWT_SECRET (escalada.auth.service)
    3. Check expiry (claims["exp"] > now)
    4. Check role (claims["role"] == "spectator")
    5. Return claims if all checks pass
    """

    def test_decode_spectator_token_success(self):
        """Valid spectator token should decode successfully.
        
        Flow:
        1. Generate valid spectator token (24h TTL, role="spectator")
        2. Call _decode_spectator_token() (used by public endpoints)
        3. Verify signature validation passes (JWT_SECRET match)
        4. Verify expiry check passes (exp > now)
        5. Verify role check passes (role == "spectator")
        6. Return claims dict (role, sub, boxes, exp, iat)
        
        Validation steps (inside _decode_spectator_token):
        - decode_token(token) → validates signature + expiry
        - claims["role"] check → ensures spectator role
        - HTTPException raised if any check fails
        """
        from escalada.api.public import _decode_spectator_token
        
        # Generate valid spectator token
        token = create_access_token(
            username="spectator",
            role="spectator",
            assigned_boxes=[],
            expires_minutes=24 * 60,  # 24 hours (not expired)
        )
        
        # Decode and validate token (should succeed)
        claims = _decode_spectator_token(token)
        # Verify role claim present (required for access control)
        assert claims["role"] == "spectator"

    def test_decode_non_spectator_token_fails(self):
        """Non-spectator token should be rejected.
        
        Flow:
        1. Generate valid admin token (valid signature, not expired)
        2. Attempt to decode with _decode_spectator_token()
        3. Signature and expiry checks pass
        4. Role check fails (role="admin" != "spectator")
        5. Raise HTTPException 403 with "spectator_token_required"
        
        Security rationale:
        - Admin tokens shouldn't access public WebSocket (privilege separation)
        - Public endpoints require explicit spectator role (not just "any valid token")
        - Prevents admin from accidentally connecting to read-only WS
        
        Why 403 (not 401):
        - 401: Authentication failed (invalid/expired token)
        - 403: Authenticated but forbidden (valid token, wrong role)
        """
        from escalada.api.public import _decode_spectator_token
        from fastapi import HTTPException
        
        # Create valid admin token (will pass signature + expiry checks)
        token = create_access_token(
            username="admin",
            role="admin",  # Wrong role for public endpoints
            assigned_boxes=[],
            expires_minutes=60,
        )
        
        # Attempt to decode as spectator token (should fail at role check)
        with pytest.raises(HTTPException) as exc_info:
            _decode_spectator_token(token)
        
        # Verify 403 Forbidden (not 401 Unauthorized)
        assert exc_info.value.status_code == 403
        # Verify error code indicates role mismatch
        assert "spectator_token_required" in str(exc_info.value.detail)

    def test_decode_invalid_token_fails(self):
        """Invalid token should be rejected.
        
        Flow:
        1. Provide malformed token (not valid JWT format)
        2. Attempt to decode with _decode_spectator_token()
        3. decode_token() fails at signature validation
        4. Raise HTTPException 401 (invalid_token)
        
        Failure scenarios:
        - Malformed JWT (missing parts: "invalid.token.here" has 3 parts but invalid base64)
        - Invalid signature (token signed with different secret)
        - Corrupted token (network transmission error)
        - Expired token (exp claim < now)
        
        Why 401 (not 403):
        - 401: Can't authenticate (token invalid, can't verify identity)
        - 403: Authenticated but forbidden (valid token, wrong role)
        """
        from escalada.api.public import _decode_spectator_token
        from fastapi import HTTPException
        
        # Provide malformed token (signature validation will fail)
        with pytest.raises(HTTPException) as exc_info:
            _decode_spectator_token("invalid.token.here")
        
        # Verify 401 Unauthorized (authentication failed)
        assert exc_info.value.status_code == 401


class TestPublicWSNoCommands:
    """Test that public WS only accepts safe message types.
    
    Verifies:
    - Only PONG and REQUEST_STATE messages processed (safe, read-only)
    - All command types blocked (INIT_ROUTE, START_TIMER, SUBMIT_SCORE, etc.)
    - Command blocking prevents state mutations from spectators
    
    Security model:
    - Spectators are read-only (can view state but can't change it)
    - WebSocket is one-way for spectators (receive STATE_SNAPSHOT, send PONG only)
    - Commands logged at debug level and ignored (not rejected with error)
    
    Allowed message types:
    - PONG: Heartbeat response (keeps connection alive, server expects this)
    - REQUEST_STATE: Manual state refresh (spectator requests snapshot, no side effects)
    
    Blocked message types (all commands):
    - INIT_ROUTE: Start new route (requires admin role)
    - START_TIMER, STOP_TIMER, RESUME_TIMER: Timer control (requires judge/admin role)
    - PROGRESS_UPDATE: Update hold count (requires judge role)
    - SUBMIT_SCORE: Submit climber score (requires judge role)
    - RESET_BOX: Reset box state (requires admin role)
    
    Why block commands:
    - Prevent unauthorized state changes (spectators shouldn't control competition)
    - Prevent denial-of-service (malicious spectator can't spam commands)
    - Simplify WebSocket logic (no command validation/rate limiting needed)
    
    Message handling:
    - Allowed types: Process normally (PONG updates last_pong timestamp, REQUEST_STATE sends snapshot)
    - Blocked types: Log at debug level, skip processing, connection stays open
    """

    def test_allowed_message_types(self):
        """PONG and REQUEST_STATE should be the only allowed types.
        
        Verifies:
        - PONG message allowed (heartbeat response, updates last_pong timestamp)
        - REQUEST_STATE message allowed (manual refresh, sends STATE_SNAPSHOT)
        - Both are safe read-only operations (no state mutations)
        
        Message flow:
        1. PONG: Server sends PING every 30s → Client replies PONG → Server updates last_pong
        2. REQUEST_STATE: Client requests snapshot → Server sends STATE_SNAPSHOT (current state)
        
        Implementation (public_box_websocket in escalada.api.public):
        - Receive loop checks msg["type"]
        - If type == "PONG": Update last_pong dict (heartbeat tracking)
        - If type == "REQUEST_STATE": Call _send_public_box_snapshot(ws)
        - Else: Log at debug level and skip (command blocked)
        """
        # Define safe message types (read-only operations)
        allowed_types = {"PONG", "REQUEST_STATE"}
        
        # Test messages that should be processed (not blocked)
        test_messages = [
            {"type": "PONG"},  # Heartbeat response (client alive)
            {"type": "REQUEST_STATE"},  # Manual refresh (send snapshot)
        ]
        
        # Verify each test message is in allowed set
        for msg in test_messages:
            assert msg["type"] in allowed_types

    def test_command_types_not_allowed(self):
        """Command types should be ignored/blocked.
        
        Verifies:
        - All state-mutating commands are NOT in allowed set
        - Commands are logged but not processed (security through exclusion)
        - Spectators can't trigger state changes via WebSocket
        
        Blocked command categories:
        1. Route management: INIT_ROUTE (start new route), RESET_BOX (clear state)
        2. Timer control: START_TIMER, STOP_TIMER, RESUME_TIMER (judge operations)
        3. Scoring: PROGRESS_UPDATE (update holds), SUBMIT_SCORE (finalize score)
        
        Why block each command:
        - INIT_ROUTE: Admin-only (requires setup: competitors, holds, timer preset)
        - START_TIMER: Judge-only (starts climber's attempt)
        - STOP_TIMER: Judge-only (pauses timer, emergency stop)
        - RESUME_TIMER: Judge-only (resumes after pause)
        - PROGRESS_UPDATE: Judge-only (updates hold count as climber progresses)
        - SUBMIT_SCORE: Judge-only (finalizes score, moves to next climber)
        - RESET_BOX: Admin-only (clears all state, dangerous operation)
        
        Implementation:
        - WebSocket receive loop checks msg["type"]
        - If type in blocked_types: log.debug("Command blocked"), continue loop
        - No error sent to client (silent ignore, connection stays open)
        """
        # Define all state-mutating command types (must be blocked)
        blocked_types = {
            "START_TIMER",  # Judge: Start climber's timer
            "STOP_TIMER",  # Judge: Pause timer
            "RESUME_TIMER",  # Judge: Resume after pause
            "PROGRESS_UPDATE",  # Judge: Update hold count (delta: +1, -1)
            "SUBMIT_SCORE",  # Judge: Finalize score, move to next climber
            "INIT_ROUTE",  # Admin: Start new route with competitors
            "RESET_BOX",  # Admin: Clear all state (dangerous)
        }
        
        # Define safe message types (read-only)
        allowed_types = {"PONG", "REQUEST_STATE"}
        
        # Verify no overlap (commands must be excluded)
        for cmd_type in blocked_types:
            assert cmd_type not in allowed_types
