"""不包含厂商类型的模型能力契约."""

from kylin.ports.model.chat import (
    ChatMessage,
    ChatModelPort,
    ChatModelUnavailableError,
)
from kylin.ports.model.gateway import ModelGatewayPort

__all__ = ["ChatMessage", "ChatModelPort", "ChatModelUnavailableError", "ModelGatewayPort"]
