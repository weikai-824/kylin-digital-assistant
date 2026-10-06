"""会话记忆模型：多轮对话的分层记忆持久化（原文归档 + 滚动摘要）。"""

from sqlalchemy import Column, DateTime, Integer, String, Text, func

from kylin.adapters.outbound.mysql.database.base import Base


class ConversationSessionModel(Base):
    """
    多轮对话会话（会话内记忆，不做跨会话长期记忆）。

    分层记忆落地：
    - messages_json：最近若干轮原文归档（JSON 数组，[{role, content}, ...]），供指代消解与生成原文窗口
    - summary：更早轮次的增量滚动摘要（LLM 压缩），放生成 prompt 的 System 位置
    - turn_count：累计问答轮数（只增不减），用于触发滚动摘要

    生命周期：查询时超过 SESSION_TTL_DAYS 未活跃视为过期重建；无物理删除任务。
    """

    __tablename__ = "conversation_session"
    id = Column(Integer, primary_key=True, autoincrement=True, comment="自增ID")
    session_id = Column(
        String(64), unique=True, index=True, nullable=False, comment="对外会话标识 sess_xxx"
    )
    kb_name = Column(String(50), nullable=False, default="", comment="关联知识库")
    summary = Column(Text, nullable=False, default="", comment="滚动摘要（增量压缩）")
    messages_json = Column(
        Text, nullable=False, default="", comment="最近 N 轮原文归档 JSON"
    )
    turn_count = Column(Integer, nullable=False, default=0, comment="累计问答轮数")
    last_active = Column(
        DateTime, default=func.now(), onupdate=func.now(), comment="最后活跃时间"
    )
    create_time = Column(DateTime, default=func.now(), comment="创建时间")

    def __repr__(self):
        return (
            f"<ConversationSession(id={self.id}, session_id='{self.session_id}', "
            f"kb='{self.kb_name}', turns={self.turn_count})>"
        )
