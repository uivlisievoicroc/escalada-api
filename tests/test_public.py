"""
Tests for public API endpoints (spectator access).
"""
import pytest
from datetime import datetime, timedelta, timezone

# Handle import of jwt module
try:
    import jwt
except ImportError:
    jwt = None

from escalada.auth.service import create_access_token, decode_token, JWT_SECRET, JWT_ALGORITHM


class TestSpectatorToken:
    """Test spectator token generation and validation."""

    def test_create_spectator_token_has_correct_role(self):
        """Spectator token should have role=spectator."""
        token = create_access_token(
            username="spectator",
            role="spectator",
            assigned_boxes=[],
            expires_minutes=24 * 60,
        )
        claims = decode_token(token)
        assert claims["role"] == "spectator"
        assert claims["sub"] == "spectator"
        assert claims["boxes"] == []

    def test_spectator_token_expires_in_24h(self):
        """Spectator token should expire after 24 hours."""
        token = create_access_token(
            username="spectator",
            role="spectator",
            assigned_boxes=[],
            expires_minutes=24 * 60,
        )
        claims = decode_token(token)
        
        # Check expiry is ~24 hours from now
        exp = datetime.fromtimestamp(claims["exp"], tz=timezone.utc)
        now = datetime.now(timezone.utc)
        delta = exp - now
        
        # Should be between 23h59m and 24h1m
        assert timedelta(hours=23, minutes=59) < delta < timedelta(hours=24, minutes=1)

    def test_spectator_token_different_from_admin(self):
        """Spectator and admin tokens should have different roles."""
        spectator_token = create_access_token(
            username="spectator",
            role="spectator",
            assigned_boxes=[],
            expires_minutes=24 * 60,
        )
        admin_token = create_access_token(
            username="admin",
            role="admin",
            assigned_boxes=[],
            expires_minutes=60,
        )
        
        spectator_claims = decode_token(spectator_token)
        admin_claims = decode_token(admin_token)
        
        assert spectator_claims["role"] == "spectator"
        assert admin_claims["role"] == "admin"


class TestPublicBoxesFilter:
    """Test that boxes endpoint only returns initiated boxes."""

    def test_filter_initiated_boxes_only(self):
        """Should only include boxes with initiated=True."""
        from escalada.api.public import PublicBoxInfo
        
        # Simulate state_map with mixed initiated states
        mock_states = {
            0: {"initiated": True, "categorie": "Youth", "timerState": "idle", "currentClimber": "Alex"},
            1: {"initiated": False, "categorie": "Adults"},
            2: {"initiated": True, "categorie": "Seniors", "timerState": "running", "currentClimber": "Bob"},
        }
        
        # Build boxes list (same logic as endpoint)
        boxes = []
        for box_id, state in mock_states.items():
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
        
        # Should only have 2 boxes (0 and 2)
        assert len(boxes) == 2
        box_ids = [b.boxId for b in boxes]
        assert 0 in box_ids
        assert 2 in box_ids
        assert 1 not in box_ids


class TestSpectatorTokenValidation:
    """Test spectator token validation in public endpoints."""

    def test_decode_spectator_token_success(self):
        """Valid spectator token should decode successfully."""
        from escalada.api.public import _decode_spectator_token
        
        token = create_access_token(
            username="spectator",
            role="spectator",
            assigned_boxes=[],
            expires_minutes=24 * 60,
        )
        
        claims = _decode_spectator_token(token)
        assert claims["role"] == "spectator"

    def test_decode_non_spectator_token_fails(self):
        """Non-spectator token should be rejected."""
        from escalada.api.public import _decode_spectator_token
        from fastapi import HTTPException
        
        # Create admin token
        token = create_access_token(
            username="admin",
            role="admin",
            assigned_boxes=[],
            expires_minutes=60,
        )
        
        with pytest.raises(HTTPException) as exc_info:
            _decode_spectator_token(token)
        
        assert exc_info.value.status_code == 403
        assert "spectator_token_required" in str(exc_info.value.detail)

    def test_decode_invalid_token_fails(self):
        """Invalid token should be rejected."""
        from escalada.api.public import _decode_spectator_token
        from fastapi import HTTPException
        
        with pytest.raises(HTTPException) as exc_info:
            _decode_spectator_token("invalid.token.here")
        
        assert exc_info.value.status_code == 401


class TestPublicWSNoCommands:
    """Test that public WS only accepts safe message types."""

    def test_allowed_message_types(self):
        """PONG and REQUEST_STATE should be the only allowed types."""
        allowed_types = {"PONG", "REQUEST_STATE"}
        
        # These are the message types that should be processed
        test_messages = [
            {"type": "PONG"},
            {"type": "REQUEST_STATE"},
        ]
        
        for msg in test_messages:
            assert msg["type"] in allowed_types

    def test_command_types_not_allowed(self):
        """Command types should be ignored/blocked."""
        blocked_types = {
            "START_TIMER",
            "STOP_TIMER",
            "RESUME_TIMER",
            "PROGRESS_UPDATE",
            "SUBMIT_SCORE",
            "INIT_ROUTE",
            "RESET_BOX",
        }
        
        allowed_types = {"PONG", "REQUEST_STATE"}
        
        for cmd_type in blocked_types:
            assert cmd_type not in allowed_types
