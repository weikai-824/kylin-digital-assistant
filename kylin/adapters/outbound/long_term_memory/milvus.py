"""长期记忆 Milvus 向量索引：memory_vec collection 的创建 / 写入 / 检索 / 删除。

与知识库子块索引（stores.py）隔离：记忆检索永远按 user_id 过滤，
collection 全局只有一张，不做知识库维度拆分。
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence

from kylin.adapters.outbound.knowledge_base.index.stores import milvus_connect
from kylin.platform.config.settings import Settings
from kylin.platform.observability.logging import build_logger

logger = build_logger()

MEMORY_COLLECTION = "memory_vec"
_USER_ID_LEN = 64
_CONTENT_LEN = 8192
_CATEGORY_LEN = 20
_STATUS_LEN = 20


def memory_dim() -> int:
    return int(getattr(Settings.kb_settings, "EMBEDDING_DIM", 1024) or 1024)


def _escape(value: str) -> str:
    """Milvus expr 字符串转义（反斜杠与双引号）。"""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def ensure_memory_collection() -> str:
    """创建（已存在则加载）memory_vec；由 bootstrap 在启动时调用。"""
    from pymilvus import Collection, CollectionSchema, DataType, FieldSchema, utility

    alias = milvus_connect()
    if utility.has_collection(MEMORY_COLLECTION, using=alias):
        Collection(MEMORY_COLLECTION, using=alias).load()
        return MEMORY_COLLECTION

    fields = [
        FieldSchema(name="id", dtype=DataType.INT64, is_primary=True, auto_id=False),
        FieldSchema(name="user_id", dtype=DataType.VARCHAR, max_length=_USER_ID_LEN),
        FieldSchema(name="content", dtype=DataType.VARCHAR, max_length=_CONTENT_LEN),
        FieldSchema(name="category", dtype=DataType.VARCHAR, max_length=_CATEGORY_LEN),
        FieldSchema(name="importance", dtype=DataType.INT32),
        FieldSchema(name="status", dtype=DataType.VARCHAR, max_length=_STATUS_LEN),
        FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=memory_dim()),
    ]
    schema = CollectionSchema(fields=fields, description="跨会话长期记忆向量索引")
    collection = Collection(name=MEMORY_COLLECTION, schema=schema, using=alias)
    collection.create_index(
        field_name="embedding",
        index_params={
            "index_type": "HNSW",
            "metric_type": "COSINE",
            "params": {"M": 16, "efConstruction": 200},
        },
    )
    try:
        collection.create_index(
            field_name="user_id",
            index_params={"index_type": "INVERTED"},
        )
    except Exception as error:  # noqa: BLE001 —— 标量索引非必需
        logger.warning(f"Milvus memory_vec user_id 标量索引创建失败（可忽略）: {error}")
    collection.load()
    logger.info(
        "已创建 Milvus collection %s dim=%s metric=COSINE",
        MEMORY_COLLECTION,
        memory_dim(),
    )
    return MEMORY_COLLECTION


def upsert_memories(rows: Sequence[Dict[str, Any]]) -> None:
    """批量写入/覆盖记忆向量行（每行必须含 id/user_id/content/category/importance/status/embedding）。"""
    from pymilvus import Collection

    if not rows:
        return
    collection = Collection(MEMORY_COLLECTION, using=milvus_connect())
    collection.upsert(list(rows))
    collection.flush()


def delete_memory_row(memory_id: int, user_id: str) -> None:
    """按 id + user_id 删除记忆向量（软删时同步清理）。"""
    from pymilvus import Collection, utility

    alias = milvus_connect()
    if not utility.has_collection(MEMORY_COLLECTION, using=alias):
        return
    collection = Collection(MEMORY_COLLECTION, using=alias)
    expr = f'id == {int(memory_id)} && user_id == "{_escape(user_id)}"'
    try:
        collection.delete(expr)
        collection.flush()
    except Exception as error:  # noqa: BLE001
        logger.warning(f"Milvus 删除记忆失败 id={memory_id}: {error}")


def search_memories(
    user_id: str,
    query_vector: Sequence[float],
    top_k: int,
) -> List[int]:
    """按向量召回该用户 active 记忆的 id 列表（相关度降序）。异常时返回空列表。"""
    from pymilvus import Collection, utility

    alias = milvus_connect()
    if not utility.has_collection(MEMORY_COLLECTION, using=alias):
        return []
    collection = Collection(MEMORY_COLLECTION, using=alias)
    expr = f'user_id == "{_escape(user_id)}" && status == "active"'
    try:
        results = collection.search(
            data=[list(query_vector)],
            anns_field="embedding",
            param={"metric_type": "COSINE", "params": {"ef": 64}},
            limit=max(top_k * 3, 10),
            expr=expr,
            output_fields=["id"],
        )
    except Exception as error:  # noqa: BLE001
        logger.warning(f"Milvus 记忆检索失败 user={user_id}: {error}")
        return []
    if not results:
        return []
    return [int(hit.entity.get("id")) for hit in results[0]]
