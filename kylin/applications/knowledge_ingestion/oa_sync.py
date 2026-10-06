"""OA document synchronization application service."""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from kylin.ports.application.knowledge_ingestion import KnowledgeIngestionPort
from kylin.ports.application.oa_sync import (
    OAAttachmentDispatchPort,
    OASourcePort,
    OASyncStatePort,
)
from kylin.ports.data.ingestion import IngestDocument
from kylin.ports.data.oa_sync import (
    AttachmentState,
    DocState,
    OASyncSettings,
    SyncReport,
    SyncState,
)
logger = logging.getLogger(__name__)


class OAKnowledgeSyncService:
    """Move the existing OA sync workflow behind application ports."""

    def __init__(
        self,
        *,
        settings: OASyncSettings,
        source_factory: Callable[[], OASourcePort],
        state_store: OASyncStatePort,
        ingestion: KnowledgeIngestionPort,
        file_dispatcher: OAAttachmentDispatchPort,
    ):
        self._settings = settings
        self._source_factory = source_factory
        self._state_store = state_store
        self._ingestion = ingestion
        self._file_dispatcher = file_dispatcher

    def failed_document_ids(self) -> list[str]:
        return self._state_store.load().failed_doc_ids()

    def sync(
        self,
        *,
        full: bool = False,
        dry_run: bool = False,
        limit: int = 0,
        only_ids: set[str] | None = None,
    ) -> SyncReport:
        settings = self._settings
        report = SyncReport()
        if not settings.enabled and not dry_run:
            message = "oa_settings.yaml 中 enabled=false，已中止。联调可先 --dry-run，或将 enabled 改为 true。"
            report.messages.append(message)
            logger.warning(message)
            return report

        _validate_settings(settings)
        state = SyncState() if full else self._state_store.load()
        if not dry_run:
            self._ensure_kb()
            flushed = self._flush_half_written(state)
            if flushed:
                report.messages.append(f"启动补跑半写文件 {flushed} 个")
                self._state_store.save(state)

        with closing(self._source_factory()) as source:
            userid = source.resolve_userid(settings)
            source.set_userid(userid)
            pending_changes = 0
            for doc in source.iter_documents(settings.page_size):
                report.scanned += 1
                doc_id = str(doc.get("id") or "")
                if not doc_id or (only_ids is not None and doc_id not in only_ids):
                    continue
                if not _pass_filters(doc, settings):
                    report.skipped_filter += 1
                    continue

                need, reason = _need_sync(doc, state, full=full, source=source)
                if not need:
                    report.skipped_unchanged += 1
                    continue
                if limit and pending_changes >= limit:
                    report.messages.append(f"已达 limit={limit}，停止继续处理变更文档")
                    break
                pending_changes += 1
                name = str(doc.get("name") or doc_id)
                logger.info(f"同步文档 {doc_id} ({name}): {reason}")
                if dry_run:
                    report.would_ingest += 1
                    report.messages.append(f"[dry-run] would sync {doc_id} {name} ({reason})")
                    continue

                try:
                    local_files, doc_state = self._sync_one_doc(
                        source,
                        doc,
                        state=state,
                        previous=state.get_doc(doc_id),
                    )
                    if local_files:
                        state.upsert_doc(doc_state)
                        report.ingested += 1
                        report.messages.append(f"ingested {doc_id}: {local_files}")
                    else:
                        report.skipped_filter += 1
                        report.messages.append(f"no ingestable content for {doc_id} ({name})")
                    self._state_store.save(state)
                except Exception as error:
                    report.failed += 1
                    message = f"同步失败 {doc_id} ({name}): {error}"
                    logger.error(message)
                    report.messages.append(message)
                    failed = state.get_doc(doc_id) or DocState(doc_id=doc_id, name=name)
                    failed.name = name
                    failed.last_update_time = str(doc.get("docLastUpdateTime") or "")
                    failed.last_error = str(error)
                    failed.retry_count += 1
                    failed.last_attempt_at = _now()
                    state.upsert_doc(failed)
                    self._state_store.save(state)

        if not dry_run:
            state.last_sync_at = _now()
            self._state_store.save(state)
        logger.info(f"OA 同步完成: {report.to_dict()}")
        return report

    def _ensure_kb(self) -> None:
        if any(item.name == self._settings.kb_name for item in self._ingestion.list_knowledge_bases()):
            return
        self._ingestion.create_knowledge_base(
            self._settings.kb_name,
            info=self._settings.kb_info,
            allow_system_managed=True,
        )

    def _sync_one_doc(
        self,
        source: OASourcePort,
        doc: Any,
        *,
        state: SyncState,
        previous: DocState | None,
    ) -> tuple[list[str], DocState]:
        doc_id = str(doc["id"])
        name = str(doc.get("name") or doc_id)
        last_update = str(doc.get("docLastUpdateTime") or "")
        old_files = list(previous.local_filenames if previous else [])
        working = state.get_doc(doc_id) or DocState(doc_id=doc_id, name=name)
        working.name = name
        working.local_filenames = list(old_files)
        working.last_error = ""
        state.upsert_doc(working)

        metadata = {"oa_doc_id": doc_id, "doc_title": name}
        department_id = source.get_document_department_id(doc_id)
        if department_id:
            metadata["department_id"] = department_id
            # access_depts：默认仅本部门；document_public_by_default=true 时全开放（不设）
            if not bool(getattr(self._settings, "document_public_by_default", False)):
                metadata["access_depts"] = [department_id]
        local_files: list[str] = []
        attachment_states: list[AttachmentState] = []
        content_fingerprint = ""
        attachments = list(source.list_attachments(doc_id))
        if not attachments:
            info = source.get_document_content(doc_id)
            doc_file = info.get("docFile")
            if isinstance(doc_file, dict) and _attachment_file_id(doc_file):
                attachments = [doc_file]

        if attachments:
            for attachment in attachments:
                file_id = _attachment_file_id(attachment)
                attachment_name = _attachment_name(attachment, name)
                if not file_id:
                    continue
                result = self._file_dispatcher(
                    attachment,
                    doc_id=doc_id,
                    doc_name=name,
                    download_fn=lambda: source.download_file(
                        file_id,
                        module=str(attachment.get("module") or self._settings.download_module or ""),
                    ),
                )
                if result is None or not result.ingest:
                    continue
                local_name = result.local_filename
                content = result.content
                self._upload(
                    local_name,
                    content,
                    metadata={
                        **metadata,
                        "oa_file_id": file_id,
                        "oa_file_name": attachment_name,
                    },
                    state=state,
                    doc_id=doc_id,
                    doc_name=name,
                )
                local_files.append(local_name)
                attachment_states.append(
                    AttachmentState(
                        file_id=file_id,
                        name=attachment_name,
                        md5=str(attachment.get("md5") or ""),
                        local_filename=local_name,
                    )
                )
        elif self._settings.ingest_html_content:
            info = source.get_document_content(doc_id)
            content = source.extract_content(info)
            if content:
                raw = content.encode("utf-8")
                content_fingerprint = _fingerprint(raw)
                local_name = _build_local_filename(doc_id, f"{name}.md")
                self._upload(
                    local_name,
                    raw,
                    metadata=metadata,
                    documents=(IngestDocument(content, metadata),),
                    state=state,
                    doc_id=doc_id,
                    doc_name=name,
                )
                local_files.append(local_name)

        if not local_files:
            return [], DocState(doc_id=doc_id, name=name, last_update_time=last_update)

        result = DocState(
            doc_id=doc_id,
            name=name,
            last_update_time=last_update,
            local_filenames=local_files,
            attachments=attachment_states,
            content_fingerprint=content_fingerprint,
            last_attempt_at=_now(),
        )
        obsolete = [filename for filename in old_files if filename not in local_files]
        if obsolete:
            self._delete_files(obsolete)
        return local_files, result

    def _upload(
        self,
        filename: str,
        content: bytes,
        *,
        metadata: dict[str, object],
        state: SyncState,
        doc_id: str,
        doc_name: str,
        documents: tuple[IngestDocument, ...] | None = None,
    ) -> None:
        working = state.get_doc(doc_id) or DocState(doc_id=doc_id, name=doc_name)
        if filename not in working.half_written:
            working.half_written.append(filename)
        state.upsert_doc(working)
        self._state_store.save(state)
        results = self._ingestion.upload_files(
            self._settings.kb_name,
            [(filename, content)],
            override=True,
            metadata_by_filename={filename: metadata},
            documents_by_filename={filename: documents} if documents else None,
            allow_system_managed=True,
        )
        result = results[0] if results else None
        if result is None or not result.success:
            raise RuntimeError(result.message if result else f"文件 {filename} 入库失败")
        working = state.get_doc(doc_id) or DocState(doc_id=doc_id, name=doc_name)
        working.half_written = [item for item in working.half_written if item != filename]
        if filename not in working.local_filenames:
            working.local_filenames.append(filename)
        state.upsert_doc(working)
        self._state_store.save(state)

    def _delete_files(self, filenames: list[str]) -> None:
        results = self._ingestion.delete_files(
            self._settings.kb_name,
            filenames,
            delete_content=True,
            allow_system_managed=True,
        )
        failures = [result.message for result in results if not result.success]
        if failures:
            raise RuntimeError("; ".join(failures))

    def _flush_half_written(self, state: SyncState) -> list[str]:
        flushed: list[str] = []
        for doc_id, doc_state in list(state.docs.items()):
            for filename in list(doc_state.half_written):
                if filename not in self._ingestion.list_files(self._settings.kb_name):
                    doc_state.half_written.remove(filename)
                    state.upsert_doc(doc_state)
                    self._state_store.save(state)
                    continue
                try:
                    results = self._ingestion.update_files(
                        self._settings.kb_name,
                        [filename],
                        metadata_by_filename={
                            filename: {"oa_doc_id": doc_id, "doc_title": doc_state.name}
                        },
                        allow_system_managed=True,
                    )
                    message = results[0].message if results else f"补跑失败: {filename}"
                except Exception as error:
                    results = ()
                    message = f"补跑失败: {filename}: {error}"
                if not results or not results[0].success:
                    logger.error(message)
                    doc_state.last_error = message
                    doc_state.retry_count += 1
                    doc_state.last_attempt_at = _now()
                    state.upsert_doc(doc_state)
                    self._state_store.save(state)
                    continue
                doc_state.half_written.remove(filename)
                state.upsert_doc(doc_state)
                self._state_store.save(state)
                flushed.append(filename)
        return flushed


