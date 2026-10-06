"""将检索文档装配为模型可引用的编号上下文."""

from __future__ import annotations

from typing import Any, Dict, List, Sequence

from kylin.ports.data.knowledge_search import KnowledgeDocument


def _meta_label(meta: Dict[str, Any]) -> str:
    source = (
        meta.get("source")
        or meta.get("doc_title")
        or meta.get("oa_doc_id")
        or "未知来源"
    )
    section = meta.get("section_title") or meta.get("title_path") or ""
    if section:
        return f"来源：{source} | 章节：{section}"
    return f"来源：{source}"


def assemble_context(docs: Sequence[Dict[str, Any] | KnowledgeDocument]) -> str:
    """将检索结果格式化为带来源编号的参考资料."""
    blocks: List[str] = []
    for index, doc in enumerate(docs, start=1):
        if isinstance(doc, KnowledgeDocument):
            meta = doc.metadata
            body = (doc.page_content or "").strip()
        else:
            meta = doc.get("metadata") or {}
            body = (doc.get("page_content") or "").strip()
        blocks.append(f"[{index}] {_meta_label(meta)}\n{body}")
    return "\n\n".join(blocks)
