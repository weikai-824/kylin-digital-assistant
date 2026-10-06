"""父子切分 → LangChain Document（供入库 / KnowledgeFile 共用）。"""

from __future__ import annotations

from typing import Dict, List, Tuple

from kylin.platform.runtime.documents import Document

from kylin.platform.config.settings import Settings
from kylin.adapters.outbound.knowledge_base.metadata import enrich_chunks
from kylin.adapters.outbound.knowledge_base.split.parent_child import split_markdown_parent_child


def split_text_to_parent_child_docs(
    text: str,
    *,
    metadata: Dict = None,
    parent_chunk_size: int = None,
    child_chunk_size: int = None,
    child_overlap: int = None,
) -> Tuple[List[Tuple[str, Document]], List[Document]]:
    """父子切分 + 元数据 enrichment → (父块列表, 子块列表)。

    子块 metadata.doc_id 指向父块 id。
    """
    doc_meta = dict(metadata or {})
    parents = split_markdown_parent_child(
        text,
        parent_chunk_size=parent_chunk_size
        if parent_chunk_size is not None
        else Settings.kb_settings.PARENT_CHUNK_SIZE,
        child_chunk_size=child_chunk_size
        if child_chunk_size is not None
        else Settings.kb_settings.CHILD_CHUNK_SIZE,
        child_overlap=child_overlap
        if child_overlap is not None
        else Settings.kb_settings.CHILD_OVERLAP_SIZE,
    )
    enrich_chunks(parents, doc_meta=doc_meta)

    parent_docs: List[Tuple[str, Document]] = []
    child_docs: List[Document] = []
    for parent in parents:
        parent_docs.append(
            (
                parent.id,
                Document(page_content=parent.text, metadata=dict(parent.metadata)),
            )
        )
        for child in parent.children:
            child_docs.append(
                Document(page_content=child.text, metadata=dict(child.metadata))
            )
    return parent_docs, child_docs


def split_text_to_documents(
    text: str,
    *,
    metadata: Dict = None,
    parent_chunk_size: int = None,
    child_chunk_size: int = None,
    child_overlap: int = None,
) -> List[Document]:
    """仅返回子块 Document 列表（子块 metadata.doc_id 指向父块 id）。"""
    _, child_docs = split_text_to_parent_child_docs(
        text,
        metadata=metadata,
        parent_chunk_size=parent_chunk_size,
        child_chunk_size=child_chunk_size,
        child_overlap=child_overlap,
    )
    return child_docs
