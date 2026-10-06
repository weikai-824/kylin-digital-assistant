"""双索引三阶段检索：Milvus 向量 + ES BM25 → RRF → Rerank → 父块回填。"""

from __future__ import annotations

import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Sequence, Tuple

from kylin.platform.runtime.documents import Document
from kylin.adapters.outbound.model.clients import get_Embeddings, get_default_embedding
from kylin.platform.config.settings import Settings
from kylin.platform.observability.logging import build_logger
from kylin.adapters.outbound.knowledge_base.index import (
    children_collection_name,
    children_index_name,
    get_es_client,
    get_parents_by_ids,
    milvus_connect,
)
from kylin.adapters.outbound.knowledge_base.index.stores import _milvus_fields
from kylin.adapters.outbound.knowledge_base.retrieve.errors import RetrievalBackendError
from kylin.adapters.outbound.knowledge_base.retrieve.rrf import doc_fusion_key, rrf_fuse

logger = build_logger()

_MILVUS_OUTPUT = (
    "pk",
    "text",
    "source",
    "oa_doc_id",
    "doc_id",
    "child_id",
    "title_path",
    "section_title",
    "content_type",
    "content_hash",
    "department_id",
    "access_depts",
)

# 同问多库/多子查询时复用向量，避免重复 embed RTT
_EMBED_CACHE_MAX = 64
_embed_cache: "OrderedDict[tuple[str, str], List[float]]" = OrderedDict()
_embed_cache_lock = threading.Lock()


def _cached_embed_query(embed_model: str | None, query: str) -> List[float]:
    """短生命周期 LRU：相同 (model, query) 复用 embedding 向量。"""
    key = (embed_model or "", query)
    with _embed_cache_lock:
        cached = _embed_cache.get(key)
        if cached is not None:
            _embed_cache.move_to_end(key)
            return cached
    vector = get_Embeddings(embed_model).embed_query(query)
    with _embed_cache_lock:
        _embed_cache[key] = vector
        _embed_cache.move_to_end(key)
        while len(_embed_cache) > _EMBED_CACHE_MAX:
            _embed_cache.popitem(last=False)
    return vector


# ---------- 部门过滤（权限访问范围） ----------

def _escape_expr(value: str) -> str:
    """转义 Milvus expr 字符串字面量中的反斜杠与双引号。"""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def build_department_filter(dept: str | None, *, dept_exempt: bool = False) -> str | None:
    """构造 Milvus expr 访问范围过滤条件（基于 access_depts）。

    语义：
    - dept_exempt=True：豁免 → 不过滤（返回 None，看全部）
    - dept 为空（匿名）：只保留全开放文档（access_depts == ""）
    - dept 非空（部门用户）：全开放 + access_depts 含本部门
      （access_depts 序列化为 `;D1;D2;`，like "%;dept;%" 精确匹配）
    """
    if dept_exempt:
        return None
    dept = (dept or "").strip()
    if not dept:
        return 'access_depts == ""'
    return (
        f'access_depts == "" or access_depts like "%{_escape_expr(dept)}%"'
    )


def build_department_es_filter(dept: str | None, *, dept_exempt: bool = False) -> list[dict] | None:
    """构造 ES bool filter 子句（基于 access_depts keyword 数组，与 Milvus 同语义）。"""
    if dept_exempt:
        return None
    dept = (dept or "").strip()
    if not dept:
        return [{"bool": {"must_not": {"exists": {"field": "access_depts"}}}}]
    return [
        {"bool": {"should": [
            {"bool": {"must_not": {"exists": {"field": "access_depts"}}}},
            {"term": {"access_depts": dept}},
        ], "minimum_should_match": 1}}
    ]


def dual_search(
    kb_name: str,
    query: str,
    *,
    top_k: int = None,
    score_threshold: float = None,
    embed_model: str = None,
    recall_top_k: int = None,
    rerank_candidate_top_k: int = None,
    rrf_k: int = None,
    dept: str | None = None,
    dept_exempt: bool = False,
) -> List[Document]:
    """
    标准三阶段 + 父块回填（Rerank 为必要阶段；不可用时按无证据拒识）。

    1) Milvus / ES 各召回 recall_top_k 子块（并行）
    2) RRF 融合后截到 rerank_candidate_top_k
    3) Cross-Encoder 对全部候选重排；低于 RERANK_SCORE_THRESHOLD 则拒识为空
    4) 按 doc_id 回填父块并去重保序，截到 top_k 父块（父块感知截断）

    dept: 用户部门 ID；dept_exempt: 是否豁免部门过滤（见 build_department_filter）。
    """
    trace = dual_search_trace(
        kb_name,
        query,
        top_k=top_k,
        score_threshold=score_threshold,
        embed_model=embed_model,
        recall_top_k=recall_top_k,
        rerank_candidate_top_k=rerank_candidate_top_k,
        rrf_k=rrf_k,
        stage_parents=False,
        dept=dept,
        dept_exempt=dept_exempt,
    )
    return list(trace.get("final") or [])


