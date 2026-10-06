"""知识缺口组合根：组装仓储、统一入库和联合检索验证."""

from __future__ import annotations

import logging
import os

from kylin.adapters.outbound.data.knowledge_search import KylinKnowledgeSearchAdapter
from kylin.adapters.outbound.mysql.knowledge_gap import MySQLKnowledgeGapRepository
from kylin.applications.knowledge_gap import KnowledgeGapService
from kylin.applications.knowledge_qa.query_plan import QueryPlan
from kylin.applications.knowledge_qa.retrieval import KnowledgeRetriever
from kylin.bootstrap.knowledge_ingestion import build_knowledge_ingestion_service
from kylin.platform.config.settings import Settings

logger = logging.getLogger(__name__)


class KnowledgeGapRetrieverVerifier:
    """通过现有多库检索器验证人工答案是否可召回."""

    def __init__(
        self,
        *,
        retriever: KnowledgeRetriever,
        top_k: int,
        score_threshold: float,
    ):
        self._retriever = retriever
        self._top_k = top_k
        self._score_threshold = score_threshold

    def verify(
        self,
        question: str,
        *,
        formal_knowledge_base: str,
        ticket_knowledge_base: str,
    ) -> bool | None:
        normalized = (question or "").strip()
        if not normalized:
            return False
        names = self._retriever.knowledge_base_names(
            formal_knowledge_base.strip(),
            ticket_knowledge_base.strip(),
        )
        if not names:
            return None
        plan = QueryPlan(standalone_query=normalized, sub_queries=[normalized])
        try:
            documents = self._retriever.search_across_kbs(
                plan,
                knowledge_base_names=names,
                top_k=self._top_k,
                score_threshold=self._score_threshold,
                # 工单验证是系统内部操作，需看全量（不按部门过滤）
                dept_exempt=True,
            )
        except Exception as error:
            logger.error("验证重查失败: %s | q=%s", error, normalized[:50])
            return None
        return bool(documents)


def get_ticket_knowledge_base_name() -> str:
    """解析环境变量优先的工单知识库名称."""
    configured = os.environ.get("TICKET_KB_NAME")
    if configured is not None:
        return configured.strip()
    return Settings.kb_settings.TICKET_KB_NAME.strip()


def build_knowledge_gap_service() -> KnowledgeGapService:
    """构造一次完整的知识缺口应用服务."""
    search = KylinKnowledgeSearchAdapter()
    verifier = KnowledgeGapRetrieverVerifier(
        retriever=KnowledgeRetriever(search, rrf_k=Settings.kb_settings.RRF_K),
        top_k=Settings.kb_settings.VECTOR_SEARCH_TOP_K,
        score_threshold=Settings.kb_settings.SCORE_THRESHOLD,
    )
    return KnowledgeGapService(
        repository=MySQLKnowledgeGapRepository(),
        ingestion=build_knowledge_ingestion_service(),
        verifier=verifier,
        ticket_knowledge_base=get_ticket_knowledge_base_name(),
        default_knowledge_base=Settings.kb_settings.DEFAULT_KNOWLEDGE_BASE,
        retry_delay=int(os.environ.get("TICKET_VERIFY_RETRY_DELAY", "300")),
    )
