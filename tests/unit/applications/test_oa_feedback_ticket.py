"""OA 答案回传端点（POST /tickets/oa-feedback）适配层测试：不连数据库/真实存储。

验证 KnowledgeGapHttpAdapter.oa_feedback_ticket 与人工 answer 走同一条
resolve 链路，错误映射一致（400/404/409/500），幂等语义由 service 层保证。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kylin.adapters.inbound.http.knowledge_gap import (
    AnswerBody,
    KnowledgeGapHttpAdapter,
    OaFeedbackBody,
)


class FakeGapService:
    """模拟 KnowledgeGapManagementPort：按工单 id 注入不同失败。"""

    def __init__(self, *, closed_ids=(), missing_ids=(), ingestion_error_ids=()):
        self._closed = set(closed_ids)
        self._missing = set(missing_ids)
        self._ingestion_error = set(ingestion_error_ids)
        self.calls: list[tuple[int, str]] = []

    def resolve_ticket(self, ticket_id: int, answer: str):
        self.calls.append((ticket_id, answer))
        if ticket_id in self._missing:
            from kylin.ports.application.knowledge_gap import (
                KnowledgeGapTicketNotFoundError,
            )

            raise KnowledgeGapTicketNotFoundError("工单不存在")
        if ticket_id in self._closed:
            from kylin.ports.application.knowledge_gap import (
                KnowledgeGapTicketClosedError,
            )

            raise KnowledgeGapTicketClosedError("工单已闭环")
        if ticket_id in self._ingestion_error:
            from kylin.ports.application.knowledge_gap import (
                KnowledgeGapIngestionError,
            )

            raise KnowledgeGapIngestionError("入库失败")
        fed_back = True
        return SimpleNamespace(ticket_id=ticket_id, fed_back=fed_back)


def _adapter(**kwargs):
    return KnowledgeGapHttpAdapter(FakeGapService(**kwargs))


def test_oa_feedback_resolves_like_manual_answer():
    """OA 回传与人工 answer 调用同一 resolve_ticket."""
    service = FakeGapService()
    adapter = KnowledgeGapHttpAdapter(service)

    oa_ret = adapter.oa_feedback_ticket(
        OaFeedbackBody(ticket_id=7, answer="OA 填的答案", oa_operator="张三")
    )
    manual_ret = adapter.answer_ticket(7, AnswerBody(answer="人工答案"))

    assert oa_ret == {"code": 200, "msg": "已解决，答案已反哺入库，验证中", "data": 7}
    assert manual_ret["code"] == 200
    # 两者最终都落到 service.resolve_ticket
    assert service.calls == [(7, "OA 填的答案"), (7, "人工答案")]


@pytest.mark.parametrize(
    ("kwargs", "ticket_id", "expected_code"),
    [
        ({"missing_ids": (1,)}, 1, 404),
        ({"closed_ids": (2,)}, 2, 409),
        ({"ingestion_error_ids": (3,)}, 3, 500),
    ],
)
def test_oa_feedback_error_mapping(kwargs, ticket_id, expected_code):
    """OA 回传的错误码与人工 answer 一致."""
    adapter = _adapter(**kwargs)
    ret = adapter.oa_feedback_ticket(
        OaFeedbackBody(ticket_id=ticket_id, answer="答案")
    )
    assert ret["code"] == expected_code


def test_oa_feedback_without_operator_ok():
    """oa_operator 可选：缺省不报错."""
    adapter = _adapter()
    ret = adapter.oa_feedback_ticket(
        OaFeedbackBody(ticket_id=9, answer="无操作人答案")
    )
    assert ret["code"] == 200
