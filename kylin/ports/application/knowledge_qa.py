"""知识问答应用入口契约。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from kylin.ports.data.knowledge_search import KnowledgeDocument

REFUSAL_NO_EVIDENCE = (
    "根据当前知识库未找到足以回答该问题的依据，无法给出确切答复。"
    "请换个问法，或联系相关部门确认。"
)


@dataclass(frozen=True, slots=True)
class KnowledgeQaRequest:
    """已由入站边界归一化的问答请求。"""

    query: str
    knowledge_base_name: str
    top_k: int
    score_threshold: float
    history: Sequence[object] = field(default_factory=tuple)
    session_id: str | None = None
    user_id: str | None = None
    """用户身份ID（OA userid）；用于长期记忆按人隔离。为空则跳过记忆读写。"""
    dept: str | None = None
    """用户所属部门 ID（OA departmentId）；用于检索按部门过滤。"""
    dept_exempt: bool = False
    """是否豁免部门过滤（系统内部调用 / 配置豁免用户或部门）。True 时检索不过滤。"""
    stream: bool = True
    model: str | None = None
    temperature: float = 0.0
    max_tokens: int | None = None
    return_direct: bool = False


# 流式进度阶段（content 存阶段键；入站层映射为用户可见文案）
PROGRESS_PLANNING = "planning"
PROGRESS_RETRIEVING = "retrieving"
PROGRESS_GENERATING = "generating"

PROGRESS_STATUS_TEXT = {
    PROGRESS_PLANNING: "正在理解问题…",
    PROGRESS_RETRIEVING: "正在检索知识库…",
    PROGRESS_GENERATING: "正在组织回答…",
}


@dataclass(frozen=True, slots=True)
class KnowledgeQaEvent:
    """应用服务向入站边界发出的结构化事件。

    kind:
    - progress: 阶段进度（content 为 planning/retrieving/generating）
    - thought: 结构化思考中间产物（虚化思维链内容，content 为一段文本，
      stage 标注所属阶段 planning/retrieving/generating）
    - start: 检索完成，携带 documents
    - token: 可见答案增量
    - complete: 完整结果（非流式带 content）；流式收尾时常 content 为空并带 finish_reason
    """

    kind: str
    session_id: str
    model: str | None = None
    content: str = ""
    stage: str | None = None
    documents: tuple[KnowledgeDocument, ...] = ()
    ticket_id: int | None = None
    finish_reason: str | None = None
    is_refusal: bool = False


class KnowledgeQaApplicationPort(Protocol):
    """知识问答应用服务提供给入站适配器的能力。"""

    def knowledge_base_exists(self, knowledge_base_name: str) -> bool:
        """判断知识库是否存在。"""

    def run(self, request: KnowledgeQaRequest) -> AsyncIterator[KnowledgeQaEvent]:
        """运行问答并返回结构化事件流。"""
