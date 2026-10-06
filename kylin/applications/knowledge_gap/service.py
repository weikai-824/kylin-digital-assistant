"""知识缺口工单创建、人工反哺与验证闭环."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime

from kylin.ports.application.knowledge_gap import (
    EmptyKnowledgeGapAnswerError,
    KnowledgeGapIngestionError,
    KnowledgeGapTicketClosedError,
    KnowledgeGapTicketDeleteError,
    KnowledgeGapTicketNotFoundError,
    KnowledgeGapTicketOpenError,
    ResolveKnowledgeGapResult,
)
from kylin.ports.application.knowledge_ingestion import KnowledgeIngestionPort
from kylin.ports.data.knowledge_gap import (
    KnowledgeGapRepositoryPort,
    KnowledgeGapTicket,
    KnowledgeGapVerificationPort,
)

logger = logging.getLogger(__name__)

OPEN = "OPEN"
RESOLVED = "RESOLVED"
CLOSED = "CLOSED"
TICKET_KB_INFO = "知识缺口工单人工答案库"


def build_feedback_content(ticket_id: int, question: str, answer: str) -> str:
    """按旧格式构造人工答案 Markdown 内容."""
    normalized_question = (question or "").strip() or "(未记录问题)"
    return (
        f"# 知识缺口工单 #{ticket_id}\n\n"
        f"## {normalized_question}\n\n"
        f"{answer}\n"
    )


class KnowledgeGapService:
    """协调工单仓储、统一入库和检索验证，不依赖传输或 ORM 类型."""

    def __init__(
        self,
        *,
        repository: KnowledgeGapRepositoryPort,
        ingestion: KnowledgeIngestionPort,
        verifier: KnowledgeGapVerificationPort,
        ticket_knowledge_base: str,
        default_knowledge_base: str,
        retry_delay: int = 300,
    ):
        self._repository = repository
        self._ingestion = ingestion
        self._verifier = verifier
        self._ticket_knowledge_base = ticket_knowledge_base.strip()
        self._default_knowledge_base = default_knowledge_base.strip()
        self._retry_delay = retry_delay
        self._retried: set[int] = set()

    def ticket_kb_name(self) -> str:
        return self._ticket_knowledge_base

    def create(self, question: str, kb_name: str = "") -> int:
        normalized_question = (question or "").strip() or "(empty)"
        existing = self._repository.find_open(normalized_question)
        if existing is not None:
            logger.info(
                "复用已有 OPEN 工单 #%s: %s",
                existing.id,
                normalized_question[:50],
            )
            return existing.id
        ticket = self._repository.create(normalized_question, kb_name=kb_name or "")
        logger.info("创建知识缺口工单 #%s: %s", ticket.id, normalized_question[:50])
        return ticket.id

    def list_tickets(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
    ) -> tuple[KnowledgeGapTicket, ...]:
        return tuple(self._repository.list(status=status, limit=limit))

    def get_ticket(self, ticket_id: int) -> KnowledgeGapTicket | None:
        return self._repository.get(ticket_id)

    def resolve_ticket(
        self,
        ticket_id: int,
        answer: str,
    ) -> ResolveKnowledgeGapResult:
        normalized_answer = (answer or "").strip()
        if not normalized_answer:
            raise EmptyKnowledgeGapAnswerError("答案不能为空")
        ticket = self._require_ticket(ticket_id)
        if ticket.status == CLOSED:
            raise KnowledgeGapTicketClosedError(
                "工单已闭环，如需重开请先 close 后重新受理"
            )

        try:
            fed_back = self._feedback(ticket, normalized_answer)
        except KnowledgeGapIngestionError:
            raise
        except Exception as error:
            logger.error("反哺入库失败 #%s: %s", ticket_id, error)
            raise KnowledgeGapIngestionError(str(error)) from error
        self._repository.update(
            ticket_id,
            status=RESOLVED,
            answer=normalized_answer,
            fed_back=fed_back,
            needs_review=False,
            resolve_time=datetime.now(),
        )
        self._retried.discard(ticket_id)
        if fed_back:
            logger.info("工单 #%s 已解决并反哺入库，触发验证闭环", ticket_id)
            self.verify_and_advance(ticket_id)
        else:
            logger.info("工单 #%s 已解决（未配置工单库，未反哺入库）", ticket_id)
        return ResolveKnowledgeGapResult(ticket_id=ticket_id, fed_back=fed_back)

    def reverify(self, ticket_id: int) -> KnowledgeGapTicket:
        ticket = self._require_ticket(ticket_id)
        if ticket.status == OPEN:
            raise KnowledgeGapTicketOpenError("工单尚未解决，请先填写答案")
        self._retried.discard(ticket_id)
        self.verify_and_advance(ticket_id)
        return self._require_ticket(ticket_id)

    def force_close(self, ticket_id: int) -> None:
        self._require_ticket(ticket_id)
        self._repository.update(ticket_id, status=CLOSED, needs_review=False)
        self._retried.discard(ticket_id)
        logger.info("工单 #%s 人工复核确认关闭", ticket_id)

    def delete_ticket(self, ticket_id: int) -> None:
        """物理删除工单；若已反哺入库，先同步清理工单库里的反哺文件."""
        ticket = self._require_ticket(ticket_id)
        if ticket.fed_back and self._ticket_knowledge_base:
            self._delete_feedback_file(ticket_id)
        self._repository.delete(ticket_id)
        self._retried.discard(ticket_id)
        logger.info("工单 #%s 已删除", ticket_id)

    def _delete_feedback_file(self, ticket_id: int) -> None:
        """删除工单库中该工单的反哺 md；文件不存在视为已清理（幂等）."""
        filename = f"knowledge_gap_{ticket_id}.md"
        try:
            names = set(self._ingestion.list_files(self._ticket_knowledge_base))
        except Exception as error:
            logger.error("删除工单 #%s 反哺文件失败：查询工单库失败: %s", ticket_id, error)
            raise KnowledgeGapTicketDeleteError(
                f"查询工单库失败，无法确认反哺文件状态: {error}"
            ) from error
        if filename not in names:
            logger.info("工单 #%s 反哺文件 %s 已不存在，跳过清理", ticket_id, filename)
            return
        try:
            results = self._ingestion.delete_files(
                self._ticket_knowledge_base,
                (filename,),
                delete_content=True,
                allow_system_managed=True,
            )
        except Exception as error:
            logger.error("删除工单 #%s 反哺文件失败: %s", ticket_id, error)
            raise KnowledgeGapTicketDeleteError(
                f"清理工单库反哺文件失败: {error}"
            ) from error
        result = next(iter(results), None)
        if result is None or not result.success:
            message = result.message if result is not None else "删除未返回结果"
            raise KnowledgeGapTicketDeleteError(
                f"清理工单库反哺文件失败: {message}"
            )
        logger.info("工单 #%s 反哺文件 %s 已清理", ticket_id, filename)

    def verify_and_advance(self, ticket_id: int) -> None:
        """执行一次验证并按旧规则推进状态或调度一次重试."""
        ticket = self._repository.get(ticket_id)
        if ticket is None or ticket.status == CLOSED:
            return
        if not self._ticket_knowledge_base:
            return
        hit = self._verifier.verify(
            ticket.question,
            formal_knowledge_base=ticket.kb_name or self._default_knowledge_base,
            ticket_knowledge_base=self._ticket_knowledge_base,
        )
        if hit is True:
            self._repository.update(ticket_id, status=CLOSED, needs_review=False)
            self._retried.discard(ticket_id)
            logger.info("工单 #%s 验证命中，闭环 CLOSED", ticket_id)
            return
        if hit is None:
            return
        if ticket_id in self._retried:
            self._repository.update(ticket_id, needs_review=True)
            self._retried.discard(ticket_id)
            logger.warning("工单 #%s 二次验证未命中，转人工复核", ticket_id)
            return
        self._retried.add(ticket_id)
        self._schedule_retry(ticket_id)
        logger.info(
            "工单 #%s 首次验证未命中，%ss 后自动重试",
            ticket_id,
            self._retry_delay,
        )

    def _feedback(self, ticket: KnowledgeGapTicket, answer: str) -> bool:
        if not self._ticket_knowledge_base:
            return False
        self._ensure_ticket_knowledge_base()
        filename = f"knowledge_gap_{ticket.id}.md"
        content = build_feedback_content(ticket.id, ticket.question, answer)
        metadata = {
            "source": filename,
            "source_ticket_id": str(ticket.id),
            "content_hash": hashlib.md5(content.encode("utf-8")).hexdigest(),
        }
        results = self._ingestion.upload_files(
            self._ticket_knowledge_base,
            ((filename, content.encode("utf-8")),),
            override=True,
            metadata_by_filename={filename: metadata},
            allow_system_managed=True,
        )
        result = next(iter(results), None)
        if result is None or not result.success:
            message = result.message if result is not None else "入库未返回结果"
            raise KnowledgeGapIngestionError(message)
        return True

    def _ensure_ticket_knowledge_base(self) -> None:
        names = {item.name for item in self._ingestion.list_knowledge_bases()}
        if self._ticket_knowledge_base in names:
            return
        try:
            self._ingestion.create_knowledge_base(
                self._ticket_knowledge_base,
                info=TICKET_KB_INFO,
                allow_system_managed=True,
            )
        except ValueError:
            names = {item.name for item in self._ingestion.list_knowledge_bases()}
            if self._ticket_knowledge_base not in names:
                raise

    def _require_ticket(self, ticket_id: int) -> KnowledgeGapTicket:
        ticket = self._repository.get(ticket_id)
        if ticket is None:
            raise KnowledgeGapTicketNotFoundError("工单不存在")
        return ticket

    def _schedule_retry(self, ticket_id: int) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning(
                "无运行中事件循环，工单 #%s 重试未调度（可手动 reverify）",
                ticket_id,
            )
            return
        loop.create_task(self._delayed_retry(ticket_id))

    async def _delayed_retry(self, ticket_id: int) -> None:
        await asyncio.sleep(self._retry_delay)
        await asyncio.to_thread(self.verify_and_advance, ticket_id)