def dual_search_trace(
    kb_name: str,
    query: str,
    *,
    top_k: int = None,
    score_threshold: float = None,
    embed_model: str = None,
    recall_top_k: int = None,
    rerank_candidate_top_k: int = None,
    rrf_k: int = None,
    stage_parents: bool = True,
    dept: str | None = None,
    dept_exempt: bool = False,
) -> Dict[str, Any]:
    """
    与 dual_search 同链路，额外返回分阶段结果供评估归因。

    返回键：
    - dense / sparse：单路召回后父块展开（top_k）；stage_parents=False 时为空列表
    - rrf：RRF 融合后父块展开；stage_parents=False 时为空列表
    - rerank：Rerank 后父块展开
    - final：与 dual_search 相同
    - meta：参数与子块数量等

    stage_parents:
        True（评估默认）保留各阶段父块展开；False（生产 dual_search）只在最终
        rerank 后展开一次，避免多余 ES mget。
    """
    query = (query or "").strip()
    empty = {
        "dense": [],
        "sparse": [],
        "rrf": [],
        "rerank": [],
        "final": [],
        "meta": {"query": query, "empty": True},
    }
    if not query:
        return empty

    top_k = int(top_k if top_k is not None else Settings.kb_settings.VECTOR_SEARCH_TOP_K)
    score_threshold = float(
        score_threshold
        if score_threshold is not None
        else Settings.kb_settings.SCORE_THRESHOLD
    )
    recall_top_k = int(
        recall_top_k
        if recall_top_k is not None
        else getattr(Settings.kb_settings, "HYBRID_RECALL_TOP_K", 30)
    )
    rerank_candidate_top_k = int(
        rerank_candidate_top_k
        if rerank_candidate_top_k is not None
        else Settings.kb_settings.RERANK_CANDIDATE_TOP_K
    )
    rrf_k = int(rrf_k if rrf_k is not None else getattr(Settings.kb_settings, "RRF_K", 60))
    embed_model = embed_model or get_default_embedding()

    top_k = max(1, top_k)
    recall_top_k = max(top_k, recall_top_k)
    rerank_candidate_top_k = max(top_k, min(rerank_candidate_top_k, recall_top_k * 2))

    dense_hits, sparse_hits = _parallel_recall(
        kb_name=kb_name,
        query=query,
        recall_top_k=recall_top_k,
        # 公开参数 score_threshold 在 Milvus 路径按 COSINE 距离上限透传；
        # ES 路径不使用该阈值（BM25 分数另由 Rerank 阶段把关）。
        distance_threshold=score_threshold,
        embed_model=embed_model,
        dept=dept,
        dept_exempt=dept_exempt,
    )
    # 父块感知截断：从完整子块列表按序去重扩到 top_k 个父块，
    # 避免「同父多子占满 top_k 子槽」导致正确父块被挤掉。
    dense_parents = (
        _expand_parents(kb_name, dense_hits, top_k=top_k) if stage_parents else []
    )
    sparse_parents = (
        _expand_parents(kb_name, sparse_hits, top_k=top_k) if stage_parents else []
    )

    if not dense_hits and not sparse_hits:
        return {
            "dense": dense_parents,
            "sparse": sparse_parents,
            "rrf": [],
            "rerank": [],
            "final": [],
            "meta": {
                "query": query,
                "empty": True,
                "top_k": top_k,
                "recall_top_k": recall_top_k,
                "rerank_candidate_top_k": rerank_candidate_top_k,
                "rrf_k": rrf_k,
                "n_dense_children": 0,
                "n_sparse_children": 0,
            },
        }

    fused = rrf_fuse(
        [dense_hits, sparse_hits],
        id_fn=doc_fusion_key,
        k=rrf_k,
    )
    candidates: List[Document] = []
    for doc, score in fused[:rerank_candidate_top_k]:
        meta = dict(doc.metadata or {})
        # relevance_score 只允许来自本轮 Rerank，避免旧元数据或 RRF 分数
        # 被误当成可用于拒识的语义相关度。
        meta.pop("relevance_score", None)
        meta["score"] = float(score)
        meta["rrf_score"] = float(score)
        meta["id"] = meta.get("child_id") or meta.get("id") or ""
        candidates.append(Document(page_content=doc.page_content, metadata=meta))

    rrf_parents = (
        _expand_parents(kb_name, candidates, top_k=top_k) if stage_parents else []
    )
    # 对全部候选重排，再按父块去重截断（而非只取 top_k 子块）
    reranked_children, rerank_ok = _rerank_children(
        query, candidates, top_n=len(candidates)
    )
    min_score = float(
        getattr(Settings.kb_settings, "RERANK_SCORE_THRESHOLD", 0.0) or 0.0
    )
    weak_threshold = float(
        getattr(Settings.kb_settings, "RERANK_WEAK_THRESHOLD", 0.0) or 0.0
    )
    top_rel = _rerank_relevance(reranked_children[0]) if reranked_children else None
    rerank_degraded = not rerank_ok
    rejected_low_score = (
        top_rel is not None and min_score > 0 and top_rel < min_score
    )
    # 企业知识问答优先保证有依据：Rerank 是必要证据门。服务不可用或
    # 返回结果缺少 relevance_score 时，不使用量纲不同的 RRF 分数冒充置信度。
    rejected = rerank_degraded or rejected_low_score
    reject_reason = None
    if rerank_degraded:
        reject_reason = "rerank_unavailable"
        logger.error(
            "Rerank 不可用或未返回 relevance_score，按无证据拒识 kb=%s",
            kb_name,
        )
    elif rejected_low_score:
        reject_reason = "low_relevance"
    if rejected:
        rerank_parents: List[Document] = []
        final: List[Document] = []
    else:
        rerank_parents = _expand_parents(kb_name, reranked_children, top_k=top_k)
        final = rerank_parents

    return {
        "dense": dense_parents,
        "sparse": sparse_parents,
        "rrf": rrf_parents,
        "rerank": rerank_parents,
        "final": final,
        "meta": {
            "query": query,
            "empty": rejected or not final,
            "rejected_low_score": rejected_low_score,
            "rerank_ok": rerank_ok,
            "rerank_degraded": rerank_degraded,
            "reject_reason": reject_reason,
            "top_relevance": round(top_rel, 4) if top_rel is not None else None,
            "rerank_score_threshold": min_score,
            "rerank_weak_threshold": weak_threshold,
            "top_k": top_k,
            "recall_top_k": recall_top_k,
            "rerank_candidate_top_k": rerank_candidate_top_k,
            "rrf_k": rrf_k,
            "n_dense_children": len(dense_hits),
            "n_sparse_children": len(sparse_hits),
            "n_rrf_candidates": len(candidates),
            "stage_parents": stage_parents,
        },
    }


