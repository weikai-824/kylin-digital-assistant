"""将现有 KBService 检索实现适配到知识问答端口."""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping, Sequence
from typing import Any

from kylin.ports.data.knowledge_search import (
    KnowledgeBaseNotFoundError,
    KnowledgeBaseUnavailableError,
    KnowledgeDocument,
    KnowledgeSearchError,
)
from kylin.adapters.outbound.knowledge_base.kb_service.factory import KBServiceFactory
from kylin.adapters.outbound.knowledge_base.retrieve.service import search_kb

# ensure_ready 结果 TTL 缓存：
# OK TTL 600s：正常 Q&A 节奏（分钟级）内不重复打 embed，
# 避免 kb_chat 第一次请求每次都被 20s 的 embed 冷探拖死（R1，trace 反查印证）。
# FAIL TTL 30s：短暂故障下重试间隔可控，长了反而会无谓放过 KB 不可用的情况。
_READY_OK_TTL_SECONDS = 600.0
_READY_FAIL_TTL_SECONDS = 30.0
_ready_cache: dict[str, tuple[float, bool, str]] = {}
_ready_lock = threading.Lock()


class KylinKnowledgeSearchAdapter:
    """复用现有 KBService，不把其类型泄漏到应用层."""

    def exists(self, knowledge_base_name: str) -> bool:
        return KBServiceFactory.get_service_by_name(knowledge_base_name) is not None

    def ensure_ready(self, knowledge_base_name: str) -> None:
        # 命中缓存：直接按上次结论放行 / 拒绝，省掉每次问答前的网络探测
        with _ready_lock:
            entry = _ready_cache.get(knowledge_base_name)
            if entry is not None:
                checked_at, ok, reason = entry
                ttl = _READY_OK_TTL_SECONDS if ok else _READY_FAIL_TTL_SECONDS
                if time.time() - checked_at < ttl:
                    if not ok:
                        raise KnowledgeBaseUnavailableError(
                            reason or knowledge_base_name
                        )
                    return
        service = KBServiceFactory.get_service_by_name(knowledge_base_name)
        if service is None:
            # 知识库可能刚被创建，不存在的情况不缓存，避免误拦新建库
            raise KnowledgeBaseNotFoundError(knowledge_base_name)
        ok, reason = service.check_embed_model()
        with _ready_lock:
            _ready_cache[knowledge_base_name] = (time.time(), ok, reason or "")
        if not ok:
            raise KnowledgeBaseUnavailableError(reason or knowledge_base_name)

    @staticmethod
    def _to_document(item: object) -> KnowledgeDocument:
        # score 语义因来源而异，切勿跨源比较：
        # - Milvus 子块（search_milvus_children）：score = COSINE distance，越小越相似（范围 [0,2]）
        # - ES 子块（search_es_children）：score = BM25 分数，越大越相关
        # - Rerank 后父块：score = RRF 融合分，越大越相关；真实相关度见 metadata.relevance_score
        if isinstance(item, dict):
            metadata = dict(item.get("metadata") or {})
            content = item.get("page_content") or ""
            document_id = item.get("id") or metadata.get("id") or metadata.get("doc_id") or ""
            raw_score = item.get("score", item.get("relevance_score", metadata.get("score", 3.0)))
        else:
            metadata = dict(getattr(item, "metadata", None) or {})
            content = getattr(item, "page_content", "") or ""
            document_id = getattr(item, "id", "") or metadata.get("id", "")
            raw_score = getattr(item, "score", metadata.get("score", 3.0))
        try:
            score = float(raw_score)
        except (TypeError, ValueError):
            score = 3.0
        return KnowledgeDocument(
            page_content=str(content),
            metadata=metadata,
            document_id=str(document_id or ""),
            score=score,
        )

    def search(
        self,
        query: str,
        *,
        knowledge_base_name: str,
        top_k: int,
        score_threshold: float,
        file_name: str = "",
        metadata: Mapping[str, Any] | None = None,
        dept: str | None = None,
        dept_exempt: bool = False,
    ) -> Sequence[KnowledgeDocument]:
        try:
            raw_documents = search_kb(
                query,
                knowledge_base_name,
                top_k=top_k,
                score_threshold=score_threshold,
                file_name=file_name,
                metadata=dict(metadata or {}),
                dept=dept,
                dept_exempt=dept_exempt,
            )
        except Exception as error:
            raise KnowledgeSearchError("knowledge_base", knowledge_base_name, str(error)) from error
        return [self._to_document(item) for item in raw_documents]