def _validate_settings(settings: OASyncSettings) -> None:
    missing = [
        key for key in ("base_url", "corpid", "app_key", "app_secret")
        if not getattr(settings, key, "").strip()
    ]
    if not settings.userid.strip() and not settings.job_num.strip() and not settings.account.strip():
        missing.append("userid|job_num|account")
    if missing:
        raise RuntimeError(f"oa_settings.yaml 缺少必要配置: {', '.join(missing)}")


def _pass_filters(doc: Any, settings: OASyncSettings) -> bool:
    status = str(doc.get("docStatusName") or "")
    if settings.skip_status_not_normal and status and status != "正常":
        return False
    whitelist = settings.folder_path_whitelist or []
    folder = str(doc.get("folderPathName") or "")
    return not whitelist or any(item in folder for item in whitelist)


def _need_sync(
    doc: Any,
    state: SyncState,
    *,
    full: bool,
    source: OASourcePort,
) -> tuple[bool, str]:
    doc_id = str(doc.get("id"))
    previous = state.get_doc(doc_id)
    last_update = str(doc.get("docLastUpdateTime") or "")
    if full:
        return True, "full_resync"
    if previous is None:
        return True, "new_doc"
    if previous.last_error:
        return True, f"retry_after_error (retry_count={previous.retry_count})"
    if last_update and last_update != previous.last_update_time:
        return True, f"updated {previous.last_update_time} -> {last_update}"
    if int(doc.get("countOfAttachment") or 0) > 0 and not previous.attachments:
        return True, "attachments_missing_locally"
    if previous.attachments:
        try:
            current = source.list_attachments(doc_id)
            current_md5s = {str(item.get("md5") or "") for item in current if item.get("md5")}
            previous_md5s = {item.md5 for item in previous.attachments if item.md5}
            if current_md5s != previous_md5s:
                return True, f"md5_changed added={len(current_md5s - previous_md5s)} removed={len(previous_md5s - current_md5s)}"
        except Exception as error:
            logger.error(f"md5 二次校验失败 {doc_id}: {error}，按需重新同步")
            return True, f"md5_check_failed {type(error).__name__}: {error}"
    return False, "unchanged"


def _attachment_file_id(attachment: Any) -> str:
    return str(
        attachment.get("fileId")
        or attachment.get("fileid")
        or attachment.get("id")
        or ""
    ).strip()


def _attachment_name(attachment: Any, fallback: str) -> str:
    name = str(attachment.get("name") or "").strip()
    if name:
        return name
    extension = str(attachment.get("extName") or "").strip().lstrip(".")
    return f"{fallback}.{extension}" if extension else fallback


def _build_local_filename(doc_id: str, original_name: str) -> str:
    safe = re.sub(r'[\\/:*?"<>|\s]+', "_", original_name).strip("._") or "file"
    path = Path(safe)
    return f"oa_{doc_id}_{path.stem[:80] or 'file'}{path.suffix.lower()}"


def _fingerprint(content: bytes) -> str:
    return f"{len(content)}:{hashlib.md5(content).hexdigest()}"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
