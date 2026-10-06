"""知识库台账与目录管理契约."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from kylin.ports.data.ingestion import KnowledgeBaseInfo


class KnowledgeBaseCatalogPort(Protocol):
    """知识库元信息和生命周期的持久化能力."""

    def list(self) -> Sequence[KnowledgeBaseInfo]:
        """列出知识库."""

    def get(self, name: str) -> KnowledgeBaseInfo | None:
        """按名称读取知识库，不存在时返回 None."""

    def create(self, info: KnowledgeBaseInfo) -> KnowledgeBaseInfo:
        """创建知识库台账."""

    def update_info(self, name: str, description: str) -> None:
        """更新知识库描述."""

    def delete(self, name: str) -> bool:
        """删除知识库台账."""
