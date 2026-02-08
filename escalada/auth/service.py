"""
Auth primitives used across the API (password hashing + JWT encode/decode).

This module intentionally stays small and dependency-free:
- Password hashing/verification via PBKDF2 (passlib)
- JWT creation/validation via PyJWT

JWT claims used by the app:
- `sub`: username (string)
- `role`: "admin" | "judge" | "viewer" | "spectator"
- `boxes`: list[int] of allowed box ids (can be empty)
- `exp`: expiry timestamp (UTC)
"""

# -------------------- Standard library imports --------------------
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

# -------------------- Third-party imports --------------------
import jwt
from fastapi import HTTPException, status
from passlib.hash import pbkdf2_sha256

# Secret and TTL are configurable via env; dev defaults are provided for local runs/tests.
JWT_SECRET = os.getenv("JWT_SECRET", "dev-secret-change-me")
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRES_MIN = int(os.getenv("ACCESS_TOKEN_EXPIRES_MIN", "60"))


def hash_password(raw_password: str) -> str:
    """Hash a raw password for storage."""
    return pbkdf2_sha256.hash(raw_password)


def verify_password(raw_password: str, password_hash: str) -> bool:
    """Verify a raw password against the stored hash."""
    return pbkdf2_sha256.verify(raw_password, password_hash)


def create_access_token(
    *,
    username: str,
    role: str,
    assigned_boxes: Optional[list[int]] = None,
    expires_minutes: int | None = None,
) -> str:
    """
    Create a signed JWT access token.

    `expires_minutes` overrides the default TTL; used for long-lived spectator tokens.
    """
    expires_delta = timedelta(
        minutes=expires_minutes or ACCESS_TOKEN_EXPIRES_MIN
    )
    payload: Dict[str, Any] = {
        "sub": username,
        "role": role,
        "boxes": assigned_boxes or [],
        "exp": datetime.now(timezone.utc) + expires_delta,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> Dict[str, Any]:
    """
    Decode/validate a JWT and return claims.

    Raises:
    - 401 token_expired: signature is valid but token is past `exp`
    - 401 invalid_token: signature/format is invalid
    """
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="token_expired",
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid_token",
        )
