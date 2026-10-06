from typing import Any, Optional

from fastapi import APIRouter, Body

from pydantic import BaseModel, Field

from kylin.platform.runtime.net import api_address
from kylin.platform.config.settings import Settings


class BaseResponse(BaseModel):
    """保持服务器状态接口的旧响应结构。"""

    code: int = Field(200, description="API status code")
    msg: str = Field("success", description="API status message")
    data: Optional[Any] = Field("", description="API data")

    @classmethod
    def success(cls, data: Optional[Any] = "", message: str = "success"):
        return cls(code=200, msg=message, data=data)

    @classmethod
    def error(
        cls,
        data: Optional[Any] = "",
        message: str = "error",
        code: int = 500,
    ):
        return cls(code=code, msg=message, data=data)


def get_prompt_template(template_type: str, name: str) -> Optional[str]:
    """读取当前配置中的提示词模板。"""
    return Settings.prompt_settings.model_dump().get(template_type, {}).get(name)


def get_server_configs() -> dict:
    """返回保持旧接口形状的服务器配置。"""
    return {"api_address": api_address()}

server_router = APIRouter(prefix="/server", tags=["Server State"])

available_template_types = list(Settings.prompt_settings.model_fields.keys())

# 服务器相关接口
server_router.post(
    "/configs",
    summary="获取服务器原始配置信息",
)(get_server_configs)


@server_router.post("/get_prompt_template", summary="获取服务区配置的 prompt 模板", response_model=BaseResponse)
def get_server_prompt_template(
        type: str = Body(
            "llm_model", description="模板类型，可选值：{available_template_types}"
        ),
        name: str = Body("default", description="模板名称"),
):
    prompt_template = get_prompt_template(template_type=type, name=name)
    if prompt_template is None:
        return BaseResponse.error("Prompt template not found")
    return BaseResponse.success(prompt_template)
