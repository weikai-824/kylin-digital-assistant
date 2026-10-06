"""回答评估执行器：端到端跑 RAG 回答并打分，输出报告。

评测对象是「完整问答链路」的最终输出（拒答是否干脆、回答是否覆盖要点、
是否出现幻觉、引用是否合法、是否啰嗦），与检索评估（runner.py，只看
父块命中）物理隔离。评估集与打分逻辑均不绑定具体知识库。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# 顶层注入路径：本模块 import 链会进入 kylin 问答应用，
# 必须在任何业务 import 之前把仓库根放上 sys.path。
# common 仅依赖标准库，可安全地在注入前导入。
from kylin.adapters.inbound.jobs.eval.common import default_repo_root

_ROOT = default_repo_root()
os.environ.setdefault("KYLIN_ROOT", os.environ.get("KYLIN_ROOT", "."))
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
import argparse
import asyncio
import json
import re
import time
from datetime import datetime
from collections.abc import Awaitable, Callable
from typing import Any, Dict, List, Optional, Sequence

from kylin.adapters.inbound.jobs.eval.common import default_results_dir

from kylin.adapters.inbound.jobs.eval.answer_dataset import (
    default_answer_eval_path,
    load_answer_dataset,
)
from kylin.adapters.inbound.jobs.eval.answer_metrics import (
    DEFAULT_MAX_CHARS,
    DEFAULT_MAX_CITATIONS,
    aggregate_details,
    judge_factuality,
    score_one_answer,
)

STAGES_NOTE = "answer eval 跑完整链路（query_plan → 检索 → 拒答门/接地生成），不拆分检索阶段"

# QwQ 等推理模型会把思考痕迹放进 message.content；行为判定/字数统计只应看最终答复
_THINK_RE = re.compile(r"<think>[\s\S]*?</think>", re.IGNORECASE)


def _strip_think(text: str) -> str:
    """剥离 <think>...</think> 思考块；兼容未闭合残留。"""
    if not text:
        return text
    cleaned = _THINK_RE.sub("", text)
    if "<think" in cleaned.lower():
        cleaned = cleaned[: cleaned.lower().find("<think")]
    return cleaned.strip()


def _extract_sources(raw_docs: List[dict]) -> List[str]:
    """从 raw_docs 提取来源文件名（附加参考，不参与打分）。"""
    out: List[str] = []
    for d in raw_docs or []:
        meta = d.get("metadata") or {}
        src = meta.get("source") or meta.get("file_name") or ""
        if src and src not in out:
            out.append(str(src))
    return out


async def run_one_query(
    *,
    chat: Callable[..., Awaitable[object]],
    kb_name: str,
    row: dict,
    top_k: int,
    score_threshold: Optional[float],
    model: Optional[str],
    judge: bool,
    max_chars: int,
    max_citations: int,
) -> dict:
    """单题：调 rag_chat 非流式入口，返回结构化记录（含打分）。"""
    qid = row["qid"]
    query = row["query"]
    expected = row.get("expected_behavior") or "answer"

    t0 = time.time()
    err = ""
    answer = ""
    raw_docs: List[dict] = []
    ticket_id = None
    try:
        # 评估不应产生业务副作用：显式传 ticket_sink=None（显式优先于组合根注入），
        # 避免 negative 题拒答时批量污染工单表。
        out_json = await chat(
            query=query,
            kb_name=kb_name,
            top_k=top_k,
            score_threshold=score_threshold,
            stream=False,
            model=model,
        )
        if not isinstance(out_json, str):
            # chat() 协议约定返回 JSON 字符串；kb 不存在等分支可能直接返回
            # BaseResponse 对象（HTTP 侧由 FastAPI 序列化，程序化调用则拿到对象）。
            code = getattr(out_json, "code", None)
            msg = getattr(out_json, "msg", None) or type(out_json).__name__
            raise TypeError(f"chat() 返回了非 JSON 字符串 (code={code}): {msg}")
        payload = json.loads(out_json)
        # 非流式输出：回答在 choices[0].message.content（顶层无 content 字段）
        choices = payload.get("choices") or []
        msg = (choices[0] or {}).get("message") or {}
        answer = _strip_think(msg.get("content") or "")
        raw_docs = payload.get("raw_docs") or []
        ticket_id = payload.get("ticket_id")
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    latency = time.time() - t0

    n_docs = len(raw_docs)
    top_rel = None
    if raw_docs:
        m0 = (raw_docs[0].get("metadata") or {}) if isinstance(raw_docs[0], dict) else {}
        try:
            top_rel = round(float(m0.get("relevance_score")), 4)
        except (TypeError, ValueError):
            top_rel = None
    scored = score_one_answer(
        row,
        answer=answer,
        n_docs=n_docs,
        max_chars=max_chars,
        max_citations=max_citations,
    )

    rec: dict = {
        "qid": qid,
        "query": query,
        "category": row.get("category", "?"),
        "expected_behavior": expected,
        "answer": answer,
        "n_docs": n_docs,
        "top_relevance": top_rel,
        "sources": _extract_sources(raw_docs),
        "ticket_id": ticket_id,
        "latency_s": round(latency, 3),
        "error": err,
        **scored,
    }

    if judge and not err and answer:
        # 懒构造 judge LLM（复用主链路同款配置），失败不阻断主流程
        try:
            from kylin.adapters.outbound.model.clients import get_ChatOpenAI, get_default_llm

            judge_llm = get_ChatOpenAI(
                model_name=model or get_default_llm(),
                temperature=0.0,
                max_tokens=512,
                streaming=False,
                callbacks=[],
                verbose=False,
            )
            rec["judge"] = judge_factuality(query, answer, row, judge_llm)
        except Exception as e:
            rec["judge"] = {"judge_pass": 0, "judge_reason": "", "judge_error": f"{type(e).__name__}: {e}"}

    return rec


async def _run_all(
    *,
    chat: Callable[..., Awaitable[object]],
    kb_name: str,
    rows: List[dict],
    top_k: int,
    score_threshold: Optional[float],
    model: Optional[str],
    judge: bool,
    max_chars: int,
    max_citations: int,
) -> List[dict]:
    details: List[dict] = []
    n = len(rows)
    for i, row in enumerate(rows, 1):
        rec = await run_one_query(
            chat=chat,
            kb_name=kb_name,
            row=row,
            top_k=top_k,
            score_threshold=score_threshold,
            model=model,
            judge=judge,
            max_chars=max_chars,
            max_citations=max_citations,
        )
        details.append(rec)
        status = "ERR" if rec.get("error") else ("OK" if rec["behavior_score"] > 0 else "FAIL")
        print(
            f"[{i}/{n}] {rec['qid']} {status} expected={rec['expected_behavior']} "
            f"actual={rec['actual_behavior']} beh={rec['behavior_score']} "
            f"cov={rec['expected_coverage']} chars={rec['conciseness']['chars']} "
            f"docs={rec['n_docs']} {rec['latency_s']:.1f}s"
            + (f" ERR={rec['error']}" if rec.get("error") else ""),
            flush=True,
        )
    return details


def run_answer_eval(
    *,
    chat: Callable[..., Awaitable[object]],
    kb_name: str = "oa_ma",
    eval_path: Optional[Path] = None,
    out_dir: Optional[Path] = None,
    top_k: int = 5,
    score_threshold: Optional[float] = None,
    model: Optional[str] = None,
    judge: bool = False,
    limit: int = 0,
    max_chars: int = DEFAULT_MAX_CHARS,
    max_citations: int = DEFAULT_MAX_CITATIONS,
) -> dict:
    """跑回答评估，结果覆盖写入 data/eval/results/（只保留最新一次）。"""
    eval_path = Path(eval_path) if eval_path else default_answer_eval_path()
    out_dir = Path(out_dir) if out_dir else default_results_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    rows, _ = load_answer_dataset(eval_path, strict=True)
    if limit and limit > 0:
        rows = rows[:limit]

    details = asyncio.run(
        _run_all(
            chat=chat,
            kb_name=kb_name,
            rows=rows,
            top_k=top_k,
            score_threshold=score_threshold,
            model=model,
            judge=judge,
            max_chars=max_chars,
            max_citations=max_citations,
        )
    )

    agg = aggregate_details(details)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    payload = {
        "created_at": stamp,
        "elapsed_s": 0.0,
        "kb": kb_name,
        "eval_file": str(eval_path),
        "top_k": top_k,
        "score_threshold": score_threshold,
        "model": model,
        "judge": judge,
        "n": len(details),
        "aggregate": agg,
        "details": details,
    }
    # 用真实耗时回填
    # （细节已在 _run_all 内记录，此处不重复计时）

    out_json = out_dir / "answer_eval.json"
    out_md = out_dir / "answer_eval.md"
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(_render_markdown(payload), encoding="utf-8")

    print(f"\nwrote {out_json}")
    print(f"wrote {out_md}")
    return payload


def _fmt(v: Any) -> str:
    return "-" if v is None else str(v)


def _render_markdown(payload: dict) -> str:
    agg = payload.get("aggregate") or {}
    overall = agg.get("overall") or {}
    lines = [
        f"# Answer eval `{payload['kb']}`",
        "",
        f"- eval: `{Path(payload['eval_file']).name}`",
        f"- n: {payload.get('n')}",
        f"- top_k={payload.get('top_k')}, score_threshold={payload.get('score_threshold')}, judge={payload.get('judge')}",
        f"- {STAGES_NOTE}",
        "",
        "## Overall",
        "",
        "| metric | value |",
        "|---|---|",
        f"| behavior_acc | {_fmt(overall.get('behavior_acc'))} |",
        f"| point_coverage | {_fmt(overall.get('point_coverage'))} |",
        f"| forbidden_violation_rate | {_fmt(overall.get('forbidden_violation_rate'))} |",
        f"| fact_score | {_fmt(overall.get('fact_score'))} |",
        f"| conciseness | {_fmt(overall.get('conciseness'))} |",
        f"| citation_valid_ratio | {_fmt(overall.get('citation_valid_ratio'))} |",
        f"| avg_chars | {_fmt(overall.get('avg_chars'))} |",
        f"| avg_citations | {_fmt(overall.get('avg_citations'))} |",
        "",
        "## By category",
        "",
        "| category | n | behavior_acc | point_cov | forbidden_rate | fact | concise | cite_valid |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for cat, s in (agg.get("by_category") or {}).items():
        lines.append(
            f"| {cat} | {s.get('n')} | {_fmt(s.get('behavior_acc'))} | "
            f"{_fmt(s.get('point_coverage'))} | {_fmt(s.get('forbidden_violation_rate'))} | "
            f"{_fmt(s.get('fact_score'))} | {_fmt(s.get('conciseness'))} | {_fmt(s.get('citation_valid_ratio'))} |"
        )

    lines += [
        "",
        "## By expected_behavior",
        "",
        "| behavior | n | behavior_acc | point_cov | forbidden_rate | fact | concise | cite_valid |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for beh, s in (agg.get("by_behavior") or {}).items():
        lines.append(
            f"| {beh} | {s.get('n')} | {_fmt(s.get('behavior_acc'))} | "
            f"{_fmt(s.get('point_coverage'))} | {_fmt(s.get('forbidden_violation_rate'))} | "
            f"{_fmt(s.get('fact_score'))} | {_fmt(s.get('conciseness'))} | {_fmt(s.get('citation_valid_ratio'))} |"
        )

    lines += ["", "## Per question", ""]
    for r in payload.get("details") or []:
        judge_line = ""
        j = r.get("judge")
        if j:
            judge_line = (
                f" judge_pass={j.get('judge_pass')}"
                + (f" ({j.get('judge_reason')})" if j.get("judge_reason") else "")
                + (f" [ERR {j.get('judge_error')}]" if j.get("judge_error") else "")
            )
        lines += [
            f"### {r['qid']} `{r['category']}` (expected={r['expected_behavior']}, actual={r['actual_behavior']})",
            "",
            f"- query: {r['query']}",
            f"- behavior_score: {r['behavior_score']} | point_coverage: {_fmt(r['expected_coverage'])} | "
            f"forbidden: {_fmt(r['forbidden_violation'])} | fact: {r['fact_score']}",
            f"- concise: {r['conciseness']['score']} ({r['conciseness']['chars']} chars, {r['conciseness']['n_citations']} cites) | "
            f"citations: {r.get('citations')} illegal={r.get('illegal_citations')} n_docs={r.get('n_docs')}",
            f"- sources: {r.get('sources')} | ticket: {r.get('ticket_id')} | latency: {r.get('latency_s')}s",
        ]
        if r.get("error"):
            lines.append(f"- **error**: {r['error']}")
        if judge_line:
            lines.append(f"- judge:{judge_line}")
        lines += ["", f"**answer**: {r.get('answer') or '（空）'}", ""]

    return "\n".join(lines) + "\n"


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    chat: Callable[..., Awaitable[object]],
) -> int:
    parser = argparse.ArgumentParser(description="Pipeline answer eval (end-to-end)")
    parser.add_argument("--kb", default="oa_ma")
    parser.add_argument("--eval", type=Path, default=None)
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--score-threshold", type=float, default=None)
    parser.add_argument("--model", default=None, help="LLM 名称，默认取 DEFAULT_LLM_MODEL")
    parser.add_argument("--judge", action="store_true", help="启用 LLM-as-judge 事实打分（慢）")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 题")
    parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    parser.add_argument("--max-citations", type=int, default=DEFAULT_MAX_CITATIONS)
    args = parser.parse_args(argv)

    payload = run_answer_eval(
        chat=chat,
        kb_name=args.kb,
        eval_path=args.eval,
        out_dir=args.out_dir,
        top_k=args.top_k,
        score_threshold=args.score_threshold,
        model=args.model,
        judge=args.judge,
        limit=args.limit,
        max_chars=args.max_chars,
        max_citations=args.max_citations,
    )
    overall = (payload.get("aggregate") or {}).get("overall") or {}
    print("\n=== ANSWER EVAL SUMMARY ===")
    print(json.dumps(overall, ensure_ascii=False, indent=2))
    return 0


