"""双索引入库：子块 → Milvus + ES，父块 → ES。

检索走 knowledge_base.retrieve.dual_search：向量 + BM25 → RRF → Rerank → 父块回填。
"""

from __future__ import annotations

import os
from typing import Dict, List, Sequence

from kylin.platform.runtime.documents import Document

from kylin.adapters.outbound.knowledge_base.kb_service.base import KBService, SupportedVSType
from kylin.adapters.outbound.knowledge_base.kb_service.files import KnowledgeFile
from kylin.adapters.outbound.knowledge_base.kb_service.paths import get_doc_path
from kylin.adapters.outbound.model.clients import get_default_embedding, get_Embeddings
from kylin.platform.observability.logging import build_logger
from kylin.adapters.outbound.knowledge_base.index import (
    delete_by_source,
    drop_indexes,
    embedding_dim,
    ensure_indexes,
    get_children_by_ids,
    write_children,
    write_parents,
)

logger = build_logger()

_EMBED_BATCH = 32


class DualIndexKBService(KBService):
    """生产入库用：双写 Milvus（向量）与 ES（关键词 + 父块）。"""

    def vs_type(self) -> str:
        return SupportedVSType.DUAL

    def do_init(self):
        pass

    def do_create_kb(self):
        ensure_indexes(self.kb_name, dim=embedding_dim())

    def do_drop_kb(self):
        drop_indexes(self.kb_name)
        if os.path.exists(self.kb_path):
            import shutil

            shutil.rmtree(self.kb_path, ignore_errors=True)

    def do_clear_vs(self):
        drop_indexes(self.kb_name)
        ensure_indexes(self.kb_name, dim=embedding_dim())

    def do_search(self, query: str, top_k: int, score_threshold: float, dept: str | None = None, dept_exempt: bool = False):
        from kylin.adapters.outbound.knowledge_base.retrieve.dual_search import dual_search

        return dual_search(
            self.kb_name,
            query,
            top_k=top_k,
            score_threshold=score_threshold,
            embed_model=self.embed_model,
            dept=dept,
            dept_exempt=dept_exempt,
        )

    def get_doc_by_ids(self, ids: List[str]) -> List[Document]:
        return get_children_by_ids(self.kb_name, ids)

    def do_delete_doc(self, kb_file: KnowledgeFile, **kwargs):
        source = kb_file.filename
        delete_by_source(self.kb_name, source)

    def do_update_doc_access_depts(
        self, kb_file: KnowledgeFile, access_depts: list
    ) -> int:
        """只改写某文件全部子块的 access_depts，不重新切分/不重新 embed。"""
        from kylin.adapters.outbound.knowledge_base.index import (
            update_children_access_depts,
        )

        return update_children_access_depts(
            self.kb_name, kb_file.filename, access_depts or []
        )

    def do_add_doc(self, docs: List[Document], **kwargs) -> List[Dict]:
        if not docs:
            return []
        ensure_indexes(self.kb_name, dim=embedding_dim())
        embeddings = _embed_docs(docs, embed_model=self.embed_model)
        doc_infos = write_children(self.kb_name, docs, embeddings)
        write_parents(self.kb_name, kwargs.get("parent_docs") or [])
        return doc_infos


def _embed_docs(docs: Sequence[Document], embed_model: str = None) -> List[List[float]]:
    embedder = get_Embeddings(embed_model)
    texts = [d.page_content or "" for d in docs]
    vectors: List[List[float]] = []
    for i in range(0, len(texts), _EMBED_BATCH):
        batch = texts[i : i + _EMBED_BATCH]
        vectors.extend(embedder.embed_documents(batch))
    return vectors


def ensure_kb(kb_name: str, kb_info: str = "", embed_model: str = None) -> DualIndexKBService:
    """确保双索引知识库存在（目录 + MySQL 台账 + 空索引），并返回服务实例。"""
    from kylin.adapters.outbound.knowledge_base.kb_service.factory import KBServiceFactory

    embed_model = embed_model or get_default_embedding()
    existing = KBServiceFactory.get_service_by_name(kb_name)
    if existing is not None:
        current = existing.vs_type()
        if current != SupportedVSType.DUAL:
            raise RuntimeError(
                f"知识库 {kb_name} 已存在，类型为 {current}，不能改成双索引。"
                f"请换一个新库名（例如 oa_kb）。"
            )
        ensure_indexes(kb_name, dim=embedding_dim())
        return existing  # type: ignore[return-value]

    os.makedirs(get_doc_path(kb_name), exist_ok=True)
    kb = DualIndexKBService(
        knowledge_base_name=kb_name,
        kb_info=kb_info,
        embed_model=embed_model,
    )
    kb.create_kb()
    logger.info(f"已创建双索引知识库 {kb_name}")
    return kb
