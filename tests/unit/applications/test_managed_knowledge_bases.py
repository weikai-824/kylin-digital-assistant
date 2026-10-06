"""系统托管知识库写保护单元测试。"""

from __future__ import annotations

import pytest

from kylin.applications.knowledge_ingestion.reserved import (
    ManagedKnowledgeBaseError,
    ManagedKnowledgeBaseGuard,
    resolve_managed_knowledge_bases,
)
from kylin.applications.knowledge_ingestion.service import KnowledgeIngestionService
from kylin.ports.data.ingestion import KnowledgeBaseInfo


class _FakeCatalog:
    def __init__(self, names: list[str]):
        self._items = {
            name: KnowledgeBaseInfo(name=name, info="", embedding_model="e")
            for name in names
        }

    def list(self):
        return list(self._items.values())

    def get(self, name: str):
        return self._items.get(name)

    def create(self, info: KnowledgeBaseInfo):
        self._items[info.name] = info
        return info

    def update_info(self, name: str, description: str) -> None:
        item = self._items[name]
        self._items[name] = KnowledgeBaseInfo(
            name=item.name,
            info=description,
            embedding_model=item.embedding_model,
            vector_store_type=item.vector_store_type,
        )

    def delete(self, name: str) -> bool:
        return self._items.pop(name, None) is not None


class _FakeStorage:
    def list(self, name: str):
        return []

    def details(self, name: str):
        return []

    def path(self, name: str, filename: str) -> str:
        return filename

    def exists(self, name: str, filename: str) -> bool:
        return False

    def size(self, name: str, filename: str) -> int:
        return 0

    def save(self, name: str, filename: str, content: bytes) -> None:
        return None

    def delete(self, name: str, filename: str) -> None:
        return None


class _FakeIndex:
    def ensure(self, name: str) -> None:
        return None

    def upsert(self, name: str, processed) -> None:
        return None

    def delete_by_source(self, name: str, filename: str) -> None:
        return None

    def clear(self, name: str) -> None:
        return None

    def check_ready(self, name: str, embedding_model: str | None = None) -> None:
        return None


class _FakeProcessor:
    def process(self, *args, **kwargs):
        raise AssertionError("should not process in these tests")


def _service(*managed: str) -> KnowledgeIngestionService:
    return KnowledgeIngestionService(
        catalog=_FakeCatalog(["oa_kb", "knowledge_tickets", "oa_manuals"]),
        storage=_FakeStorage(),
        processor=_FakeProcessor(),
        index=_FakeIndex(),
        managed_knowledge_bases=managed,
    )


def test_resolve_managed_knowledge_bases() -> None:
    names = resolve_managed_knowledge_bases(
        ticket_kb_name="knowledge_tickets",
        oa_kb_name="oa_kb",
        extra=["", "  "],
    )
    assert names == frozenset({"knowledge_tickets", "oa_kb"})


def test_guard_allows_when_flag_set() -> None:
    guard = ManagedKnowledgeBaseGuard(["oa_kb"])
    guard.assert_mutable("oa_kb", allow_system_managed=True, action="上传")


def test_guard_blocks_user_mutation() -> None:
    guard = ManagedKnowledgeBaseGuard(["oa_kb"])
    with pytest.raises(ManagedKnowledgeBaseError):
        guard.assert_mutable("oa_kb", allow_system_managed=False, action="上传")


def test_upload_to_managed_kb_blocked() -> None:
    service = _service("oa_kb", "knowledge_tickets")
    with pytest.raises(ManagedKnowledgeBaseError):
        service.upload_files("oa_kb", [("a.md", b"x")])


def test_upload_to_business_kb_allowed() -> None:
    service = _service("oa_kb", "knowledge_tickets")
    results = service.upload_files("oa_manuals", [("a.md", b"x")], to_vector_store=False)
    assert len(results) == 1
    assert results[0].success


def test_system_managed_upload_allowed_with_flag() -> None:
    service = _service("oa_kb")
    results = service.upload_files(
        "oa_kb",
        [("oa_1.md", b"x")],
        to_vector_store=False,
        allow_system_managed=True,
    )
    assert results[0].success


def test_rebuild_managed_blocked_without_flag() -> None:
    service = _service("oa_kb")
    with pytest.raises(ManagedKnowledgeBaseError):
        service.rebuild("oa_kb")


def test_rebuild_managed_allowed_with_system_flag() -> None:
    """CLI --system 透传 allow_system_managed=True，托管库可重建（应用新字段）。"""
    service = _service("oa_kb")
    results = service.rebuild("oa_kb", allow_system_managed=True)
    assert isinstance(results, (list, tuple))


def test_create_managed_name_blocked() -> None:
    service = _service("oa_kb")
    with pytest.raises(ManagedKnowledgeBaseError):
        service.create_knowledge_base("oa_kb")
