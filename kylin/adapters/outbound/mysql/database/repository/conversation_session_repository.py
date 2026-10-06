"""会话记忆仓储：conversation_session 表的读写。"""

import uuid
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func

from kylin.adapters.outbound.mysql.database.models.conversation_session_model import ConversationSessionModel
from kylin.adapters.outbound.mysql.database.session import with_session

# 会话过期阈值：超过 30 天未活跃视为过期，下次访问重建新会话
SESSION_TTL_DAYS = 30


def _new_session_id() -> str:
    return f"sess_{uuid.uuid4().hex[:12]}"


def _detach(session, s: ConversationSessionModel) -> ConversationSessionModel:
    """强制加载全部列并脱离 session，避免 with_session commit 后 detached 过期属性报错。"""
    for col in s.__table__.columns:
        getattr(s, col.name)
    session.expunge(s)
    return s


@with_session
def get_or_create_session(
    session, session_id: str = None, kb_name: str = ""
) -> ConversationSessionModel:
    """按 session_id 加载会话；不存在 / 已过期则新建。返回 ORM 实例。"""
    if session_id:
        s = (
            session.query(ConversationSessionModel)
            .filter(ConversationSessionModel.session_id == session_id)
            .first()
        )
        if s:
            if s.last_active and s.last_active < datetime.now() - timedelta(
                days=SESSION_TTL_DAYS
            ):
                # 过期会话视为失效，走新建
                s = None
            else:
                s.last_active = func.now()
                return _detach(session, s)
    sid = session_id if (session_id and session_id.startswith("sess_")) else _new_session_id()
    s = ConversationSessionModel(session_id=sid, kb_name=kb_name)
    session.add(s)
    session.flush()
    return _detach(session, s)


@with_session
def get_session(session, session_id: str) -> Optional["ConversationSessionModel"]:
    """只读加载会话（不更新 last_active、不新建）。不存在返回 None。"""
    if not session_id:
        return None
    s = (
        session.query(ConversationSessionModel)
        .filter(ConversationSessionModel.session_id == session_id)
        .first()
    )
    return _detach(session, s) if s else None


@with_session
def update_session_fields(session, session_id: str, **fields) -> bool:
    """按 session_id 更新字段（summary / messages_json / turn_count / last_active 等）。"""
    s = (
        session.query(ConversationSessionModel)
        .filter(ConversationSessionModel.session_id == session_id)
        .first()
    )
    if not s:
        return False
    for k, v in fields.items():
        setattr(s, k, v)
    return True


@with_session
def delete_session(session, session_id: str) -> bool:
    """物理删除会话（管理功能预留）。"""
    s = (
        session.query(ConversationSessionModel)
        .filter(ConversationSessionModel.session_id == session_id)
        .first()
    )
    if s:
        session.delete(s)
        return True
    return False
