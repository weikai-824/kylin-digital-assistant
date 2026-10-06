"""块级元数据约定（最小集，可追加）。"""

from __future__ import annotations

METADATA_SCHEMA_VERSION = "1"

# 文档级（调用方传入，可空）
DOC_FIELDS = (
    "source",
    "oa_doc_id",
    "doc_title",
    "doc_updated_at",
    "content_hash",
)

# 块级（enrich 写入）
CHUNK_FIELDS = (
    "schema_version",
    "doc_id",  # ParentDocumentRetriever 约定：指向父块 id
    "child_id",
    "parent_index",
    "child_index",
    "title_path",
    "section_title",
    "content_type",  # text | table
)
