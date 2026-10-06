"""数据访问能力契约."""

from kylin.ports.data.conversation_memory import (
    ConversationMemoryPort,
    ConversationMessage,
    ConversationSession,
)
from kylin.ports.data.knowledge_search import (
    KnowledgeBaseNotFoundError,
    KnowledgeBaseUnavailableError,
    KnowledgeDocument,
    KnowledgeSearchError,
    KnowledgeSearchPort,
)
from kylin.ports.data.ingestion import (
    FileOperationResult,
    IngestionProgress,
    IngestDocument,
    KnowledgeBaseInfo,
    KnowledgeFileDetails,
    ProcessedDocument,
)
from kylin.ports.data.document_processing import DocumentProcessingPort
from kylin.ports.data.file_storage import KnowledgeFileStoragePort
from kylin.ports.data.index import KnowledgeIndexPort
from kylin.ports.data.knowledge_base import KnowledgeBaseCatalogPort
from kylin.ports.data.knowledge_gap import (
    KnowledgeGapRepositoryPort,
    KnowledgeGapTicket,
    KnowledgeGapVerificationPort,
)

__all__ = [
    "ConversationMemoryPort",
    "ConversationMessage",
    "ConversationSession",
    "KnowledgeBaseNotFoundError",
    "KnowledgeBaseUnavailableError",
    "KnowledgeDocument",
    "KnowledgeSearchError",
    "KnowledgeSearchPort",
    "FileOperationResult",
    "IngestionProgress",
    "IngestDocument",
    "KnowledgeBaseInfo",
    "KnowledgeFileDetails",
    "ProcessedDocument",
    "DocumentProcessingPort",
    "KnowledgeFileStoragePort",
    "KnowledgeIndexPort",
    "KnowledgeBaseCatalogPort",
    "KnowledgeGapRepositoryPort",
    "KnowledgeGapTicket",
    "KnowledgeGapVerificationPort",
    "AttachmentState",
    "DocState",
    "OAAttachmentDispatchResult",
    "OASyncSettings",
    "SyncReport",
    "SyncState",
]

from kylin.ports.data.oa_sync import (
    AttachmentState,
    DocState,
    OAAttachmentDispatchResult,
    OASyncSettings,
    SyncReport,
    SyncState,
)
