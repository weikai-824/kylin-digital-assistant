"""长期记忆适配器：MySQL（事实源 + 去重）+ Milvus（向量召回）+ bge-m3（向量化）。

写入链路 remember：content_hash 精确去重 → MySQL 落库 → 向量化 → Milvus upsert。
读取链路 recall：query 向量化 → Milvus 按 user_id 过滤召回 id → MySQL 回表 →
按 importance 加权截断。任一步失败都降级，不影响主问答链路。
"""

from __future__ import annotations

from collections.abc import Sequence

from kylin.adapters.outbound.long_term_memory import milvus
from kylin.adapters.outbound.mysql.database.repository import (
    long_term_memory_repository as repo,
)
from kylin.adapters.outbound.model.clients import get_Embeddings, get_default_embedding
from kylin.ports.data.long_term_memory import LongTermMemory
from kylin.platform.observability.logging import build_logger

logger = build_logger()


class KylinLongTermMemoryAdapter:
    """把长期记忆端口映射到 MySQL + Milvus 双写实现。"""

    def __init__(self):
        self._embeddings = None

    # ---------- embedding ----------

    def _get_embeddings(self):
        if self._embeddings is None:
            self._embeddings = get_Embeddings(embed_model=get_default_embedding())
        return self._embeddings

    def _embed(self, texts: Sequence[str]):
        """向量化；失败返回 None（调用方降级）。"""
        try:
            return self._get_embeddings().embed_documents(list(texts))
        except Exception as error:  # noqa: BLE001
            logger.warning("长期记忆向量化失败: %s", error)
            return None

    # ---------- 写入 ----------

    def remember(
        self,
        user_id: str,
        memories: Sequence[LongTermMemory],
        *,
        session_id: str = "",
        kb_name: str = "",
    ) -> int:
        """批量写入记忆；同 user 重复内容跳过。返回新增条数（0 = 无可写或全重复）。"""
        user_id = (user_id or "").strip()
        if not user_id or not memories:
            return 0
        added = 0
        rows_to_upsert = []
        for memory in memories:
            content = (memory.content or "").strip()
            if not content:
                continue
            h = repo.content_hash(content)
            if repo.find_active_by_content_hash(user_id, h):
                continue  # 完全同内容直接忽略
            row = repo.create_memory(
                user_id=user_id,
                content=content,
                category=memory.category or "fact",
                importance=max(1, min(5, int(memory.importance or 3))),
                session_id=session_id or "",
                kb_name=kb_name or "",
                content_hash_value=h,
            )
            added += 1
            rows_to_upsert.append(row)

        if rows_to_upsert:
            vectors = self._embed([row.content for row in rows_to_upsert])
            if vectors is not None:
                milvus.upsert_memories(
                    [
                        {
                            "id": row.id,
                            "user_id": row.user_id,
                            "content": row.content,
                            "category": row.category,
                            "importance": row.importance,
                            "status": row.status,
                            "embedding": list(vec),
                        }
                        for row, vec in zip(rows_to_upsert, vectors)
                    ]
                )
            else:
                # 向量化失败：MySQL 已落库，检索侧暂时召回不到（保底不丢事实）
                logger.warning(
                    "长期记忆向量化失败，%d 条仅落 MySQL（待后续补向量）",
                    len(rows_to_upsert),
                )
        return added

    # ---------- 读取 ----------

    def recall(self, user_id: str, query: str, top_k: int = 5) -> Sequence[LongTermMemory]:
        """按语义相似度召回该用户最相关的记忆；任一步失败降级为 importance 兜底。"""
        user_id = (user_id or "").strip()
        if not user_id or not (query or "").strip() or top_k <= 0:
            return []
        vector = self._embed([query])
        if vector is None:
            # 向量化失败：退化为"高重要度最新"（保证链路不断）
            rows = repo.list_active(user_id)[:top_k]
            return [self._to_memory(row) for row in rows]
        ids = milvus.search_memories(user_id, vector[0], top_k=top_k)
        if not ids:
            return []
        by_id = repo.list_by_ids(ids)
        rows = [by_id[i] for i in ids if i in by_id]
        if not rows:
            return []
        repo.touch_retrieved([row.id for row in rows])
        # importance 加权：语义相近时高重要度优先
        rows.sort(key=lambda r: (r.importance, r.id), reverse=True)
        return [self._to_memory(row) for row in rows[:top_k]]

    def list_memories(self, user_id: str) -> Sequence[LongTermMemory]:
        user_id = (user_id or "").strip()
        if not user_id:
            return []
        return [self._to_memory(row) for row in repo.list_active(user_id)]

    def forget(self, user_id: str, memory_id: int) -> bool:
        user_id = (user_id or "").strip()
        if not user_id or not memory_id:
            return False
        ok = repo.supersede_memory(user_id, memory_id)
        if ok:
            milvus.delete_memory_row(memory_id, user_id)
        return ok

    # ---------- helpers ----------

    @staticmethod
    def _to_memory(row) -> LongTermMemory:
        return LongTermMemory(
            id=row.id,
            user_id=row.user_id,
            session_id=row.session_id or "",
            kb_name=row.kb_name or "",
            content=row.content,
            category=row.category,
            importance=row.importance,
            status=row.status,
            created_at=row.create_time,
        )
