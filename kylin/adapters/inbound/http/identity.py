"""身份校验 HTTP 路由：POST /knowledge_base/verify_user。"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from kylin.adapters.outbound.oa.client import OAClient
from kylin.adapters.outbound.oa.config import load_oa_settings
from kylin.applications.identity.service import IdentityService
from kylin.platform.runtime.schemas import BaseResponse


class VerifyUserRequest(BaseModel):
    """前端登录请求：仅一个工号字段；可扩展其他身份字段."""

    job_num: str = Field("", description="OA 工号", max_length=64)


def create_identity_router(service: IdentityService | None = None) -> APIRouter:
    """创建身份校验路由（复用 /knowledge_base 前缀，WebUI 登录调用）."""
    if service is None:
        settings = load_oa_settings()
        service = IdentityService(settings=settings, oa=OAClient(settings))
    router = APIRouter(prefix="/knowledge_base", tags=["Identity"])

    @router.post(
        "/verify_user",
        summary="校验工号并返回用户身份",
        response_model=BaseResponse,
    )
    async def verify_user(req: VerifyUserRequest):
        job_num = (req.job_num or "").strip()
        if not job_num:
            return BaseResponse(code=400, msg="工号不能为空")
        try:
            identity = service.verify(job_num)
        except ValueError as error:
            return BaseResponse(code=400, msg=str(error))
        except RuntimeError as error:
            return BaseResponse(code=404, msg=str(error))
        return BaseResponse(
            code=200,
            msg="success",
            data={
                "userid": identity.userid,
                "job_num": identity.job_num,
                "username": identity.username,
                "dept": identity.dept,
                "mock": identity.mock,
            },
        )

    return router
