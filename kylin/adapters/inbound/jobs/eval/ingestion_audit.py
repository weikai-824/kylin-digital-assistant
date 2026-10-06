"""入库轻量间接审计：用检索金标反推解析/切块质量，不重解析、不重向量化。

原则：入库段（解析 → 切块 → 向量化 → 双写）没有人工金标，无法直接评中间产物；
这里只做两类可判定的间接检查：

1) snippet 包含率（snippet containment）
   检索金标里 relevant_parent_doc_ids 指向的父块，其 ES 全文应完整包含
   expected_snippet（去空白后子串判定）。若解析丢字、切块把原文拆碎/改写，
   该比率会直接掉。父块在 ES parents index，_id 即 doc_id。

2) 子块统计哨兵（chunk stats sentinels）
   扫 <kb>_children index：空文本子块、超长子块（疑似吞了多块）、
   单文档块数与块长分布。异常仅告警展示，不参与对比回归判定。

产物：data/eval/results/ingestion_audit.json（compare.py 读它并入对比报告）。
退出码：0 正常（含检出告警）；2 环境/数据问题。入库质量告警不置非零退出码——
它不是"本轮 vs 基线"的回归信号，避免误挡正常改动。

跑法（仓库根）：
    python -m kylin.adapters.inbound.jobs.eval.ingestion_audit
    python -m kylin.adapters.inbound.jobs.eval.ingestion_audit --kb oa_ma
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from kylin.adapters.inbound.jobs.eval.common import (
    default_results_dir,
    setup_sys_path as _setup_sys_path,
)
from kylin.adapters.inbound.jobs.eval.dataset import default_eval_path, load_eval_dataset

# 超长子块哨兵阈值（字符）：正常子块为可检索片段，远超此值说明疑似吞了多个块
DEFAULT_OVERLONG_CHARS = 8000
_SCROLL_SIZE = 1000


def _norm(text: str) -> str:
    """去全部空白（含换行/全角空格），用于子串判定。"""
    return re.sub(r"\s+", "", text or "")


# ---------------------------------------------------------------- snippet 包含率 -


def _audit_containment(
    kb_name: str,
    rows: Sequence[dict],
) -> Dict[str, Any]:
    """逐正例核对：金标 snippet 是否为父块 ES 全文的子串。"""
    from kylin.adapters.outbound.knowledge_base.index.stores import get_parents_by_ids

    checked = contained = missing = parent_missing = 0
    failures: List[dict] = []
    for row in rows:
        gold = [str(i) for i in (row.get("relevant_parent_doc_ids") or [])]
        snippet = (row.get("expected_snippet") or "").strip()
        if not gold or not snippet:
            continue
        checked += 1
        try:
            parents = get_parents_by_ids(kb_name, gold)
        except Exception as error:
            # ES 故障时整段结果不可信，交由调用方按异常处理（此处抛出让审计中止）
            raise
        if not parents:
            parent_missing += 1
            failures.append(
                {
                    "qid": row.get("qid"),
                    "query": row.get("query"),
                    "query_type": row.get("query_type"),
                    "parent_ids": gold,
                    "snippet": snippet,
                    "contained": None,
                    "reason": "金标父块不在 ES parents index（删库重导 / id 漂移？）",
                }
            )
            continue
        joined = _norm("".join(p.page_content for p in parents.values()))
        hit = _norm(snippet) in joined
        if hit:
            contained += 1
        else:
            missing += 1
            failures.append(
                {
                    "qid": row.get("qid"),
                    "query": row.get("query"),
                    "query_type": row.get("query_type"),
                    "parent_ids": gold,
                    "snippet": snippet,
                    "contained": False,
                    "reason": "snippet 不在父块 ES 全文中（解析丢字 / 切块改写 / 金标漂移？）",
                }
            )
    judged = contained + missing
    return {
        "n_checked": checked,
        "n_contained": contained,
        "n_missing": missing,
        "n_parent_missing": parent_missing,
        "containment_rate": round(contained / judged, 4) if judged else None,
        "failures": failures,
    }


# ---------------------------------------------------------------- 子块统计哨兵 ----


def _percentile(sorted_lens: List[int], p: float) -> Optional[int]:
    if not sorted_lens:
        return None
    idx = min(len(sorted_lens) - 1, int(len(sorted_lens) * p))
    return sorted_lens[idx]


def _audit_chunk_stats(kb_name: str, overlong_chars: int) -> Dict[str, Any]:
    """扫 children index：空块/超长块/块长分布/每父块块数。"""
    from kylin.adapters.outbound.knowledge_base.index.stores import (
        children_index_name,
        get_es_client,
        parents_index_name,
    )

    es = get_es_client()
    cidx, pidx = children_index_name(kb_name), parents_index_name(kb_name)
    if not es.indices.exists(index=cidx):
        raise FileNotFoundError(f"ES children index 不存在: {cidx}")
    total_parents = es.count(index=pidx)["count"] if es.indices.exists(index=pidx) else 0

    lens: List[int] = []
    empty = 0
    overlong = 0
    chunks_per_doc: Dict[str, int] = {}
    docs_with = set()
    scroll = es.search(
        index=cidx,
        scroll="1m",
        size=_SCROLL_SIZE,
        _source_includes=["text", "doc_id"],
    )
    try:
        while True:
            for hit in scroll["hits"]["hits"]:
                src = hit.get("_source") or {}
                text = src.get("text") or ""
                n = len(text)
                lens.append(n)
                if not text.strip():
                    empty += 1
                elif n > overlong_chars:
                    overlong += 1
                pid = str(src.get("doc_id") or "")
                if pid:
                    chunks_per_doc[pid] = chunks_per_doc.get(pid, 0) + 1
            if len(scroll["hits"]["hits"]) < _SCROLL_SIZE:
                break
            scroll = es.scroll(scroll_id=scroll["_scroll_id"], scroll="1m")
    finally:
        es.clear_scroll(scroll_id=scroll["_scroll_id"])
    total_children = len(lens)
    sorted_lens = sorted(lens)
    n_docs = len(chunks_per_doc)
    counts = sorted(chunks_per_doc.values()) if chunks_per_doc else []
    single = sum(1 for c in counts if c == 1)

    stats: Dict[str, Any] = {
        "total_children": total_children,
        "total_parents": total_parents,
        "n_docs_with_chunks": n_docs,
        "empty_children": empty,
        "empty_ratio": round(empty / total_children, 4) if total_children else None,
        "overlong_children": overlong,
        "overlong_ratio": round(overlong / total_children, 4) if total_children else None,
        "overlong_chars_threshold": overlong_chars,
        "chars": {
            "min": sorted_lens[0] if sorted_lens else None,
            "avg": round(sum(lens) / total_children, 1) if total_children else None,
            "p95": _percentile(sorted_lens, 0.95),
            "max": sorted_lens[-1] if sorted_lens else None,
        },
        "chunks_per_doc": {
            "single_chunk_docs": single,
            "single_chunk_ratio": round(single / n_docs, 4) if n_docs else None,
            "max_chunks": counts[-1] if counts else None,
        },
    }

    sentinels: List[dict] = []
    if empty:
        sentinels.append(
            {"name": "empty_chunk", "level": "warn", "count": empty, "hint": "空文本子块会静默拉低召回与答案引用"}
        )
    if overlong:
        sentinels.append(
            {
                "name": "overlong_chunk",
                "level": "warn",
                "count": overlong,
                "hint": f"超过 {overlong_chars} 字符的子块 {overlong} 个，疑似切块吞块",
            }
        )
    if single and n_docs and single / n_docs > 0.5:
        sentinels.append(
            {
                "name": "single_chunk_docs",
                "level": "info",
                "count": single,
                "hint": "超过半数父块只有 1 个子块，检索粒度可能过粗（仅供参考）",
            }
        )
    stats["sentinels"] = sentinels
    return stats


# ---------------------------------------------------------------- 主流程 ---------


def run_ingestion_audit(
    *,
    kb_name: str = "oa_ma",
    eval_path: Optional[Path] = None,
    out_dir: Optional[Path] = None,
    overlong_chars: int = DEFAULT_OVERLONG_CHARS,
    limit: int = 0,
) -> dict:
    """跑入库审计，结果覆盖写入 data/eval/results/ingestion_audit.json。"""
    eval_path = Path(eval_path) if eval_path else default_eval_path()
    out_dir = Path(out_dir) if out_dir else default_results_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    rows, _ = load_eval_dataset(eval_path, strict=True)
    if limit and limit > 0:
        rows = rows[:limit]

    t0 = time.time()
    containment = _audit_containment(kb_name, rows)
    chunk_stats = _audit_chunk_stats(kb_name, overlong_chars)
    payload = {
        "created_at": datetime.now().strftime("%Y%m%d_%H%M%S"),
        "elapsed_s": round(time.time() - t0, 2),
        "kb": kb_name,
        "eval_file": str(eval_path),
        "overlong_chars": overlong_chars,
        "containment": containment,
        "chunk_stats": chunk_stats,
    }
    out_json = out_dir / "ingestion_audit.json"
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {out_json}")
    return payload


def main(argv: Optional[Sequence[str]] = None) -> int:
    _setup_sys_path()
    parser = argparse.ArgumentParser(description="入库轻量间接审计（snippet 包含率 + 子块统计哨兵）")
    parser.add_argument("--kb", default="oa_ma")
    parser.add_argument("--eval", type=Path, default=None)
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument("--overlong-chars", type=int, default=DEFAULT_OVERLONG_CHARS)
    parser.add_argument("--limit", type=int, default=0, help="只审计前 N 条金标（调试用）")
    args = parser.parse_args(argv)

    payload = run_ingestion_audit(
        kb_name=args.kb,
        eval_path=args.eval,
        out_dir=args.out_dir,
        overlong_chars=args.overlong_chars,
        limit=args.limit,
    )
    c = payload["containment"]
    s = payload["chunk_stats"]
    judged = c["n_contained"] + c["n_missing"]
    print("\n=== INGESTION AUDIT SUMMARY ===")
    print(
        f"snippet containment: {c['n_contained']}/{judged} "
        f"(rate={c['containment_rate']}, parent_missing={c['n_parent_missing']})"
    )
    print(
        f"chunks: {s['total_children']} children / {s['total_parents']} parents | "
        f"empty={s['empty_children']} overlong={s['overlong_children']} "
        f"chars_avg={s['chars']['avg']} p95={s['chars']['p95']} max={s['chars']['max']}"
    )
    print(f"sentinels: {len(s['sentinels'])}")
    for w in s["sentinels"]:
        print(f"  [{w['level']}] {w['name']} x{w['count']}: {w['hint']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
