"""知识入库应用服务的调用契约."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from kylin.ports.data.ingestion import (
    FileOperationResult,
    IngestionProgress,
    IngestDocument,
    KnowledgeBaseInfo,
    KnowledgeFileDetails,
)


class IngestionProgressSink(Protocol):
    """接收重建进度的非 HTTP 回调契约."""

    def on_progress(self, event: IngestionProgress) -> None:
        """消费一个文件级进度事件."""


class KnowledgeIngestionPort(Protocol):
    """创建知识库并管理其文件生命周期."""

    def list_knowledge_bases(self) -> Sequence[KnowledgeBaseInfo]:
        """列出已登记的知识库."""

    def create_knowledge_base(
        self,
        name: str,
        *,
        info: str = "",
        embedding_model: str | None = None,
        allow_system_managed: bool = False,
    ) -> KnowledgeBaseInfo:
        """创建知识库及其索引.

        ``allow_system_managed=True`` 仅供 OA 同步 / 工单反哺创建托管库。
        """

    def delete_knowledge_base(
        self,
        name: str,
        *,
        allow_system_managed: bool = False,
    ) -> bool:
        """删除知识库及其索引和文件."""

    def update_info(
        self,
        name: str,
        description: str,
        *,
        allow_system_managed: bool = False,
    ) -> None:
        """更新知识库描述."""

    def list_files(self, name: str) -> Sequence[str]:
        """列出知识库文件."""

    def list_file_details(self, name: str) -> Sequence[KnowledgeFileDetails]:
        """列出知识库文件详情."""

    def file_path(self, name: str, filename: str) -> str:
        """返回知识库文件的本地路径."""

    def upload_files(
        self,
        name: str,
        files: Sequence[tuple[str, bytes]],
        *,
        override: bool = False,
        to_vector_store: bool = True,
        chunk_size: int | None = None,
        chunk_overlap: int | None = None,
        metadata_by_filename: Mapping[str, Mapping[str, object]] | None = None,
        documents_by_filename: Mapping[str, Sequence[IngestDocument]] | None = None,
        allow_system_managed: bool = False,
    ) -> Sequence[FileOperationResult]:
        """保存一批文件；to_vector_store=True 时顺带解析并写入索引（OA 同步专用）。"""

    def update_files(
        self,
        name: str,
        filenames: Sequence[str],
        *,
        chunk_size: int | None = None,
        chunk_overlap: int | None = None,
        metadata_by_filename: Mapping[str, Mapping[str, object]] | None = None,
        documents_by_filename: Mapping[str, Sequence[IngestDocument]] | None = None,
        allow_system_managed: bool = False,
    ) -> Sequence[FileOperationResult]:
        """重新解析并写入指定文件."""

    def delete_files(
        self,
        name: str,
        filenames: Sequence[str],
        *,
        delete_content: bool = False,
        allow_system_managed: bool = False,
    ) -> Sequence[FileOperationResult]:
        """删除指定文件及索引."""

    def refresh_doc_access_depts(
        self,
        name: str,
        filename: str,
        access_depts: Sequence[str] = (),
    ) -> int:
        """只改写某文件全部子块的 access_depts，不重新切分/不重新 embed。

        空列表 = 全开放。系统托管库（OA/工单）也可用：仅改权限元数据。
        返回被更新的子块数。
        """

    def rebuild(
        self,
        name: str,
        *,
        allow_empty_kb: bool = True,
        vector_store_type: str = "dual",
        embedding_model: str | None = None,
        chunk_size: int | None = None,
        chunk_overlap: int | None = None,
        progress: IngestionProgressSink | None = None,
        allow_system_managed: bool = False,
    ) -> Sequence[FileOperationResult]:
        """清空并重建知识库索引."""
