"""文档解析、清洗和父子切分契约."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from kylin.ports.data.ingestion import ProcessedDocument


class DocumentProcessingPort(Protocol):
    """把一个文件转换为可写入索引的父子块."""

    def process(
        self,
        path: str,
        filename: str,
        *,
        metadata: Mapping[str, object] | None = None,
        chunk_size: int | None = None,
        chunk_overlap: int | None = None,
    ) -> ProcessedDocument:
        """解析、清洗、切分并补齐元数据."""
