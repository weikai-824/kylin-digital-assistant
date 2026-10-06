"""知识缺口工单与反哺闭环应用."""

from kylin.applications.knowledge_gap.service import (
    CLOSED,
    OPEN,
    RESOLVED,
    KnowledgeGapService,
    build_feedback_content,
)
from kylin.ports.application.knowledge_gap import (
    EmptyKnowledgeGapAnswerError,
    KnowledgeGapIngestionError,
    KnowledgeGapTicketClosedError,
    KnowledgeGapTicketNotFoundError,
    KnowledgeGapTicketOpenError,
)

__all__ = [
    "CLOSED",
    "OPEN",
    "RESOLVED",
    "EmptyKnowledgeGapAnswerError",
    "KnowledgeGapIngestionError",
    "KnowledgeGapService",
    "KnowledgeGapTicketClosedError",
    "KnowledgeGapTicketNotFoundError",
    "KnowledgeGapTicketOpenError",
    "build_feedback_content",
]
