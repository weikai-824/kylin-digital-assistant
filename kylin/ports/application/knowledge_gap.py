"""知识问答调用知识缺口应用的契约."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from kylin.ports.data.knowledge_gap import KnowledgeGapTicket


class EmptyKnowledgeGapAnswerError(ValueError):
    """人工答案为空."""


class KnowledgeGapTicketNotFoundError(LookupError):
    """工单不存在."""


class KnowledgeGapTicketClosedError(RuntimeError):
    """工单已经闭环，不能再次提交答案."""


class KnowledgeGapTicketOpenError(RuntimeError):
    """工单尚未解决，不能执行验证."""


class KnowledgeGapIngestionError(RuntimeError):
    """人工答案未能写入知识库."""


class KnowledgeGapTicketDeleteError(RuntimeError):
    """工单删除失败（已反哺内容清理失败等）."""


class KnowledgeGapPort(Protocol):
    """查询工单知识库并创建知识缺口工单."""

    def ticket_kb_name(self) -> str:
        """返回工单答案库名；空字符串表示未启用."""

    def create(self, question: str, kb_name: str = "") -> int | None:
        """创建或复用工单并返回工单号；调用方可用 ``None`` 表示未创建."""


@dataclass(frozen=True, slots=True)
class ResolveKnowledgeGapResult:
    """一次人工回答的业务结果."""

    ticket_id: int
    fed_back: bool


class KnowledgeGapManagementPort(Protocol):
    """人工处理台调用知识缺口应用的契约."""

    def list_tickets(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
    ) -> Sequence[KnowledgeGapTicket]:
        """列出工单."""

    def get_ticket(self, ticket_id: int) -> KnowledgeGapTicket | None:
        """查询工单详情."""

    def resolve_ticket(
        self,
        ticket_id: int,
        answer: str,
    ) -> ResolveKnowledgeGapResult:
        """提交人工答案并触发反哺和验证."""

    def reverify(self, ticket_id: int) -> KnowledgeGapTicket:
        """重新验证已解决工单并返回最新快照."""

    def force_close(self, ticket_id: int) -> None:
        """人工确认后关闭工单."""

    def delete_ticket(self, ticket_id: int) -> None:
        """物理删除工单；已反哺内容同步清理."""
