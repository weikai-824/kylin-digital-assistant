"""模型网关出站适配器。"""

from __future__ import annotations

from typing import Any

from kylin.adapters.outbound.model.clients import (
    get_OpenAI,
    get_OpenAIClient,
    get_config_platforms,
    get_default_llm,
    get_model_info,
)


class KylinModelGatewayAdapter:
    """将旧模型工厂适配到入站协议需要的最小网关。"""

    def default_llm(self) -> str:
        return get_default_llm()

    def config_platforms(self) -> dict[str, dict]:
        return get_config_platforms()

    def model_info(
        self,
        *,
        model_name: str | None = None,
        platform_name: str | None = None,
        multiple: bool = False,
    ) -> dict:
        return get_model_info(
            model_name=model_name,
            platform_name=platform_name,
            multiple=multiple,
        )

    def openai_client(
        self,
        *,
        platform_name: str | None = None,
        model_name: str | None = None,
        is_async: bool = True,
    ) -> Any:
        return get_OpenAIClient(
            platform_name=platform_name,
            model_name=model_name,
            is_async=is_async,
        )

    def openai_model(
        self,
        *,
        model_name: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        local_wrap: bool = False,
    ) -> Any:
        return get_OpenAI(
            model_name=model_name,
            temperature=temperature,
            max_tokens=max_tokens,
            local_wrap=local_wrap,
        )


__all__ = ["KylinModelGatewayAdapter"]
