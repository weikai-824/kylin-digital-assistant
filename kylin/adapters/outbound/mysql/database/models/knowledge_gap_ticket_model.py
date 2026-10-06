"""知识缺口工单模型：RAG 拒答 → 人工回答 → 反哺闭环的状态台账。"""

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text

from kylin.adapters.outbound.mysql.database.base import Base


class KnowledgeGapTicketModel(Base):
    """
    知识缺口工单。

    生命周期：OPEN（拒答自动建单）→ RESOLVED（人工已答，fed_back 标记已交付）
    → CLOSED（反哺后验证重查命中，闭环完成）。
    验证重查未命中自动重试一次仍失败 → needs_review=True 挂起人工复核。
    """

    __tablename__ = "knowledge_gap_ticket"
    id = Column(Integer, primary_key=True, autoincrement=True, comment="工单ID")
    question = Column(Text, nullable=False, comment="原始问题")
    kb_name = Column(String(50), nullable=False, default="", comment="拒答时使用的正式知识库")
    status = Column(
        String(16),
        nullable=False,
        default="OPEN",
        index=True,
        comment="状态: OPEN / RESOLVED / CLOSED",
    )
    answer = Column(Text, nullable=True, comment="人工答案（RESOLVED 时填写）")
    fed_back = Column(
        Boolean,
        nullable=False,
        default=False,
        comment="人工答案是否已交付上游知识库流程",
    )
    needs_review = Column(
        Boolean,
        nullable=False,
        default=False,
        comment="验证重查重试后仍失败，挂起人工复核",
    )
    # 使用应用进程本地时间（与 resolve_time=datetime.now() 一致），
    # 不依赖 MySQL NOW()：Docker 中 MySQL 默认 UTC，会造成创建时间偏慢约 8 小时。
    create_time = Column(DateTime, default=datetime.now, comment="创建时间")
    resolve_time = Column(DateTime, nullable=True, comment="解决时间（人工回答时间）")

    def __repr__(self):
        return (
            f"<KnowledgeGapTicket(id={self.id}, status='{self.status}', "
            f"fed_back={self.fed_back}, needs_review={self.needs_review})>"
        )
