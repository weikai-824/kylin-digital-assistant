"""将现有知识库台账和生命周期实现适配到应用端口."""

from __future__ import annotations

from collections.abc import Sequence

from kylin.ports.data.ingestion import KnowledgeBaseInfo
from kylin.adapters.outbound.knowledge_base.ingest.dual_writer import ensure_kb
from kylin.adapters.outbound.knowledge_base.kb_service.base import KBService
from kylin.adapters.outbound.knowledge_base.kb_service.factory import KBServiceFactory
from kylin.adapters.outbound.knowledge_base.kb_service.paths import validate_kb_name


class KylinKnowledgeBaseCatalogAdapter:
    """复用 KBServiceFactory，隐藏台账与索引初始化细节."""

    def list(self) -> Sequence[KnowledgeBaseInfo]:
        result: list[KnowledgeBaseInfo] = []
        for item in KBService.list_kbs():
            data = item.model_dump() if hasattr(item, "model_dump") else dict(item)
            result.append(KnowledgeBaseInfo(
                name=str(data.get("kb_name") or data.get("name") or ""),
                info=str(data.get("kb_info") or data.get("info") or ""),
                vector_store_type=str(data.get("vs_type") or "dual"),
                embedding_model=str(data.get("embed_model") or ""),
            ))
        return tuple(item for item in result if item.name)

    def get(self, name: str) -> KnowledgeBaseInfo | None:
        service = KBServiceFactory.get_service_by_name(name)
        if service is None:
            return None
        return KnowledgeBaseInfo(
            name=service.kb_name,
            info=service.kb_info,
            vector_store_type=service.vs_type(),
            embedding_model=service.embed_model,
        )

    def create(self, info: KnowledgeBaseInfo) -> KnowledgeBaseInfo:
        if not validate_kb_name(info.name):
            raise ValueError(f"非法知识库名称: {info.name}")
        if self.get(info.name) is not None:
            raise ValueError(f"已存在同名知识库 {info.name}")
        ensure_kb(
            info.name,
            kb_info=info.info,
            embed_model=info.embedding_model or None,
        )
        return self.get(info.name) or info

    def update_info(self, name: str, description: str) -> None:
        service = KBServiceFactory.get_service_by_name(name)
        if service is None:
            raise LookupError(f"未找到知识库 {name}")
        service.update_info(description)

    def delete(self, name: str) -> bool:
        service = KBServiceFactory.get_service_by_name(name)
        if service is None:
            return False
        # drop_kb 已清理索引与台账；勿先 clear_vs（会 drop 后再重建空索引）
        return bool(service.drop_kb())