def _rerank_relevance(doc: Document) -> Optional[float]:
    """读取真实 Rerank 相关度；缺失或非法时返回 None。"""
    meta = doc.metadata or {}
    val = meta.get("relevance_score")
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _parallel_recall(
    *,
    kb_name: str,
    query: str,
    recall_top_k: int,
    distance_threshold: float,
    embed_model: str,
    dept: str | None = None,
    dept_exempt: bool = False,
) -> Tuple[List[Document], List[Document]]:
    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_dense = pool.submit(
            search_milvus_children,
            kb_name,
            query,
            top_k=recall_top_k,
            distance_threshold=distance_threshold,
            embed_model=embed_model,
            dept=dept,
            dept_exempt=dept_exempt,
        )
        fut_sparse = pool.submit(
            search_es_children,
            kb_name,
            query,
            top_k=recall_top_k,
            dept=dept,
            dept_exempt=dept_exempt,
        )
        errors = []
        dense, sparse = [], []
        for name, fut in (("milvus", fut_dense), ("es", fut_sparse)):
            try:
                if name == "milvus":
                    dense = fut.result()
                else:
                    sparse = fut.result()
            except RetrievalBackendError as e:
                errors.append(e)
    if errors:
        # 一路故障：降级用另一路（记 warning，保持可观测）；两路全故障：明确抛异常
        if len(errors) == 2:
            raise RetrievalBackendError(
                "milvus+es",
                kb_name,
                "; ".join(str(e) for e in errors),
            ) from errors[0]
        logger.warning(
            "单路召回故障，降级为另一路 kb=%s: %s", kb_name, errors[0]
        )
    logger.info(
        f"双路召回 kb={kb_name}: milvus={len(dense)} es={len(sparse)} recall_k={recall_top_k}"
    )
    return dense, sparse


