"""Milvus / Elasticsearch 索引：建表、探测分词、按文件增删。

约定：
- 子块主键 child_id，Milvus / ES 共用同一 ID；
- Milvus 只做向量召回；ES 子块只做关键词（BM25），父块按 id 取全文。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

import time

from kylin.platform.runtime.documents import Document

from kylin.platform.config.settings import Settings
from kylin.platform.observability.logging import build_logger

logger = build_logger()

# Milvus collection / ES index 仅允许 ASCII 字母数字下划线；中文等非 ASCII 用短哈希兜底。
_SAFE_NAME_RE = re.compile(r"[^0-9A-Za-z_]+")
_SAFE_NAME_UNDERSCORES = re.compile(r"_+")

_MILVUS_ALIAS = "kylin_dual"

# ES 配置在进程启动时由 settings 模块一次性加载,运行期不变；
# IK 检测结果在进程内按 client 指纹缓存,默认 60s TTL 过期自动重探测,
# 覆盖"ES 容器重启后装/卸 IK 插件"这类 Python 进程不知情的场景。
# 显式 reset_ik_probe_cache() 立即清空（调试 / 装完插件想立刻重探测时用）。
_IK_PROBE_TTL_SECONDS: float = 60.0
_IK_PROBE_CACHE: Dict[str, Tuple[bool, float]] = {}

_VARCHAR = {
    "pk": 64,
    "text": 8192,
    "source": 512,
    "oa_doc_id": 64,
    "doc_id": 64,
    "child_id": 64,
    "title_path": 1024,
    "section_title": 256,
    "content_type": 16,
    "content_hash": 64,
    "department_id": 64,
    "access_depts": 512,
}
_SCALAR_FIELDS = (
    "source",
    "oa_doc_id",
    "doc_id",
    "child_id",
    "title_path",
    "section_title",
    "content_type",
    "content_hash",
    "department_id",
    "access_depts",
)

ParentInput = Union[Document, Tuple[str, Document]]


def safe_kb_name(kb_name: str) -> str:
    """把知识库名收成 Milvus/ES 可用的 ASCII 标识。

    Python ``str.isalnum()`` 会把中文当成合法字符，导致
    ``oa操作手册_children`` 被 Milvus 拒绝（code=1100）。
    含非 ASCII 时附加短哈希，避免只剩 ``oa`` 这类碰撞名。
    """
    raw = (kb_name or "").strip()
    ascii_part = _SAFE_NAME_UNDERSCORES.sub("_", _SAFE_NAME_RE.sub("_", raw)).strip("_").lower()
    has_non_ascii = any(ord(ch) > 127 for ch in raw)
    if not ascii_part or has_non_ascii:
        digest = hashlib.md5(raw.encode("utf-8")).hexdigest()[:10]
        ascii_part = f"{ascii_part}_{digest}" if ascii_part else f"kb_{digest}"
    if ascii_part[0].isdigit():
        ascii_part = f"kb_{ascii_part}"
    return ascii_part[:200]


def children_collection_name(kb_name: str) -> str:
    return f"{safe_kb_name(kb_name)}_children"


def children_index_name(kb_name: str) -> str:
    return f"{safe_kb_name(kb_name)}_children"


def parents_index_name(kb_name: str) -> str:
    return f"{safe_kb_name(kb_name)}_parents"


def embedding_dim() -> int:
    return int(getattr(Settings.kb_settings, "EMBEDDING_DIM", 1024) or 1024)


# ---------- connections ----------

def get_es_client():
    from elasticsearch import Elasticsearch

    cfg = Settings.kb_settings.kbs_config.get("es") or {}
    scheme = cfg.get("scheme") or "http"
    host = cfg.get("host") or "127.0.0.1"
    port = cfg.get("port") or "9200"
    kwargs: Dict[str, Any] = {"hosts": [f"{scheme}://{host}:{port}"]}
    user, password = cfg.get("user") or "", cfg.get("password") or ""
    if user and password:
        kwargs["basic_auth"] = (user, password)
    return Elasticsearch(**kwargs)


def milvus_connect() -> str:
    from pymilvus import connections

    if connections.has_connection(_MILVUS_ALIAS):
        return _MILVUS_ALIAS
    cfg = Settings.kb_settings.kbs_config.get("milvus") or {}
    connections.connect(
        alias=_MILVUS_ALIAS,
        host=cfg.get("host") or "127.0.0.1",
        port=str(cfg.get("port") or "19530"),
        user=cfg.get("user") or None,
        password=cfg.get("password") or None,
        secure=bool(cfg.get("secure") or False),
    )
    return _MILVUS_ALIAS


def es_has_ik(client=None, *, force: bool = False) -> bool:
    """探测 ES 是否已装 analysis-ik。未装时建索引走默认分词，不在代码里下载插件。

    进程级缓存：按 ES client 指纹（hosts 串）做 key，结果带 TTL 过期。
    同一进程同一 ES client 在 TTL 内复用；force=True 跳过缓存立即重探测，
    覆盖"装完 IK 还没到 TTL"这种想立刻确认的场景。
    """
    client = client or get_es_client()
    key = _es_client_key(client)
    now = time.monotonic()
    if not force:
        cached = _IK_PROBE_CACHE.get(key)
        if cached is not None:
            has_ik, ts = cached
            if (now - ts) < _IK_PROBE_TTL_SECONDS:
                return has_ik
    has_ik = _probe_ik_via_indices(client)
    _IK_PROBE_CACHE[key] = (has_ik, now)
    return has_ik


def reset_ik_probe_cache() -> None:
    """清空缓存。下次 es_has_ik 会重新探测（不论 TTL 是否过期）。"""
    _IK_PROBE_CACHE.clear()


def _es_client_key(client) -> str:
    """用 client 的 hosts 配置做轻量指纹。失败兜底 id()。"""
    try:
        from elasticsearch import Elasticsearch

        if not isinstance(client, Elasticsearch):
            return f"id:{id(client)}"
        hosts = list(getattr(client, "hosts", []) or [])
        if not hosts:
            return f"id:{id(client)}"
        # hosts 可能重复,先排序再拼
        sig = "|".join(sorted(str(h) for h in hosts))
        return sig
    except Exception:
        return f"id:{id(client)}"


def _probe_ik_via_indices(client) -> bool:
    """实际打一次 ES probe:ik_max_word、ik_smart 任一能跑通即视为已装。"""
    try:
        client.indices.analyze(analyzer="ik_max_word", text="中文分词测试")
        logger.info("IK 探测命中 (indices.analyze analyzer=ik_max_word)")
        return True
    except Exception:
        pass
    try:
        client.indices.analyze(
            body={"analyzer": "ik_max_word", "text": "中文分词测试"}
        )
        logger.info("IK 探测命中 (indices.analyze body=...)")
        return True
    except Exception:
        pass
    return False


def _text_field_mapping(has_ik: bool) -> Dict[str, Any]:
    if has_ik:
        return {
            "type": "text",
            "analyzer": "ik_max_word",
            "search_analyzer": "ik_smart",
        }
    return {"type": "text"}


# ---------- ensure ----------

def ensure_indexes(kb_name: str, dim: Optional[int] = None) -> Dict[str, str]:
    """创建（已存在则跳过）Milvus 子块 collection、ES 子块/父块 index。"""
    dim = dim or embedding_dim()
    es = get_es_client()
    has_ik = es_has_ik(es)
    if has_ik:
        logger.info("Elasticsearch 已启用 IK 分词（ik_max_word / ik_smart）")
    else:
        logger.warning(
            "Elasticsearch 未检测到 IK 插件，子块/父块将用默认分词。"
            "中文关键词召回会变弱。可在 ES 容器内安装 analysis-ik 后重启。"
        )
    _ensure_es_children(es, kb_name, has_ik)
    _ensure_es_parents(es, kb_name, has_ik)
    _ensure_milvus_children(kb_name, dim)
    return {
        "milvus_children": children_collection_name(kb_name),
        "es_children": children_index_name(kb_name),
        "es_parents": parents_index_name(kb_name),
        "analyzer": "ik_max_word" if has_ik else "default",
    }


def _ensure_es_children(es, kb_name: str, has_ik: bool) -> None:
    name = children_index_name(kb_name)
    if es.indices.exists(index=name):
        return
    text = _text_field_mapping(has_ik)
    keyword = {"type": "keyword"}
    mappings = {
        "properties": {
            "text": text,
            "title_path": {**text, "fields": {"keyword": keyword}},
            "section_title": {**text, "fields": {"keyword": keyword}},
            "doc_title": {**text, "fields": {"keyword": keyword}},
            "source": keyword,
            "oa_doc_id": keyword,
            "doc_id": keyword,
            "child_id": keyword,
            "content_type": keyword,
            "content_hash": keyword,
            "department_id": keyword,
            "access_depts": keyword,
            "schema_version": keyword,
            "parent_index": {"type": "integer"},
            "child_index": {"type": "integer"},
        }
    }
    es.indices.create(index=name, mappings=mappings)
    logger.info(f"已创建 ES 子块索引 {name}")


def _ensure_es_parents(es, kb_name: str, has_ik: bool) -> None:
    name = parents_index_name(kb_name)
    if es.indices.exists(index=name):
        return
    text = _text_field_mapping(has_ik)
    keyword = {"type": "keyword"}
    mappings = {
        "properties": {
            "text": text,
            "title_path": {**text, "fields": {"keyword": keyword}},
            "section_title": {**text, "fields": {"keyword": keyword}},
            "doc_title": {**text, "fields": {"keyword": keyword}},
            "source": keyword,
            "oa_doc_id": keyword,
            "doc_id": keyword,
            "content_type": keyword,
            "content_hash": keyword,
            "department_id": keyword,
            "access_depts": keyword,
            "schema_version": keyword,
            "parent_index": {"type": "integer"},
            "level": {"type": "integer"},
        }
    }
    es.indices.create(index=name, mappings=mappings)
    logger.info(f"已创建 ES 父块索引 {name}")


def _ensure_milvus_children(kb_name: str, dim: int) -> None:
    from pymilvus import (
        Collection,
        CollectionSchema,
        DataType,
        FieldSchema,
        utility,
    )

    alias = milvus_connect()
    name = children_collection_name(kb_name)
    if utility.has_collection(name, using=alias):
        Collection(name, using=alias).load()
        return

    fields = [
        FieldSchema(
            name="pk",
            dtype=DataType.VARCHAR,
            is_primary=True,
            auto_id=False,
            max_length=_VARCHAR["pk"],
        ),
        FieldSchema(name="text", dtype=DataType.VARCHAR, max_length=_VARCHAR["text"]),
        FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=dim),
    ]
    for key in _SCALAR_FIELDS:
        fields.append(
            FieldSchema(name=key, dtype=DataType.VARCHAR, max_length=_VARCHAR[key])
        )
    schema = CollectionSchema(fields=fields, description=f"child chunks for {kb_name}")
    collection = Collection(name=name, schema=schema, using=alias)
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
            field_name="source",
            index_params={"index_type": "INVERTED"},
        )
    except Exception as e:
        logger.warning(f"Milvus source 标量索引创建失败（可忽略）: {e}")
    collection.load()
    logger.info(f"已创建 Milvus collection {name} dim={dim} metric=COSINE")


# ---------- helpers ----------

def _clip(value: Any, max_len: int) -> str:
    text = "" if value is None else str(value)
    if len(text) <= max_len:
        return text
    return text[:max_len]


def _serialize_access_depts(value: Any) -> str:
    """把 access_depts（list[str] 或已序列化字符串）归一为 `;D1;D2;`。

    空列表/None/空串 = 全开放（返回空串）。
    """
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        items = [str(v).strip() for v in value if str(v).strip()]
        return ";" + ";".join(items) + ";" if items else ""
    text = str(value).strip()
    if not text:
        return ""
    if not text.startswith(";"):
        items = [v.strip() for v in text.split(";") if v.strip()]
        return ";" + ";".join(items) + ";" if items else ""
    return text


def _access_depts_list(value: Any) -> list[str]:
    """access_depts → ES keyword 数组（空 = 全开放，存空数组）。"""
    serialized = _serialize_access_depts(value)
    if not serialized:
        return []
    return [item for item in serialized.split(";") if item]


def _meta(doc: Document) -> Dict[str, Any]:
    return dict(doc.metadata or {})


def _child_id(doc: Document) -> str:
    meta = _meta(doc)
    cid = meta.get("child_id") or meta.get("id") or ""
    if not cid:
        raise ValueError("子块缺少 child_id，无法写入双索引")
    return _clip(cid, _VARCHAR["pk"])


def _parent_id(item: ParentInput) -> Tuple[str, Document]:
    if isinstance(item, tuple) and len(item) == 2:
        pid, doc = item
        return str(pid), doc
    doc = item  # type: ignore[assignment]
    pid = _meta(doc).get("doc_id") or ""
    if not pid:
        raise ValueError("父块缺少 doc_id，无法写入 ES")
    return str(pid), doc


def _escape_milvus(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _child_row(doc: Document) -> Dict[str, str]:
    meta = _meta(doc)
    cid = _child_id(doc)
    return {
        "pk": cid,
        "text": _clip(doc.page_content or "", _VARCHAR["text"]),
        "source": _clip(meta.get("source"), _VARCHAR["source"]),
        "oa_doc_id": _clip(meta.get("oa_doc_id"), _VARCHAR["oa_doc_id"]),
        "doc_id": _clip(meta.get("doc_id"), _VARCHAR["doc_id"]),
        "child_id": cid,
        "title_path": _clip(meta.get("title_path"), _VARCHAR["title_path"]),
        "section_title": _clip(meta.get("section_title"), _VARCHAR["section_title"]),
        "content_type": _clip(meta.get("content_type") or "text", _VARCHAR["content_type"]),
        "content_hash": _clip(meta.get("content_hash"), _VARCHAR["content_hash"]),
        "department_id": _clip(meta.get("department_id"), _VARCHAR["department_id"]),
        "access_depts": _clip(
            _serialize_access_depts(meta.get("access_depts")), _VARCHAR["access_depts"]
        ),
    }


def _es_child_body(doc: Document) -> Dict[str, Any]:
    meta = _meta(doc)
    body = {
        "text": doc.page_content or "",
        "source": meta.get("source") or "",
        "oa_doc_id": meta.get("oa_doc_id") or "",
        "doc_id": meta.get("doc_id") or "",
        "child_id": _child_id(doc),
        "title_path": meta.get("title_path") or "",
        "section_title": meta.get("section_title") or "",
        "doc_title": meta.get("doc_title") or "",
        "content_type": meta.get("content_type") or "text",
        "content_hash": meta.get("content_hash") or "",
        "department_id": meta.get("department_id") or "",
        "access_depts": _access_depts_list(meta.get("access_depts")),
        "schema_version": meta.get("schema_version") or "",
    }
    if meta.get("parent_index") is not None:
        body["parent_index"] = int(meta["parent_index"])
    if meta.get("child_index") is not None:
        body["child_index"] = int(meta["child_index"])
    return body


def _es_parent_body(doc: Document) -> Dict[str, Any]:
    meta = _meta(doc)
    body = {
        "text": doc.page_content or "",
        "source": meta.get("source") or "",
        "oa_doc_id": meta.get("oa_doc_id") or "",
        "doc_id": meta.get("doc_id") or "",
        "title_path": meta.get("title_path") or "",
        "section_title": meta.get("section_title") or "",
        "doc_title": meta.get("doc_title") or "",
        "content_type": meta.get("content_type") or "text",
        "content_hash": meta.get("content_hash") or "",
        "department_id": meta.get("department_id") or "",
        "access_depts": _access_depts_list(meta.get("access_depts")),
        "schema_version": meta.get("schema_version") or "",
    }
    if meta.get("parent_index") is not None:
        body["parent_index"] = int(meta["parent_index"])
    if meta.get("level") is not None:
        body["level"] = int(meta["level"])
    return body


# ---------- write / delete ----------

def delete_by_source(kb_name: str, source: str) -> None:
    if not source:
        return
    _delete_milvus_by_source(kb_name, source)
    _delete_es_by_source(kb_name, source)


def _delete_milvus_by_source(kb_name: str, source: str) -> None:
    from pymilvus import Collection, utility

    alias = milvus_connect()
    name = children_collection_name(kb_name)
    if not utility.has_collection(name, using=alias):
        return
    collection = Collection(name, using=alias)
    expr = f'source == "{_escape_milvus(source)}"'
    try:
        collection.delete(expr)
        collection.flush()
    except Exception as e:
        logger.warning(f"Milvus 按 source 删除失败 {source}: {e}")


def _delete_es_by_source(kb_name: str, source: str) -> None:
    es = get_es_client()
    query = {"term": {"source": source}}
    for index in (children_index_name(kb_name), parents_index_name(kb_name)):
        if not es.indices.exists(index=index):
            continue
        try:
            es.delete_by_query(index=index, query=query, refresh=True, conflicts="proceed")
        except Exception as e:
            logger.warning(f"ES 按 source 删除失败 {index} {source}: {e}")


def write_children(
    kb_name: str,
    docs: Sequence[Document],
    embeddings: Sequence[Sequence[float]],
) -> List[Dict[str, Any]]:
    if len(docs) != len(embeddings):
        raise ValueError("子块数量与向量数量不一致")
    if not docs:
        return []
    _write_milvus_children(kb_name, docs, embeddings)
    _write_es_children(kb_name, docs)
    return [{"id": _child_id(doc), "metadata": _meta(doc)} for doc in docs]


# Milvus query 需要输出的标量字段（含主键 pk 与向量 embedding）。
# 与 ES 子块 body 字段保持一致，供「只刷权限不重灌」读回后原样重写。
_CHILD_QUERY_SCALARS = ("pk", "text") + _SCALAR_FIELDS + ("embedding",)


def query_children_by_source(
    kb_name: str, source: str
) -> List[Tuple[Document, List[float]]]:
    """按源文件从 Milvus 读回全部子块（含原始向量）。

    用于「只更新元数据、不重新切分/不重新 embed」的刷权限操作：
    调用方改 metadata 后用原向量 write_children 原样回写。
    返回 [(Document, embedding), ...]；无子块或 collection 不存在返回 []。
    """
    from pymilvus import Collection, utility

    alias = milvus_connect()
    name = children_collection_name(kb_name)
    if not utility.has_collection(name, using=alias):
        return []
    collection = Collection(name, using=alias)
    schema_fields = _milvus_fields(collection)
    output_fields = list(_CHILD_QUERY_SCALARS)
    # 存量 collection 可能缺新增字段：只请求实际存在的字段，避免 query 报错。
    if schema_fields is not None:
        output_fields = [f for f in output_fields if f in schema_fields]
    expr = f'source == "{_escape_milvus(source)}"'
    try:
        rows = collection.query(expr=expr, output_fields=output_fields)
    except Exception as e:
        from kylin.adapters.outbound.knowledge_base.retrieve.errors import (
            RetrievalBackendError,
        )

        raise RetrievalBackendError(
            "milvus", kb_name, f"按 source 查询子块失败: {e}"
        ) from e

    out: List[Tuple[Document, List[float]]] = []
    for row in rows:
        meta = {k: row.get(k) for k in _SCALAR_FIELDS if k in output_fields}
        if "pk" in output_fields:
            meta["pk"] = row.get("pk")
        if "child_id" not in meta:
            meta["child_id"] = row.get("pk") or ""
        text = row.get("text") or ""
        vec = row.get("embedding") or []
        out.append((Document(page_content=text, metadata=meta), list(vec)))
    return out


def update_children_access_depts(
    kb_name: str, source: str, access_depts: Sequence[str]
) -> int:
    """只改写某个源文件全部子块的 access_depts，不动内容/向量。

    空列表 = 全开放。从 Milvus 读回原始向量，改 access_depts 后原样
    写回 Milvus + ES（引用 write_children），避免重新切分/重新 embed。
    返回被更新的子块数。
    """
    new_depts = [str(v).strip() for v in (access_depts or []) if str(v).strip()]
    items = query_children_by_source(kb_name, source)
    if not items:
        return 0
    docs = []
    vecs = []
    for doc, vec in items:
        doc.metadata["access_depts"] = new_depts
        docs.append(doc)
        vecs.append(vec)
    write_children(kb_name, docs, vecs)
    return len(docs)


def write_parents(kb_name: str, parent_docs: Iterable[ParentInput]) -> None:
    items = list(parent_docs or [])
    if not items:
        return
    from elasticsearch.helpers import bulk

    es = get_es_client()
    index = parents_index_name(kb_name)
    actions = []
    for item in items:
        pid, doc = _parent_id(item)
        actions.append(
            {
                "_op_type": "index",
                "_index": index,
                "_id": pid,
                "_source": _es_parent_body(doc),
            }
        )
    bulk(es, actions, refresh=True)


def _write_milvus_children(
    kb_name: str,
    docs: Sequence[Document],
    embeddings: Sequence[Sequence[float]],
) -> None:
    from pymilvus import Collection

    alias = milvus_connect()
    collection = Collection(children_collection_name(kb_name), using=alias)
    # 存量 collection 可能缺新增字段（如 department_id/access_depts）：按实际 schema 裁剪，
    # 避免 upsert 报"字段不存在"。缺字段的库 = 无权限字段 = 文档默认全开放（语义一致）。
    schema_fields = _milvus_fields(collection)
    rows = []
    for doc, vec in zip(docs, embeddings):
        row = _child_row(doc)
        if schema_fields is not None:
            row = {k: v for k, v in row.items() if k in schema_fields}
        row["embedding"] = list(vec)
        rows.append(row)
    collection.upsert(rows)
    collection.flush()


# 进程内缓存：collection 字段集（按 collection 名）。重建后字段变化，需失效缓存。
_MILVUS_FIELDS_CACHE: Dict[str, set] = {}


def _milvus_fields(collection) -> Optional[set]:
    """返回 collection 的实际标量字段集（缓存）；探测失败返回 None（不裁剪）。

    缓存以 collection.name 为 key；drop_indexes 重建时会清空缓存。
    """
    name = collection.name
    cached = _MILVUS_FIELDS_CACHE.get(name)
    if cached is not None:
        return cached
    try:
        fields = {f.name for f in collection.schema.fields}
        _MILVUS_FIELDS_CACHE[name] = fields
        return fields
    except Exception as e:
        logger.warning(f"Milvus schema 探测失败 {name}: {e}")
        return None


def _write_es_children(kb_name: str, docs: Sequence[Document]) -> None:
    from elasticsearch.helpers import bulk

    es = get_es_client()
    index = children_index_name(kb_name)
    actions = [
        {
            "_op_type": "index",
            "_index": index,
            "_id": _child_id(doc),
            "_source": _es_child_body(doc),
        }
        for doc in docs
    ]
    bulk(es, actions, refresh=True)


def get_children_by_ids(kb_name: str, ids: Sequence[str]) -> List[Document]:
    if not ids:
        return []
    es = get_es_client()
    index = children_index_name(kb_name)
    docs: List[Document] = []
    for doc_id in ids:
        try:
            resp = es.get(index=index, id=doc_id)
            src = resp.get("_source") or {}
            text = src.pop("text", "")
            docs.append(Document(page_content=text, metadata=src))
        except Exception as e:
            logger.warning(f"ES 取子块失败 {doc_id}: {e}")
    return docs


def get_parents_by_ids(kb_name: str, ids: Sequence[str]) -> Dict[str, Document]:
    """按 doc_id 批量取父块，返回 {doc_id: Document}（缺的 id 不在结果里）。"""
    ordered = [str(i) for i in ids if i]
    if not ordered:
        return {}
    es = get_es_client()
    index = parents_index_name(kb_name)
    if not es.indices.exists(index=index):
        return {}
    try:
        resp = es.mget(index=index, ids=ordered)
    except Exception as e:
        # ES 故障 ≠ 无父块：抛类型化异常，避免父块回填失败被伪装成「缺证据」
        from kylin.adapters.outbound.knowledge_base.retrieve.errors import RetrievalBackendError

        raise RetrievalBackendError(
            "elasticsearch", kb_name, f"批量取父块失败: {e}"
        ) from e
    out: Dict[str, Document] = {}
    for item in resp.get("docs") or []:
        if not item.get("found"):
            continue
        pid = str(item.get("_id") or "")
        src = dict(item.get("_source") or {})
        text = src.pop("text", "") or ""
        if not pid:
            pid = str(src.get("doc_id") or "")
        if not pid:
            continue
        meta = src
        meta.setdefault("doc_id", pid)
        meta.setdefault("id", pid)
        out[pid] = Document(page_content=text, metadata=meta)
    return out


def drop_indexes(kb_name: str) -> None:
    """删除双索引；集合/索引不存在或命名非法时不阻断删库。"""
    from pymilvus import utility

    name = children_collection_name(kb_name)
    _MILVUS_FIELDS_CACHE.pop(name, None)
    try:
        alias = milvus_connect()
        if utility.has_collection(name, using=alias):
            utility.drop_collection(name, using=alias)
    except Exception as error:
        logger.warning(f"删除 Milvus collection 跳过 kb={kb_name} name={name}: {error}")

    try:
        es = get_es_client()
        for index in (children_index_name(kb_name), parents_index_name(kb_name)):
            try:
                if es.indices.exists(index=index):
                    es.indices.delete(index=index)
            except Exception as error:
                logger.warning(f"删除 ES index 跳过 kb={kb_name} index={index}: {error}")
    except Exception as error:
        logger.warning(f"连接 ES 删除索引跳过 kb={kb_name}: {error}")
