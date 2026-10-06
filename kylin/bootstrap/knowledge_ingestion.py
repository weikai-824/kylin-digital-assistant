"""知识入库组合根：组装处理、存储、台账和双索引适配器."""

from __future__ import annotations

import os

from kylin.adapters.outbound.document_processing import KylinDocumentProcessingAdapter
from kylin.adapters.outbound.file_storage import KylinKnowledgeFileStorageAdapter
from kylin.adapters.outbound.knowledge_base_catalog import KylinKnowledgeBaseCatalogAdapter
from kylin.adapters.outbound.knowledge_index import KylinKnowledgeIndexAdapter
from kylin.adapters.outbound.model.clients import get_default_embedding
from kylin.applications.knowledge_ingestion.reserved import resolve_managed_knowledge_bases
from kylin.applications.knowledge_ingestion.service import KnowledgeIngestionService
from kylin.platform.config.settings import Settings


def _ticket_knowledge_base_name() -> str:
    """与 bootstrap.knowledge_gap 一致，避免循环导入."""
    configured = os.environ.get("TICKET_KB_NAME")
    if configured is not None:
        return configured.strip()
    return Settings.kb_settings.TICKET_KB_NAME.strip()


def _managed_knowledge_bases() -> frozenset[str]:
    """动态解析 OA / 工单托管库，避免配置热更新后仍用旧名单."""
    oa_kb = ""
    try:
        from kylin.adapters.outbound.oa.config import load_oa_settings

        oa_kb = load_oa_settings().kb_name
    except Exception:
        oa_kb = "oa_kb"
    return resolve_managed_knowledge_bases(
        ticket_kb_name=_ticket_knowledge_base_name(),
        oa_kb_name=oa_kb,
    )


def build_knowledge_ingestion_service() -> KnowledgeIngestionService:
    """构造知识库管理与入库应用服务."""
    default_embedding = get_default_embedding()
    return KnowledgeIngestionService(
        catalog=KylinKnowledgeBaseCatalogAdapter(),
        storage=KylinKnowledgeFileStorageAdapter(),
        processor=KylinDocumentProcessingAdapter(),
        index=KylinKnowledgeIndexAdapter(),
        default_embedding_model=default_embedding,
        default_chunk_size=Settings.kb_settings.CHILD_CHUNK_SIZE,
        default_chunk_overlap=Settings.kb_settings.CHILD_OVERLAP_SIZE,
        managed_knowledge_bases=_managed_knowledge_bases,
    )
