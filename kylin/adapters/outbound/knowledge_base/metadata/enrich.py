"""给父子块补齐元数据。"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from kylin.adapters.outbound.knowledge_base.metadata.schema import METADATA_SCHEMA_VERSION
from kylin.adapters.outbound.knowledge_base.split.parent_child import ChildChunk, ParentChunk, TITLE_SEP

from kylin.adapters.outbound.knowledge_base.clean.patterns import TABLE_ROW, TABLE_SEP


def enrich_chunks(
    parents: List[ParentChunk],
    doc_meta: Optional[Dict[str, Any]] = None,
) -> List[ParentChunk]:
    """在切分结果上写入规范 metadata（原地填充并返回同一列表）。

    doc_meta 可选文档级字段，例如::
        source / oa_doc_id / doc_title / doc_updated_at / content_hash
    """
    base = _normalize_doc_meta(doc_meta)
    for parent in parents:
        section_title = parent.title or _tail_title(parent.title_path)
        parent.metadata = {
            **base,
            "schema_version": METADATA_SCHEMA_VERSION,
            "doc_id": parent.id,
            "parent_index": parent.index,
            "title_path": parent.title_path,
            "section_title": section_title,
            "title": parent.title,
            "level": parent.level,
            "content_type": _detect_content_type(parent.text),
        }
        for child in parent.children:
            child.metadata = {
                **base,
                "schema_version": METADATA_SCHEMA_VERSION,
                "doc_id": parent.id,
                "child_id": child.id,
                "parent_index": parent.index,
                "child_index": child.index,
                "title_path": child.title_path or parent.title_path,
                "section_title": section_title,
                "content_type": _detect_content_type(child.text),
            }
    return parents


def _normalize_doc_meta(doc_meta: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not doc_meta:
        return {}
    out: Dict[str, Any] = {}
    for k, v in doc_meta.items():
        if v is None or v == "":
            continue
        out[str(k)] = v
    if "source" in out and isinstance(out["source"], str):
        out["source"] = out["source"].replace("\\", "/")
    return out


def _tail_title(title_path: str) -> str:
    if not title_path:
        return ""
    return title_path.split(TITLE_SEP)[-1].strip()


def _detect_content_type(text: str) -> str:
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    if len(lines) >= 2 and all(
        TABLE_ROW.match(ln) or TABLE_SEP.match(ln) for ln in lines
    ):
        return "table"
    # 含整表的混合块也标 table，便于后续过滤
    table_lines = sum(
        1 for ln in lines if TABLE_ROW.match(ln) or TABLE_SEP.match(ln)
    )
    if table_lines >= 2 and table_lines >= len(lines) * 0.5:
        return "table"
    return "text"
