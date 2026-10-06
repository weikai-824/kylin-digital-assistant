"""Ports used by the OA knowledge synchronization application."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any, Protocol

from kylin.ports.data.oa_sync import (
    OAAttachmentDispatchResult,
    OASyncSettings,
    SyncState,
)


class OASourcePort(Protocol):
    """Vendor API capabilities required by OA synchronization."""

    def close(self) -> None:
        """Release the source client."""

    def resolve_userid(self, settings: OASyncSettings) -> str:
        """Resolve the service account used by document APIs."""

    def set_userid(self, userid: str) -> None:
        """Set the resolved service account."""

    def iter_documents(self, page_size: int) -> Iterator[Mapping[str, Any]]:
        """Yield all documents visible to the configured account."""

    def list_attachments(self, doc_id: str) -> Sequence[Mapping[str, Any]]:
        """Return attachment metadata for a document."""

    def download_file(self, file_id: str, module: str = "") -> bytes:
        """Download one attachment."""

    def get_document_content(self, doc_id: str) -> Mapping[str, Any]:
        """Return the document detail response."""

    def get_document_department_id(self, doc_id: str) -> str:
        """Return the owning department id of a document ('' when unavailable)."""

    def extract_content(self, info: Mapping[str, Any]) -> str | None:
        """Extract the configured document body from a detail response."""


class OASyncStatePort(Protocol):
    """Persistence boundary for the OA sync state file."""

    def load(self) -> SyncState:
        """Load the current state."""

    def save(self, state: SyncState) -> None:
        """Persist the current state."""


class OAAttachmentDispatchPort(Protocol):
    """按类型分派 OA 附件/主文档：下载策略与入库内容由出站适配器实现。"""

    def __call__(
        self,
        item: Any,
        *,
        doc_id: str,
        doc_name: str,
        download_fn: Callable[[], bytes],
    ) -> OAAttachmentDispatchResult | None:
        """返回分派结果；None 表示跳过（已由实现侧记日志）。"""
