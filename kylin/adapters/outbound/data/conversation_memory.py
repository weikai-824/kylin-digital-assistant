"""将现有 ConversationManager 适配到知识问答会话记忆端口."""

from __future__ import annotations

from collections.abc import Sequence

from kylin.ports.data.conversation_memory import (
    ConversationArchive,
    ConversationMessage,
    ConversationSession,
)
from kylin.ports.model.chat import ChatMessage
from kylin.adapters.outbound.knowledge_base.memory.manager import manager


class KylinConversationMemoryAdapter:
    """复用现有会话持久化与摘要策略."""

    def open(self, session_id: str | None, knowledge_base_name: str) -> ConversationSession:
        return manager.get_or_create(session_id, knowledge_base_name)

    def load_history(self, session: ConversationSession) -> Sequence[ConversationMessage]:
        return [
            ConversationMessage(role=item.role, content=item.content)
            for item in manager.load_history(session)
        ]

    def context_messages(self, session: ConversationSession) -> Sequence[ChatMessage]:
        return [dict(message) for message in manager.assemble_context_messages(session)]

    def save_turn(self, session: ConversationSession, query: str, answer: str) -> None:
        manager.save_turn(session, query, answer)

    def maybe_summarize(self, session: ConversationSession, *, model: str) -> bool:
        return manager.maybe_run_summary(session, model=model)

    def find(self, session_id: str) -> ConversationArchive | None:
        session = manager.get_by_id(session_id)
        if session is None:
            return None
        return ConversationArchive(
            session_id=session.session_id,
            knowledge_base_name=session.kb_name or "",
            summary=session.summary or "",
            messages=tuple(manager.load_items(session)),
        )
