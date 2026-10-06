"""将知识库目录映射到知识入库文件存储端口."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from kylin.ports.data.ingestion import KnowledgeFileDetails
from kylin.adapters.outbound.mysql.database.repository.knowledge_file_repository import (
    get_file_permission_metadata,
)
from kylin.adapters.outbound.knowledge_base.kb_service.views import get_kb_file_details
from kylin.adapters.outbound.knowledge_base.kb_service.paths import (
    get_file_path,
    list_files_from_folder,
    validate_kb_name,
)


class KylinKnowledgeFileStorageAdapter:
    """复用现有知识库路径规则并执行文件内容操作."""

    def path(self, knowledge_base_name: str, filename: str) -> str:
        if not validate_kb_name(knowledge_base_name):
            raise ValueError(f"非法知识库名称: {knowledge_base_name}")
        file_path = get_file_path(knowledge_base_name, filename)
        if not file_path:
            raise ValueError(f"非法文件路径: {filename}")
        return str(file_path)

    def save(self, knowledge_base_name: str, filename: str, content: bytes) -> str:
        file_path = Path(self.path(knowledge_base_name, filename))
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(content)
        return str(file_path)

    def exists(self, knowledge_base_name: str, filename: str) -> bool:
        return Path(self.path(knowledge_base_name, filename)).is_file()

    def size(self, knowledge_base_name: str, filename: str) -> int:
        return Path(self.path(knowledge_base_name, filename)).stat().st_size

    def delete(self, knowledge_base_name: str, filename: str) -> None:
        file_path = Path(self.path(knowledge_base_name, filename))
        if file_path.exists():
            file_path.unlink()

    def list(self, knowledge_base_name: str) -> Sequence[str]:
        if not validate_kb_name(knowledge_base_name):
            raise ValueError(f"非法知识库名称: {knowledge_base_name}")
        return tuple(list_files_from_folder(knowledge_base_name))

    def details(self, knowledge_base_name: str) -> Sequence[KnowledgeFileDetails]:
        if not validate_kb_name(knowledge_base_name):
            raise ValueError(f"非法知识库名称: {knowledge_base_name}")
        details = []
        for item in get_kb_file_details(knowledge_base_name):
            details.append(
                KnowledgeFileDetails(
                    filename=str(item.get("file_name") or ""),
                    file_ext=str(item.get("file_ext") or ""),
                    docs_count=int(item.get("docs_count") or 0),
                    create_time=item.get("create_time"),
                    in_folder=bool(item.get("in_folder")),
                    in_db=bool(item.get("in_db")),
                    sequence=int(item.get("No") or 0),
                    department_id=str(item.get("department_id") or ""),
                    access_depts=tuple(item.get("access_depts") or ()),
                )
            )
        return tuple(item for item in details if item.filename)

    def permission_metadata(self, knowledge_base_name: str, filename: str) -> dict:
        """读取文件已设置的权限 metadata（department_id / access_depts）。"""
        if not validate_kb_name(knowledge_base_name):
            raise ValueError(f"非法知识库名称: {knowledge_base_name}")
        return get_file_permission_metadata(knowledge_base_name, filename)
