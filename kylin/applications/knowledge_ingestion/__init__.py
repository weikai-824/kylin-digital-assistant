"""知识同步、解析、切分与入库应用服务。"""

from kylin.applications.knowledge_ingestion.service import KnowledgeIngestionService
from kylin.applications.knowledge_ingestion.oa_sync import OAKnowledgeSyncService
from kylin.applications.knowledge_ingestion.reserved import (
    ManagedKnowledgeBaseError,
    resolve_managed_knowledge_bases,
)

__all__ = [
    "KnowledgeIngestionService",
    "OAKnowledgeSyncService",
    "ManagedKnowledgeBaseError",
    "resolve_managed_knowledge_bases",
]
