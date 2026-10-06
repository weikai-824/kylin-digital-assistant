"""评估对比层：本轮 results 与基线快照的逐指标对比 + 回退归因报告。

只读两边已落盘的 JSON（runner.py / answer_runner.py 的产物），自身不跑任何评估：
- 检索：results/retrieval_eval.json  vs baseline/<最新>_retrieval_eval.json
- 回答：results/answer_eval.json     vs baseline/<最新>_answer_eval*.json

产出 data/eval/results/compare_report.md。任一质量指标回退超过阈值（默认
5 个百分点，与 data/eval/BASELINE.md 的回归约定一致），或幻觉违规率从 0
变为非 0，进程退出码置 1，便于后续接 CI / 钩子。

跑法（仓库根目录）：
    python -m kylin.adapters.inbound.jobs.eval.compare
    python -m kylin.adapters.inbound.jobs.eval.compare --stdout          # 只打印不落盘
    python -m kylin.adapters.inbound.jobs.eval.compare \
        --baseline data/eval/baseline/20260831_144713_retrieval_eval.json \
        --baseline-answer data/eval/baseline/20260831_144713_answer_eval_BEFORE_queryplan_fix.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from kylin.adapters.inbound.jobs.eval.common import (
    default_eval_dir,
    default_results_dir,
    setup_sys_path as _setup_sys_path,
)

# 与 runner.py 保持一致的五阶段顺序（本地定义，避免为常量引入对 runner 的耦合）
STAGES = ("dense", "sparse", "rrf", "rerank", "final")

# 越低越好的指标（其余质量指标默认越高越好）。
# 注意：empty_rate_at_5 不在其中——它是 negative 组「final top-5 为空的比例」，
# 越高说明负例越干净地被拒答（runner.py negative.note 同口径），是越高越好。
LOWER_BETTER = {"forbidden_violation_rate"}

# 中性指标：只展示不参与回归判定（延迟属环境因素，字数/引用数无方向性）
NEUTRAL = {"avg_latency_s", "avg_chars", "avg_citations"}

DEFAULT_FAIL_THRESHOLD = 0.05


# ---------------------------------------------------------------- 加载 ----------


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _pick_latest_baseline(eval_dir: Path, kind: str) -> Optional[Path]:
    """在 baseline/ 里按文件名（时间戳前缀）取最新一份指定类型的快照。"""
    base_dir = eval_dir / "baseline"
    if not base_dir.is_dir():
        return None
    if kind == "retrieval":
        candidates = [p for p in base_dir.glob("*.json") if p.name.endswith("_retrieval_eval.json")]
    else:
        candidates = [
            p
            for p in base_dir.glob("*.json")
            if "_answer_eval" in p.name and not p.name.endswith("_retrieval_eval.json")
        ]
    return max(candidates, key=lambda p: p.name) if candidates else None


def _retr_primary(payload: dict) -> Tuple[dict, List[dict]]:
    """取检索 payload 的主 run（与 runner 的 primary 口径一致：runs[0]）。"""
    runs = payload.get("runs") or []
    if not runs:
        raise ValueError("retrieval payload 无 runs，格式不符")
    run = runs[0]
    return run.get("summary") or {}, run.get("details") or []


# ---------------------------------------------------------------- 指标行 -------


def _status(delta: Optional[float], key: str, thr: float) -> str:
    if delta is None:
        return "-"
    if key in NEUTRAL:
        return "➖"
    worse = delta >= thr if key in LOWER_BETTER else delta <= -thr
    better = delta <= -thr if key in LOWER_BETTER else delta >= thr
    return "⚠" if worse else ("✅" if better else "➖")


def _is_regression(delta: Optional[float], key: str, thr: float) -> bool:
    if delta is None or key in NEUTRAL:
        return False
    return delta >= thr if key in LOWER_BETTER else delta <= -thr


def _metric_rows(
    base: dict,
    cur: dict,
    keys: Sequence[str],
    thr: float,
) -> List[dict]:
    rows = []
    for k in keys:
        b, c = base.get(k), cur.get(k)
        delta = None if (b is None or c is None) else round(c - b, 4)
        rows.append(
            {
                "key": k,
                "base": b,
                "cur": c,
                "delta": delta,
                "status": _status(delta, k, thr),
                "regressed": _is_regression(delta, k, thr),
            }
        )
    return rows


RETR_KEYS = ("mrr", "hit@1", "recall@1", "hit@3", "recall@3", "hit@5", "recall@5")
ANS_KEYS = (
    "behavior_acc",
    "point_coverage",
    "forbidden_violation_rate",
    "fact_score",
    "citation_valid_ratio",
    "conciseness",
)


def _stage_rows(base_sum: dict, cur_sum: dict, thr: float) -> List[dict]:
    """五阶段归因行：每阶段看 mrr 与 recall@5 的变化，定位回退发生在哪一跳。"""
    bs = base_sum.get("by_stage") or {}
    cs = cur_sum.get("by_stage") or {}
    rows = []
    for s in STAGES:
        b, c = bs.get(s) or {}, cs.get(s) or {}
        dm = None if (b.get("mrr") is None or c.get("mrr") is None) else round(c["mrr"] - b["mrr"], 4)
        dr = None
        if b.get("recall@5") is not None and c.get("recall@5") is not None:
            dr = round(c["recall@5"] - b["recall@5"], 4)
        rows.append(
            {
                "stage": s,
                "base_mrr": b.get("mrr"),
                "cur_mrr": c.get("mrr"),
                "d_mrr": dm,
                "base_r5": b.get("recall@5"),
                "cur_r5": c.get("recall@5"),
                "d_r5": dr,
                "regressed": _is_regression(dm, "mrr", thr) or _is_regression(dr, "recall@5", thr),
            }
        )
    return rows


def _regressed_qids(base_det: List[dict], cur_det: List[dict]) -> List[dict]:
    """逐题回退清单：本轮 MRR 低于基线的题 + 首个劣化阶段。"""
    bmap = {d.get("qid"): d for d in base_det}
    out = []
    for d in cur_det:
        qid = d.get("qid")
        b = bmap.get(qid)
        if b is None or d.get("negative"):
            continue
        bm, cm = b.get("mrr"), d.get("mrr")
        if bm is None or cm is None or cm >= bm - 1e-6:
            continue
        first_stage = ""
        bs = b.get("stages") or {}
        cs = d.get("stages") or {}
        for s in STAGES:
            sm_b = (bs.get(s) or {}).get("mrr")
            sm_c = (cs.get(s) or {}).get("mrr")
            if sm_b is not None and sm_c is not None and sm_c < sm_b - 1e-6:
                first_stage = s
                break
        out.append(
            {
                "qid": qid,
                "query_type": d.get("query_type"),
                "base_mrr": bm,
                "cur_mrr": cm,
                "first_stage": first_stage or "n/a",
            }
        )
    return out


def _ans_case_lists(base_det: List[dict], cur_det: List[dict]) -> Dict[str, List[dict]]:
    """回答层逐 case 归因：新增幻觉 / 要点覆盖下降 / 行为回退。"""
    bmap = {d.get("qid"): d for d in base_det}
    violations: List[dict] = []
    cov_drops: List[dict] = []
    beh_drops: List[dict] = []
    for d in cur_det:
        qid = d.get("qid")
        b = bmap.get(qid)
        if b is None:
            continue
        if d.get("forbidden_violation") and not b.get("forbidden_violation"):
            violations.append({"qid": qid, "category": d.get("category"), "query": d.get("query")})
        bc, cc = b.get("expected_coverage"), d.get("expected_coverage")
        if bc is not None and cc is not None and cc < bc - 1e-6:
            cov_drops.append({"qid": qid, "base": bc, "cur": cc, "query": d.get("query")})
        bs, cs = b.get("behavior_score"), d.get("behavior_score")
        if bs is not None and cs is not None and cs < bs - 1e-6:
            beh_drops.append(
                {
                    "qid": qid,
                    "expected": d.get("expected_behavior"),
                    "base_actual": b.get("actual_behavior"),
                    "cur_actual": d.get("actual_behavior"),
                }
            )
    return {"violations": violations, "cov_drops": cov_drops, "beh_drops": beh_drops}


# ---------------------------------------------------------------- 渲染 ---------


def _fmt(v: Any) -> str:
    return "-" if v is None else (f"{v:.4f}" if isinstance(v, float) else str(v))


def _render_table(rows: List[dict]) -> List[str]:
    lines = ["| 指标 | 基线 | 本轮 | Δ | 状态 |", "|---|---|---|---|---|"]
    for r in rows:
        d = "-" if r["delta"] is None else f"{r['delta']:+.4f}"
        lines.append(
            f"| {r['key']} | {_fmt(r['base'])} | {_fmt(r['cur'])} | {d} | {r['status']} |"
        )
    return lines


def _render_report(
    retr: Optional[dict],
    ans: Optional[dict],
    thr: float,
    notes: List[str],
    audit: Optional[dict] = None,
) -> Tuple[str, bool]:
    """返回 (markdown 全文, 是否判定回归)。retr/ans 各为一侧对比所需的全部数据。"""
    lines: List[str] = [
        "# 评估对比报告",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 回归阈值：{thr:.2f}（任一质量指标回退超过该值即判回归，另幻觉违规率 0→非 0 直接判回归）",
    ]
    lines += [f"- {n}" for n in notes]
    regressed = False

    if retr:
        base_sum, base_det = retr["base_sum"], retr["base_det"]
        cur_sum, cur_det = retr["cur_sum"], retr["cur_det"]
        rows = _metric_rows(base_sum.get("overall_final") or {}, cur_sum.get("overall_final") or {}, RETR_KEYS, thr)
        regressed = regressed or any(r["regressed"] for r in rows)

        lines += [
            "",
            "## 检索层（overall_final）",
            "",
            f"- 基线：{retr['base_path'].name}（kb={base_sum.get('kb')}, n={base_sum.get('n_total')}, created_at={retr['base_payload'].get('created_at')}）",
            f"- 本轮：{retr['cur_path'].name}（kb={cur_sum.get('kb')}, n={cur_sum.get('n_total')}, created_at={retr['cur_payload'].get('created_at')}）",
            "",
        ]
        lines += _render_table(rows)

        srows = _stage_rows(base_sum, cur_sum, thr)
        lines += [
            "",
            "### 五阶段归因（回退定位：看 Δ 首先塌下去的那一跳）",
            "",
            "| 阶段 | 基线MRR | 本轮MRR | ΔMRR | 基线R@5 | 本轮R@5 | ΔR@5 | 状态 |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for r in srows:
            regressed = regressed or r["regressed"]
            lines.append(
                f"| {r['stage']} | {_fmt(r['base_mrr'])} | {_fmt(r['cur_mrr'])} | "
                f"{_fmt(r['d_mrr'])} | {_fmt(r['base_r5'])} | {_fmt(r['cur_r5'])} | "
                f"{_fmt(r['d_r5'])} | {'⚠' if r['regressed'] else '➖'} |"
            )

        neg_b = (base_sum.get("negative") or {}).get("empty_rate_at_5")
        neg_c = (cur_sum.get("negative") or {}).get("empty_rate_at_5")
        dn = None if (neg_b is None or neg_c is None) else round(neg_c - neg_b, 4)
        neg_status = "➖"
        if dn is not None:
            if _is_regression(dn, "empty_rate_at_5", thr):
                neg_status = "⚠"
                regressed = True
            elif dn >= thr:
                neg_status = "✅"
        if dn is not None:
            lines += [
                "",
                "### Negative 拒答（empty_rate@5 越高越好）",
                "",
                f"| 基线 | 本轮 | Δ | 状态 |",
                "|---|---|---|---|",
                f"| {_fmt(neg_b)} | {_fmt(neg_c)} | {('-' if dn is None else f'{dn:+.4f}')} | {neg_status} |",
                "",
            ]

        rids = _regressed_qids(base_det, cur_det)
        lines += ["", f"### 逐题回退（{len(rids)} 题）", ""]
        if rids:
            lines += ["| qid | 类型 | 基线MRR | 本轮MRR | 首个劣化阶段 |", "|---|---|---|---|---|"]
            lines += [
                f"| {r['qid']} | {r['query_type']} | {_fmt(r['base_mrr'])} | {_fmt(r['cur_mrr'])} | {r['first_stage']} |"
                for r in rids
            ]
        else:
            lines.append("无。")

    if ans:
        base, cur = ans["base_payload"], ans["cur_payload"]
        rows = _metric_rows(
            (base.get("aggregate") or {}).get("overall") or {},
            (cur.get("aggregate") or {}).get("overall") or {},
            ANS_KEYS,
            thr,
        )
        regressed = regressed or any(r["regressed"] for r in rows)
        for r in rows:
            if r["key"] == "forbidden_violation_rate" and (r["base"] or 0) == 0 and (r["cur"] or 0) > 0:
                regressed = True  # BASELINE.md 约定：幻觉违规率从 0 变非 0 即回归

        lines += [
            "",
            "## 回答层（aggregate.overall）",
            "",
            f"- 基线：{ans['base_path'].name}（kb={base.get('kb')}, n={base.get('n')}, judge={base.get('judge')}）",
            f"- 本轮：{ans['cur_path'].name}（kb={cur.get('kb')}, n={cur.get('n')}, judge={cur.get('judge')}）",
            "",
        ]
        lines += _render_table(rows)

        cases = _ans_case_lists(base.get("details") or [], cur.get("details") or [])
        lines += ["", f"### 新增幻觉违规（{len(cases['violations'])} 题）", ""]
        if cases["violations"]:
            lines += ["| qid | 类别 | 问题 |", "|---|---|---|"]
            lines += [f"| {c['qid']} | {c['category']} | {c['query']} |" for c in cases["violations"]]
        else:
            lines.append("无。")

        lines += ["", f"### 要点覆盖下降（{len(cases['cov_drops'])} 题）", ""]
        if cases["cov_drops"]:
            lines += ["| qid | 基线覆盖 | 本轮覆盖 | 问题 |", "|---|---|---|---|"]
            lines += [
                f"| {c['qid']} | {_fmt(c['base'])} | {_fmt(c['cur'])} | {c['query']} |"
                for c in cases["cov_drops"]
            ]
        else:
            lines.append("无。")

        lines += ["", f"### 行为回退（{len(cases['beh_drops'])} 题）", ""]
        if cases["beh_drops"]:
            lines += ["| qid | 期望 | 基线实际 | 本轮实际 |", "|---|---|---|---|"]
            lines += [
                f"| {c['qid']} | {c['expected']} | {c['base_actual']} | {c['cur_actual']} |"
                for c in cases["beh_drops"]
            ]
        else:
            lines.append("无。")

    if audit:
        c = audit.get("containment") or {}
        s = audit.get("chunk_stats") or {}
        chars = s.get("chars") or {}
        cpd = s.get("chunks_per_doc") or {}
        rate = c.get("containment_rate")
        judged = (c.get("n_contained") or 0) + (c.get("n_missing") or 0)
        lines += [
            "",
            "## 入库审计（轻量间接，不参与回归判定）",
            "",
            f"- kb={audit.get('kb')}，基于检索金标反推（created_at={audit.get('created_at')}）",
            "",
            "### snippet 包含率（金标片段 ⊂ 父块 ES 全文）",
            "",
        ]
        if rate is None:
            lines.append(f"无可用正例（n_checked={c.get('n_checked')}）。")
        else:
            lines.append(
                f"- **包含率 {rate:.2%}**（{c.get('n_contained')}/{judged}），"
                f"父块缺失 {c.get('n_parent_missing')} 条"
            )
            fails = c.get("failures") or []
            lines += ["", f"### 未包含 / 父块缺失清单（{len(fails)} 条）", ""]
            if fails:
                lines += ["| qid | 类型 | 状态 | snippet（截断） |", "|---|---|---|---|"]
                for f in fails:
                    status = "父块缺失" if f["contained"] is None else "未包含"
                    lines.append(
                        f"| {f['qid']} | {f.get('query_type')} | {status} | "
                        f"{(f.get('snippet') or '')[:48]} |"
                    )
            else:
                lines.append("无。")
        lines += [
            "",
            "### 子块统计哨兵",
            "",
            f"- 子块 {s.get('total_children')} / 父块 {s.get('total_parents')} / "
            f"含子块文档 {s.get('n_docs_with_chunks')}",
            f"- 空文本子块 **{s.get('empty_children')}**（ratio={s.get('empty_ratio')}）｜ "
            f"超长子块 **{s.get('overlong_children')}**（> {audit.get('overlong_chars')} 字符，"
            f"ratio={s.get('overlong_ratio')}）",
            f"- 块长：avg={chars.get('avg')} p95={chars.get('p95')} max={chars.get('max')} 字符",
            f"- 单子块父块 {cpd.get('single_chunk_docs')}（ratio={cpd.get('single_chunk_ratio')}）｜ "
            f"单文档最多 {cpd.get('max_chunks')} 块",
            "",
        ]
        sentinels = s.get("sentinels") or []
        if sentinels:
            lines += ["| 哨兵 | 级别 | 数量 | 提示 |", "|---|---|---|---|"]
            lines += [
                f"| {w['name']} | {w['level']} | {w['count']} | {w['hint']} |" for w in sentinels
            ]
        else:
            lines.append("无告警。")

    lines += [
        "",
        "## 结论",
        "",
        ("⚠ **存在回归**：上方 ⚠ 项超过阈值，先排查再继续。" if regressed else "✅ 无回归：所有指标变化在阈值内。"),
        "",
    ]
    return "\n".join(lines), regressed


# ---------------------------------------------------------------- 入口 ---------


def main(argv: Optional[Sequence[str]] = None) -> int:
    _setup_sys_path()
    parser = argparse.ArgumentParser(description="评估对比层：本轮 results vs 基线快照")
    parser.add_argument("--results-dir", default=None, help="本轮结果目录（默认 data/eval/results）")
    parser.add_argument("--baseline", default=None, help="检索基线 JSON（默认 baseline/ 最新）")
    parser.add_argument("--baseline-answer", default=None, help="回答基线 JSON（默认 baseline/ 最新）")
    parser.add_argument(
        "--retrieval-current", default=None, help="覆盖本轮检索结果路径（测试/任意快照对比用）"
    )
    parser.add_argument(
        "--answer-current", default=None, help="覆盖本轮回答结果路径（测试/任意快照对比用）"
    )
    parser.add_argument("--threshold", type=float, default=DEFAULT_FAIL_THRESHOLD, help="回归判定阈值")
    parser.add_argument("--stdout", action="store_true", help="只打印报告，不写 compare_report.md")
    args = parser.parse_args(argv)

    eval_dir = default_eval_dir()
    results_dir = Path(args.results_dir) if args.results_dir else default_results_dir()
    notes: List[str] = []
    retr_data = ans_data = None

    # ---- 检索侧 ----
    cur_r = Path(args.retrieval_current) if args.retrieval_current else results_dir / "retrieval_eval.json"
    base_r = Path(args.baseline) if args.baseline else _pick_latest_baseline(eval_dir, "retrieval")
    if cur_r.is_file() and base_r and base_r.is_file():
        cur_p, base_p = _load_json(cur_r), _load_json(base_r)
        cur_sum, cur_det = _retr_primary(cur_p)
        base_sum, base_det = _retr_primary(base_p)
        if base_sum.get("kb") != cur_sum.get("kb"):
            notes.append(f"⚠ 检索侧 kb 不一致：基线 {base_sum.get('kb')} vs 本轮 {cur_sum.get('kb')}，指标不可直接比")
        if base_sum.get("n_total") != cur_sum.get("n_total"):
            notes.append(
                f"⚠ 检索评测题数变化：基线 {base_sum.get('n_total')} vs 本轮 {cur_sum.get('n_total')}，均值口径有偏"
            )
        retr_data = {
            "base_payload": base_p,
            "cur_payload": cur_p,
            "base_sum": base_sum,
            "base_det": base_det,
            "cur_sum": cur_sum,
            "cur_det": cur_det,
            "base_path": base_r,
            "cur_path": cur_r,
        }
    else:
        missing = cur_r.name if not cur_r.is_file() else base_r.name if base_r else "baseline/ 无检索快照"
        notes.append(f"跳过检索层对比（缺 {missing}）")

    # ---- 回答侧 ----
    cur_a = Path(args.answer_current) if args.answer_current else results_dir / "answer_eval.json"
    base_a = Path(args.baseline_answer) if args.baseline_answer else _pick_latest_baseline(eval_dir, "answer")
    if cur_a.is_file() and base_a and base_a.is_file():
        cur_p, base_p = _load_json(cur_a), _load_json(base_a)
        if base_p.get("judge") != cur_p.get("judge"):
            notes.append(
                f"⚠ 回答侧 judge 设置不一致：基线 judge={base_p.get('judge')} vs 本轮 judge={cur_p.get('judge')}，分数口径有偏"
            )
        if base_p.get("kb") != cur_p.get("kb"):
            notes.append(f"⚠ 回答侧 kb 不一致：基线 {base_p.get('kb')} vs 本轮 {cur_p.get('kb')}")
        ans_data = {"base_payload": base_p, "cur_payload": cur_p, "base_path": base_a, "cur_path": cur_a}
    else:
        missing = cur_a.name if not cur_a.is_file() else base_a.name if base_a else "baseline/ 无回答快照"
        notes.append(f"跳过回答层对比（缺 {missing}）")

    if retr_data is None and ans_data is None:
        print("没有可对比的两侧数据：既缺本轮 results，也缺对应基线。先跑 runner / eval_answer。")
        return 2

    # ---- 入库审计（可选：results/ingestion_audit.json 存在则并入报告）----
    audit_data: Optional[dict] = None
    audit_path = results_dir / "ingestion_audit.json"
    if audit_path.is_file():
        try:
            audit_data = _load_json(audit_path)
        except Exception as e:
            notes.append(f"⚠ 入库审计文件解析失败，已跳过：{type(e).__name__}: {e}")

    report, regressed = _render_report(retr_data, ans_data, args.threshold, notes, audit=audit_data)

    if args.stdout:
        print(report)
    else:
        out = results_dir / "compare_report.md"
        results_dir.mkdir(parents=True, exist_ok=True)
        out.write_text(report, encoding="utf-8")
        print(f"wrote {out}")

    print("结论：" + ("存在回归（exit 1）" if regressed else "无回归"))
    return 1 if regressed else 0


if __name__ == "__main__":
    sys.exit(main())
