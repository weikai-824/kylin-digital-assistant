"""API 鉴权中间件（P0-SEC-02 全 API 鉴权的最小实现）。

行为（默认关闭，行为完全不变）：
- ``Settings.basic_settings.api_token`` 为空 → 不启用，所有请求放行
- ``KYLIN_DEV=1``（开发模式）→ 放行，本地联调不被卡
- OPTIONS 预检请求 → 放行（CORS）
- Swagger 文档（/docs、/openapi.json、/redoc）与静态资源（/media、/img）→ 放行
- 其余所有 API 请求：校验 ``X-API-Key`` 或 ``Authorization: Bearer <token>``，
  与配置 token 用常数时间比较；不匹配返回 401 JSON

仅依赖 stdlib，零新依赖。
"""

from __future__ import annotations

import hmac
import json
from typing import Iterable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from kylin.platform.config.settings import Settings
from kylin.platform.security.login_user import LoginUser
from kylin.platform.security.request_context import reset_current_user, set_current_user


# 文档 / 健康路径：浏览器直开 Swagger 或健康检查不应被 token 卡住
_DOC_PREFIXES = ("/docs", "/redoc", "/openapi.json")
# 静态资源：WebUI 通过 <img>/<a> 直连加载，无法附加自定义头
_STATIC_PREFIXES = ("/media", "/img")
# 根路径（重定向到 /docs）：仅精确匹配，不参与前缀放行
_ROOT_PATH = "/"
_HEALTH_PATHS = ("/health", "/healthz")


def _is_dev_mode() -> bool:
    import os

    return os.environ.get("KYLIN_DEV", "").strip().lower() in ("1", "true", "yes")


def _extract_token(request: Request) -> str:
    """从请求头提取 token：X-API-Key 优先，其次 Authorization: Bearer。"""
    api_key = request.headers.get("x-api-key", "")
    if api_key:
        return api_key.strip()
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def _read_login_user(request: Request) -> LoginUser | None:
    value = request.headers.get("login-user", "").strip()
    if not value:
        return None
    try:
        return LoginUser.from_header(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _token_matches(provided: str, expected: str) -> bool:
    """常数时间比较，防时序侧信道。"""
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


class APIAuthMiddleware(BaseHTTPMiddleware):
    """业务 API 全量鉴权；未配置 token 时完全放行。"""

    def __init__(self, app, api_token: str):
        super().__init__(app)
        self._api_token = (api_token or "").strip()
        self._exempt_prefixes: tuple[str, ...] = tuple(_DOC_PREFIXES + _STATIC_PREFIXES)

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if request.method == "OPTIONS":
            return await call_next(request)
        if path == _ROOT_PATH:
            return await call_next(request)
        if path in _HEALTH_PATHS:
            return await call_next(request)
        if any(path == prefix or path.startswith(f"{prefix.rstrip('/')}/") for prefix in self._exempt_prefixes):
            return await call_next(request)

        shared_secret = (Settings.basic_settings.gateway_shared_secret or "").strip()
        user = _read_login_user(request)
        supplied_secret = request.headers.get("x-kylin-gateway-key", "").strip()
        trusted_user = bool(shared_secret) and user is not None and _token_matches(
            supplied_secret, shared_secret
        )
        token_valid = bool(self._api_token) and _token_matches(
            _extract_token(request), self._api_token
        )
        security_enabled = bool(
            self._api_token
            or shared_secret
            or Settings.basic_settings.require_gateway_identity
        )
        if not security_enabled or _is_dev_mode():
            return await call_next(request)

        if trusted_user or token_valid:
            if Settings.basic_settings.require_gateway_identity and not trusted_user:
                return JSONResponse(
                    status_code=401,
                    content={"code": 401, "msg": "缺少统一登录用户上下文"},
                )
            token = set_current_user(user if trusted_user else None)
            request.state.login_user = user if trusted_user else None
            request.state.api_token_authenticated = token_valid
            try:
                return await call_next(request)
            finally:
                reset_current_user(token)

        return JSONResponse(
            status_code=401,
            content={
                "code": 401,
                "msg": "未授权：请在请求头携带 X-API-Key 或 Authorization: Bearer <token>",
            },
        )


def create_auth_middleware(api_token: str):
    """创建鉴权中间件实例（api_token 为空时内部自动放行）。"""
    return APIAuthMiddleware, {"api_token": api_token}
