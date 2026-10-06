"""FastAPI dependencies for the shared permission-code contract."""

from __future__ import annotations

import logging

from fastapi import HTTPException, Request

from kylin.platform.config.settings import Settings
from kylin.platform.security.login_user import LoginUser

logger = logging.getLogger(__name__)


def _has_scope(user: LoginUser, permission: str) -> bool:
    return permission in user.scopes or "*" in user.scopes


async def _check_remote_permission(
    user: LoginUser, permission: str, request: Request
) -> bool:
    """Optionally delegate RBAC checks to Java when scopes are not embedded."""
    url = (Settings.basic_settings.permission_check_url or "").strip()
    if not url:
        return False

    import httpx

    headers = {}
    # Some deployments protect the direct system-service RPC endpoint too.
    # Reuse the already validated Java access token; never log this header.
    authorization = request.headers.get("authorization", "").strip()
    if authorization:
        headers["Authorization"] = authorization

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(
                url,
                params={"userId": user.id, "permissions": permission},
                headers=headers,
            )
            if response.status_code < 200 or response.status_code >= 300:
                logger.warning(
                    "Java permission check rejected request: status=%s user_id=%s permission=%s body=%s",
                    response.status_code,
                    user.id,
                    permission,
                    response.text[:200],
                )
                return False
            payload = response.json()
    except httpx.HTTPError as exc:
        logger.warning(
            "Java permission check unavailable: user_id=%s permission=%s error=%s",
            user.id,
            permission,
            exc,
        )
        return False
    except ValueError as exc:
        logger.warning(
            "Java permission check returned invalid JSON: user_id=%s permission=%s error=%s",
            user.id,
            permission,
            exc,
        )
        return False
    # Java CommonResult uses code=0 for success; 200 is kept for compatibility
    # with services that expose an HTTP-style response envelope.
    success_code = payload.get("code", 200) if isinstance(payload, dict) else None
    allowed = (
        isinstance(payload, dict)
        and bool(payload.get("data"))
        and success_code in (0, 200, "0", "200")
    )
    if not allowed:
        logger.info(
            "Java permission check denied request: user_id=%s permission=%s code=%s data=%s",
            user.id,
            permission,
            success_code,
            payload.get("data") if isinstance(payload, dict) else None,
        )
    return allowed


def require_permission(permission: str):
    """Create a FastAPI dependency using the Java permission-code convention."""

    async def dependency(request: Request) -> LoginUser:
        user = getattr(request.state, "login_user", None)
        if user is None:
            # Preserve the existing local/dev contract until unified auth is enabled.
            if not (
                Settings.basic_settings.gateway_shared_secret
                or Settings.basic_settings.require_gateway_identity
            ):
                return None  # type: ignore[return-value]
            if getattr(request.state, "api_token_authenticated", False):
                return None  # type: ignore[return-value]
            raise HTTPException(status_code=401, detail="缺少统一登录用户上下文")
        if _has_scope(user, permission) or await _check_remote_permission(
            user, permission, request
        ):
            return user
        raise HTTPException(status_code=403, detail=f"缺少权限: {permission}")

    return dependency
