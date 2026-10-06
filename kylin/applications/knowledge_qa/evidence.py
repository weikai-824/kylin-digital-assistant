"""知识问答的证据相关度判定规则."""

from __future__ import annotations

from typing import Optional, Sequence


def is_weak_evidence(
    top_rel: Optional[float],
    min_score: float,
    weak_thr: float,
) -> bool:
    """判断相关度是否达到硬门槛但仍处于弱证据区间."""
    return (
        top_rel is not None
        and weak_thr > min_score
        and min_score <= top_rel < weak_thr
    )


def top_relevance(docs: Sequence) -> Optional[float]:
    """返回首个检索结果的相关度；缺失或非法时返回 None."""
    if not docs:
        return None
    doc = docs[0]
    meta = (
        (doc.get("metadata") or {})
        if isinstance(doc, dict)
        else (getattr(doc, "metadata", None) or {})
    )
    try:
        return float(meta.get("relevance_score"))
    except (TypeError, ValueError):
        return None
