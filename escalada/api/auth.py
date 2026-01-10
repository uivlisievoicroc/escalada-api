import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from escalada.auth.deps import get_current_claims, require_role
from escalada.auth.service import (
    create_access_token,
    hash_password,
    verify_password,
)
from escalada.db.database import get_session
from escalada.db.repositories import UserRepository
from escalada.storage.json_store import get_users_with_default_admin, is_json_mode, save_users

logger = logging.getLogger(__name__)

router = APIRouter()


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    boxes: list[int]


@router.post("/auth/login", response_model=TokenResponse)
async def login(
    payload: LoginRequest, session: AsyncSession = Depends(get_session)
) -> TokenResponse:
    if is_json_mode():
        users = get_users_with_default_admin()
        user = users.get(payload.username)
        if not user or not user.get("is_active", True):
            logger.warning("Login failed for %s: user not found or inactive", payload.username)
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_credentials")
        if not verify_password(payload.password, user.get("password_hash") or ""):
            logger.warning("Login failed for %s: invalid password", payload.username)
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_credentials")
        token = create_access_token(
            username=user.get("username") or payload.username,
            role=user.get("role") or "viewer",
            assigned_boxes=user.get("assigned_boxes") or [],
        )
        return TokenResponse(
            access_token=token,
            role=user.get("role") or "viewer",
            boxes=user.get("assigned_boxes") or [],
        )
    user = await UserRepository.get_by_username(session, payload.username)
    if not user or not user.is_active:
        logger.warning("Login failed for %s: user not found or inactive", payload.username)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_credentials")

    if not verify_password(payload.password, user.password_hash):
        logger.warning("Login failed for %s: invalid password", payload.username)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_credentials")

    token = create_access_token(
        username=user.username,
        role=user.role,
        assigned_boxes=user.assigned_boxes or [],
    )
    return TokenResponse(access_token=token, role=user.role, boxes=user.assigned_boxes or [])


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
    """
    Magic tokens dezactivate.
    """
    raise HTTPException(status_code=status.HTTP_410_GONE, detail="magic_token_disabled")


class SetJudgePasswordRequest(BaseModel):
    password: str
    username: Optional[str] = None


@router.post("/admin/auth/boxes/{box_id}/password")
async def set_judge_password(
    box_id: int,
    payload: SetJudgePasswordRequest,
    session: AsyncSession = Depends(get_session),
    claims=Depends(require_role(["admin"])),
):
    """Setează/creează parola pentru userul judge al box-ului (implicit username=Box {id})."""
    if is_json_mode():
        users = get_users_with_default_admin()
        username = payload.username or f"Box {box_id}"
        now = datetime.now(timezone.utc).isoformat()
        users[username] = {
            "username": username,
            "password_hash": hash_password(payload.password),
            "role": "judge",
            "assigned_boxes": [box_id],
            "is_active": True,
            "created_at": users.get(username, {}).get("created_at") or now,
            "updated_at": now,
        }
        save_users(users)
        return {"status": "ok", "boxId": box_id}
    pwd_hash = hash_password(payload.password)
    await UserRepository.upsert_judge(
        session,
        box_id=box_id,
        password_hash=pwd_hash,
        username=payload.username,
    )
    await session.commit()
    return {"status": "ok", "boxId": box_id}
