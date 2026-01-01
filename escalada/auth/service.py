import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import jwt
from fastapi import HTTPException, status
from passlib.hash import pbkdf2_sha256

JWT_SECRET = os.getenv("JWT_SECRET", "dev-secret-change-me")
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRES_MIN = int(os.getenv("ACCESS_TOKEN_EXPIRES_MIN", "60"))


def hash_password(raw_password: str) -> str:
    return pbkdf2_sha256.hash(raw_password)


def verify_password(raw_password: str, password_hash: str) -> bool:
    return pbkdf2_sha256.verify(raw_password, password_hash)


def create_access_token(
    *,
    username: str,
    role: str,
    assigned_boxes: Optional[list[int]] = None,
    expires_minutes: int | None = None,
) -> str:
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
