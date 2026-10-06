"""检索评估任务指标：以父块 doc_id 是否落入金标集合为准。"""

from __future__ import annotations

from typing import Iterable, List, Sequence, Set


def parent_ids_from_docs(docs: Sequence) -> List[str]:
    """从检索 Document 列表提取有序、去重的父块 doc_id。"""
    ids: List[str] = []
    seen: Set[str] = set()
    for d in docs or []:
        meta = getattr(d, "metadata", None) or {}
        if isinstance(d, dict):
            meta = d.get("metadata") or d
        pid = str(meta.get("doc_id") or meta.get("id") or "").strip()
        if not pid or pid in seen:
            continue
        seen.add(pid)
        ids.append(pid)
    return ids


def hit_at_k(retrieved: Sequence[str], gold: Set[str], k: int) -> float:
    """top-k 内是否命中任一金标（0/1）。"""
    if not gold:
        return 0.0
    return 1.0 if any(x in gold for x in list(retrieved)[:k]) else 0.0


def hit_rate_at_k(
    retrieved_lists: Sequence[Sequence[str]],
    gold_lists: Sequence[Set[str]],
    k: int,
) -> float:
    """多题 Hit@k 均值。"""
    if not retrieved_lists:
        return 0.0
    scores = [
        hit_at_k(ret, gold, k)
        for ret, gold in zip(retrieved_lists, gold_lists)
        if gold
    ]
    return sum(scores) / len(scores) if scores else 0.0


def recall_at_k(retrieved: Sequence[str], gold: Set[str], k: int) -> float:
    """top-k 覆盖了多少比例的金标父块。"""
    if not gold:
        return 0.0
    got = set(list(retrieved)[:k]) & gold
    return len(got) / len(gold)


def mean_reciprocal_rank(retrieved: Sequence[str], gold: Set[str]) -> float:
    """第一个命中金标的位次倒数；未命中为 0。"""
    if not gold:
        return 0.0
    for i, pid in enumerate(retrieved, 1):
        if pid in gold:
            return 1.0 / i
    return 0.0


def metrics_at_ks(
    retrieved: Sequence[str],
    gold: Set[str],
    ks: Iterable[int] = (1, 3, 5),
) -> dict:
    """单题：对各 k 计算 hit / recall，以及 MRR。"""
    out = {"mrr": mean_reciprocal_rank(retrieved, gold)}
    for k in ks:
        out[f"hit@{k}"] = hit_at_k(retrieved, gold, k)
        out[f"recall@{k}"] = recall_at_k(retrieved, gold, k)
    return out
