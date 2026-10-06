"""将 Milvus / Elasticsearch 双索引写入实现适配到知识入库端口."""

from __future__ import annotations

from typing import Sequence

from kylin.platform.runtime.documents import Document
from kylin.adapters.outbound.model.clients import check_embed_model
from kylin.ports.data.ingestion import IngestDocument, ProcessedDocument
from kylin.adapters.outbound.knowledge_base.index import (
    drop_indexes,
    embedding_dim,
    ensure_indexes,
)
from kylin.adapters.outbound.knowledge_base.kb_service.factory import KBServiceFactory
from kylin.adapters.outbound.knowledge_base.kb_service.files import KnowledgeFile


class KylinKnowledgeIndexAdapter:
    """通过既有 KBService 同步写入双索引和文件台账."""

    def check_ready(
        self,
        knowledge_base_name: str,
        embedding_model: str | None = None,
    ) -> None:
        ok, reason = check_embed_model(embedding_model or None)
        if not ok:
            detail = reason or f"知识库 {knowledge_base_name} 的 Embedding 不可用"
            raise RuntimeError(detail)

    def ensure(self, knowledge_base_name: str) -> None:
        ensure_indexes(knowledge_base_name, dim=embedding_dim())

    def clear(self, knowledge_base_name: str) -> None:
        service = KBServiceFactory.get_service_by_name(knowledge_base_name)
        if service is None:
            drop_indexes(knowledge_base_name)
            self.ensure(knowledge_base_name)
            return
        service.clear_vs()

    def upsert(self, knowledge_base_name: str, processed: ProcessedDocument) -> None:
        service = KBServiceFactory.get_service_by_name(knowledge_base_name)
        if service is None:
            raise LookupError(f"未找到知识库 {knowledge_base_name}")
        children = [self._to_document(document) for document in processed.children]
        if not children:
            raise ValueError(f"文件 {processed.filename} 没有可写入的子块")
        parents = [self._to_document(document) for document in processed.parents]
        parent_records = []
        for document in parents:
            doc_id = str(document.metadata.get("doc_id") or "").strip()
            if not doc_id:
                raise ValueError(f"文件 {processed.filename} 的父块缺少 doc_id")
            parent_records.append((doc_id, document))
        added = service.add_doc(
            KnowledgeFile(
                filename=processed.filename,
                knowledge_base_name=knowledge_base_name,
            ),
            docs=children,
            parent_docs=parent_records,
            not_refresh_vs_cache=False,
        )
        if not added:
            raise RuntimeError(f"文件 {processed.filename} 写入索引或台账失败")

    def delete_by_source(self, knowledge_base_name: str, filename: str) -> None:
        service = KBServiceFactory.get_service_by_name(knowledge_base_name)
        if service is None:
            return
        service.delete_doc(
            KnowledgeFile(filename=filename, knowledge_base_name=knowledge_base_name),
            delete_content=False,
            not_refresh_vs_cache=False,
        )

    def update_access_depts(
        self,
        knowledge_base_name: str,
        filename: str,
        access_depts: Sequence[str] = (),
    ) -> int:
        """只改写某文件全部子块的 access_depts，不重新切分/不重新 embed。"""
        service = KBServiceFactory.get_service_by_name(knowledge_base_name)
        if service is None:
            raise LookupError(f"未找到知识库 {knowledge_base_name}")
        return service.update_doc_access_depts(
            KnowledgeFile(
                filename=filename,
                knowledge_base_name=knowledge_base_name,
            ),
            access_depts=list(access_depts or []),
        )

    @staticmethod
    def _to_document(document: IngestDocument) -> Document:
        return Document(
            page_content=document.page_content,
            metadata=dict(document.metadata),
        )
