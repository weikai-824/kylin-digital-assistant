"""统一 Session 管理（P8：三套合一）。

历史上有三套：FastAPI Depends 的 get_db/get_db0（已无引用，删除）、
tickets 自建事务（已改为复用本模块）、仓储层 with_session。现在
session_scope / with_session 是全库唯一 Session 获取模式。
"""

from contextlib import contextmanager
from functools import wraps

from sqlalchemy.orm import Session

from kylin.adapters.outbound.mysql.database.base import SessionLocal


@contextmanager
def session_scope() -> Session:
    """上下文管理器用于自动获取 Session, 避免错误"""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def with_session(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        # session_scope 已负责 commit/rollback/close，此处不再重复提交
        with session_scope() as session:
            return f(session, *args, **kwargs)

    return wrapper
