# -*- coding: utf-8 -*-
"""DOCX 标题修复：按 Word 的 outlineLvl 重建 Markdown 标题。

背景（2026-09-10 实测，详见 data/eval/parse_eval/results/REPORT.md 第 7.6 节）：
docling 的 ``msword_backend._get_label_and_level()`` 判定标题时只认样式名 / style_id
里的英文 "heading" 字样，而 Word 里真正权威的标题级别来源 ``outlineLvl`` 的查询恰被锁在
该判断**内部**。后果：

1. 中文自定义标题样式（如"标准文件_章标题""标准文件_一级条标题"，outlineLvl 全部合法）
   整份文档不被识别 —— 标题降级为正文/列表项，父子块切分拿不到 title_path；
2. 标准 Heading 样式的层级可能偏移（实测 ``Heading 2`` → 输出 ``###``，偏移 +1）；
3. 代码行（如 ``#!/bin/bash``）被启发式误升级为标题，污染标题树，使其后正文挂错父节。

本模块**不修改第三方库**：解析完成后用 python-docx 读段落 outlineLvl，重写 Markdown 中
对应行的标题级别，并把不在 outlineLvl 清单里的伪标题行降级回正文。

安全设计（三条兜底，任一条不满足都原样返回）：

- 只在"文档里存在 docling 认不出的标题样式"时启用，否则输出与改动前逐字节一致；
- 对齐命中率不足一半时放弃（宁可漏修，不可改坏）；
- 任何异常都被吞掉，解析流程不受影响。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence, Union

PathLike = Union[str, Path]

# outlineLvl 语义：0-8 对应 1-9 级大纲；9 表示正文（不算标题）
_MAX_OUTLINE = 8
_MAX_MD_LEVEL = 6

_MD_HEADING = re.compile(r"^(#{1,6})[ \t]*(.*?)[ \t]*$")
_WS = re.compile(r"[\s\u3000]+")


@dataclass
class Heading:
    """从 DOCX 段落大纲级别读出的标题。"""

    text: str
    level: int


@dataclass
class PatchStats:
    expected: int = 0
    rewritten: int = 0
    demoted: int = 0
    dropped_empty: int = 0
    unmatched: list[str] = field(default_factory=list)


def _norm(s: str) -> str:
    """归一化：去空白与全角空格，用于跨格式匹配。"""
    return _WS.sub("", s or "")


def _w(tag: str) -> str:
    from docx.oxml.ns import qn

    return qn(tag)


def _outline_from_element(el) -> Optional[int]:
    if el is None:
        return None
    try:
        node = el.find(".//" + _w("w:outlineLvl"))
    except Exception:
        return None
    if node is None:
        return None
    try:
        value = int(node.get(_w("w:val")))
    except (TypeError, ValueError):
        return None
    return value if 0 <= value <= _MAX_OUTLINE else None


def _paragraph_outline(p) -> Optional[int]:
    """段落的大纲级别：段落直接格式优先，其次样式定义（含基样式链）。"""
    try:
        ppr = p._p.find(_w("w:pPr"))
    except Exception:
        ppr = None
    level = _outline_from_element(ppr)
    if level is not None:
        return level

    style = getattr(p, "style", None)
    guard = 0
    while style is not None and guard < 10:
        level = _outline_from_element(getattr(style, "element", None))
        if level is not None:
            return level
        style = getattr(style, "base_style", None)
        guard += 1
    return None


def _style_is_heading_for_docling(style) -> bool:
    """复刻 docling 的判定：样式名 / style_id / 基样式链里含 "heading"。"""
    guard = 0
    while style is not None and guard < 10:
        label = (getattr(style, "style_id", "") or "").lower()
        name = (getattr(style, "name", "") or "").lower()
        if "heading" in label or "heading" in name:
            return True
        style = getattr(style, "base_style", None)
        guard += 1
    return False


def scan_docx(docx_path: PathLike) -> tuple[list[Heading], bool]:
    """扫描 DOCX 段落，返回 (标题序列, docling 是否存在漏识别风险)。

    ``risky`` 为真表示文档里存在"有合法 outlineLvl、但样式名不含 heading"的段落，
    这类段落在 docling 输出里会退化为正文或列表项。
    """
    import docx

    document = docx.Document(str(docx_path))
    headings: list[Heading] = []
    risky = False
    for para in document.paragraphs:
        text = (para.text or "").strip()
        if not text:
            continue
        outline = _paragraph_outline(para)
        if outline is None:
            continue
        level = min(outline + 1, _MAX_MD_LEVEL)
        headings.append(Heading(text=text, level=level))
        if not _style_is_heading_for_docling(getattr(para, "style", None)):
            risky = True
    return headings, risky


_MD_MARKS = re.compile(r"^[\s*_`]+|[\s*_`]+$")
_LIST_MARK = re.compile(r"^[-*\u2022>\s]+")
# 列表标记 / 前导章节编号（如 "- 1"、"1.2.3"）与尾部页码（如 "\t1"、"II"）
_DECOR_LEAD = re.compile(r"^[-*\u2022>`\s`]*(?:[0-9]+(?:\.[0-9]+)*)?")
_DECOR_TAIL = re.compile(r"[0-9ivxIVX]+$")


def _strip_decorations(norm_text: str) -> str:
    """剥掉强调标记 / 列表标记 / 前导章节编号 / 尾部页码，得到标题本体。

    docling 会把带自动编号的中文标题输出成列表项（如 ``- 1　 安装指南``），
    目录条目则形如 ``1 安装指南\\t1``，剥掉装饰后都等于标题本体。
    """
    s = _MD_MARKS.sub("", norm_text or "")
    s = _DECOR_LEAD.sub("", s)
    return _DECOR_TAIL.sub("", s)


def _match_score(want: str, got: str) -> int:
    """匹配强度：2=完全一致，1=剥掉装饰后等于标题本体，0=不匹配。

    刻意不做"宽松包含"匹配——短标题（如"工具"）会被长句误命中，
    让候选标题被错误消耗。
    """
    if not want or not got:
        return 0
    if want == got:
        return 2
    if _strip_decorations(got) == want:
        return 1
    return 0


def _is_toc_like(norm_line: str, norm_head: str) -> bool:
    """目录条目判定：带尾部页码、且剥掉装饰后等于标题本体。

    目录里的同名文本若不排除，会抢占正文标题的位置。
    """
    if not norm_line or not norm_head:
        return False
    if not _DECOR_TAIL.search(norm_line):
        return False  # 没有页码 → 不是目录条目
    return _strip_decorations(norm_line) == norm_head


def _clean_title_text(text: str) -> str:
    """标题行写回前清理：去强调标记、去列表标记（保留章节编号）。"""
    s = _MD_MARKS.sub("", text or "")
    s = _LIST_MARK.sub("", s)
    return s.strip()


def patch_markdown(
    markdown: str, headings: Sequence[Heading]
) -> tuple[str, PatchStats]:
    """按标题清单重写 Markdown：命中则改正级别，未命中的标题行降级为正文。

    匹配策略（两阶段）：

    ① 每行挑选一个最佳候选标题（匹配分优先，同分取序号小者）；
    ② 对全部候选求**最长递增子序列**（按标题序号递增），只保留这条链。

    为什么需要 ②：正文里会出现与标题同名的图内 OCR 文字（示意图里的"工具""文件及样式"），
    它们位置零散、无法与前后候选串成递增链，会在求 LIS 时被自然剔除；而真正的标题
    沿文档顺序构成最长链。仅用"顺序游标"或"单调约束"都不行——一次误配就会吞掉其后的标题。
    """
    stats = PatchStats(expected=len(headings))
    parsed: list[tuple[str, Optional["re.Match"], str, str]] = []
    for raw in (markdown or "").split("\n"):
        m = _MD_HEADING.match(raw)
        body = m.group(2).strip() if m else raw.strip()
        parsed.append((raw, m, body, _norm(body)))

    # ① 每行一个最佳候选
    line_cand: dict[int, tuple[int, int]] = {}  # 行号 -> (标题序号, 匹配分)
    for idx, (_raw, _m, _body, key) in enumerate(parsed):
        if not key:
            continue
        best: Optional[tuple[int, int]] = None
        for j, h in enumerate(headings):
            want = _norm(h.text)
            if _is_toc_like(key, want):
                continue
            score = _match_score(want, key)
            if score and (best is None or score > best[1]):
                best = (j, score)
        if best is not None:
            line_cand[idx] = best

    # ② 最长递增子序列，剔除孤立的同名图内文字
    items = sorted(line_cand.items())
    total = len(items)
    dp = [1] * total
    back = [-1] * total
    for a in range(total):
        for b in range(a):
            if items[b][1][0] < items[a][1][0] and dp[b] + 1 > dp[a]:
                dp[a] = dp[b] + 1
                back[a] = b

    taken: set[int] = set()
    assign: dict[int, int] = {}
    if total:
        cursor = max(range(total), key=lambda t: dp[t])
        while cursor != -1:
            line_idx, (j, _score) = items[cursor]
            assign[line_idx] = j
            taken.add(j)
            cursor = back[cursor]

    out: list[str] = []
    for idx, (raw, m, body, key) in enumerate(parsed):
        if not key:
            if m:
                stats.dropped_empty += 1  # 空标题行（如 "## "）直接丢弃
                continue
            out.append(raw)
            continue
        j = assign.get(idx)
        if j is not None:
            out.append("#" * headings[j].level + " " + (_clean_title_text(body) or body))
            stats.rewritten += 1
            continue
        if m:
            out.append(_clean_title_text(body))  # 伪标题（无 outlineLvl 背书）→ 降级为正文
            stats.demoted += 1
            continue
        out.append(raw)

    stats.unmatched = [h.text for j, h in enumerate(headings) if j not in taken]
    return "\n".join(out), stats


def patch_docx_markdown(markdown: str, docx_path: PathLike) -> str:
    """DOCX 解析后置钩子：按 outlineLvl 重写标题。

    启用条件（三者同时满足，否则返回原 Markdown）：
    1. 补丁确实产生了改动；
    2. 存在"docling 处理不当"的迹象（自定义标题样式 / 伪标题被降级 / 空标题被丢弃）；
    3. 对齐命中率至少一半。

    另外，任何异常都被吞掉并原样返回。
    """
    if not markdown:
        return markdown
    try:
        headings, risky = scan_docx(docx_path)
    except Exception:
        return markdown
    if not headings:
        return markdown
    try:
        patched, stats = patch_markdown(markdown, headings)
    except Exception:
        return markdown
    if patched == markdown:
        return markdown
    worth = risky or stats.demoted > 0 or stats.dropped_empty > 0
    if not worth:
        return markdown
    if stats.rewritten * 2 < len(headings):
        return markdown
    return patched
