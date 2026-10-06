"""应用之间的调用契约."""

from kylin.ports.application.knowledge_gap import (
    EmptyKnowledgeGapAnswerError,
    KnowledgeGapIngestionError,
    KnowledgeGapManagementPort,
    KnowledgeGapPort,
    KnowledgeGapTicketClosedError,
    KnowledgeGapTicketNotFoundError,
    KnowledgeGapTicketOpenError,
    ResolveKnowledgeGapResult,
)
from kylin.ports.application.knowledge_ingestion import (
    IngestionProgressSink,
    KnowledgeIngestionPort,
)
from kylin.ports.application.oa_sync import (
    OAAttachmentDispatchPort,
    OASourcePort,
    OASyncStatePort,
)

__all__ = [
    "IngestionProgressSink",
    "EmptyKnowledgeGapAnswerError",
    "KnowledgeGapIngestionError",
    "KnowledgeGapPort",
    "KnowledgeGapManagementPort",
    "KnowledgeGapTicketClosedError",
    "KnowledgeGapTicketNotFoundError",
    "KnowledgeGapTicketOpenError",
    "ResolveKnowledgeGapResult",
    "KnowledgeIngestionPort",
    "OAAttachmentDispatchPort",
    "OASourcePort",
    "OASyncStatePort",
]
