"""双索引存储层：Milvus（向量）+ Elasticsearch（BM25 / 父块）。

入库（ingest）与检索（retrieve）共用本包，避免 retrieve → ingest 反向依赖。
"""

from kylin.adapters.outbound.knowledge_base.index.stores import (
    children_collection_name,
    children_index_name,
    delete_by_source,
    drop_indexes,
    embedding_dim,
    ensure_indexes,
    es_has_ik,
    get_children_by_ids,
    get_es_client,
    get_parents_by_ids,
    milvus_connect,
    parents_index_name,
    query_children_by_source,
    reset_ik_probe_cache,
    safe_kb_name,
    update_children_access_depts,
    write_children,
    write_parents,
)

__all__ = [
    "children_collection_name",
    "children_index_name",
    "delete_by_source",
    "drop_indexes",
    "embedding_dim",
    "ensure_indexes",
    "es_has_ik",
    "get_children_by_ids",
    "get_es_client",
    "get_parents_by_ids",
    "milvus_connect",
    "parents_index_name",
    "query_children_by_source",
    "reset_ik_probe_cache",
    "safe_kb_name",
    "update_children_access_depts",
    "write_children",
    "write_parents",
]
