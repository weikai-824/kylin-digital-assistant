"""MySQL 仓储所需的结构化接口。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class KnowledgeFileLike(Protocol):
    """知识库文件的最小结构：库名 + 文件名。"""

    kb_name: str
    filename: str
