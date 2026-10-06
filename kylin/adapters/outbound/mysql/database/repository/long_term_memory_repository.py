"""长期记忆仓储：long_term_memory 表的 CRUD。"""

import hashlib
from datetime import datetime
from typing import Optional

from kylin.adapters.outbound.mysql.database.models.long_term_memory_model import LongTermMemoryModel
from kylin.adapters.outbound.mysql.database.session import with_session


def content_hash(content: str) -> str:
    """内容指纹：SHA-256 前 32 位，配合唯一索引 (user_id, content_hash) 去重。"""
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()[:32]


def _detach(session, row: LongTermMemoryModel) -> LongTermMemoryModel:
    """强制加载全部列并脱离 session，避免 with_session commit 后 detached 属性报错。"""
    for col in row.__table__.columns:
        getattr(row, col.name)
    session.expunge(row)
    return row


@with_session
def create_memory(
    session,
    *,
    user_id: str,
    content: str,
    category: str = "fact",
    importance: int = 3,
    session_id: str = "",
    kb_name: str = "",
    content_hash_value: Optional[str] = None,
) -> LongTermMemoryModel:
    """写入一条记忆；返回 detached ORM 实例。"""
    row = LongTermMemoryModel(
        user_id=user_id,
        session_id=session_id or "",
        kb_name=kb_name or "",
        content=content,
        content_hash=content_hash_value or content_hash(content),
        category=category,
        importance=importance,
    )
    session.add(row)
    session.flush()
    return _detach(session, row)


@with_session
def find_active_by_content_hash(session, user_id: str, h: str) -> Optional[LongTermMemoryModel]:
    """按内容指纹查同 user 的 active 记忆（去重）。"""
    return (
        session.query(LongTermMemoryModel)
        .filter(
            LongTermMemoryModel.user_id == user_id,
            LongTermMemoryModel.content_hash == h,
            LongTermMemoryModel.status == "active",
        )
        .first()
    )


@with_session
def list_active(session, user_id: str) -> list[LongTermMemoryModel]:
    """列出该用户全部 active 记忆，importance 高者在前。"""
    rows = (
        session.query(LongTermMemoryModel)
        .filter(
            LongTermMemoryModel.user_id == user_id,
            LongTermMemoryModel.status == "active",
        )
        .order_by(
            LongTermMemoryModel.importance.desc(),
            LongTermMemoryModel.create_time.desc(),
        )
        .all()
    )
    return [_detach(session, row) for row in rows]


@with_session
def list_by_ids(session, ids: list[int]) -> dict[int, LongTermMemoryModel]:
    """按 id 批量取记忆（保持检索顺序回表），返回 {id: row}。"""
    if not ids:
        return {}
    rows = (
        session.query(LongTermMemoryModel)
        .filter(LongTermMemoryModel.id.in_(ids))
        .all()
    )
    out: dict[int, LongTermMemoryModel] = {}
    for row in rows:
        out[row.id] = _detach(session, row)
    return out


@with_session
def touch_retrieved(session, ids: list[int]) -> None:
    """批量更新最近召回时间（遗忘策略数据源）。"""
    if not ids:
        return
    rows = (
        session.query(LongTermMemoryModel)
        .filter(LongTermMemoryModel.id.in_(ids))
        .all()
    )
    now = datetime.now()
    for row in rows:
        row.last_retrieved_at = now


@with_session
def supersede_memory(session, user_id: str, memory_id: int) -> bool:
    """软删：status active → superseded（保留溯源，不物理删除）。"""
    row = (
        session.query(LongTermMemoryModel)
        .filter(
            LongTermMemoryModel.id == memory_id,
            LongTermMemoryModel.user_id == user_id,
        )
        .first()
    )
    if not row:
        return False
    row.status = "superseded"
    return True


@with_session
def count_active(session, user_id: str) -> int:
    """该用户 active 记忆条数（配额/上限控制预留）。"""
    return (
        session.query(LongTermMemoryModel)
        .filter(
            LongTermMemoryModel.user_id == user_id,
            LongTermMemoryModel.status == "active",
        )
        .count()
    )
