"""访问外部模型能力的适配器."""

from kylin.adapters.outbound.model.chat import KylinChatModelAdapter
from kylin.adapters.outbound.model.gateway import KylinModelGatewayAdapter

__all__ = ["KylinChatModelAdapter", "KylinModelGatewayAdapter"]
