"""通过一次模型调用生成查询计划，失败时确定性降级."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from kylin.applications.knowledge_qa.query_plan.prompt import (
    PLAN_SYSTEM,
    PLAN_USER_TEMPLATE,
)
from kylin.applications.knowledge_qa.query_plan.schema import QueryPlan
from kylin.ports.model.chat import ChatMessage, ChatModelPort

logger = logging.getLogger(__name__)

_NO_HISTORY = "（无）"


def _fallback(query: str, *, notes: str) -> QueryPlan:
    normalized_query = (query or "").strip() or query
    return QueryPlan(
        need_retrieval=True,
        standalone_query=normalized_query,
        query_type="factual",
        sub_queries=[normalized_query] if normalized_query else [],
        filters={},
        notes=notes,
    )


def _history_item(item: object) -> tuple[object, object]:
    if isinstance(item, Mapping):
        return item.get("role", ""), item.get("content", "")
    if isinstance(item, (list, tuple)) and len(item) >= 2:
        return item[0], item[1]
    return getattr(item, "role"), getattr(item, "content")


def _history_text(
    history: Sequence[object] | None,
    *,
    max_turns: int = 6,
) -> str:
    if not history:
        return _NO_HISTORY

    lines: list[str] = []
    for item in list(history)[-max_turns:]:
        role, raw_content = _history_item(item)
        label = "用户" if role in ("user", "human") else "助手"
        content = str(raw_content or "").strip()
        if content:
            lines.append(f"{label}: {content}")
    return "\n".join(lines) if lines else _NO_HISTORY


def _extract_json(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    candidate = text.strip()
    candidate = re.sub(
        r"<think>[\s\S]*?</think>",
        "",
        candidate,
        flags=re.IGNORECASE,
    )
    candidate = re.sub(r"</?think>", "", candidate, flags=re.IGNORECASE)
    if "```" in candidate:
        match = re.search(
            r"```(?:json)?\s*([\s\S]*?)```",
            candidate,
            re.IGNORECASE,
        )
        if match:
            candidate = match.group(1).strip()

    decoder = json.JSONDecoder()
    starts = [match.start() for match in re.finditer(r"\{", candidate)]
    for start in reversed(starts):
        try:
            data, _ = decoder.raw_decode(candidate[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and (
            "standalone_query" in data or "need_retrieval" in data
        ):
            return data
    return None


def _coerce_plan(data: Mapping[str, Any], *, raw_query: str) -> QueryPlan:
    standalone = str(data.get("standalone_query") or raw_query).strip() or raw_query
    need_retrieval = data.get("need_retrieval")
    if not isinstance(need_retrieval, bool):
        need_retrieval = True
    query_type = str(data.get("query_type") or "factual").strip() or "factual"

    sub_queries = data.get("sub_queries")
    if not isinstance(sub_queries, list) or not sub_queries:
        normalized_sub_queries = [standalone]
    else:
        normalized_sub_queries = [
            str(query).strip() for query in sub_queries if str(query).strip()
        ] or [standalone]

    filters = data.get("filters")
    if not isinstance(filters, dict):
        filters = {}

    plan = QueryPlan(
        need_retrieval=need_retrieval,
        standalone_query=standalone,
        query_type=query_type,
        sub_queries=normalized_sub_queries,
        filters=filters,
        notes=str(data.get("notes") or ""),
    )
    return replace(plan, sub_queries=plan.normalized_sub_queries())


class QueryPlanner:
    """通过 Chat 模型端口创建检索计划的应用服务."""

    def __init__(self, chat_model: ChatModelPort, *, history_max_turns: int = 6):
        if history_max_turns <= 0:
            raise ValueError("history_max_turns must be greater than zero")
        self._chat_model = chat_model
        self._history_max_turns = history_max_turns

    def build(
        self,
        query: str,
        history: Sequence[object] | None = None,
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> QueryPlan:
        """创建检索计划；模型不可用或响应非法时回退到原问题."""
        history = history or []
        query = query or ""
        if not query.strip():
            return _fallback(query, notes="empty_query")

        try:
            messages: list[ChatMessage] = [
                {"role": "system", "content": PLAN_SYSTEM},
                {
                    "role": "user",
                    "content": PLAN_USER_TEMPLATE.format(
                        history=_history_text(
                            history,
                            max_turns=self._history_max_turns,
                        ),
                        query=query.strip(),
                    ),
                },
            ]
            content = self._chat_model.complete(
                messages,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            if content is None:
                return _fallback(query, notes="llm_unavailable")

            data = _extract_json(content)
            if not data:
                logger.warning(
                    "query_plan JSON 解析失败，回退原文。raw=%s",
                    content[:500],
                )
                return _fallback(query, notes="json_parse_failed")
            return _coerce_plan(data, raw_query=query.strip())
        except Exception as error:
            logger.warning("query_plan 调用失败，回退原文: %s", error)
            return _fallback(query, notes=f"exception:{type(error).__name__}")
