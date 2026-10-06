"""检索评估执行器：分阶段归因 + 可选参数扫描 + 报告输出。"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from kylin.adapters.inbound.jobs.eval.common import (
    default_results_dir,
    mean as _mean,
    setup_sys_path as _setup_sys_path,
)
from kylin.adapters.inbound.jobs.eval.dataset import default_eval_path, load_eval_dataset
from kylin.adapters.inbound.jobs.eval.metrics import metrics_at_ks, parent_ids_from_docs

STAGES = ("dense", "sparse", "rrf", "rerank", "final")
DEFAULT_KS = (1, 3, 5)


def _aggregate(rows: Sequence[dict], ks: Sequence[int] = DEFAULT_KS) -> dict:
    if not rows:
        return {"n": 0}
    out: dict = {"n": len(rows), "mrr": round(_mean(r["mrr"] for r in rows), 4)}
    for k in ks:
        out[f"hit@{k}"] = round(_mean(r[f"hit@{k}"] for r in rows), 4)
        out[f"recall@{k}"] = round(_mean(r[f"recall@{k}"] for r in rows), 4)
    out["avg_latency_s"] = round(_mean(r.get("latency_s", 0.0) for r in rows), 3)
    return out


def _score_stage(
    docs: Sequence,
    gold: Set[str],
    *,
    ks: Sequence[int] = DEFAULT_KS,
) -> dict:
    retrieved = parent_ids_from_docs(docs)
    m = metrics_at_ks(retrieved, gold, ks=ks)
    m["retrieved"] = retrieved
    return m


def run_one_query(
    *,
    kb_name: str,
    row: dict,
    top_k: int,
    recall_top_k: Optional[int] = None,
    rerank_candidate_top_k: Optional[int] = None,
    rrf_k: Optional[int] = None,
    ks: Sequence[int] = DEFAULT_KS,
) -> dict:
    from kylin.adapters.outbound.knowledge_base.retrieve.dual_search import dual_search_trace

    qid = row["qid"]
    query = row["query"]
    qtype = row["query_type"]
    gold = set(row.get("relevant_parent_doc_ids") or [])
    is_neg = qtype == "negative" or not gold

    t0 = time.time()
    err = ""
    try:
        trace = dual_search_trace(
            kb_name,
            query,
            top_k=top_k,
            recall_top_k=recall_top_k,
            rerank_candidate_top_k=rerank_candidate_top_k,
            rrf_k=rrf_k,
        )
    except Exception as e:
        trace = {s: [] for s in STAGES}
        trace["meta"] = {}
        err = f"{type(e).__name__}: {e}"
    latency = time.time() - t0

    rec: dict = {
        "qid": qid,
        "query": query,
        "query_type": qtype,
        "source": row.get("source") or "",
        "negative": is_neg,
        "gold": sorted(gold),
        "latency_s": round(latency, 3),
        "error": err,
        "meta": trace.get("meta") or {},
        "stages": {},
    }

    if is_neg:
        final_ids = parent_ids_from_docs(trace.get("final") or [])
        rec["stages"]["final"] = {
            "retrieved": final_ids,
            "empty_at_5": len(final_ids[:5]) == 0,
        }
        return rec

    for stage in STAGES:
        rec["stages"][stage] = _score_stage(trace.get(stage) or [], gold, ks=ks)

    # 主表字段取 final，便于汇总
    final_m = rec["stages"]["final"]
    for k in ks:
        rec[f"hit@{k}"] = final_m[f"hit@{k}"]
        rec[f"recall@{k}"] = final_m[f"recall@{k}"]
    rec["mrr"] = final_m["mrr"]
    rec["retrieved"] = final_m["retrieved"]
    return rec


def run_retrieval_eval(
    *,
    kb_name: str = "oa_ma",
    eval_path: Optional[Path] = None,
    out_dir: Optional[Path] = None,
    top_k: int = 5,
    recall_top_k: Optional[int] = None,
    rerank_candidate_top_k: Optional[int] = None,
    rrf_k: Optional[int] = None,
    limit: int = 0,
    ks: Sequence[int] = DEFAULT_KS,
    sweep: bool = False,
) -> dict:
    """
    跑完整评估：默认五阶段归因；sweep=True 时额外扫少量参数组合。
    结果覆盖写入 data/eval/results/ 固定文件名（只保留最新一次）。
    """
    eval_path = Path(eval_path) if eval_path else default_eval_path()
    out_dir = Path(out_dir) if out_dir else default_results_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    rows, _ = load_eval_dataset(eval_path, strict=True)
    if limit and limit > 0:
        rows = rows[:limit]

    param_grid: List[dict] = [
        {
            "name": "default",
            "top_k": top_k,
            "recall_top_k": recall_top_k,
            "rerank_candidate_top_k": rerank_candidate_top_k,
            "rrf_k": rrf_k,
        }
    ]
    if sweep:
        param_grid.extend(
            [
                {
                    "name": "recall_30",
                    "top_k": top_k,
                    "recall_top_k": 30,
                    "rerank_candidate_top_k": rerank_candidate_top_k,
                    "rrf_k": rrf_k,
                },
                {
                    "name": "recall_100",
                    "top_k": top_k,
                    "recall_top_k": 100,
                    "rerank_candidate_top_k": rerank_candidate_top_k,
                    "rrf_k": rrf_k,
                },
                {
                    "name": "rrf_k_30",
                    "top_k": top_k,
                    "recall_top_k": recall_top_k,
                    "rerank_candidate_top_k": rerank_candidate_top_k,
                    "rrf_k": 30,
                },
                {
                    "name": "rerank_cand_20",
                    "top_k": top_k,
                    "recall_top_k": recall_top_k,
                    "rerank_candidate_top_k": 20,
                    "rrf_k": rrf_k,
                },
            ]
        )

    all_runs = []
    t_all = time.time()
    for params in param_grid:
        details: List[dict] = []
        print(f"\n=== run `{params['name']}` ===", flush=True)
        for i, row in enumerate(rows, 1):
            rec = run_one_query(
                kb_name=kb_name,
                row=row,
                top_k=int(params["top_k"]),
                recall_top_k=params.get("recall_top_k"),
                rerank_candidate_top_k=params.get("rerank_candidate_top_k"),
                rrf_k=params.get("rrf_k"),
                ks=ks,
            )
            details.append(rec)
            if rec.get("negative"):
                status = "NEG"
            else:
                status = "HIT" if rec.get("hit@5") else "MISS"
            print(
                f"[{i}/{len(rows)}] {rec['qid']} {status} {rec['latency_s']:.1f}s "
                f"type={rec['query_type']}"
                + (f" ERR={rec['error']}" if rec.get("error") else ""),
                flush=True,
            )

        pos = [d for d in details if not d.get("negative")]
        neg = [d for d in details if d.get("negative")]

        by_stage = {}
        for stage in STAGES:
            stage_rows = []
            for d in pos:
                sm = (d.get("stages") or {}).get(stage) or {}
                if not sm:
                    continue
                stage_rows.append(
                    {
                        **{f"hit@{k}": sm[f"hit@{k}"] for k in ks},
                        **{f"recall@{k}": sm[f"recall@{k}"] for k in ks},
                        "mrr": sm["mrr"],
                        "latency_s": d.get("latency_s", 0.0),
                    }
                )
            by_stage[stage] = _aggregate(stage_rows, ks=ks)

        by_type: Dict[str, dict] = {}
        grouped: Dict[str, List[dict]] = defaultdict(list)
        for d in pos:
            grouped[d["query_type"]].append(d)
        for qtype, items in sorted(grouped.items()):
            by_type[qtype] = _aggregate(items, ks=ks)

        summary = {
            "name": params["name"],
            "params": params,
            "kb": kb_name,
            "eval_file": str(eval_path),
            "n_total": len(details),
            "n_positive": len(pos),
            "n_negative": len(neg),
            "ks": list(ks),
            "overall_final": _aggregate(pos, ks=ks),
            "by_stage": by_stage,
            "by_type": by_type,
            "negative": {
                "n": len(neg),
                "empty_rate_at_5": round(
                    _mean(
                        1.0
                        if ((d.get("stages") or {}).get("final") or {}).get("empty_at_5")
                        else 0.0
                        for d in neg
                    ),
                    4,
                )
                if neg
                else None,
                "note": "negative 无 gold id；empty_rate_at_5 表示 final top-5 为空的比例",
            },
        }
        all_runs.append({"summary": summary, "details": details})

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    primary = all_runs[0]
    payload = {
        "created_at": stamp,
        "elapsed_s": round(time.time() - t_all, 1),
        "runs": all_runs,
    }
    # 固定文件名覆盖写，目录里只保留最新一轮
    out_json = out_dir / "retrieval_eval.json"
    out_md = out_dir / "retrieval_eval.md"
    out_miss = out_dir / "retrieval_eval_misses.jsonl"

    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(_render_markdown(primary["summary"], all_runs), encoding="utf-8")

    misses = [
        d
        for d in primary["details"]
        if not d.get("negative") and not d.get("hit@5")
    ]
    with out_miss.open("w", encoding="utf-8", newline="\n") as f:
        for d in misses:
            f.write(
                json.dumps(
                    {
                        "qid": d["qid"],
                        "query": d["query"],
                        "query_type": d["query_type"],
                        "source": d.get("source"),
                        "gold": d.get("gold"),
                        "stages": {
                            s: (d.get("stages") or {}).get(s, {}).get("retrieved")
                            for s in STAGES
                        },
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    print(f"\nwrote {out_json}")
    print(f"wrote {out_md}")
    print(f"wrote {out_miss}")
    return payload


def _render_markdown(summary: dict, all_runs: List[dict]) -> str:
    ks = summary.get("ks") or list(DEFAULT_KS)
    lines = [
        f"# Retrieval eval `{summary['kb']}`",
        "",
        f"- eval: `{Path(summary['eval_file']).name}`",
        f"- n: {summary['n_total']} (pos={summary['n_positive']}, neg={summary['n_negative']})",
        f"- params: `{summary['name']}` {summary.get('params')}",
        "",
        "## Final (positive)",
        "",
        "| " + " | ".join([f"Hit@{k}" for k in ks] + ["MRR"] + [f"Recall@{k}" for k in ks]) + " |",
        "|" + "|".join(["---"] * (len(ks) * 2 + 1)) + "|",
    ]
    o = summary["overall_final"]
    lines.append(
        "| "
        + " | ".join(
            [str(o.get(f"hit@{k}")) for k in ks]
            + [str(o.get("mrr"))]
            + [str(o.get(f"recall@{k}")) for k in ks]
        )
        + " |"
    )

    lines += ["", "## Stage attribution (Hit@5 / MRR)", "", "| stage | n | Hit@5 | MRR | Recall@5 |", "|---|---|---|---|---|"]
    for stage in STAGES:
        s = (summary.get("by_stage") or {}).get(stage) or {}
        lines.append(
            f"| {stage} | {s.get('n', 0)} | {s.get('hit@5')} | {s.get('mrr')} | {s.get('recall@5')} |"
        )

    lines += ["", "## By query_type (final)", "", "| type | n | Hit@5 | MRR | Recall@5 |", "|---|---|---|---|---|"]
    for qtype, s in (summary.get("by_type") or {}).items():
        lines.append(
            f"| {qtype} | {s.get('n')} | {s.get('hit@5')} | {s.get('mrr')} | {s.get('recall@5')} |"
        )

    neg = summary.get("negative") or {}
    lines += [
        "",
        "## Negative",
        "",
        f"- n={neg.get('n')}, empty_rate@5={neg.get('empty_rate_at_5')}",
        f"- {neg.get('note')}",
    ]

    if len(all_runs) > 1:
        lines += ["", "## Parameter sweep (final Hit@5)", "", "| name | Hit@5 | MRR |", "|---|---|---|"]
        for run in all_runs:
            s = run["summary"]
            o = s.get("overall_final") or {}
            lines.append(f"| {s.get('name')} | {o.get('hit@5')} | {o.get('mrr')} |")

    return "\n".join(lines) + "\n"


def main(argv: Optional[Sequence[str]] = None) -> int:
    _setup_sys_path()
    parser = argparse.ArgumentParser(description="Pipeline retrieval eval")
    parser.add_argument("--kb", default="oa_ma")
    parser.add_argument("--eval", type=Path, default=None)
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--recall-top-k", type=int, default=None)
    parser.add_argument("--rerank-candidate-top-k", type=int, default=None)
    parser.add_argument("--rrf-k", type=int, default=None)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--sweep", action="store_true", help="额外扫描 recall/rrf/rerank_candidate")
    args = parser.parse_args(argv)

    payload = run_retrieval_eval(
        kb_name=args.kb,
        eval_path=args.eval,
        out_dir=args.out_dir,
        top_k=args.top_k,
        recall_top_k=args.recall_top_k,
        rerank_candidate_top_k=args.rerank_candidate_top_k,
        rrf_k=args.rrf_k,
        limit=args.limit,
        sweep=args.sweep,
    )
    primary = payload["runs"][0]["summary"]
    print("\n=== FINAL SUMMARY ===")
    print(json.dumps(primary.get("overall_final"), ensure_ascii=False, indent=2))
    print("=== BY STAGE Hit@5 ===")
    for stage in STAGES:
        s = (primary.get("by_stage") or {}).get(stage) or {}
        print(f"  {stage}: hit@5={s.get('hit@5')} mrr={s.get('mrr')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
