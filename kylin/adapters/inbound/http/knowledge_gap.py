"""知识缺口人工处理台的 FastAPI 入口适配器."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from kylin.ports.application.knowledge_gap import (
    EmptyKnowledgeGapAnswerError,
    KnowledgeGapIngestionError,
    KnowledgeGapManagementPort,
    KnowledgeGapTicketClosedError,
    KnowledgeGapTicketDeleteError,
    KnowledgeGapTicketNotFoundError,
    KnowledgeGapTicketOpenError,
)
from kylin.ports.data.knowledge_gap import KnowledgeGapTicket
from kylin.platform.security.authorization import require_permission

logger = logging.getLogger(__name__)


class AnswerBody(BaseModel):
    """人工答案请求体."""

    answer: str = Field(..., description="人工答案")


class OaFeedbackBody(BaseModel):
    """OA 侧答案回传请求体（外部系统回调）."""

    ticket_id: int = Field(..., description="本系统工单 ID（建单后由推送侧带回）")
    answer: str = Field(..., description="OA 专人填写的人工答案（将反哺进独立知识库）")
    oa_operator: Optional[str] = Field(
        default=None, description="OA 侧填写人工（可选，仅作审计日志）"
    )


class KnowledgeGapHttpAdapter:
    """将旧 HTTP 响应契约映射到知识缺口应用端口."""

    def __init__(self, service: KnowledgeGapManagementPort):
        self._service = service

    def list_tickets(self, status: Optional[str] = None, limit: int = 100) -> dict:
        items = [
            self._to_dict(ticket)
            for ticket in self._service.list_tickets(status=status, limit=limit)
        ]
        return {"code": 200, "data": items}

    def get_ticket(self, ticket_id: int) -> dict:
        ticket = self._service.get_ticket(ticket_id)
        if ticket is None:
            return {"code": 404, "msg": "工单不存在"}
        return {"code": 200, "data": self._to_dict(ticket)}

    def answer_ticket(self, ticket_id: int, body: AnswerBody) -> dict:
        return self._resolve(ticket_id, body.answer)

    def oa_feedback_ticket(self, body: OaFeedbackBody) -> dict:
        """OA 侧答案回传：与人工 answer 同一条 resolve 链路（复用反哺+验证闭环）.

        幂等语义：同一工单重复回传在 CLOSED 后返回 409；
        OPEN/RESOLVED 期间重复回传会覆盖 answer 并再次触发验证。
        """
        if body.oa_operator:
            logger.info(
                "OA 答案回传 ticket_id=%s operator=%s", body.ticket_id, body.oa_operator
            )
        return self._resolve(body.ticket_id, body.answer)

    def _resolve(self, ticket_id: int, answer: str) -> dict:
        """共享的工单解决入口：反哺入库 + 触发验证闭环."""
        try:
            result = self._service.resolve_ticket(ticket_id, answer)
        except EmptyKnowledgeGapAnswerError as error:
            return {"code": 400, "msg": str(error)}
        except KnowledgeGapTicketNotFoundError as error:
            return {"code": 404, "msg": str(error)}
        except KnowledgeGapTicketClosedError as error:
            return {"code": 409, "msg": str(error)}
        except KnowledgeGapIngestionError as error:
            return {"code": 500, "msg": f"反哺入库失败，请稍后重试: {error}"}
        if result.fed_back:
            message = "已解决，答案已反哺入库，验证中"
        else:
            message = "已解决（工单库未配置，未反哺入库）"
        return {"code": 200, "msg": message, "data": result.ticket_id}

    def reverify_ticket(self, ticket_id: int) -> dict:
        try:
            ticket = self._service.reverify(ticket_id)
        except KnowledgeGapTicketNotFoundError as error:
            return {"code": 404, "msg": str(error)}
        except KnowledgeGapTicketOpenError as error:
            return {"code": 409, "msg": str(error)}
        return {
            "code": 200,
            "data": {
                "id": ticket.id,
                "status": ticket.status,
                "needs_review": ticket.needs_review,
            },
        }

    def close_ticket(self, ticket_id: int) -> dict:
        try:
            self._service.force_close(ticket_id)
        except KnowledgeGapTicketNotFoundError as error:
            return {"code": 404, "msg": str(error)}
        return {"code": 200, "msg": "已关闭"}

    def delete_ticket(self, ticket_id: int) -> dict:
        try:
            self._service.delete_ticket(ticket_id)
        except KnowledgeGapTicketNotFoundError as error:
            return {"code": 404, "msg": str(error)}
        except KnowledgeGapTicketDeleteError as error:
            return {"code": 500, "msg": str(error)}
        return {"code": 200, "msg": "已删除"}

    @staticmethod
    def _to_dict(ticket: KnowledgeGapTicket) -> dict:
        return {
            "id": ticket.id,
            "question": ticket.question,
            "kb_name": ticket.kb_name,
            "status": ticket.status,
            "answer": ticket.answer,
            "fed_back": ticket.fed_back,
            "needs_review": ticket.needs_review,
            "create_time": (
                ticket.create_time.isoformat() if ticket.create_time else None
            ),
            "resolve_time": (
                ticket.resolve_time.isoformat() if ticket.resolve_time else None
            ),
        }


def create_ticket_router(service: KnowledgeGapManagementPort) -> APIRouter:
    """创建保持旧路径和响应结构的工单路由."""
    adapter = KnowledgeGapHttpAdapter(service)
    router = APIRouter(prefix="/tickets", tags=["知识缺口工单"])

    @router.get(
        "",
        summary="工单列表（可按状态过滤）",
        dependencies=[Depends(require_permission("ai:digital-human:ticket:query"))],
    )
    async def list_tickets(status: Optional[str] = None, limit: int = 100):
        return adapter.list_tickets(status=status, limit=limit)

    @router.get(
        "/{ticket_id}",
        summary="工单详情",
        dependencies=[Depends(require_permission("ai:digital-human:ticket:query"))],
    )
    async def get_ticket(ticket_id: int):
        return adapter.get_ticket(ticket_id)

    # 豁免路由级 require_permission：OA 侧为外部系统服务间回调，非登录用户，
    # 身份由中间件网关认证兜底（trusted_user = 网关 key + login-user header）；
    # 待网关侧服务间权限模型就绪后再补充细粒度权限码。
    @router.post(
        "/oa-feedback",
        summary="OA 答案回传（外部 OA 系统调用，触发反哺+验证闭环）",
    )
    async def oa_feedback(body: OaFeedbackBody):
        return adapter.oa_feedback_ticket(body)

    @router.post(
        "/{ticket_id}/answer",
        summary="人工回答并标记解决（触发验证闭环）",
        dependencies=[Depends(require_permission("ai:digital-human:ticket:answer"))],
    )
    async def answer_ticket(ticket_id: int, body: AnswerBody):
        return adapter.answer_ticket(ticket_id, body)

    @router.post(
        "/{ticket_id}/reverify",
        summary="手动重跑验证重查",
        dependencies=[Depends(require_permission("ai:digital-human:ticket:answer"))],
    )
    async def reverify_ticket(ticket_id: int):
        return adapter.reverify_ticket(ticket_id)

    @router.post(
        "/{ticket_id}/close",
        summary="人工复核后关闭归档",
        dependencies=[Depends(require_permission("ai:digital-human:ticket:close"))],
    )
    async def close_ticket(ticket_id: int):
        return adapter.close_ticket(ticket_id)

    @router.delete(
        "/{ticket_id}",
        summary="删除工单（已反哺内容同步清理）",
        dependencies=[Depends(require_permission("ai:digital-human:ticket:close"))],
    )
    async def delete_ticket(ticket_id: int):
        return adapter.delete_ticket(ticket_id)

    return router
