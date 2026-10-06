"""自有文档值对象（替换 langchain Document）。

P7 决策（用户确认）：彻底移除 langchain 全家。本项目文档对象只需
page_content + metadata 两个字段与少量兼容接口，dataclass 即可满足，
不再依赖 langchain_core 的 pydantic 基类。

兼容接口（对照 langchain.docstore.document.Document 的使用面）：
- 构造：Document(page_content=..., metadata=...)
- 读取：doc.page_content / doc.metadata（metadata 可变 dict）
- 序列化：doc.dict() -> {"page_content": ..., "metadata": {...}}
- 解包：Document(**x) 构造
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict


@dataclass
class Document:
    """知识块/文档值对象：正文 + 元数据。"""

    page_content: str
    metadata: Dict[str, Any] = field(default_factory=dict)

    def dict(self) -> Dict[str, Any]:
        """langchain 兼容序列化：调用方依赖 {"page_content", "metadata"} 键。"""
        return {
            "page_content": self.page_content,
            "metadata": dict(self.metadata),
        }

    def __getitem__(self, key: str) -> Any:
        """兼容 dict 风格访问（d["page_content"] / d["metadata"]）。"""
        if key == "page_content":
            return self.page_content
        if key == "metadata":
            return self.metadata
        raise KeyError(key)
