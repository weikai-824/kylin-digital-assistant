"""向量与全文索引写入契约."""

from __future__ import annotations

from typing import Protocol, Sequence

from kylin.ports.data.ingestion import ProcessedDocument


class KnowledgeIndexPort(Protocol):
    """知识块索引生命周期能力."""

    def check_ready(
        self,
        knowledge_base_name: str,
        embedding_model: str | None = None,
    ) -> None:
        """在任何破坏性索引操作前确认后端和 Embedding 可用."""

    def ensure(self, knowledge_base_name: str) -> None:
        """确保知识库索引存在."""

    def clear(self, knowledge_base_name: str) -> None:
        """清空知识库索引."""

    def upsert(
        self,
        knowledge_base_name: str,
        processed: ProcessedDocument,
    ) -> None:
        """写入一个文件的父子块."""

    def delete_by_source(self, knowledge_base_name: str, filename: str) -> None:
        """按源文件删除父子块."""

    def update_access_depts(
        self,
        knowledge_base_name: str,
        filename: str,
        access_depts: Sequence[str] = (),
    ) -> int:
        """只改写某文件全部子块的 access_depts，不重新切分/不重新 embed。

        空列表 = 全开放。返回被更新的子块数。实现此方法时索引层需要
        读回原向量再写回，避免重新 Embedding。
        """
