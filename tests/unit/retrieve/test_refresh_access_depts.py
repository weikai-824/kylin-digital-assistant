"""「只刷权限」链路离线测试：query_children_by_source / update_children_access_depts /
update_doc_meta_access_depts / KnowledgeIngestionService.refresh_doc_access_depts。"""

from __future__ import annotations

import pytest

from kylin.adapters.outbound.knowledge_base.index import stores
from kylin.applications.knowledge_ingestion.service import KnowledgeIngestionService


# ---------- query_children_by_source ----------

class _FakeMilvusRowQuery:
    def __init__(self, rows):
        self._rows = rows

    def query(self, expr, output_fields):  # noqa: ARG002
        return self._rows


def test_query_children_by_source_reads_rows_with_vector_and_scalars(monkeypatch):
    name = "children_oa_kb"
    rows = [
        {
            "pk": "c1",
            "text": "t1",
            "source": "a.pdf",
            "child_id": "c1",
            "department_id": "D1",
            "access_depts": ";D1;",
            "embedding": [0.1, 0.2, 0.3],
        }
    ]
    col = _FakeMilvusRowQuery(rows)
    monkeypatch.setattr(stores, "children_collection_name", lambda kb: name)
    monkeypatch.setattr(stores, "milvus_connect", lambda: "alias")
    monkeypatch.setattr(
        "pymilvus.utility.has_collection", lambda nm, using=None: True
    )
    monkeypatch.setattr("pymilvus.Collection", lambda n, using=None: col)
    monkeypatch.setattr(stores, "_milvus_fields", lambda c: {k for k in rows[0] if k != "embedding"})

    items = stores.query_children_by_source("oa_kb", "a.pdf")
    assert len(items) == 1
    doc, vec = items[0]
    assert doc.page_content == "t1"
    assert doc.metadata["access_depts"] == ";D1;"
    assert doc.metadata["department_id"] == "D1"
    assert vec == [0.1, 0.2, 0.3]


def test_query_children_by_source_empty_collection_returns_empty(monkeypatch):
    monkeypatch.setattr(stores, "milvus_connect", lambda: "alias")
    monkeypatch.setattr(stores, "children_collection_name", lambda kb: "children_x")
    monkeypatch.setattr("pymilvus.utility.has_collection", lambda nm, using=None: False)
    assert stores.query_children_by_source("oa_kb", "a.pdf") == []


# ---------- update_children_access_depts ----------

def test_update_children_access_depts_rewrites_metadata_and_passes_vectors(monkeypatch):
    capture = {}

    def _fake_query(kb, source):
        from kylin.platform.runtime.documents import Document

        return [
            (
                Document(
                    page_content="t1",
                    metadata={"child_id": "c1", "department_id": "D1", "source": source},
                ),
                [0.1, 0.2],
            ),
            (
                Document(
                    page_content="t2",
                    metadata={"child_id": "c2", "department_id": "D1", "source": source},
                ),
                [0.3, 0.4],
            ),
        ]

    def _fake_write_children(kb, docs, embeddings):
        capture["kb"] = kb
        capture["docs"] = docs
        capture["embeddings"] = embeddings

    monkeypatch.setattr(stores, "query_children_by_source", _fake_query)
    monkeypatch.setattr(stores, "write_children", _fake_write_children)

    n = stores.update_children_access_depts("oa_kb", "a.pdf", ["D2"])
    assert n == 2
    assert capture["kb"] == "oa_kb"
    assert [d.metadata["access_depts"] for d in capture["docs"]] == [["D2"], ["D2"]]
    assert capture["embeddings"] == [[0.1, 0.2], [0.3, 0.4]]


def test_update_children_access_depts_public_uses_empty_list(monkeypatch):
    capture = {}

    def _fake_query(kb, source):
        from kylin.platform.runtime.documents import Document

        return [
            (Document(page_content="t1", metadata={"child_id": "c1", "source": source}), [1.0])
        ]

    def _fake_write_children(kb, docs, embeddings):
        capture["docs"] = docs

    monkeypatch.setattr(stores, "query_children_by_source", _fake_query)
    monkeypatch.setattr(stores, "write_children", _fake_write_children)
    stores.update_children_access_depts("oa_kb", "a.pdf", [])
    assert capture["docs"][0].metadata["access_depts"] == []


# ---------- update_doc_meta_access_depts ----------

class _Ilkie:
    def ilike(self, v):  # noqa: ARG002
        return self


