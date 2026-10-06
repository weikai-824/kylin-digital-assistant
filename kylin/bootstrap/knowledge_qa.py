"""知识问答组合根：组装端口、适配器与应用服务."""

from __future__ import annotations

from kylin.adapters.outbound.data.conversation_memory import KylinConversationMemoryAdapter
from kylin.adapters.outbound.data.knowledge_search import KylinKnowledgeSearchAdapter
from kylin.adapters.outbound.data.long_term_memory import KylinLongTermMemoryAdapter
from kylin.adapters.outbound.model import KylinChatModelAdapter
from kylin.adapters.outbound.observability import build_trace_port
from kylin.applications.knowledge_qa.query_plan import QueryPlanner
from kylin.applications.knowledge_qa.retrieval import KnowledgeRetriever
from kylin.applications.knowledge_qa.service import KnowledgeQaService
from kylin.applications.long_term_memory.extractor import MemoryExtractor
from kylin.ports.application.knowledge_gap import KnowledgeGapPort
from kylin.platform.config.settings import Settings
from kylin.ports.data.conversation_memory import ConversationMemoryPort
from kylin.ports.data.knowledge_search import KnowledgeSearchPort
from kylin.ports.data.long_term_memory import LongTermMemoryPort
from kylin.ports.model.chat import ChatModelPort
from kylin.ports.observability.trace import TracePort


def build_knowledge_retriever(
    search_port: KnowledgeSearchPort | None = None,
) -> KnowledgeRetriever:
    """构造共享检索编排器，供问答与验证脚本复用."""
    return KnowledgeRetriever(
        search_port or KylinKnowledgeSearchAdapter(),
        rrf_k=Settings.kb_settings.RRF_K,
    )


def build_long_term_memory() -> LongTermMemoryPort | None:
    """按配置构造长期记忆适配器；未启用返回 None（问答链路完全不感知记忆）。"""
    if not getattr(Settings.memory_settings, "enabled", False):
        return None
    return KylinLongTermMemoryAdapter()


def build_knowledge_qa_service(
    *,
    knowledge_gap: KnowledgeGapPort | None = None,
    search_port: KnowledgeSearchPort | None = None,
    chat_model: ChatModelPort | None = None,
    memory: ConversationMemoryPort | None = None,
    long_term_memory: LongTermMemoryPort | None = None,
    trace: TracePort | None = None,
) -> KnowledgeQaService:
    """按当前配置构造一次完整的知识问答应用服务。

    所有依赖均可由组合根显式传入；未传入时才创建默认适配器。
    长期记忆受 memory_settings.enabled 控制：未启用时注入 None。
    Trace 受 langfuse_settings.enabled 控制：未启用时为 NoOp。
    """
    trace = trace if trace is not None else build_trace_port()
    chat_model = (
        chat_model
        if chat_model is not None
        else KylinChatModelAdapter(trace=trace)
    )
    search_port = search_port if search_port is not None else KylinKnowledgeSearchAdapter()
    memory = memory if memory is not None else KylinConversationMemoryAdapter()
    if long_term_memory is None:
        long_term_memory = build_long_term_memory()
    return KnowledgeQaService(
        planner=QueryPlanner(
            chat_model,
            history_max_turns=Settings.model_settings.HISTORY_LEN,
        ),
        retriever=build_knowledge_retriever(search_port),
        search_port=search_port,
        chat_model=chat_model,
        memory=memory,
        knowledge_gap=knowledge_gap,
        long_term_memory=long_term_memory,
        memory_extractor=MemoryExtractor(chat_model=chat_model),
        trace=trace,
    )
