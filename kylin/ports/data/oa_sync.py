"""OA sync state and source-independent settings contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True, slots=True)
class OAAttachmentDispatchResult:
    """附件/主文档分派结果：是否入库及落盘所需内容。"""

    local_filename: str
    content: bytes
    ingest: bool
    kind: str


class OASyncSettings(Protocol):
    """The configuration values required by the OA sync application."""

    enabled: bool
    base_url: str
    corpid: str
    app_key: str
    app_secret: str
    userid: str
    job_num: str
    account: str
    kb_name: str
    kb_info: str
    page_size: int
    folder_path_whitelist: list[str]
    skip_status_not_normal: bool
    ingest_html_content: bool
    download_module: str


@dataclass
class AttachmentState:
    file_id: str
    name: str
    md5: str = ""
    local_filename: str = ""


@dataclass
class DocState:
    doc_id: str
    name: str = ""
    last_update_time: str = ""
    local_filenames: list[str] = field(default_factory=list)
    attachments: list[AttachmentState] = field(default_factory=list)
    content_fingerprint: str = ""
    last_error: str = ""
    retry_count: int = 0
    last_attempt_at: str = ""
    half_written: list[str] = field(default_factory=list)


@dataclass
class SyncState:
    last_sync_at: str = ""
    docs: dict[str, DocState] = field(default_factory=dict)

    def get_doc(self, doc_id: str) -> DocState | None:
        return self.docs.get(doc_id)

    def upsert_doc(self, doc: DocState) -> None:
        self.docs[doc.doc_id] = doc

    def failed_doc_ids(self) -> list[str]:
        return [doc_id for doc_id, doc in self.docs.items() if doc.last_error]


@dataclass
class SyncReport:
    scanned: int = 0
    skipped_filter: int = 0
    skipped_unchanged: int = 0
    ingested: int = 0  # 真同步成功入库篇数
    would_ingest: int = 0  # dry-run：将会同步的篇数（未入库）
    failed: int = 0
    messages: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "scanned": self.scanned,
            "skipped_filter": self.skipped_filter,
            "skipped_unchanged": self.skipped_unchanged,
            "ingested": self.ingested,
            "would_ingest": self.would_ingest,
            "failed": self.failed,
            "messages": self.messages,
        }
