"""
Test suite for escalada.auth.service JWT authentication
Run: poetry run pytest tests/test_auth.py -v
"""

import importlib
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import jwt
from fastapi import HTTPException


def load_service(env: dict | None = None):
    """Reload auth.service with optional env overrides so constants pick up new values."""
    import escalada.auth.service as svc

    if env is None:
        importlib.reload(svc)
        return svc
    with patch.dict("os.environ", env, clear=False):
        importlib.reload(svc)
    return svc


class JWTTokenCreationTest(unittest.TestCase):
    """Test JWT token creation using auth.service."""

    def test_create_access_token_basic(self):
        service = load_service()
        token = service.create_access_token(username="testuser", role="viewer")

        self.assertIsNotNone(token)
        self.assertIsInstance(token, str)
        self.assertGreater(len(token), 50)

    def test_create_access_token_with_custom_expiry(self):
        service = load_service()
        token = service.create_access_token(
            username="testuser", role="admin", expires_minutes=30
        )

        decoded = jwt.decode(token, options={"verify_signature": False})
        self.assertEqual(decoded["sub"], "testuser")
        exp_timestamp = decoded["exp"]
        now_timestamp = datetime.now(timezone.utc).timestamp()
        delta = exp_timestamp - now_timestamp
        self.assertGreater(delta, 25 * 60)  # ~30 minutes with buffer
        self.assertLess(delta, 35 * 60)

    def test_create_access_token_preserves_claims(self):
        service = load_service()
        token = service.create_access_token(
            username="admin", role="judge", assigned_boxes=[1, 2]
        )

        decoded = jwt.decode(token, options={"verify_signature": False})
        self.assertEqual(decoded["sub"], "admin")
        self.assertEqual(decoded["role"], "judge")
        self.assertEqual(decoded["boxes"], [1, 2])

    def test_create_access_token_default_expiry_from_env(self):
        service = load_service({"ACCESS_TOKEN_EXPIRES_MIN": "45"})
        token = service.create_access_token(username="user", role="viewer")

        decoded = jwt.decode(token, options={"verify_signature": False})
        exp_timestamp = decoded["exp"]
        now_timestamp = datetime.now(timezone.utc).timestamp()
        delta = exp_timestamp - now_timestamp
        self.assertGreater(delta, 40 * 60)  # close to 45 minutes
        self.assertLess(delta, 50 * 60)


class JWTTokenDecodeTest(unittest.TestCase):
    """Test decode_token behavior and errors."""

    def test_decode_token_valid(self):
        service = load_service()
        token = service.create_access_token(
            username="testuser", role="viewer", expires_minutes=5
        )

        claims = service.decode_token(token)
        self.assertEqual(claims["sub"], "testuser")
        self.assertEqual(claims["role"], "viewer")
        self.assertIn("exp", claims)

    def test_decode_token_expired(self):
        service = load_service()
        token = service.create_access_token(
            username="expired", role="viewer", expires_minutes=-1
        )

        with self.assertRaises(HTTPException) as context:
            service.decode_token(token)
        self.assertEqual(context.exception.status_code, 401)
        self.assertEqual(context.exception.detail, "token_expired")

    def test_decode_token_invalid_signature(self):
        service = load_service({"JWT_SECRET": "real-secret"})
        payload = {
            "sub": "user",
            "role": "viewer",
            "boxes": [],
            "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        }
        forged = jwt.encode(payload, "wrong-secret", algorithm="HS256")

        with self.assertRaises(HTTPException) as context:
            service.decode_token(forged)
        self.assertEqual(context.exception.status_code, 401)
        self.assertEqual(context.exception.detail, "invalid_token")


class JWTConfigTest(unittest.TestCase):
    """Test environment-driven configuration."""

    def test_secret_key_from_environment(self):
        service = load_service({"JWT_SECRET": "test_secret_key_123"})
        token = service.create_access_token(username="envuser", role="viewer")

        decoded = jwt.decode(token, "test_secret_key_123", algorithms=["HS256"])
        self.assertEqual(decoded["sub"], "envuser")


if __name__ == "__main__":
    unittest.main()
