"""检索服务层：知识库检索 / 列表的唯一服务入口。

背景：此前 `retrieve/search.py:search_docs` 是 FastAPI 路由函数（Body 注解），
却被 query_plan 当内部服务调用（双身份，核心层绑定 HTTP）。本模块提供纯服务
函数 search_kb；路由层（search.py）只做参数绑定与转发，query_plan 直接调本服务。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from kylin.adapters.outbound.knowledge_base.kb_service.factory import KBServiceFactory
from kylin.adapters.outbound.knowledge_base.kb_service.models import DocumentWithVSId


def search_kb(
    query: str = "",
    knowledge_base_name: str = "",
    top_k: int = None,
    score_threshold: float = None,
    file_name: str = "",
    metadata: Optional[Dict[str, Any]] = None,
    dept: Optional[str] = None,
    dept_exempt: bool = False,
) -> List[Dict[str, Any]]:
    """在指定知识库检索（双索引三阶段）或按文件名/metadata 列出。

    - query 非空：走 KBService.search_docs（→ dual_search 三阶段），返回父块 dict
    - query 为空且 file_name/metadata 非空：按台账列出子块文档
    - dept：用户部门 ID；dept_exempt：是否豁免部门过滤（见 dual_search）
    - 返回与旧 search_docs 完全一致的 dict 列表（DocumentWithVSId.dict()）
    """
    kb = KBServiceFactory.get_service_by_name(knowledge_base_name)
    if kb is None:
        return []
    if query:
        docs = kb.search_docs(query, top_k, score_threshold, dept=dept, dept_exempt=dept_exempt)
        data: List[Dict[str, Any]] = []
        for x in docs:
            meta = dict(getattr(x, "metadata", None) or {})
            doc_id = (
                meta.get("id")
                or meta.get("doc_id")
                or meta.get("child_id")
                or ""
            )
            score = meta.get("score", meta.get("relevance_score", 3.0))
            try:
                score = float(score)
            except (TypeError, ValueError):
                score = 3.0
            data.append(
                DocumentWithVSId(
                    **{
                        **x.dict(),
                        "id": doc_id,
                        "score": score,
                    }
                )
            )
        return [x.dict() for x in data]

    if file_name or metadata:
        data = kb.list_docs(file_name=file_name, metadata=metadata or {})
        for d in data:
            if "vector" in d.metadata:
                del d.metadata["vector"]
        return [x.dict() for x in data]

    return []
