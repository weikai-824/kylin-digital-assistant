"""OA 同步部门打标模拟测试：验证 department_id 写入上传 metadata（不连真实 OA/存储）。"""

from __future__ import annotations

from types import SimpleNamespace

from kylin.applications.knowledge_ingestion.oa_sync import OAKnowledgeSyncService
from kylin.adapters.outbound.oa.file_dispatcher import dispatch as dispatch_oa_attachment
from kylin.ports.data.oa_sync import DocState, SyncState


def _make_settings(**overrides):
    defaults = dict(
        enabled=True,
        base_url="http://fake-oa",
        corpid="c",
        app_key="k",
        app_secret="s",
        userid="u1",
        job_num="",
        account="",
        kb_name="oa_kb",
        kb_info="",
        page_size=10,
        folder_path_whitelist=[],
        skip_status_not_normal=True,
        ingest_html_content=True,
        download_module="",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class FakeSource:
    """模拟 OA 文档源：返回带部门 ID 的文档 + 一个 docx 附件。"""

    def __init__(self, department_by_doc: dict[str, str]):
        self._dept = department_by_doc
        self.closed = False
        self.department_calls: list[str] = []

    def close(self):
        self.closed = True

    def resolve_userid(self, settings):
        return settings.userid

    def set_userid(self, userid):
        pass

    def iter_documents(self, page_size):
        for doc_id in self._dept:
            yield {
                "id": doc_id,
                "name": f"文档{doc_id}",
                "docLastUpdateTime": "2026-09-01 10:00:00",
                "docStatusName": "正常",
                "countOfAttachment": 1,
            }

    def list_attachments(self, doc_id):
        return [
            {
                "fileId": f"att_{doc_id}",
                "name": f"附件{doc_id}.docx",
                "extName": "docx",
                "module": "workflow",
                "md5": f"md5_{doc_id}",
            }
        ]

    def download_file(self, file_id, module=""):
        return b"fake-docx-content"

    def get_document_content(self, doc_id):
        return {"docFile": None}

    def get_document_department_id(self, doc_id):
        self.department_calls.append(doc_id)
        return self._dept.get(doc_id, "")

    def extract_content(self, info):
        return None


class FakeIngestion:
    """记录上传调用，不真正入库。"""

    def __init__(self):
        self.uploads: list[dict] = []
        self.deleted: list[str] = []

    def list_knowledge_bases(self):
        return [SimpleNamespace(name="oa_kb")]

    def create_knowledge_base(self, name, **kwargs):
        return SimpleNamespace(name=name)

    def upload_files(self, name, files, *, metadata_by_filename=None, **kwargs):
        for filename, content in files:
            self.uploads.append(
                {
                    "filename": filename,
                    "metadata": dict(metadata_by_filename or {}).get(filename, {}),
                }
            )
        return [SimpleNamespace(success=True, message="ok")]

    def update_files(self, name, filenames, **kwargs):
        return [SimpleNamespace(success=True, message="ok")]

    def delete_files(self, name, filenames, **kwargs):
        self.deleted.extend(filenames)
        return [SimpleNamespace(success=True, message="ok")]

    def list_files(self, name):
        return []


class FakeStateStore:
    def __init__(self):
        self._state = SyncState()

    def load(self):
        return self._state

    def save(self, state):
        self._state = state


def _run_sync(settings, source, ingestion) -> FakeIngestion:
    service = OAKnowledgeSyncService(
        settings=settings,
        source_factory=lambda: source,
        state_store=FakeStateStore(),
        ingestion=ingestion,
        file_dispatcher=dispatch_oa_attachment,
    )
    service.sync()
    return ingestion


def test_oa_sync_writes_department_id_into_metadata():
    """文档带部门 → 上传 metadata 含 department_id + access_depts=[本部门]（默认仅本部门可见）。"""
    ingestion = _run_sync(
        _make_settings(),
        FakeSource({"doc-1": "dept-100"}),
        FakeIngestion(),
    )
    assert ingestion.uploads
    upload = ingestion.uploads[0]
    assert upload["metadata"]["oa_doc_id"] == "doc-1"
    assert upload["metadata"]["department_id"] == "dept-100"
    assert upload["metadata"]["access_depts"] == ["dept-100"]


def test_oa_sync_public_by_default_omits_access_depts():
    """document_public_by_default=true → OA 文档全开放（不写 access_depts）。"""
    ingestion = _run_sync(
        _make_settings(document_public_by_default=True),
        FakeSource({"doc-1": "dept-100"}),
        FakeIngestion(),
    )
    assert ingestion.uploads
    upload = ingestion.uploads[0]
    assert upload["metadata"]["department_id"] == "dept-100"
    assert "access_depts" not in upload["metadata"]


def test_oa_sync_omits_department_id_when_missing():
    """文档无部门 → 上传 metadata 不含 department_id（未打标=全员可见）。"""
    ingestion = _run_sync(
        _make_settings(),
        FakeSource({"doc-2": ""}),
        FakeIngestion(),
    )
    assert ingestion.uploads
    assert "department_id" not in ingestion.uploads[0]["metadata"]


def test_oa_sync_uses_docid_from_metadata():
    """验证 department_calls 只对同步的文档触发。"""
    source = FakeSource({"doc-1": "dept-100", "doc-3": "dept-300"})
    _run_sync(_make_settings(), source, FakeIngestion())
    assert set(source.department_calls) == {"doc-1", "doc-3"}
