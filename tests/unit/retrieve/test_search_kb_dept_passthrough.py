"""检索部门过滤透传模拟测试：验证 dept / dept_exempt 从 search_kb 穿透到 KBService.do_search。"""

from __future__ import annotations

import pytest

from kylin.adapters.outbound.knowledge_base.retrieve.service import search_kb


class FakeKBService:
    """记录 search_docs 收到的参数，不真检索。"""

    def __init__(self):
        self.calls: list[dict] = []

    def search_docs(self, query, top_k=None, score_threshold=None, dept=None, dept_exempt=False):
        self.calls.append(
            {
                "query": query,
                "top_k": top_k,
                "score_threshold": score_threshold,
                "dept": dept,
                "dept_exempt": dept_exempt,
            }
        )
        return []


@pytest.fixture
def fake_service(monkeypatch):
    svc = FakeKBService()
    monkeypatch.setattr(
        "kylin.adapters.outbound.knowledge_base.retrieve.service.KBServiceFactory.get_service_by_name",
        lambda name: svc,
    )
    return svc


def test_search_kb_passes_dept_to_service(fake_service):
    search_kb("问题", "oa_kb", top_k=5, score_threshold=2.0, dept="dept-100")
    assert fake_service.calls == [
        {
            "query": "问题",
            "top_k": 5,
            "score_threshold": 2.0,
            "dept": "dept-100",
            "dept_exempt": False,
        }
    ]


def test_search_kb_passes_dept_exempt(fake_service):
    search_kb("问题", "oa_kb", top_k=5, score_threshold=2.0, dept="dept-100", dept_exempt=True)
    assert fake_service.calls[0]["dept"] == "dept-100"
    assert fake_service.calls[0]["dept_exempt"] is True


def test_search_kb_passes_none_when_absent(fake_service):
    search_kb("问题", "oa_kb", top_k=5, score_threshold=2.0)
    assert fake_service.calls[0]["dept"] is None
    assert fake_service.calls[0]["dept_exempt"] is False
