"""知识入库端口共享的数据对象."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SUPPORTED_INGESTION_FILE_EXTENSIONS = (".docx", ".md", ".pdf")


@dataclass(frozen=True, slots=True)
class KnowledgeBaseInfo:
    """知识库目录和台账中的最小业务信息."""

    name: str
    info: str = ""
    vector_store_type: str = "dual"
    embedding_model: str = ""


@dataclass(frozen=True, slots=True)
class IngestDocument:
    """不携带第三方 SDK 类型的文档块."""

    page_content: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProcessedDocument:
    """单个文件解析、清洗、切分后的父子块."""

    filename: str
    children: tuple[IngestDocument, ...]
    parents: tuple[IngestDocument, ...] = ()
    embedding_model: str = ""


@dataclass(frozen=True, slots=True)
class FileOperationResult:
    """批量文件操作的单文件结果."""

    filename: str
    success: bool
    message: str = ""


@dataclass(frozen=True, slots=True)
class KnowledgeFileDetails:
    """文件目录与索引台账合并后的展示信息."""

    filename: str
    file_ext: str = ""
    docs_count: int = 0
    create_time: object | None = None
    in_folder: bool = False
    in_db: bool = False
    sequence: int = 0
    department_id: str = ""
    """文档归属部门 ID（OA 打标）；空 = 无归属。仅展示。"""
    access_depts: tuple[str, ...] = ()
    """允许访问的部门 ID 列表；空 = 全开放。"""


@dataclass(frozen=True, slots=True)
class IngestionProgress:
    """一次重建中的进度事件（含文件内阶段）。"""

    filename: str
    finished: int
    total: int
    result: FileOperationResult
    stage: str = "done"
    """parse / embed / done"""
