"""将现有解析、清洗、切分能力适配到知识入库端口."""

from __future__ import annotations

from collections.abc import Mapping

from kylin.ports.data.ingestion import (
    IngestDocument,
    ProcessedDocument,
)
from kylin.adapters.outbound.knowledge_base.ingest.run import load_document
from kylin.adapters.outbound.knowledge_base.split.to_documents import split_text_to_parent_child_docs


class KylinDocumentProcessingAdapter:
    """复用现有文档处理规则并隐藏其第三方 Document 类型."""

    def process(
        self,
        path: str,
        filename: str,
        *,
        metadata: Mapping[str, object] | None = None,
        chunk_size: int | None = None,
        chunk_overlap: int | None = None,
    ) -> ProcessedDocument:
        documents = load_document(path, filename, doc_meta=dict(metadata or {}))
        if not documents:
            raise ValueError(f"文件无可解析文本: {filename}")
        text = "\n\n".join(
            document.page_content for document in documents if document.page_content
        )
        if not text.strip():
            raise ValueError(f"文件无可解析文本: {filename}")
        base_metadata = dict(documents[0].metadata or {})
        base_metadata.update(metadata or {})
        parents, children = split_text_to_parent_child_docs(
            text,
            metadata=base_metadata,
            child_chunk_size=chunk_size,
            child_overlap=chunk_overlap,
        )
        if not children:
            raise ValueError(f"文件无可切分文本: {filename}")
        parent_documents = []
        for parent_id, document in parents:
            parent_metadata = dict(document.metadata or {})
            parent_metadata.setdefault("doc_id", parent_id)
            parent_documents.append(
                IngestDocument(document.page_content, parent_metadata)
            )
        return ProcessedDocument(
            filename=filename,
            children=tuple(
                IngestDocument(document.page_content, dict(document.metadata or {}))
                for document in children
            ),
            parents=tuple(parent_documents),
        )
