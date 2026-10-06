"""Markdown 清洗：解析之后、切分之前。

原则：只删能证明是噪声的内容，拿不准就留。
不做 LLM 重写、不改正文错字、不删变更记录整节、不删「图 N」。

来源分治（2026-09-10 新增）：解析层会给「图区 OCR 文本」夹一层来源标记
（``<!-- ocr:<来源> --> ... <!-- /ocr -->``，见 ``parse/picture_text.py``）。
截图型 OA 手册的正文有 51%~64% 来自图区 OCR，且其中 81%~86% 的行是 ≤8 字碎片。
清洗层**只在该标记范围内**启用「超短残片」判据；正文层一条规则都不加。

为什么不按「重复次数」删：实测（通知公告）会把 ``办公协同平台上线通知``（该通知的
标题，出现在 6 张截图里）整份删掉——重复既可能是 UI 固定元素，也可能是真内容，
纯频次判据无法区分，故不采用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

from kylin.adapters.outbound.knowledge_base.clean.patterns import TABLE_ROW_CELLS, TABLE_SEP

from kylin.adapters.outbound.knowledge_base.clean.domain_words import (
    DOMAIN_WORDS,
    OCR_FIX_MAP,
    PROTECTED_NEIGHBORS,
)

# 叠字标题：整段重复「快速入门 快速入门」「发起流程 发起流程 发起流程」
_HEADING_EXACT_REPEAT = re.compile(
    r"^(#{1,6}\s+)(.+?)(?:\s+\2)+\s*$"
)
# 首尾同词：`第一部分 登录功能说明 第一部分`
_HEADING_BOOKEND = re.compile(
    r"^(#{1,6}\s+)(\S+)\s+(.+?)\s+\2\s*$"
)
# 非标题整行叠字：`质量与信息化流程部 质量与信息化流程部`
_LINE_EXACT_REPEAT = re.compile(r"^(.+?)(?:\s+\1)+\s*$")

_IMAGE_PLACEHOLDER = re.compile(r"^\s*<!--\s*image\s*-->\s*$", re.IGNORECASE)

# 图区 OCR 来源标记（成对出现，标记行本身保留，供下游按来源分治）
_OCR_MARK_OPEN = re.compile(r"^\s*<!--\s*ocr:[^>]*?-->\s*$")
_OCR_MARK_CLOSE = re.compile(r"^\s*<!--\s*/ocr\s*-->\s*$")

# 图区 OCR 超短残片：去空白后 <=2 字符且不含汉字。
# 典型：勾选标记被 OCR 成 ``V``、关闭按钮 ``×``、步骤序号 ``2``/``3``、徽标数字 ``0``。
# 判据保守——只要出现一个汉字就放行，正文层不适用此规则。
_OCR_FRAGMENT_MAX_LEN = 2

_MULTI_BLANK = re.compile(r"\n{3,}")


@dataclass
class CleanResult:
    text: str
    removed_dup_headings: int = 0
    removed_dup_lines: int = 0
    removed_empty_table_rows: int = 0
    removed_watermarks: int = 0
    removed_image_placeholders: int = 0
    fixed_domain_words: int = 0
    ocr_lines: int = 0
    removed_ocr_fragments: int = 0


def clean_markdown(text: str) -> CleanResult:
    """对解析得到的 Markdown 做中档清洗，返回结果与计数。"""
    if not text:
        return CleanResult(text="")

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: List[str] = []
    stats = {
        "removed_dup_headings": 0,
        "removed_dup_lines": 0,
        "removed_empty_table_rows": 0,
        "removed_watermarks": 0,
        "removed_image_placeholders": 0,
        "fixed_domain_words": 0,
        "ocr_lines": 0,
        "removed_ocr_fragments": 0,
    }
    prev_kept: Optional[str] = None
    in_ocr_region = False

    for line in lines:
        # 0) 图区 OCR 区域边界：标记行原样保留（下游按来源分治要用）
        if _OCR_MARK_OPEN.match(line):
            in_ocr_region = True
            out.append(line)
            prev_kept = line
            continue
        if _OCR_MARK_CLOSE.match(line):
            in_ocr_region = False
            out.append(line)
            prev_kept = line
            continue

        # 1) 空图占位
        if _IMAGE_PLACEHOLDER.match(line):
            stats["removed_image_placeholders"] += 1
            continue

        # 1.5) 图区 OCR 区域内的超短残片（仅此区域生效，正文层不受影响）
        if in_ocr_region:
            stats["ocr_lines"] += 1
            if _is_ocr_fragment(line):
                stats["removed_ocr_fragments"] += 1
                continue

        # 2) 水印 / 纯页脚域名
        if _is_watermark_line(line):
            stats["removed_watermarks"] += 1
            continue

        # 3) 空表行（保留表头与已填行、分隔行）
        if _is_empty_table_row(line):
            stats["removed_empty_table_rows"] += 1
            continue

        # 4) 叠字标题 / 同行整段叠字
        new_line, heading_hit = _collapse_dup_heading(line)
        if heading_hit:
            stats["removed_dup_headings"] += 1
            line = new_line
        elif not TABLE_ROW_CELLS.match(line) and not TABLE_SEP.match(line):
            m = _LINE_EXACT_REPEAT.match(line.strip())
            if m and " " in line.strip():
                line = m.group(1)
                stats["removed_dup_headings"] += 1

        # 5) 连续完全相同行（含清洗后）
        if prev_kept is not None and line.strip() and line.strip() == prev_kept.strip():
            stats["removed_dup_lines"] += 1
            continue

        out.append(line)
        prev_kept = line

    text_out = "\n".join(out)
    text_out = _MULTI_BLANK.sub("\n\n", text_out).strip() + ("\n" if text.strip() else "")

    # 6) 领域词典纠错（OCR 专名误读修正，最后做，只改字不改结构）
    text_out, n_fixes = correct_domain_words(text_out)
    stats["fixed_domain_words"] = n_fixes

    return CleanResult(text=text_out, **stats)


def clean_markdown_text(text: str) -> str:
    """只返回清洗后的字符串。"""
    return clean_markdown(text).text


def _collapse_dup_heading(line: str) -> tuple[str, bool]:
    m = _HEADING_EXACT_REPEAT.match(line)
    if m:
        return f"{m.group(1)}{m.group(2)}", True
    m = _HEADING_BOOKEND.match(line)
    if m:
        return f"{m.group(1)}{m.group(2)} {m.group(3)}", True
    return line, False


def _is_empty_table_row(line: str) -> bool:
    if TABLE_SEP.match(line):
        return False
    m = TABLE_ROW_CELLS.match(line)
    if not m:
        return False
    cells = [c.strip() for c in m.group(1).split("|")]
    if not cells:
        return False
    return all(c == "" for c in cells)


def _is_watermark_line(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    if s.lower() == "www.example.com":
        return True
    # 去掉已知水印片段后若无实质内容，则整行丢弃
    t = s
    t = re.sub(r"示例页脚", "", t)
    t = re.sub(r"(?i)www\.kylinsec\.com\.cn", "", t)
    t = re.sub(r"[|/\s·•\-—_]+", "", t)
    return bool(re.search(r"示例页脚", s)) and t == ""


def _is_ocr_fragment(line: str) -> bool:
    """图区 OCR 区域内的超短残片（仅在 ``<!-- ocr -->`` 区域内调用）。

    判据刻意保守：去空白后 1~2 字符**且不含任何汉字**才判为残片。
    含一个汉字就放行——单字中文可能是真实字段值（如"是""否"），漏删代价远小于误删。
    纯符号/拉丁字母/数字的 1~2 字符行在截图里几乎只可能是 UI 元素（勾选、关闭、
    步骤序号、徽标数字）被 OCR 出来的碎片。
    """
    s = (line or "").strip()
    if not s or len(s) > _OCR_FRAGMENT_MAX_LEN:
        return False
    return not any("\u4e00" <= ch <= "\u9fff" for ch in s)


# ---------------------------------------------------------------- 领域词典纠错
# 只处理 OCR 对专有名词的误读；通用错字不在此层（避免误伤）。

_FUZZY_MIN_LEN = 4  # 白名单模糊匹配的最小词长，更短的词误伤风险高，只走显式映射


def correct_domain_words(text: str) -> tuple[str, int]:
    """用领域词典修正 OCR 专名误读，返回 (修正后文本, 修正次数)。

    两层策略：
    1. 显式映射 OCR_FIX_MAP：处理乱序/增删字等模糊匹配覆盖不了的错误；
    2. 白名单 DOMAIN_WORDS：等长且恰有 1 个字符不同的近似形态（首字符须相同）。
    只改字符、不改行结构，可直接嵌入清洗流程。
    """
    if not text:
        return text, 0
    fixed = 0
    for wrong, right in OCR_FIX_MAP.items():
        if wrong in text:
            text = text.replace(wrong, right)
            fixed += 1
    for word in DOMAIN_WORDS:
        if len(word) < _FUZZY_MIN_LEN:
            continue
        text, n = _fuzzy_replace(text, word)
        fixed += n
    return text, fixed


def _fuzzy_replace(text: str, word: str) -> tuple[str, int]:
    """全文扫描，把与 word 恰有 1 个字符不同的等长子串替换为 word。

    防误伤三闸门（缺一不可）：
    1. 长词内部检测：候选词与前/后紧邻的一个中文字拼接后若构成白名单词，
       说明它是「城市间交通费」这类长词的内部子串，放行不替换；
    2. 合法邻居保护：候选词在 PROTECTED_NEIGHBORS（源文件文字层真实存在的
       近似词）里时放行，避免把「对公付款后」「对公收款单」这类合法文本改掉；
    3. 歧义保护：候选词与其它白名单词（等长、非 target）也单字差异时放弃。
    首字符（不区分大小写）必须相同才进入比较；原样出现的正确形态（距离 0）不替换。
    """
    wl = len(word)
    if wl < 2:
        return text, 0
    low_text = text.lower()
    low_word = word.lower()
    needle = low_word[0]
    out = text
    fixed = 0
    pos = 0
    while True:
        i = low_text.find(needle, pos)
        if i < 0 or i + wl > len(text):
            break
        cand = text[i : i + wl]
        if cand != word and _hamming_eq1(cand.lower(), low_word):
            if (
                _inside_long_word(text, i, cand)
                or cand in PROTECTED_NEIGHBORS
                or _ambiguous_candidate(cand, word)
            ):
                pos = i + 1
                continue
            out = out[:i] + word + out[i + wl :]
            low_text = low_text[:i] + low_word + low_text[i + wl :]
            fixed += 1
            pos = i + wl
        else:
            pos = i + 1
    return out, fixed


def _inside_long_word(text: str, i: int, cand: str) -> bool:
    """候选词与前/后紧邻一个中文字拼接后是白名单词 → 属于长词内部子串。"""
    wl = len(cand)
    if i > 0:
        prev_ch = text[i - 1]
        if _is_cjk(prev_ch) and (prev_ch + cand) in DOMAIN_WORDS:
            return True
    if i + wl < len(text):
        next_ch = text[i + wl]
        if _is_cjk(next_ch) and (cand + next_ch) in DOMAIN_WORDS:
            return True
    return False


def _is_cjk(ch: str) -> bool:
    return "\u4e00" <= ch <= "\u9fff"


def _ambiguous_candidate(cand: str, target: str) -> bool:
    """候选词与其它白名单词（等长、非 target）也单字差异时，视为歧义不替换。"""
    low_cand = cand.lower()
    for w in DOMAIN_WORDS:
        if w == target or len(w) != len(cand):
            continue
        if _hamming_eq1(low_cand, w.lower()):
            return True
    return False


def _hamming_eq1(a: str, b: str) -> bool:
    """等长字符串恰好 1 个字符不同（等长时 Levenshtein==1 与此等价）。"""
    if len(a) != len(b):
        return False
    diff = 0
    for x, y in zip(a, b):
        if x != y:
            diff += 1
            if diff > 1:
                return False
    return diff == 1


if __name__ == "__main__":
    sample = """## 快速入门 快速入门

质量与信息化流程部 质量与信息化流程部

示例页脚|www.example.com

| 版本 | 修订时间 | 内容 |
|------|----------|------|
| V1.0 | 2025.11.26 | 生成全文 |
|      |            |          |
|      |            |          |

<!-- image -->

## 第一部分 登录功能说明 第一部分

步骤一
步骤一

访问 https://oa.example.com:9100 即可。

www.example.com
"""
    r = clean_markdown(sample)
    print(r.text)
    print("--- stats ---")
    print(
        f"dup_headings={r.removed_dup_headings} dup_lines={r.removed_dup_lines} "
        f"empty_rows={r.removed_empty_table_rows} watermarks={r.removed_watermarks} "
        f"img={r.removed_image_placeholders} "
        f"ocr_lines={r.ocr_lines} ocr_frags={r.removed_ocr_fragments}"
    )
