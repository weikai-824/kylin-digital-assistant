"""Reciprocal Rank Fusion：按排名融合多路召回，不依赖分数量纲。"""

from __future__ import annotations

from typing import Dict, Hashable, List, Sequence, Tuple, TypeVar

T = TypeVar("T")


def doc_fusion_key(doc) -> str:
    """统一 RRF / 去重键（dict 或 Document 均可）。

    优先级：显式 id → doc_id → child_id → pk；全空时内容哈希兜底，
    避免 id 缺失时 RRF 塌缩成一条。dual_search 与 query_plan 共用，
    保证同批文档的融合去重键一致。
    """
    if isinstance(doc, dict):
        meta = doc.get("metadata") or {}
        explicit = doc.get("id")
    else:
        meta = getattr(doc, "metadata", None) or {}
        explicit = None
    for key in ("id", "doc_id", "child_id", "pk"):
        v = explicit if key == "id" and explicit else meta.get(key)
        if v:
            return str(v)
    content = (
        doc.get("page_content")
        if isinstance(doc, dict)
        else getattr(doc, "page_content", "")
    )
    return f"content:{hash(content or '')}"


def rrf_fuse(
    ranked_lists: Sequence[Sequence[T]],
    *,
    id_fn=None,
    k: int = 60,
    weights: Sequence[float] | None = None,
) -> List[Tuple[T, float]]:
    """
    对多路已排序结果做 RRF 融合。

    score(d) = Σ_i weight_i / (k + rank_i(d))，rank 从 1 起。
    同一 id 只保留首次出现的对象实例；返回按 score 降序的 (item, score)。
    """
    if not ranked_lists:
        return []
    if id_fn is None:
        id_fn = lambda x: x  # noqa: E731
    if weights is None:
        weights = [1.0] * len(ranked_lists)
    if len(weights) != len(ranked_lists):
        raise ValueError("weights 长度必须与 ranked_lists 一致")

    scores: Dict[Hashable, float] = {}
    first: Dict[Hashable, T] = {}

    for weight, ranked in zip(weights, ranked_lists):
        w = float(weight)
        if w == 0:
            continue
        for rank, item in enumerate(ranked, start=1):
            key = id_fn(item)
            if key not in first:
                first[key] = item
            scores[key] = scores.get(key, 0.0) + w / (k + rank)

    fused = [(first[key], score) for key, score in scores.items()]
    fused.sort(key=lambda x: x[1], reverse=True)
    return fused
