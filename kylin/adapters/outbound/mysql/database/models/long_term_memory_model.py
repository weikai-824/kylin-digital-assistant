"""长期记忆模型：跨会话用户事实 / 偏好 / 决策的持久化。"""

from sqlalchemy import Column, DateTime, Integer, String, Text, UniqueConstraint, func

from kylin.adapters.outbound.mysql.database.base import Base


class LongTermMemoryModel(Base):
    """跨会话长期记忆（按 user_id 隔离）。

    - content：事实文本（如"用户是财务部，关注报销制度"）
    - category：preference（偏好）/ fact（身份事实）/ decision（决策）
    - importance：1-5，LLM 提取时打分，检索后用于加权排序
    - status：active / superseded（新事实覆盖旧事实时旧记录软删，保留溯源）
    - superseded_by：覆盖该记忆的新记忆ID（M2 使用，当前为 NULL）
    - last_retrieved_at：最近一次被召回时间（M2 遗忘策略预留）
    - content_hash：内容指纹；与 user_id 组成唯一索引做去重
    """

    __tablename__ = "long_term_memory"
    __table_args__ = (
        UniqueConstraint("user_id", "content_hash", name="uq_ltm_user_content"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True, comment="自增ID")
    user_id = Column(String(64), index=True, nullable=False, comment="OA userid（跨会话隔离维度）")
    session_id = Column(String(64), nullable=False, default="", comment="来源会话 sess_xxx（溯源）")
    kb_name = Column(String(50), nullable=False, default="", comment="来源知识库（可选限定）")
    content = Column(Text, nullable=False, comment="记忆事实文本")
    content_hash = Column(String(64), nullable=False, default="", comment="内容指纹（去重用）")
    category = Column(String(20), nullable=False, default="fact", comment="preference/fact/decision")
    importance = Column(Integer, nullable=False, default=3, comment="1-5 重要度（LLM 打分）")
    status = Column(String(20), nullable=False, default="active", comment="active/superseded")
    superseded_by = Column(Integer, nullable=True, comment="覆盖该记忆的新记忆ID（预留）")
    last_retrieved_at = Column(DateTime, nullable=True, comment="最近召回时间（遗忘策略预留）")
    create_time = Column(DateTime, default=func.now(), comment="创建时间")
    update_time = Column(DateTime, default=func.now(), onupdate=func.now(), comment="更新时间")

    def __repr__(self):
        return (
            f"<LongTermMemory(id={self.id}, user='{self.user_id}', "
            f"cat='{self.category}', imp={self.importance}, status='{self.status}')>"
        )
