"""知识库文档模型（从 libs 下沉）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from kylin.platform.runtime.documents import Document


@dataclass
class DocumentWithVSId(Document):
    """矢量化后的文档：正文 + 元数据 + 向量库 id / 分数。"""

    id: Optional[str] = None
    score: float = 3.0

    def dict(self) -> dict:
        """序列化含 id/score（与旧 pydantic 版行为一致，调用方依赖）。"""
        return {**super().dict(), "id": self.id, "score": self.score}
