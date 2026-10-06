"""统一登录上下文接口。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from kylin.applications.identity.service import fixed_dev_identity, identity_to_session_dict
from kylin.platform.runtime.schemas import BaseResponse
from kylin.platform.security.login_user import LoginUser


def _first_value(info: dict, *keys: str) -> str:
    for key in keys:
        value = info.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _user_payload(user: LoginUser) -> dict:
    """Map the Gateway contract to the small user shape needed by the console."""
    info = user.info or {}
    userid = _first_value(info, "userid", "userId", "id") or str(user.id)
    username = _first_value(
        info, "username", "userName", "nickname", "nickName", "name"
    ) or userid
    dept_id = _first_value(info, "deptId", "departmentId", "department_id")
    dept = _first_value(
        info, "dept", "deptName", "department", "departmentName"
    ) or dept_id
    return {
        "userid": userid,
        "username": username,
        "dept": dept,
        "dept_id": dept_id,
        "tenant_id": user.tenant_id,
        "permissions": list(user.scopes),
    }


def create_auth_router() -> APIRouter:
    """Expose the trusted Gateway user to the Vue console."""
    router = APIRouter(prefix="/auth", tags=["统一认证"])

    @router.get("/me", response_model=BaseResponse, summary="获取当前登录用户")
    async def current_user(request: Request):
        user = getattr(request.state, "login_user", None)
        if user is None:
            # Docker dev mode has no Java Gateway; keep the local fixed identity usable.
            import os

            if os.environ.get("KYLIN_DEV", "").strip().lower() in ("1", "true", "yes"):
                return BaseResponse(
                    code=200,
                    msg="success",
                    data=identity_to_session_dict(fixed_dev_identity()),
                )
            raise HTTPException(status_code=401, detail="缺少统一登录用户上下文")
        return BaseResponse(code=200, msg="success", data=_user_payload(user))

    return router
