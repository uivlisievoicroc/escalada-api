"""
Authentication/authorization dependencies for FastAPI routes.

These helpers implement the common access-control rules used across the API:
- Extract JWT from either Authorization header (legacy) or httpOnly cookie (preferred)
- Decode/validate JWT and expose its claims to route handlers
- Enforce role-based access (admin/judge/viewer/spectator)
- Enforce per-box access for roles that are scoped to specific boxes

Claims shape (see `escalada.auth.service.create_access_token`):
- `sub`: username (string)
- `role`: "admin" | "judge" | "viewer" | "spectator"
- `boxes`: list[int] of allowed box ids (may be empty = no restriction for some roles)
"""

# -------------------- Standard library imports --------------------
from typing import Any, Dict, Iterable, Optional

# -------------------- Third-party imports --------------------
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer

# -------------------- Local application imports --------------------
from escalada.auth.service import decode_token

# Cookie name must match auth.py
COOKIE_NAME = "escalada_token"

# OAuth2PasswordBearer provides the "Authorization: Bearer <token>" parsing.
# We set `auto_error=False` so cookie auth can be used as a fallback without FastAPI
# raising a 401 before our custom logic runs.
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
    """
    Decode the JWT and return its claims.

    `decode_token()` raises HTTPException for invalid/expired tokens; those propagate to the client.
    """
    return decode_token(token)


def require_role(allowed: Iterable[str]):
    """
    Dependency factory: enforce that the current user has one of the allowed roles.

    Usage:
        @router.get(...)
        async def endpoint(claims=Depends(require_role(["admin"]))):
            ...
    """
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
    # Admins can access all boxes.
    if claims.get("role") == "admin":
        return claims

    # Judges are scoped to an allow-list of boxes.
    allowed_boxes = set(claims.get("boxes") or [])
    box_id = None

    # Try to extract boxId from JSON body if available
    if request.method in ("POST", "PUT", "PATCH"):
        try:
            # `Request.json()` is safe to call here; Starlette caches the body for subsequent reads.
            body = await request.json()
            box_id = body.get("boxId") if isinstance(body, dict) else None
        except Exception:
            box_id = None

    # Fallback to path parameter for GET state/{box_id}
    if box_id is None:
        box_id = request.path_params.get("box_id")

    if box_id is None or int(box_id) not in allowed_boxes:
        # If box id is missing or outside the allow-list, reject with a consistent error code.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="forbidden_box",
        )

    return claims


async def require_view_access(
    claims: Dict[str, Any] = Depends(require_role(["viewer", "judge", "admin"])),
) -> Dict[str, Any]:
    """Allow any authenticated non-spectator viewer (viewer/judge/admin)."""
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
        # Admins can view any box.
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
