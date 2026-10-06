"""长期记忆事实提取器：从一轮问答中提取可跨会话复用的事实 / 偏好 / 决策。

设计要点（参照 mem0 的 LLM 事实提取，但收窄范围，控制污染）：
- 只提取身份事实 / 用户偏好 / 明确决策三类；具体事务问答细节不提取（那属于知识库）
- 身份类事实（部门/职级/历史关注点/未解决问题）给高 importance 权重——制度咨询场景价值最高
- 容错：LLM 调用失败 / JSON 解析失败一律返回空列表，绝不影响主问答链路
"""

from __future__ import annotations

import json
import re
from typing import Any

from kylin.ports.data.long_term_memory import LongTermMemory
from kylin.ports.model.chat import ChatMessage, ChatModelPort
from kylin.platform.observability.logging import build_logger

logger = build_logger()

EXTRACT_PROMPT = """\
你是一个记忆提取器。从下面这段「用户提问 + 助手回答」中，提取值得长期记住、可在未来跨会话复用的信息。

提取规则：
1. 只提取这三类：
   - 身份事实（最重要）：用户的部门、职级、岗位、负责范围、历史关注点、未解决的问题
   - 偏好：用户对回答形式 / 详略 / 语言风格的偏好
   - 决策：用户明确表示的决定或待办（如"我决定申请年假""我需要办理报销"）
2. 一次性提问的具体事务细节（如"某制度某条规定是什么"）不提取，那属于知识库内容。
3. 寒暄、无信息量内容不提取。没有可提取内容时返回空数组。
4. importance 打分 1-5：身份事实与未解决问题给 4-5；一般偏好 2-3；琐碎 1。
5. 事实内容用中文，以「用户」开头。

只输出 JSON，不要任何其他文字，格式：
{"memories": [{"content": "用户是财务部员工，关注报销制度", "category": "fact", "importance": 5}]}

对话内容：
用户：{query}
助手：{answer}
"""


class MemoryExtractor:
    """把一轮问答压成长期记忆候选；任何异常都降级为空列表。"""

    def __init__(
        self,
        model: str | None = None,
        chat_model: ChatModelPort | None = None,
    ):
        """``chat_model`` 由组合根注入；应用层不感知模型客户端如何构造。"""
        self._model = model
        self._chat_model = chat_model

    def extract(
        self,
        query: str,
        answer: str,
        *,
        user_id: str = "",
        session_id: str = "",
        kb_name: str = "",
    ) -> list[LongTermMemory]:
        query = (query or "").strip()
        answer = (answer or "").strip()
        if not query or not answer:
            return []
        if self._chat_model is None:
            logger.warning("长期记忆提取：未注入 ChatModelPort，跳过")
            return []
        messages: list[ChatMessage] = [
            {"role": "user", "content": EXTRACT_PROMPT.format(query=query, answer=answer)}
        ]
        try:
            raw = self._chat_model.complete(
                messages,
                model=self._model,
                temperature=0.1,
                max_tokens=512,
            )
        except Exception as error:  # noqa: BLE001
            logger.warning(
                f"长期记忆提取 LLM 调用失败，跳过: {type(error).__name__}: {error}"
            )
            logger.exception("extractor.extract 完整堆栈")
            return []
        if raw is None:
            logger.warning("长期记忆提取：无法创建 LLM，跳过")
            return []
        raw = str(raw or "")
        payload = self._parse(raw)
        if not payload and raw:
            logger.warning(
                f"长期记忆提取：解析 LLM 输出为空，原文前 300 字：\n{raw[:300]!r}"
            )
            return []
        memories: list[LongTermMemory] = []
        for item in payload:
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            category = str(item.get("category") or "fact").strip().lower()
            if category not in ("preference", "fact", "decision"):
                category = "fact"
            try:
                importance = int(item.get("importance") or 3)
            except (TypeError, ValueError):
                importance = 3
            memories.append(
                LongTermMemory(
                    user_id=user_id,
                    session_id=session_id,
                    kb_name=kb_name,
                    content=content,
                    category=category,
                    importance=max(1, min(5, importance)),
                )
            )
        return memories

    @staticmethod
    def _parse(raw: str) -> list[dict[str, Any]]:
        """宽松解析 LLM 输出：剥离 markdown 围栏与前后噪声，取首个 JSON 对象。"""
        if not raw:
            return []
        text = raw.strip()
        fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
        if fence:
            text = fence.group(1).strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            start, end = text.find("{"), text.rfind("}")
            if start == -1 or end <= start:
                return []
            try:
                data = json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return []
        if isinstance(data, dict):
            items = data.get("memories")
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
        return []
