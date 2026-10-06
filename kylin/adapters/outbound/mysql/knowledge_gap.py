"""知识缺口工单的 SQLAlchemy/MySQL 仓储适配器."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select

from kylin.adapters.outbound.mysql.database.models.knowledge_gap_ticket_model import KnowledgeGapTicketModel
from kylin.adapters.outbound.mysql.database.session import session_scope
from kylin.ports.data.knowledge_gap import KnowledgeGapTicket


class MySQLKnowledgeGapRepository:
    """把既有工单表映射为不携带 ORM 状态的工单快照."""

    def create(self, question: str, kb_name: str = "") -> KnowledgeGapTicket:
        with session_scope() as session:
            # 与 resolve_time 一致：用应用本地时间写入，避免依赖 MySQL 容器
            # 默认 UTC 导致创建时间比本地墙钟慢 8 小时。
            model = KnowledgeGapTicketModel(
                question=question,
                kb_name=kb_name or "",
                create_time=datetime.now(),
            )
            session.add(model)
            session.flush()
            session.refresh(model)
            return self._to_ticket(model)

    def find_open(self, question: str) -> KnowledgeGapTicket | None:
        normalized = (question or "").strip()
        if not normalized:
            return None
        with session_scope() as session:
            statement = (
                select(KnowledgeGapTicketModel)
                .where(
                    KnowledgeGapTicketModel.status == "OPEN",
                    KnowledgeGapTicketModel.question == normalized,
                )
                .order_by(KnowledgeGapTicketModel.id.desc())
                .limit(1)
            )
            model = session.scalar(statement)
            return self._to_ticket(model) if model is not None else None

    def get(self, ticket_id: int) -> KnowledgeGapTicket | None:
        with session_scope() as session:
            model = session.get(KnowledgeGapTicketModel, ticket_id)
            return self._to_ticket(model) if model is not None else None

    def list(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
    ) -> Sequence[KnowledgeGapTicket]:
        with session_scope() as session:
            statement = select(KnowledgeGapTicketModel).order_by(
                KnowledgeGapTicketModel.id.desc()
            )
            if status:
                statement = statement.where(KnowledgeGapTicketModel.status == status)
            return tuple(
                self._to_ticket(model)
                for model in session.scalars(statement.limit(limit))
            )

    def update(
        self,
        ticket_id: int,
        *,
        status: str | None = None,
        answer: str | None = None,
        fed_back: bool | None = None,
        needs_review: bool | None = None,
        resolve_time: datetime | None = None,
    ) -> bool:
        changes = {
            "status": status,
            "answer": answer,
            "fed_back": fed_back,
            "needs_review": needs_review,
            "resolve_time": resolve_time,
        }
        selected = {name: value for name, value in changes.items() if value is not None}
        if not selected:
            return False
        with session_scope() as session:
            model = session.get(KnowledgeGapTicketModel, ticket_id)
            if model is None:
                return False
            for name, value in selected.items():
                setattr(model, name, value)
            return True

    def delete(self, ticket_id: int) -> bool:
        with session_scope() as session:
            model = session.get(KnowledgeGapTicketModel, ticket_id)
            if model is None:
                return False
            session.delete(model)
            return True

    @staticmethod
    def _to_ticket(model: KnowledgeGapTicketModel) -> KnowledgeGapTicket:
        return KnowledgeGapTicket(
            id=int(model.id),
            question=str(model.question),
            kb_name=str(model.kb_name or ""),
            status=str(model.status),
            answer=model.answer,
            fed_back=bool(model.fed_back),
            needs_review=bool(model.needs_review),
            create_time=model.create_time,
            resolve_time=model.resolve_time,
        )
