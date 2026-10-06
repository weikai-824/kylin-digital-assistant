"""HTTP 兼容边界所需的模型网关契约。"""

from __future__ import annotations

from typing import Any, Protocol


class ModelGatewayPort(Protocol):
    """为入站协议提供模型目录与客户端，不暴露厂商构造细节。"""

    def default_llm(self) -> str:
        """返回默认对话模型名称。"""

    def config_platforms(self) -> dict[str, dict]:
        """返回已配置的模型平台。"""

    def model_info(
        self,
        *,
        model_name: str | None = None,
        platform_name: str | None = None,
        multiple: bool = False,
    ) -> dict:
        """返回模型连接信息。"""

    def openai_client(
        self,
        *,
        platform_name: str | None = None,
        model_name: str | None = None,
        is_async: bool = True,
    ) -> Any:
        """返回 OpenAI 兼容客户端。"""

    def openai_model(
        self,
        *,
        model_name: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        local_wrap: bool = False,
    ) -> Any:
        """返回应用侧可调用的对话模型。"""


__all__ = ["ModelGatewayPort"]
