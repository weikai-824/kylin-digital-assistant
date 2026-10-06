"""长期记忆端口契约：跨会话用户记忆的写入 / 检索 / 管理。

设计参考业界（mem0 结构化记忆 + Zep supersede）但砍掉知识图谱：
- 写入侧 remember：LLM 从对话提取事实 → 向量化 → MySQL + Milvus 双写
- 读取侧 recall：按语义相似度召回该用户最相关记忆，注入生成 prompt
- 管理侧 list_memories / forget：查看与删除（合规必需，M3 管理界面使用）
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class LongTermMemory:
    """一条长期记忆事实。

    category:
    - preference：用户偏好（语气/格式/关注方向）
    - fact：身份事实（部门/职级/历史关注点），制度咨询场景价值最高
    - decision：用户明确作出的决策/待办
    importance: 1-5，LLM 提取时打分，检索后参与排序；越低越可能被忽略。
    """

    content: str
    user_id: str = ""
    session_id: str = ""
    kb_name: str = ""
    category: str = "fact"
    importance: int = 3
    id: int | None = None
    status: str = "active"  # active / superseded
    created_at: datetime | None = None


class LongTermMemoryPort(Protocol):
    """长期记忆能力契约。"""

    def remember(
        self,
        user_id: str,
        memories: Sequence[LongTermMemory],
        *,
        session_id: str = "",
        kb_name: str = "",
    ) -> int:
        """批量写入记忆；同 user 下重复内容自动跳过（content_hash / 向量相似度）。返回新增条数。"""

    def recall(self, user_id: str, query: str, top_k: int = 5) -> Sequence[LongTermMemory]:
        """按语义相似度召回该用户最相关的记忆（importance 加权）。"""

    def list_memories(self, user_id: str) -> Sequence[LongTermMemory]:
        """列出该用户全部 active 记忆（管理界面用）。"""

    def forget(self, user_id: str, memory_id: int) -> bool:
        """删除一条记忆（软删 status=superseded 并清 Milvus 向量）。"""