def search_milvus_children(
    kb_name: str,
    query: str,
    *,
    top_k: int,
    distance_threshold: float,
    embed_model: str = None,
    dept: str | None = None,
    dept_exempt: bool = False,
) -> List[Document]:
    """Milvus COSINE 向量召回子块。distance 越小越相似；超过 distance_threshold 丢弃。

    注意：`distance_threshold` 是 COSINE 距离上限（值越小越严格，范围 [0,2]），
    与 ES 的 BM25 分数（越大越相关）语义相反——不要把两者当成同一把尺子。
    上层公开参数 `score_threshold` 在 Milvus 路径即按本距离语义透传。

    dept: 用户部门 ID；dept_exempt: 是否豁免过滤（见 build_department_filter）。
    """
    from pymilvus import Collection, utility

    alias = milvus_connect()
    name = children_collection_name(kb_name)
    if not utility.has_collection(name, using=alias):
        logger.warning(f"Milvus collection 不存在: {name}")
        return []

    vector = _cached_embed_query(embed_model, query)
    collection = Collection(name, using=alias)
    collection.load()

    # 旧 collection（加 access_depts 前创建）没有该字段 → 无访问控制，不过滤（默认全开放）
    expr = None
    schema_fields = _milvus_fields(collection)
    if not dept_exempt:
        if schema_fields is not None and "access_depts" in schema_fields:
            expr = build_department_filter(dept, dept_exempt=False)
        # schema_fields 为 None（探测失败）时保守不设过滤，避免误伤
    # output_fields 同样按实际 schema 裁剪：请求 collection 不存在的字段会让
    # 整条 Milvus 检索抛错 → 降级只用 ES（历史踩坑：加 department_id/access_depts
    # 字段后存量 collection 缺字段，检索静默降级导致低分拒答）。
    output_fields = list(_MILVUS_OUTPUT)
    if schema_fields is not None:
        output_fields = [f for f in output_fields if f in schema_fields]
    search_kwargs: Dict[str, Any] = {
        "data": [vector],
        "anns_field": "embedding",
        "param": {"metric_type": "COSINE", "params": {"ef": max(64, top_k)}},
        "limit": top_k,
        "output_fields": output_fields,
    }
    if expr:
        search_kwargs["expr"] = expr

    try:
        results = collection.search(**search_kwargs)
    except Exception as e:
        # 故障 ≠ 无结果：抛类型化异常，由上层映射为「检索服务暂不可用」
        raise RetrievalBackendError(
            "milvus", kb_name, f"{type(e).__name__}: {e}"
        ) from e

    docs: List[Document] = []
    for hits in results or []:
        for hit in hits:
            # Milvus COSINE 返回的是「相似度」（越大越相似，约 [-1,1]），不是距离。
            # 上层 score_threshold 语义为「距离上限，越小越严，[0,2]，2.0=不筛」，
            # 故先换算 distance = 1 - similarity（∈ [0,2]）再套用同一判断，
            # 否则 threshold < 1 时会把最相关（相似度高）的结果全部误杀。
            similarity = float(hit.distance)
            cosine_distance = 1.0 - similarity
            if (
                distance_threshold is not None
                and cosine_distance > distance_threshold
            ):
                continue
            entity = getattr(hit, "entity", None)
            meta = _entity_meta(entity) if entity is not None else {}
            if not meta:
                # 不同 pymilvus 版本的 hit 字段访问方式不同，逐个兜底
                for key in _MILVUS_OUTPUT:
                    if key == "text":
                        continue
                    try:
                        val = hit.get(key) if hasattr(hit, "get") else None
                    except Exception:
                        val = None
                    if val is not None:
                        meta[key] = val
            cid = str(meta.get("child_id") or meta.get("pk") or getattr(hit, "id", "") or "")
            meta["child_id"] = cid
            meta["id"] = cid
            meta["pk"] = str(meta.get("pk") or cid)
            meta["vector_distance"] = cosine_distance
            meta["score"] = similarity
            text = ""
            if entity is not None:
                try:
                    text = entity.get("text") or ""
                except Exception:
                    text = ""
            if not text and hasattr(hit, "get"):
                try:
                    text = hit.get("text") or ""
                except Exception:
                    text = ""
            docs.append(Document(page_content=text, metadata=meta))
    return docs


