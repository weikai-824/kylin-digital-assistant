"""知识问答的多查询、多知识库检索编排."""

from __future__ import annotations

import hashlib
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Sequence

from kylin.applications.knowledge_qa.query_plan.schema import QueryPlan
from kylin.ports.data.knowledge_search import KnowledgeDocument, KnowledgeSearchPort

logger = logging.getLogger(__name__)


def _document_key(document: KnowledgeDocument) -> str:
    """为融合稳定生成文档键，避免修改底层 metadata."""
    if document.document_id:
        return document.document_id
    for name in ("id", "doc_id", "child_id", "pk"):
        value = document.metadata.get(name)
        if value:
            return str(value)
    return hashlib.sha1(document.page_content.encode("utf-8")).hexdigest()


def _rrf_fuse(
    ranked_lists: Sequence[Sequence[KnowledgeDocument]],
    *,
    rrf_k: int,
) -> list[KnowledgeDocument]:
    scores: dict[str, float] = {}
    representative: dict[str, KnowledgeDocument] = {}
    for ranked in ranked_lists:
        for rank, document in enumerate(ranked, start=1):
            key = _document_key(document)
            if document.metadata.get("_kb_name"):
                key = f"{document.metadata['_kb_name']}:{key}"
            scores[key] = scores.get(key, 0.0) + 1.0 / (rrf_k + rank)
            representative.setdefault(key, document)
    ordered = sorted(scores, key=lambda key: scores[key], reverse=True)
    return [representative[key].with_score(scores[key]) for key in ordered]


class KnowledgeRetriever:
    """通过 ``KnowledgeSearchPort`` 执行并行子查询及跨库 RRF 融合."""

    def __init__(self, search_port: KnowledgeSearchPort, *, rrf_k: int = 60):
        if rrf_k <= 0:
            raise ValueError("rrf_k must be greater than zero")
        self._search_port = search_port
        self._rrf_k = rrf_k

    @staticmethod
    def knowledge_base_names(
        formal_knowledge_base: str,
        ticket_knowledge_base: str = "",
    ) -> list[str]:
        """返回去重后的正式库与工单库名称."""
        return list(dict.fromkeys(
            name for name in (formal_knowledge_base, ticket_knowledge_base) if name
        ))

    def search(
        self,
        plan: QueryPlan,
        *,
        knowledge_base_name: str,
        top_k: int,
        score_threshold: float,
        dept: str | None = None,
        dept_exempt: bool = False,
    ) -> list[KnowledgeDocument]:
        """执行单库计划；闲聊计划不访问检索端口.

        子问均无命中时，若尚未单独搜过 standalone，则再兜底检索一次。
        """
        if not plan.need_retrieval:
            return []
        queries = plan.normalized_sub_queries()
        if not queries:
            return []
        documents = self._search_queries(
            queries,
            knowledge_base_name=knowledge_base_name,
            top_k=top_k,
            score_threshold=score_threshold,
            dept=dept,
            dept_exempt=dept_exempt,
        )
        if documents:
            return documents

        fallback = (plan.standalone_query or "").strip()
        if not fallback:
            return []
        # 仅当并行子问集合不是「只搜过这一句」时再兜底，避免重复 RTT。
        if len(queries) == 1 and queries[0] == fallback:
            return []
        logger.info(
            "子问均无命中，兜底检索 kb=%s standalone=%r tried=%s",
            knowledge_base_name,
            fallback,
            queries,
        )
        return list(self._search_port.search(
            fallback,
            knowledge_base_name=knowledge_base_name,
            top_k=top_k,
            score_threshold=score_threshold,
            dept=dept,
            dept_exempt=dept_exempt,
        ))

    def _search_queries(
        self,
        queries: Sequence[str],
        *,
        knowledge_base_name: str,
        top_k: int,
        score_threshold: float,
        dept: str | None = None,
        dept_exempt: bool = False,
    ) -> list[KnowledgeDocument]:
        if len(queries) == 1:
            return list(self._search_port.search(
                queries[0],
                knowledge_base_name=knowledge_base_name,
                top_k=top_k,
                score_threshold=score_threshold,
                dept=dept,
                dept_exempt=dept_exempt,
            ))
        return self._search_parallel(
            queries,
            knowledge_base_name=knowledge_base_name,
            top_k=top_k,
            score_threshold=score_threshold,
            dept=dept,
            dept_exempt=dept_exempt,
        )

    def _search_parallel(
        self,
        queries: Sequence[str],
        *,
        knowledge_base_name: str,
        top_k: int,
        score_threshold: float,
        dept: str | None = None,
        dept_exempt: bool = False,
    ) -> list[KnowledgeDocument]:
        lists: list[Sequence[KnowledgeDocument] | None] = [None] * len(queries)
        with ThreadPoolExecutor(max_workers=min(len(queries), 4)) as executor:
            futures = [executor.submit(
                self._search_port.search,
                query,
                knowledge_base_name=knowledge_base_name,
                top_k=top_k,
                score_threshold=score_threshold,
                dept=dept,
                dept_exempt=dept_exempt,
            ) for query in queries]
            future_indices = {future: index for index, future in enumerate(futures)}
            for future in as_completed(futures):
                index = future_indices[future]
                try:
                    lists[index] = future.result()
                except Exception as error:
                    logger.warning("子查询检索失败，忽略该分支: %s", error)
                    lists[index] = []
        return _rrf_fuse([items or [] for items in lists], rrf_k=self._rrf_k)[:top_k]

    def search_across_kbs(
        self,
        plan: QueryPlan,
        *,
        knowledge_base_names: Sequence[str],
        top_k: int,
        score_threshold: float,
        dept: str | None = None,
        dept_exempt: bool = False,
    ) -> list[KnowledgeDocument]:
        """跨正式库和工单库检索；多库结果按文档键做 RRF 融合."""
        names = list(dict.fromkeys(name for name in knowledge_base_names if name))
        if not plan.need_retrieval or not names:
            return []
        if len(names) == 1:
            return self.search(
                plan,
                knowledge_base_name=names[0],
                top_k=top_k,
                score_threshold=score_threshold,
                dept=dept,
                dept_exempt=dept_exempt,
            )
        lists: list[tuple[str, Sequence[KnowledgeDocument]] | None] = [None] * len(names)
        with ThreadPoolExecutor(max_workers=min(len(names), 4)) as executor:
            futures = {
                executor.submit(
                    self.search,
                    plan,
                    knowledge_base_name=name,
                    top_k=top_k,
                    score_threshold=score_threshold,
                    dept=dept,
                    dept_exempt=dept_exempt,
                ): name for name in names
            }
            name_indices = {name: index for index, name in enumerate(names)}
            for future in as_completed(futures):
                name = futures[future]
                index = name_indices[name]
                try:
                    lists[index] = (name, future.result())
                except Exception as error:
                    logger.warning("知识库 %s 检索失败，忽略该库: %s", name, error)
                    lists[index] = (name, [])
        ordered_lists = [item for item in lists if item is not None]
        tagged_lists = [
            [document.with_metadata(_kb_name=name) for document in documents]
            for name, documents in ordered_lists
        ]
        non_empty = [documents for documents in tagged_lists if documents]
        if len(non_empty) == 1:
            return non_empty[0][:top_k]
        return _rrf_fuse(tagged_lists, rrf_k=self._rrf_k)[:top_k]
