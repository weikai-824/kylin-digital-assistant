"""知识库文件生命周期与入库编排服务."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import replace

from kylin.applications.knowledge_ingestion.reserved import ManagedKnowledgeBaseGuard
from kylin.ports.application.knowledge_ingestion import IngestionProgressSink
from kylin.ports.data.document_processing import DocumentProcessingPort
from kylin.ports.data.file_storage import KnowledgeFileStoragePort
from kylin.ports.data.index import KnowledgeIndexPort
from kylin.ports.data.ingestion import (
    FileOperationResult,
    IngestionProgress,
    IngestDocument,
    KnowledgeBaseInfo,
    KnowledgeFileDetails,
    ProcessedDocument,
)
from kylin.ports.data.knowledge_base import KnowledgeBaseCatalogPort

logger = logging.getLogger(__name__)


class KnowledgeIngestionService:
    """通过端口协调知识库台账、文件、文档处理和索引写入."""

    def __init__(
        self,
        *,
        catalog: KnowledgeBaseCatalogPort,
        storage: KnowledgeFileStoragePort,
        processor: DocumentProcessingPort,
        index: KnowledgeIndexPort,
        default_embedding_model: str = "",
        default_chunk_size: int | None = None,
        default_chunk_overlap: int | None = None,
        managed_knowledge_bases: Sequence[str] | Callable[[], Iterable[str]] = (),
    ):
        self._catalog = catalog
        self._storage = storage
        self._processor = processor
        self._index = index
        self._default_embedding_model = default_embedding_model
        self._default_chunk_size = default_chunk_size
        self._default_chunk_overlap = default_chunk_overlap
        self._managed = ManagedKnowledgeBaseGuard(managed_knowledge_bases)

    def list_knowledge_bases(self) -> Sequence[KnowledgeBaseInfo]:
        return self._catalog.list()

    def managed_knowledge_base_names(self) -> frozenset[str]:
        """返回 OA / 工单等系统托管库名（供 UI 过滤）."""
        return self._managed.names()

    def list_files(self, name: str) -> Sequence[str]:
        self._require_kb(name)
        return self._storage.list(name)

    def list_file_details(self, name: str) -> Sequence[KnowledgeFileDetails]:
        self._require_kb(name)
        return self._storage.details(name)

    def file_path(self, name: str, filename: str) -> str:
        self._require_kb(name)
        return self._storage.path(name, filename)

    def create_knowledge_base(
        self,
        name: str,
        *,
        info: str = "",
        embedding_model: str | None = None,
        allow_system_managed: bool = False,
    ) -> KnowledgeBaseInfo:
        normalized = (name or "").strip()
        if not normalized:
            raise ValueError("知识库名称不能为空")
        self._managed.assert_mutable(
            normalized,
            allow_system_managed=allow_system_managed,
            action="创建或占用同名",
        )
        if self._catalog.get(normalized) is not None:
            raise ValueError(f"已存在同名知识库 {normalized}")
        knowledge_base = KnowledgeBaseInfo(
            name=normalized,
            info=info or "",
            embedding_model=embedding_model or self._default_embedding_model,
        )
        created = self._catalog.create(knowledge_base)
        self._index.ensure(created.name)
        return created

    def update_info(
        self,
        name: str,
        description: str,
        *,
        allow_system_managed: bool = False,
    ) -> None:
        self._require_kb(name)
        self._managed.assert_mutable(
            name, allow_system_managed=allow_system_managed, action="修改简介"
        )
        self._catalog.update_info(name, description)

    def delete_knowledge_base(
        self,
        name: str,
        *,
        allow_system_managed: bool = False,
    ) -> bool:
        self._require_kb(name)
        self._managed.assert_mutable(
            name, allow_system_managed=allow_system_managed, action="删除"
        )
        return self._catalog.delete(name)

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
        """保存一批文件；to_vector_store=True 时顺带解析并写入索引。

        ``to_vector_store=True`` 仅供 OA 同步等需要 save+ingest 一次完成的内部路径使用。
        普通入库请保持 save-only（False）并随后显式调用 update_files（幂等），
        避免「保存」与「入库」两个语义在一个参数里打架、被调用方误写成双写。
        """
        self._require_kb(name)
        self._managed.assert_mutable(
            name, allow_system_managed=allow_system_managed, action="上传文件"
        )
        results: list[FileOperationResult] = []
        for filename, content in files:
            try:
                if (
                    self._storage.exists(name, filename)
                    and not override
                    and self._storage.size(name, filename) == len(content)
                ):
                    results.append(FileOperationResult(filename, False, f"文件 {filename} 已存在。"))
                    continue
                self._storage.save(name, filename, content)
                if to_vector_store:
                    self._ingest_one(
                        name,
                        filename,
                        chunk_size,
                        chunk_overlap,
                        metadata=(metadata_by_filename or {}).get(filename),
                        documents=(documents_by_filename or {}).get(filename),
                    )
                results.append(FileOperationResult(filename, True, f"成功上传文件 {filename}"))
            except Exception as error:
                results.append(FileOperationResult(filename, False, f"{filename} 文件上传失败: {error}"))
        return tuple(results)

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
        self._require_kb(name)
        self._managed.assert_mutable(
            name, allow_system_managed=allow_system_managed, action="更新文件"
        )
        return tuple(
            self._process_file(
                name,
                filename,
                chunk_size,
                chunk_overlap,
                metadata=(metadata_by_filename or {}).get(filename),
                documents=(documents_by_filename or {}).get(filename),
            )
            for filename in filenames
        )

    def delete_files(
        self,
        name: str,
        filenames: Sequence[str],
        *,
        delete_content: bool = False,
        allow_system_managed: bool = False,
    ) -> Sequence[FileOperationResult]:
        self._require_kb(name)
        self._managed.assert_mutable(
            name, allow_system_managed=allow_system_managed, action="删除文件"
        )
        results: list[FileOperationResult] = []
        for filename in filenames:
            try:
                self._index.delete_by_source(name, filename)
                if delete_content:
                    self._storage.delete(name, filename)
                results.append(FileOperationResult(filename, True, f"成功删除文件 {filename}"))
            except Exception as error:
                results.append(FileOperationResult(filename, False, f"{filename} 文件删除失败: {error}"))
        return tuple(results)

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
        if (vector_store_type or "dual").lower() != "dual":
            raise ValueError("仅支持 dual 知识库类型")
        self._managed.assert_mutable(
            name, allow_system_managed=allow_system_managed, action="重建索引"
        )
        knowledge_base = self._get_or_create_rebuild_kb(
            name,
            allow_empty_kb=allow_empty_kb,
            embedding_model=embedding_model,
            allow_system_managed=allow_system_managed,
        )
        # 重建会清空旧索引，必须先确认 Embedding 可用，再执行破坏性操作。
        self._index.check_ready(
            knowledge_base.name,
            knowledge_base.embedding_model or embedding_model,
        )
        filenames = tuple(self._storage.list(knowledge_base.name))
        # clear() 会连 MySQL 台账一起删（clear_vs → delete_files_from_db），而
        # access_depts / department_id 恰恰是从那张台账读回的（见 _process_file
        # 的 metadata=None 分支）。因此必须在清空之前先把权限快照出来，否则
        # 重建后所有文档的访问范围会静默退化成「全开放」。
        permissions = self._snapshot_permissions(knowledge_base.name, filenames)
        self._index.clear(knowledge_base.name)
        results: list[FileOperationResult] = []
        total = len(filenames)
        for finished, filename in enumerate(filenames, start=1):
            def on_stage(stage: str, *, _filename=filename, _finished=finished) -> None:
                if progress is None:
                    return
                labels = {"parse": "解析中", "embed": "向量化写入中"}
                label = labels.get(stage, stage)
                progress.on_progress(
                    IngestionProgress(
                        filename=_filename,
                        finished=_finished - 1,
                        total=total,
                        result=FileOperationResult(
                            _filename, True, f"({_finished - 1} / {total}) {label}: {_filename}"
                        ),
                        stage=stage,
                    )
                )

            result = self._process_file(
                knowledge_base.name,
                filename,
                chunk_size,
                chunk_overlap,
                metadata=permissions.get(filename),
                on_stage=on_stage if progress is not None else None,
            )
            results.append(result)
            if progress is not None:
                progress.on_progress(
                    IngestionProgress(filename, finished, total, result, stage="done")
                )
        return tuple(results)

    def _get_or_create_rebuild_kb(
        self,
        name: str,
        *,
        allow_empty_kb: bool,
        embedding_model: str | None,
        allow_system_managed: bool = False,
    ) -> KnowledgeBaseInfo:
        normalized = (name or "").strip()
        if not normalized:
            raise ValueError("知识库名称不能为空")
        knowledge_base = self._catalog.get(normalized)
        if knowledge_base is not None:
            return knowledge_base
        if not allow_empty_kb:
            raise LookupError(f"未找到知识库 {normalized}")
        return self.create_knowledge_base(
            normalized,
            embedding_model=embedding_model or self._default_embedding_model,
            allow_system_managed=allow_system_managed,
        )

    def _ingest_one(
        self,
        name: str,
        filename: str,
        chunk_size: int | None,
        chunk_overlap: int | None,
        *,
        metadata: Mapping[str, object] | None = None,
        documents: Sequence[IngestDocument] | None = None,
        on_stage=None,
    ) -> None:
        knowledge_base = self._require_kb(name)
        if on_stage is not None:
            on_stage("parse")
        if documents is not None:
            if not documents:
                raise ValueError(f"文件 {filename} 的自定义 docs 不能为空")
            processed = ProcessedDocument(
                filename=filename,
                children=tuple(documents),
            )
        else:
            processed = self._processor.process(
                self._storage.path(name, filename),
                filename,
                metadata=dict(metadata or {}),
                chunk_size=chunk_size or self._default_chunk_size,
                chunk_overlap=chunk_overlap or self._default_chunk_overlap,
            )
        processed = replace(processed, embedding_model=knowledge_base.embedding_model)
        if on_stage is not None:
            on_stage("embed")
        self._index.upsert(name, processed)

    def _snapshot_permissions(
        self, name: str, filenames: Sequence[str]
    ) -> dict[str, dict]:
        """清空台账之前，先把每个文件的权限 metadata 快照出来。

        返回 {filename: {department_id / access_depts}}；没有配置权限的文件不进结果，
        调用方按 None 处理，行为与「重建前不读台账」一致。
        """
        snapshot: dict[str, dict] = {}
        for filename in filenames:
            try:
                stored = self._storage.permission_metadata(name, filename)
            except Exception as error:
                logger.warning(
                    "重建前读取权限 metadata 失败，该文件按无权限处理 kb=%s file=%s: %s",
                    name,
                    filename,
                    error,
                )
                continue
            if stored:
                snapshot[filename] = dict(stored)
        return snapshot

    def _process_file(
        self,
        name: str,
        filename: str,
        chunk_size: int | None,
        chunk_overlap: int | None,
        *,
        metadata: Mapping[str, object] | None = None,
        documents: Sequence[IngestDocument] | None = None,
        on_stage=None,
    ) -> FileOperationResult:
        try:
            if documents is None and not self._storage.exists(name, filename):
                raise FileNotFoundError(filename)
            # 重建/更新索引时若未显式传 metadata（如 WebUI 重建索引），
            # 从台账读回已设置的权限字段（access_depts/department_id），避免重建后丢失。
            effective_metadata = metadata
            if effective_metadata is None:
                try:
                    stored_permission = self._storage.permission_metadata(name, filename)
                except Exception:
                    stored_permission = {}
                if stored_permission:
                    effective_metadata = dict(stored_permission)
            self._ingest_one(
                name,
                filename,
                chunk_size,
                chunk_overlap,
                metadata=effective_metadata,
                documents=documents,
                on_stage=on_stage,
            )
            return FileOperationResult(filename, True, f"更新文件 {filename} 成功")
        except Exception as error:
            return FileOperationResult(filename, False, f"加载文档 {filename} 时出错: {error}")

    def _require_kb(self, name: str) -> KnowledgeBaseInfo:
        knowledge_base = self._catalog.get(name)
        if knowledge_base is None:
            raise LookupError(f"未找到知识库 {name}")
        return knowledge_base

    def refresh_doc_access_depts(
        self,
        name: str,
        filename: str,
        access_depts: Sequence[str] = (),
    ) -> int:
        """只改写某文件全部子块的 access_depts，不重新切分/不重新 embed。

        空列表 = 全开放。同时更新双索引（读回原向量写回）与 MySQL 台账，
        保证检索过滤与后续重建一致。返回被更新的子块数。
        """
        self._require_kb(name)
        normalized = [
            str(v).strip() for v in (access_depts or []) if str(v).strip()
        ]
        return self._index.update_access_depts(
            name, filename, access_depts=normalized
        )
