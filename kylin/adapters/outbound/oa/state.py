"""JSON adapter for the OA synchronization state."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from kylin.adapters.outbound.oa.config import OA_SYNC_STATE_PATH
from kylin.ports.data.oa_sync import AttachmentState, DocState, SyncState


class JsonOASyncStateStore:
    """Persist the existing OA state shape under KYLIN_ROOT/data."""

    def __init__(self, path: Path | None = None):
        self._path = path or OA_SYNC_STATE_PATH

    def load(self) -> SyncState:
        if not self._path.is_file():
            return SyncState()
        with open(self._path, "r", encoding="utf-8") as file:
            raw = json.load(file)
        docs = {
            doc_id: _doc_from_dict(value)
            for doc_id, value in (raw.get("docs") or {}).items()
        }
        return SyncState(last_sync_at=raw.get("last_sync_at", ""), docs=docs)

    def save(self, state: SyncState) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "last_sync_at": state.last_sync_at,
            "docs": {doc_id: asdict(doc) for doc_id, doc in state.docs.items()},
        }
        with open(self._path, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)


def _doc_from_dict(raw: dict) -> DocState:
    attachments = [
        AttachmentState(**attachment)
        for attachment in raw.get("attachments", [])
    ]
    return DocState(
        doc_id=raw["doc_id"],
        name=raw.get("name", ""),
        last_update_time=raw.get("last_update_time", ""),
        local_filenames=list(raw.get("local_filenames", [])),
        attachments=attachments,
        content_fingerprint=raw.get("content_fingerprint", ""),
        last_error=raw.get("last_error", ""),
        retry_count=int(raw.get("retry_count") or 0),
        last_attempt_at=raw.get("last_attempt_at", ""),
        half_written=list(raw.get("half_written", [])),
    )