def test_update_doc_meta_access_depts_merges_ledger(monkeypatch):
    class _Row:
        def __init__(self):
            self.meta_data = {"source": "a.pdf", "department_id": "D1", "access_depts": ["D1"]}

    row = _Row()
    captured = {}

    class _Q:
        def __init__(self, row):
            self._row = row

        def filter(self, *a, **k):
            captured["called"] = True
            return self

        def all(self):
            return [self._row]

    class _Session:
        def __init__(self):
            self._row = row
            self.committed = False

        def query(self, model):
            captured["model"] = model
            return _Q(self._row)

        def commit(self):
            self.committed = True

    FakeFileDocModel = type(
        "FileDocModel", (), {"kb_name": _Ilkie(), "file_name": _Ilkie()}
    )
    monkeypatch.setattr(
        "kylin.adapters.outbound.mysql.database.repository."
        "knowledge_file_repository.FileDocModel",
        FakeFileDocModel,
    )

    s = _Session()
    from kylin.adapters.outbound.mysql.database.repository import (
        knowledge_file_repository as repo,
    )

    raw = repo.update_doc_meta_access_depts.__wrapped__
    raw(s, "oa_kb", "a.pdf", ["D9", " D1 "])
    assert captured["called"] is True
    assert captured["model"] is FakeFileDocModel
    assert row.meta_data["access_depts"] == ["D9", "D1"]
    assert "source" in row.meta_data


# ---------- KnowledgeIngestionService.refresh_doc_access_depts ----------

class _FakeIndex:
    def __init__(self):
        self.calls = []

    def update_access_depts(self, kb, filename, access_depts=()):
        self.calls.append((kb, filename, list(access_depts)))
        return 3


class _FakeCatalog:
    def __init__(self, name):
        self._name = name

    def get(self, name):
        return self._name if name == self._name else None


class _FakeStorage:
    def list(self, name):
        return ["a.pdf"]


def test_refresh_doc_access_depts_delegates_to_index():
    idx = _FakeIndex()
    svc = KnowledgeIngestionService(
        catalog=_FakeCatalog("oa_kb"),
        storage=_FakeStorage(),
        processor=None,
        index=idx,
    )
    n = svc.refresh_doc_access_depts("oa_kb", "a.pdf", [])
    assert n == 3
    assert idx.calls == [("oa_kb", "a.pdf", [])]


def test_refresh_doc_access_depts_normalizes_depts():
    idx = _FakeIndex()
    svc = KnowledgeIngestionService(
        catalog=_FakeCatalog("oa_kb"),
        storage=_FakeStorage(),
        processor=None,
        index=idx,
    )
    svc.refresh_doc_access_depts("oa_kb", "a.pdf", [" D2 ", "D3", ""])
    assert idx.calls[0][2] == ["D2", "D3"]


# ---------- HTTP adapter：update_access_depts（托管库纯刷权限可用） ----------

def test_http_update_access_depts_calls_refresh_per_file():
    from kylin.adapters.inbound.http.knowledge_base import KnowledgeBaseHttpAdapter

    class _FakeService:
        def __init__(self):
            self.calls = []

        def refresh_doc_access_depts(self, name, filename, access_depts=()):
            self.calls.append((name, filename, list(access_depts)))
            return 2

    service = _FakeService()
    adapter = KnowledgeBaseHttpAdapter(service)
    resp = adapter.update_access_depts(
        knowledge_base_name="oa_kb",
        file_names=["a.pdf", "b.pdf"],
        access_depts=["D2"],
    )
    assert resp.code == 200
    assert service.calls == [
        ("oa_kb", "a.pdf", ["D2"]),
        ("oa_kb", "b.pdf", ["D2"]),
    ]
    assert resp.data == {"updated": {"a.pdf": 2, "b.pdf": 2}}


def test_http_update_access_depts_public_uses_empty_list():
    from kylin.adapters.inbound.http.knowledge_base import KnowledgeBaseHttpAdapter

    captured = {}

    class _FakeService:
        def refresh_doc_access_depts(self, name, filename, access_depts=()):
            captured["access_depts"] = list(access_depts)
            return 1

    adapter = KnowledgeBaseHttpAdapter(_FakeService())
    resp = adapter.update_access_depts(
        knowledge_base_name="oa_kb", file_names=["a.pdf"], access_depts=[]
    )
    assert resp.code == 200
    assert captured["access_depts"] == []