def search_es_children(
    kb_name: str,
    query: str,
    *,
    top_k: int,
    dept: str | None = None,
    dept_exempt: bool = False,
) -> List[Document]:
    """ES BM25 关键词召回子块。

    dept: 用户部门 ID；dept_exempt: 是否豁免过滤（见 build_department_es_filter）。
    """
    es = get_es_client()
    index = children_index_name(kb_name)
    if not es.indices.exists(index=index):
        logger.warning(f"ES 子块索引不存在: {index}")
        return []

    match_query = {
        "multi_match": {
            "query": query,
            "fields": [
                "text^3",
                "source^2.5",
                "title_path^2",
                "section_title^2",
                "doc_title^1.5",
            ],
            "type": "best_fields",
        }
    }
    filters = build_department_es_filter(dept, dept_exempt=dept_exempt)
    if filters:
        query_body = {"bool": {"must": [match_query], "filter": filters}}
    else:
        query_body = match_query

    body = {
        "size": top_k,
        "query": query_body,
    }
    try:
        resp = es.search(index=index, body=body)
    except TypeError:
        # elasticsearch-py 新版本用 query= 等关键字
        try:
            resp = es.search(
                index=index,
                size=top_k,
                query=body["query"],
            )
        except Exception as e:
            raise RetrievalBackendError(
                "elasticsearch", kb_name, f"{type(e).__name__}: {e}"
            ) from e
    except Exception as e:
        raise RetrievalBackendError(
            "elasticsearch", kb_name, f"{type(e).__name__}: {e}"
        ) from e

    docs: List[Document] = []
    for hit in (resp.get("hits") or {}).get("hits") or []:
        src = dict(hit.get("_source") or {})
        text = src.pop("text", "") or ""
        cid = str(src.get("child_id") or hit.get("_id") or "")
        src["child_id"] = cid
        src["id"] = cid
        src["bm25_score"] = float(hit.get("_score") or 0.0)
        src["score"] = float(hit.get("_score") or 0.0)
        docs.append(Document(page_content=text, metadata=src))
    return docs


def _entity_meta(entity) -> Dict[str, Any]:
    meta: Dict[str, Any] = {}
    for key in _MILVUS_OUTPUT:
        if key == "text":
            continue
        try:
            val = entity.get(key)
        except Exception:
            val = None
        if val is not None:
            meta[key] = val
    return meta


def _rerank_children(
    query: str, docs: List[Document], top_n: int
) -> Tuple[List[Document], bool]:
    """重排子块，并显式返回是否取得了真实 Rerank 相关度。"""
    from kylin.adapters.outbound.knowledge_base.retrieve.rerank import rerank_documents

    payload = [
        {
            "page_content": d.page_content,
            "metadata": dict(d.metadata or {}),
            "score": (d.metadata or {}).get("score"),
        }
        for d in docs
    ]
    reranked = rerank_documents(query, payload, top_n=top_n)
    out: List[Document] = []
    for item in reranked:
        meta = dict(item.get("metadata") or {})
        relevance = _rerank_relevance(
            Document(page_content="", metadata=meta)
        )
        if relevance is not None:
            meta["score"] = relevance
            meta["relevance_score"] = relevance
        out.append(
            Document(page_content=item.get("page_content") or "", metadata=meta)
        )
    rerank_ok = bool(out) and _rerank_relevance(out[0]) is not None
    return out, rerank_ok


def _expand_parents(
    kb_name: str,
    children: Sequence[Document],
    *,
    top_k: int,
) -> List[Document]:
    """子块命中 → 按 doc_id 取父块；无父块则回退子块。保序去重。"""
    doc_ids: List[str] = []
    for child in children:
        did = str((child.metadata or {}).get("doc_id") or "")
        if did and did not in doc_ids:
            doc_ids.append(did)
    parents_map = get_parents_by_ids(kb_name, doc_ids) if doc_ids else {}

    results: List[Document] = []
    seen: set = set()
    for child in children:
        if len(results) >= top_k:
            break
        meta_c = dict(child.metadata or {})
        doc_id = str(meta_c.get("doc_id") or "")
        key = doc_id or doc_fusion_key(child)
        if key in seen:
            continue
        seen.add(key)

        parent = parents_map.get(doc_id) if doc_id else None
        if parent is not None:
            meta = dict(parent.metadata or {})
            meta["id"] = doc_id
            meta["doc_id"] = doc_id
            meta["score"] = meta_c.get("score")
            meta["rrf_score"] = meta_c.get("rrf_score")
            meta["relevance_score"] = meta_c.get("relevance_score")
            meta["hit_child_id"] = meta_c.get("child_id")
            meta["retrieve_level"] = "parent"
            results.append(Document(page_content=parent.page_content, metadata=meta))
        else:
            meta_c["id"] = meta_c.get("child_id") or meta_c.get("id") or ""
            meta_c["retrieve_level"] = "child"
            results.append(Document(page_content=child.page_content, metadata=meta_c))

    return results
