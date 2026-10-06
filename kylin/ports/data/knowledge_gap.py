"""知识缺口工单的数据对象与出口契约."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class KnowledgeGapTicket:
    """不泄漏 ORM 类型的知识缺口工单快照."""

    id: int
    question: str
    kb_name: str = ""
    status: str = "OPEN"
    answer: str | None = None
    fed_back: bool = False
    needs_review: bool = False
    create_time: datetime | None = None
    resolve_time: datetime | None = None


class KnowledgeGapRepositoryPort(Protocol):
    """知识缺口工单持久化契约."""

    def create(self, question: str, kb_name: str = "") -> KnowledgeGapTicket:
        """创建并返回工单."""

    def find_open(self, question: str) -> KnowledgeGapTicket | None:
        """查找同问题最近的未处理工单."""

    def get(self, ticket_id: int) -> KnowledgeGapTicket | None:
        """按主键查询工单."""

    def list(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
    ) -> Sequence[KnowledgeGapTicket]:
        """按状态倒序列出工单."""

    def update(
        self,
        ticket_id: int,
        *,
        status: str | None = None,
        answer: str | None = None,
        fed_back: bool | None = None,
        needs_review: bool | None = None,
        resolve_time: datetime | None = None,
    ) -> bool:
        """更新指定字段并返回是否命中工单."""

    def delete(self, ticket_id: int) -> bool:
        """物理删除工单并返回是否命中."""


class KnowledgeGapVerificationPort(Protocol):
    """验证人工答案能否被正式库与工单库联合检索命中."""

    def verify(
        self,
        question: str,
        *,
        formal_knowledge_base: str,
        ticket_knowledge_base: str,
    ) -> bool | None:
        """命中返回 True，未命中返回 False，无法验证返回 None."""
