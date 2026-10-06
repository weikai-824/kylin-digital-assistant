"""知识库文件存储契约."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from kylin.ports.data.ingestion import KnowledgeFileDetails


class KnowledgeFileStoragePort(Protocol):
    """知识库文件的安全路径和内容操作."""

    def path(self, knowledge_base_name: str, filename: str) -> str:
        """返回校验后的文件绝对路径."""

    def save(self, knowledge_base_name: str, filename: str, content: bytes) -> str:
        """保存文件并返回绝对路径."""

    def exists(self, knowledge_base_name: str, filename: str) -> bool:
        """判断文件是否存在."""

    def size(self, knowledge_base_name: str, filename: str) -> int:
        """返回文件字节数."""

    def delete(self, knowledge_base_name: str, filename: str) -> None:
        """删除文件内容."""

    def list(self, knowledge_base_name: str) -> Sequence[str]:
        """列出知识库目录中的相对文件名."""

    def details(self, knowledge_base_name: str) -> Sequence[KnowledgeFileDetails]:
        """列出目录与索引台账合并后的文件详情."""

    def permission_metadata(self, knowledge_base_name: str, filename: str) -> dict:
        """读取文件已设置的权限 metadata（department_id / access_depts）。

        用于重建索引时保留访问范围；无记录返回 {}。
        """
