"""注册全部 ORM 模型，供 create_all / 迁移脚本使用。"""

from kylin.adapters.outbound.mysql.database.models.conversation_session_model import ConversationSessionModel  # noqa: F401
from kylin.adapters.outbound.mysql.database.models.knowledge_base_model import KnowledgeBaseModel  # noqa: F401
from kylin.adapters.outbound.mysql.database.models.knowledge_file_model import FileDocModel, KnowledgeFileModel  # noqa: F401
from kylin.adapters.outbound.mysql.database.models.knowledge_gap_ticket_model import KnowledgeGapTicketModel  # noqa: F401
from kylin.adapters.outbound.mysql.database.models.long_term_memory_model import LongTermMemoryModel  # noqa: F401

__all__ = [
    "ConversationSessionModel",
    "KnowledgeBaseModel",
    "KnowledgeFileModel",
    "FileDocModel",
    "KnowledgeGapTicketModel",
    "LongTermMemoryModel",
]
