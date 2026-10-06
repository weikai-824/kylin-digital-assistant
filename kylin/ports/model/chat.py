"""应用服务所需的 Chat 模型能力契约."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Literal, Protocol, Sequence, TypedDict


class ChatModelUnavailableError(RuntimeError):
    """没有可用的 Chat 模型实例."""


class ChatMessage(TypedDict):
    """不包含厂商类型的文本消息."""

    role: Literal["system", "user", "assistant"]
    content: str


class ChatModelPort(Protocol):
    """应用服务使用的文本生成契约."""

    def default_model(self) -> str:
        """返回当前默认模型名."""

    def grounding_style(self, model_name: str | None = None) -> str:
        """返回模型所属平台的接地判定风格（"strict"/"lenient"），供 prompt 选择使用."""

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> str | None:
        """返回模型文本；无法创建模型时返回 ``None``."""

    async def complete_async(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> str:
        """异步返回完整文本；模型不可用时抛出领域异常."""

    def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        """流式返回文本片段；模型不可用时抛出领域异常."""
