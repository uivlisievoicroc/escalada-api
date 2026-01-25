import logging
import os
import unicodedata
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel

from escalada.auth.deps import get_current_claims, require_role
from escalada.auth.service import create_access_token, hash_password, verify_password
from escalada.storage.json_store import get_users_with_default_admin, save_users

logger = logging.getLogger(__name__)

router = APIRouter()

# Cookie settings - secure in production
COOKIE_NAME = "escalada_token"
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() in ("true", "1", "yes")
COOKIE_SAMESITE = os.getenv("COOKIE_SAMESITE", "lax")  # "strict", "lax", or "none"
COOKIE_MAX_AGE = 60 * 60 * 24  # 24 hours


def _canonical_username(username: str) -> str:
    # Normalize unicode (handles NFC/NFD differences from mobile keyboards/QR scans)
    s = unicodedata.normalize("NFKC", username or "")
    # Drop format characters (e.g. zero-width spaces)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Cf")
    # Normalize whitespace (incl. NBSP)
    s = "".join(" " if ch.isspace() else ch for ch in s)
    return " ".join(s.strip().split())



class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    boxes: list[int]


@router.post("/auth/login", response_model=TokenResponse)
async def login(payload: LoginRequest, response: Response) -> TokenResponse:
    users = get_users_with_default_admin()
    requested = payload.username
    canonical = _canonical_username(requested)

    user_key = requested
    user = users.get(user_key) or users.get(canonical)

    # If caller passes just the box number (e.g. "0"), allow it as an alias for judge accounts.
    if user is None and canonical.isdigit():
        user_key = canonical
        user = users.get(user_key)
        if user is None:
            boxed = _canonical_username(f"Box {canonical}")
            user_key = boxed
            user = users.get(user_key)
    if user is None:
        for k, v in users.items():
            if _canonical_username(k).casefold() == canonical.casefold():
                user_key = k
                user = v
                break
    if not user or not user.get("is_active", True):
        logger.warning("Login failed for %s: user not found or inactive", payload.username)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid_credentials",
        )

    if not verify_password(payload.password, user.get("password_hash") or ""):
        logger.warning("Login failed for %s: invalid password", payload.username)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid_credentials",
        )

    role = user.get("role") or "viewer"
    boxes = user.get("assigned_boxes") or []
    token = create_access_token(
        username=user.get("username") or user_key,
        role=role,
        assigned_boxes=boxes,
    )

    # Set httpOnly cookie for enhanced XSS protection
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite=COOKIE_SAMESITE,
        max_age=COOKIE_MAX_AGE,
        path="/",
    )

    # Also return token in body for backwards compatibility with existing clients
    return TokenResponse(access_token=token, role=role, boxes=boxes)


@router.post("/auth/logout")
async def logout(response: Response):
    """Clear the auth cookie to log out the user."""
    response.delete_cookie(
        key=COOKIE_NAME,
        path="/",
        httponly=True,
        secure=COOKIE_SECURE,
        samesite=COOKIE_SAMESITE,
    )
    return {"status": "logged_out"}


@router.get("/auth/me")
async def me(claims=Depends(get_current_claims)):
    return claims


class MagicLoginRequest(BaseModel):
    token: str


@router.post("/auth/magic-login", response_model=TokenResponse)
async def magic_login(payload: MagicLoginRequest) -> TokenResponse:
    """Magic login dezactivat."""
    raise HTTPException(status_code=status.HTTP_410_GONE, detail="magic_login_disabled")


@router.post("/admin/auth/boxes/{box_id}/magic-token")
async def issue_magic_token(box_id: int, claims=Depends(require_role(["admin"]))):
    """Magic tokens dezactivate."""
    raise HTTPException(status_code=status.HTTP_410_GONE, detail="magic_token_disabled")


class SetJudgePasswordRequest(BaseModel):
    password: str
    username: Optional[str] = None


@router.post("/admin/auth/boxes/{box_id}/password")
async def set_judge_password(
    box_id: int,
    payload: SetJudgePasswordRequest,
    claims=Depends(require_role(["admin"])),
):
    """Setează/creează parola pentru userul judge al box-ului (implicit username=Box {id})."""
    users = get_users_with_default_admin()
    raw_username = payload.username or f"Box {box_id}"
    username = _canonical_username(raw_username) or f"Box {box_id}"
    alias_username = _canonical_username(f"Box {box_id}") or f"Box {box_id}"
    id_username = _canonical_username(str(box_id)) or str(box_id)

    password = (payload.password or "").strip()
    if not password:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="password_required")

    # Migrate legacy key with non-canonical whitespace (if any).
    if raw_username in users and username != raw_username and username not in users:
        users[username] = users.pop(raw_username)

    aliases = [username, alias_username, id_username]

    # Preserve earliest created_at across possible aliases.
    created_candidates = []
    for u in aliases:
        v = users.get(u, {})
        if isinstance(v, dict) and v.get("created_at"):
            created_candidates.append(v.get("created_at"))

    now = datetime.now(timezone.utc).isoformat()
    created_at = min(created_candidates) if created_candidates else now

    record = {
        "password_hash": hash_password(password),
        "role": "judge",
        "assigned_boxes": [box_id],
        "is_active": True,
        "created_at": created_at,
        "updated_at": now,
    }

    for u in aliases:
        users[u] = {"username": u, **record}

    save_users(users)
    return {"status": "ok", "boxId": box_id, "username": username, "alias": alias_username, "id_alias": id_username}
