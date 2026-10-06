"""知识问答会话记忆契约."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, Sequence

from kylin.ports.model.chat import ChatMessage


@dataclass(frozen=True, slots=True)
class ConversationMessage:
    """不依赖持久化模型的会话消息."""

    role: str
    content: str


@dataclass(frozen=True, slots=True)
class ConversationArchive:
    """供会话历史入口读取的持久化快照。"""

    session_id: str
    knowledge_base_name: str
    summary: str
    messages: tuple[dict, ...]


class ConversationSession(Protocol):
    """应用层可识别的最小会话句柄."""

    session_id: str


class ConversationMemoryPort(Protocol):
    """加载、组装和保存会话记忆的能力."""

    def open(
        self,
        session_id: str | None,
        knowledge_base_name: str,
    ) -> ConversationSession:
        """打开已有会话或创建新会话."""

    def load_history(
        self,
        session: ConversationSession,
    ) -> Sequence[ConversationMessage]:
        """返回供查询规划使用的完整归档历史."""

    def context_messages(
        self,
        session: ConversationSession,
    ) -> Sequence[ChatMessage]:
        """返回供回答生成使用的预算内上下文."""

    def save_turn(
        self,
        session: ConversationSession,
        query: str,
        answer: str,
    ) -> None:
        """保存一轮问答."""

    def maybe_summarize(
        self,
        session: ConversationSession,
        *,
        model: str,
    ) -> bool:
        """按现有策略按需更新滚动摘要."""

    def find(self, session_id: str) -> Optional[ConversationArchive]:
        """只读查询会话归档，不创建或更新会话。"""
