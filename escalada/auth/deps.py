from typing import Any, Dict, Iterable, Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer

from escalada.auth.service import decode_token

# Cookie name must match auth.py
COOKIE_NAME = "escalada_token"

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)


async def get_token_from_request(
    request: Request,
    header_token: Optional[str] = Depends(oauth2_scheme),
) -> str:
    """
    Extract JWT token from:
    1. Authorization header (Bearer token) - for backwards compatibility
    2. httpOnly cookie - preferred for XSS protection
    """
    # Try Authorization header first (backwards compatible)
    if header_token:
        return header_token

    # Fallback to httpOnly cookie
    cookie_token = request.cookies.get(COOKIE_NAME)
    if cookie_token:
        return cookie_token

    # No token found
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="not_authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_claims(token: str = Depends(get_token_from_request)) -> Dict[str, Any]:
    return decode_token(token)


def require_role(allowed: Iterable[str]):
    async def checker(claims: Dict[str, Any] = Depends(get_current_claims)) -> Dict[str, Any]:
        role = claims.get("role")
        if role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="forbidden_role",
            )
        return claims

    return checker


async def require_box_access(
    request: Request,
    claims: Dict[str, Any] = Depends(require_role(["judge", "admin"])),
) -> Dict[str, Any]:
    """
    Validate that the caller can operate on the requested box.
    Works for body-based commands that include boxId or path params `box_id`.
    """
    if claims.get("role") == "admin":
        return claims

    allowed_boxes = set(claims.get("boxes") or [])
    box_id = None

    # Try to extract boxId from JSON body if available
    if request.method in ("POST", "PUT", "PATCH"):
        try:
            body = await request.json()
            box_id = body.get("boxId") if isinstance(body, dict) else None
        except Exception:
            box_id = None

    # Fallback to path parameter for GET state/{box_id}
    if box_id is None:
        box_id = request.path_params.get("box_id")

    if box_id is None or int(box_id) not in allowed_boxes:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="forbidden_box",
        )

    return claims


async def require_view_access(claims: Dict[str, Any] = Depends(require_role(["viewer", "judge", "admin"]))) -> Dict[str, Any]:
    return claims


def require_view_box_access(param_name: str = "box_id"):
    """
    Allow viewer/judge/admin; if boxes are specified in claims, enforce membership.
    Admin bypasses box checks.
    """

    async def checker(
        request: Request,
        claims: Dict[str, Any] = Depends(require_role(["viewer", "judge", "admin"])),
    ) -> Dict[str, Any]:
        if claims.get("role") == "admin":
            return claims

        allowed_boxes = set(claims.get("boxes") or [])
        box_id = request.path_params.get(param_name)

        # If caller has an explicit allow-list, enforce membership
        if allowed_boxes and (box_id is None or int(box_id) not in allowed_boxes):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="forbidden_box",
            )
        return claims

    return checker
