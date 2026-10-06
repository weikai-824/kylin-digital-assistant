"""无证据拒答与回答引用校验规则."""

from __future__ import annotations

import re
from typing import List, Tuple

from kylin.ports.application.knowledge_qa import REFUSAL_NO_EVIDENCE

_CITE_RE = re.compile(r"\[(\d+)\]")

# LLM 生成侧拒答句式：RAG prompt 规则 3 约定的固定句 + REFUSAL_NO_EVIDENCE 核心
# （LLM 偶尔不按模板、转述拒答常量，两种都认）
_MODEL_REFUSAL_PATTERNS = (
    "根据已知信息无法回答该问题",
    "根据当前知识库未找到足以回答",
    "无法回答该问题",
)
# 拒答判定长度上限：超过说明是正常长回答，即便含类似字样也不判拒答（防误伤）
_MODEL_REFUSAL_MAX_LEN = 80


def is_model_refusal(answer: str) -> bool:
    """识别 LLM 生成侧拒答：答案很短且命中拒答句式。

    背景：检索到文档（documents 非空）时 service 不走代码门拒答，LLM 依据
    prompt 规则 3 输出"根据已知信息无法回答该问题"——这类"软拒答"此前不建
    知识缺口工单，闭环断在中间。本判定让生成侧拒答也能触发建单。
    """
    text = (answer or "").strip()
    if not text or len(text) > _MODEL_REFUSAL_MAX_LEN:
        return False
    return any(pattern in text for pattern in _MODEL_REFUSAL_PATTERNS)
_THINK_BLOCK_RE = re.compile(
    r"<think>[\s\S]*?</think>",
    re.IGNORECASE,
)
_REASONING_BLOCK_RES = (
    re.compile(
        r"<redacted_reasoning>[\s\S]*?</redacted_reasoning>",
        re.IGNORECASE,
    ),
    re.compile(r"<thinking>[\s\S]*?</thinking>", re.IGNORECASE),
)
_THINK_CLOSE = "</think>"
_THINK_CLOSE_RE = re.compile(r"</think>", re.IGNORECASE)
_REASONING_CLOSE_RES = (
    re.compile(r"</redacted_reasoning>", re.IGNORECASE),
    re.compile(r"</thinking>", re.IGNORECASE),
)


def _strip_reasoning_blocks(text: str) -> str:
    cleaned = _THINK_BLOCK_RE.sub("", text)
    for pattern in _REASONING_BLOCK_RES:
        cleaned = pattern.sub("", cleaned)
    return cleaned


def strip_think(text: str) -> str:
    """剥离推理模型的思考块，返回用户可见的最终答复.

    兼容三种常见形态（探针实测 oa-llm）:
    1. 成对 ``<think>...</think>``
    2. 无开标签、仅有闭标签：``裸思考...</think>最终答案``（本环境主路径）
    3. 未闭合的 ``<think`` / ``<thinking`` 前缀
    """
    if not text:
        return text
    cleaned = _strip_reasoning_blocks(text)
    lower = cleaned.lower()
    close_idx = lower.rfind(_THINK_CLOSE.lower())
    if close_idx >= 0:
        cleaned = cleaned[close_idx + len(_THINK_CLOSE) :]
    else:
        think_idx = lower.find("<think")
        if think_idx >= 0:
            cleaned = cleaned[:think_idx]
    return cleaned.strip()


def _find_think_boundary(buffer: str) -> int | None:
    """若已越过思考段，返回答案在 buffer 中的起始下标；否则 None."""
    match = _THINK_CLOSE_RE.search(buffer)
    if match:
        return match.end()
    for pattern in _REASONING_CLOSE_RES:
        match = pattern.search(buffer)
        if match:
            return match.end()
    return None


class ThinkStreamFilter:
    """流式过滤器：缓冲到思考结束后再增量放出答案.

    oa-llm 常先吐无开标签裸思考，``</think>`` 很晚才出现；在闭标签之前
    边收边发会把思考泄漏到 UI。策略：

    - 见到思考闭标签后，其后内容立刻作为可见增量返回
    - 全程无闭标签时，在 finalize() 用 strip_think 一次性放出（避免裸思考泄漏）
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._streaming = False
        self._answer_start = 0
        self._emitted_len = 0
        self._trimmed_leading = False

    def feed(self, chunk: str) -> str:
        """吞入流式增量；越过思考边界后返回可见答案增量，否则返回空串。"""
        if not chunk:
            return ""
        self._buffer += chunk
        if not self._streaming:
            boundary = _find_think_boundary(self._buffer)
            if boundary is None:
                return ""
            self._streaming = True
            self._answer_start = boundary
        return self._emit_new_tail()

    def finalize(self) -> str:
        """流结束：若已在流式放出则冲刷尾部；否则整段 strip_think 后一次性返回。"""
        if self._streaming:
            return self._emit_new_tail()
        full = strip_think(self._buffer)
        if not full:
            return ""
        # 与 feed 路径对齐：整段视为尚未发出的可见答案
        self._streaming = True
        self._buffer = full
        self._answer_start = 0
        self._emitted_len = 0
        self._trimmed_leading = False
        return self._emit_new_tail()

    def _emit_new_tail(self) -> str:
        answer = self._buffer[self._answer_start :]
        if not self._trimmed_leading:
            if not answer:
                return ""
            stripped = answer.lstrip()
            if not stripped:
                # 闭标签后暂时只有空白，等后续 token 再开始放出
                return ""
            self._answer_start += len(answer) - len(stripped)
            answer = stripped
            self._trimmed_leading = True
        if self._emitted_len >= len(answer):
            return ""
        new = answer[self._emitted_len :]
        self._emitted_len = len(answer)
        return new


def sanitize_citations(answer: str, *, max_index: int) -> Tuple[str, List[int]]:
    """移除越界引用，并返回清理后的回答与非法编号列表."""
    if max_index <= 0 or not answer:
        return answer, []

    invalid: List[int] = []

    def _replace(match: re.Match) -> str:
        number = int(match.group(1))
        if 1 <= number <= max_index:
            return match.group(0)
        invalid.append(number)
        return ""

    cleaned = _CITE_RE.sub(_replace, answer)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r" *\n *", "\n", cleaned)
    return cleaned.strip(), invalid
