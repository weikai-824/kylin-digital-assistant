"""回答评估任务指标：通用、不绑定具体知识库。

核心打分只依赖 (query, answer, expected_points, forbidden_points, expected_behavior)，
换库后同一套题仍可复用。
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence

from kylin.ports.application.knowledge_qa import REFUSAL_NO_EVIDENCE

# ---- 拒答话术集合：只认协议规定的固定形态 ----
# 教训（2026-08-27 扩集 50 题实测）：
# 1. 模糊短语（无法获取/无法提供/无法给出）会命中事实句本身——
#    「紧急恢复密钥用于在无法获取手机动态口令时…」是完整答案而非拒答；
# 2. 弱证据规则 2 的标准结尾「无法给出完整答复，请咨询相关部门」是
#    合规的部分回答（已交付带引用的事实），不是拒答。
# 因此：短语收紧为协议形态，弱证据声明结尾单独判（见 classify_behavior）。
_REFUSE_PHRASES = (
    "无法回答该问题",      # 弱证据规则 3 / 接地模板拒答句
    "无法给出确切答复",    # 硬门固定话术
    "未找到足以回答",      # 硬门固定话术
    "不足以回答",
    "没有找到相关",
    "知识库中没有",
    "暂无相关",
    "没有相关资料",
    "没有足够信息",
    "不能回答",
    "不知道",
    "不清楚",
)
# 弱证据规则 2 的标准声明局限结尾：出现即说明走了「部分回答」路径
_WEAK_DISCLAIMERS = (
    "资料未完整覆盖",
    "无法给出完整答复",
)
# REFUSAL_NO_EVIDENCE 固定话术本身必然命中，单列方便精确比较
REFUSAL_FIXED = REFUSAL_NO_EVIDENCE

_CITE_RE = re.compile(r"\[(\d+)\]")

# 默认阈值（可被 runner 覆盖）
DEFAULT_MAX_CHARS = 300
DEFAULT_MAX_CITATIONS = 5


def _tail_block(text: str) -> str:
    """取最终答复所在的末段：QwQ 等推理模型的思考可能不带标签混入正文，
    但最终答复必然在尾部。按换行切段取最后一个非空段，抗思考污染。"""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[-1] if lines else text


def _final_segment(text: str) -> str:
    """取  Mild 闭标签之后的最终答复段。

    QwQ 有时不输出开标签只输出闭标签，_strip_think 兜不住；
    引用标记是否属于「最终答复」应以闭标签之后的内容为准，不含思考文字。
    """
    idx = text.rfind("</think>")
    return text[idx:] if idx >= 0 else text


def classify_behavior(answer: str) -> str:
    """
    规则判定系统最终行为：refuse / answer。

    判定依据：最终答复（末段）是否命中拒答话术集合。只判定末段而非全文，
    避免推理模型思考文字里的「无法回答」等自问自答污染结果。

    弱证据规则 2 例外：末段命中标准声明局限（资料未完整覆盖/无法给出完整答复）
    且最终答复段带引用标记 [N] —— 说明已交付带引用的事实内容，属合规「部分回答」，
    判 answer 而非 refuse。纯拒答（规则 3/硬门）不带引用也不带该声明，不受影响。
    """
    text = (answer or "").strip()
    if not text:
        return "answer"  # 空输出视为异常 answer，由其他指标暴露
    tail = _tail_block(text)
    if any(p in tail for p in _WEAK_DISCLAIMERS):
        seg = _final_segment(text)
        if _CITE_RE.search(seg):
            return "answer"
    for phrase in _REFUSE_PHRASES:
        if phrase in tail:
            return "refuse"
    return "answer"


def is_fixed_refusal(answer: str) -> bool:
    """是否完整命中固定拒答话术（未做任何润色/扩展）。"""
    return (answer or "").strip() == REFUSAL_FIXED.strip()


def behavior_score(expected_behavior: str, actual_behavior: str) -> float:
    """
    行为准确率（0/1，partial 用 0.5 表达部分正确）。

    - expected=refuse  → 实际 refuse 得 1，answer 得 0
    - expected=answer  → 实际 answer 得 1，refuse 得 0
    - expected=partial → 实际 answer 得 1（给了一部分），refuse 得 0.5（过度保守）
    """
    if expected_behavior == "refuse":
        return 1.0 if actual_behavior == "refuse" else 0.0
    if expected_behavior == "answer":
        return 1.0 if actual_behavior == "answer" else 0.0
    # partial
    if actual_behavior == "answer":
        return 1.0
    if actual_behavior == "refuse":
        return 0.5
    return 0.0


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def point_coverage(answer: str, expected_points: Sequence[str]) -> Optional[float]:
    """
    要点覆盖度：命中 expected_points 的比例。无期望点时返回 None（跳过）。
    匹配用去空白子串包含，宽松处理模型措辞差异。
    """
    if not expected_points:
        return None
    norm_answer = _norm(answer)
    hit = sum(1 for p in expected_points if _norm(p) in norm_answer)
    return hit / len(expected_points)


def forbidden_violation(answer: str, forbidden_points: Sequence[str]) -> Optional[int]:
    """
    是否出现禁止事实点（幻觉/张冠李戴）：返回 0/1；无禁止点时 None。
    出现任意一个即视为违规（扣分项，单独成指标）。
    """
    if not forbidden_points:
        return None
    norm_answer = _norm(answer)
    for p in forbidden_points:
        if _norm(p) and _norm(p) in norm_answer:
            return 1
    return 0


def extract_citations(answer: str) -> List[int]:
    """提取回答中出现的引用编号（去重、保序）。"""
    if not answer:
        return []
    seen: List[int] = []
    for m in _CITE_RE.finditer(answer):
        n = int(m.group(1))
        if n not in seen:
            seen.append(n)
    return seen


def citation_quality(answer: str, n_docs: int) -> Dict[str, Any]:
    """
    引用质量：编号是否落在 [1, n_docs]（即真实存在对应出处）。

    返回 {citations, illegal, ratio}；n_docs<=0（无证据）时引用任何编号都非法。
    """
    cites = extract_citations(answer)
    if not cites:
        return {"citations": [], "illegal": [], "ratio": None}
    illegal = [n for n in cites if n < 1 or n > n_docs]
    return {
        "citations": cites,
        "illegal": illegal,
        "ratio": 1.0 - (len(illegal) / len(cites)) if cites else None,
    }


def conciseness_score(
    answer: str,
    *,
    expected_behavior: str,
    max_chars: int = DEFAULT_MAX_CHARS,
    max_citations: int = DEFAULT_MAX_CITATIONS,
) -> Dict[str, Any]:
    """
    简洁度。

    - refuse 期望：应输出固定话术，长度超固定话术 + 50% 即扣分
    - answer / partial：字数超 max_chars 或引用超 max_citations 即 0 分
    返回 {chars, n_citations, score(0/1)}
    """
    text = (answer or "").strip()
    chars = len(text)
    n_cites = len(extract_citations(text))

    if expected_behavior == "refuse":
        fixed_len = len(REFUSAL_FIXED.strip())
        ok = chars <= int(fixed_len * 1.5)
    else:
        ok = chars <= max_chars and n_cites <= max_citations
    return {"chars": chars, "n_citations": n_cites, "score": 1.0 if ok else 0.0}


def score_one_answer(
    row: Dict[str, Any],
    *,
    answer: str,
    n_docs: int,
    max_chars: int = DEFAULT_MAX_CHARS,
    max_citations: int = DEFAULT_MAX_CITATIONS,
) -> Dict[str, Any]:
    """单题规则打分（不含 LLM-judge）。返回完整指标字典。"""
    expected = row.get("expected_behavior") or "answer"
    actual = classify_behavior(answer)

    cov = point_coverage(answer, row.get("expected_points") or [])
    forb = forbidden_violation(answer, row.get("forbidden_points") or [])
    cite = citation_quality(answer, n_docs)
    concise = conciseness_score(
        answer,
        expected_behavior=expected,
        max_chars=max_chars,
        max_citations=max_citations,
    )

    # 综合事实分：期望 answer/partial 时 = 覆盖度 * (无禁止事实)；无期望点则仅看行为
    if cov is None:
        fact_score = 1.0 if forb != 1 else 0.0
    else:
        fact_score = cov * (0.0 if forb == 1 else 1.0)

    return {
        "expected_behavior": expected,
        "actual_behavior": actual,
        "is_fixed_refusal": is_fixed_refusal(answer),
        "behavior_score": behavior_score(expected, actual),
        "expected_coverage": cov,
        "forbidden_violation": forb,
        "fact_score": round(fact_score, 4),
        "citations": cite["citations"],
        "illegal_citations": cite["illegal"],
        "citation_ratio": cite["ratio"],
        "conciseness": concise,
    }


def judge_factuality(
    query: str,
    answer: str,
    row: Dict[str, Any],
    llm: Any,
    *,
    max_tokens: int = 256,
) -> Dict[str, Any]:
    """
    LLM-as-judge（可选）：裁判模型判定回答是否事实一致、无幻觉。

    llm 为 langchain 可 invoke 的 chat 对象（与主链路同款 get_ChatOpenAI）。
    返回 {judge_pass: 0/1, judge_reason, judge_error}。
    """
    expected_points = "；".join(row.get("expected_points") or []) or "（无）"
    forbidden_points = "；".join(row.get("forbidden_points") or []) or "（无）"
    system = (
        "你是回答质量裁判。给定问题、参考答案要点、禁止出现的事实点和一个 AI 回答，"
        "判断该回答是否：1) 覆盖了应覆盖的要点（允许措辞不同）；"
        "2) 没有出现禁止的事实点；3) 没有编造。"
        "只输出 JSON：{\"pass\": 0 或 1, \"reason\": \"一句话理由\"}"
    )
    user = (
        f"问题：{query}\n"
        f"应覆盖要点：{expected_points}\n"
        f"禁止出现：{forbidden_points}\n"
        f"AI 回答：{answer}"
    )
    try:
        resp = llm.invoke(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ]
        )
        raw = getattr(resp, "content", None)
        text = raw if isinstance(raw, str) else str(resp)
        # 提取 JSON
        import json

        s = text.strip()
        start, end = s.find("{"), s.rfind("}")
        if start >= 0 and end > start:
            data = json.loads(s[start : end + 1])
            return {
                "judge_pass": 1 if int(data.get("pass", 0)) == 1 else 0,
                "judge_reason": str(data.get("reason", "")),
                "judge_error": "",
            }
        return {"judge_pass": 0, "judge_reason": f"judge 输出非 JSON: {text[:200]}", "judge_error": "bad_json"}
    except Exception as e:
        return {"judge_pass": 0, "judge_reason": "", "judge_error": f"{type(e).__name__}: {e}"}


# ---- 汇总 ----
def _mean(xs: Iterable[float]) -> float:
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0


def aggregate_details(details: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """对逐题结果做总体 + 分组汇总。"""
    if not details:
        return {"n": 0}

    def _slice(rows: Sequence[dict]) -> dict:
        out: dict = {"n": len(rows)}
        out["behavior_acc"] = round(_mean(r["behavior_score"] for r in rows), 4)
        covs = [r["expected_coverage"] for r in rows if r["expected_coverage"] is not None]
        out["point_coverage"] = round(_mean(covs), 4) if covs else None
        forb = [r["forbidden_violation"] for r in rows if r["forbidden_violation"] is not None]
        out["forbidden_violation_rate"] = round(_mean(forb), 4) if forb else None
        out["fact_score"] = round(_mean(r["fact_score"] for r in rows), 4)
        out["conciseness"] = round(_mean((r["conciseness"] or {}).get("score", 0.0) for r in rows), 4)
        # 引用质量：只看确实引用了的回答
        cited = [r["citation_ratio"] for r in rows if r.get("citation_ratio") is not None]
        out["citation_valid_ratio"] = round(_mean(cited), 4) if cited else None
        out["avg_chars"] = round(_mean((r["conciseness"] or {}).get("chars", 0) for r in rows), 1)
        out["avg_citations"] = round(_mean((r["conciseness"] or {}).get("n_citations", 0) for r in rows), 2)
        return out

    overall = _slice(details)

    by_category: Dict[str, dict] = {}
    for cat in sorted({r.get("category", "?") for r in details}):
        rows = [r for r in details if r.get("category") == cat]
        by_category[cat] = _slice(rows)

    by_behavior: Dict[str, dict] = {}
    for beh in sorted({r.get("expected_behavior", "?") for r in details}):
        rows = [r for r in details if r.get("expected_behavior") == beh]
        by_behavior[beh] = _slice(rows)

    return {"overall": overall, "by_category": by_category, "by_behavior": by_behavior}
