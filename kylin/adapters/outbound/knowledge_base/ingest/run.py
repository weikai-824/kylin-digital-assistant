"""入库编排：落盘 → 解析 → 切分 → 写入双索引。

数据源适配器只负责拉取与变更决策；写入统一走本模块。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Dict, List, Optional

from kylin.adapters.outbound.knowledge_base.kb_service.files import KnowledgeFile
from kylin.adapters.outbound.knowledge_base.kb_service.paths import get_file_path
from kylin.platform.observability.logging import build_logger
from kylin.adapters.outbound.knowledge_base.parse import SUPPORTED_PARSE_EXTS, TextDocumentLoader
from kylin.adapters.outbound.knowledge_base.split.to_documents import split_text_to_parent_child_docs

logger = build_logger()

# 支持类型与 parse 层保持单一来源
SUPPORTED_EXTS = set(SUPPORTED_PARSE_EXTS) | {".md"}


def write_kb_file(kb_name: str, filename: str, content: bytes) -> str:
    """把文件内容写入知识库目录，返回绝对路径。"""
    file_path = get_file_path(knowledge_base_name=kb_name, doc_name=filename)
    if not file_path:
        raise RuntimeError(f"非法文件路径: {filename}")
    Path(file_path).parent.mkdir(parents=True, exist_ok=True)
    with open(file_path, "wb") as f:
        f.write(content)
    return file_path


def load_document(file_path: str, filename: str, doc_meta: Optional[Dict] = None):
    """解析单个文档 → 清洗 → Document（PDF/DOCX 走 Docling，Markdown 走文本加载器）。"""
    from kylin.platform.runtime.documents import Document

    from kylin.adapters.outbound.knowledge_base.clean import clean_markdown_text
    from kylin.adapters.outbound.knowledge_base.parse import parse_document

    if not file_path or not Path(file_path).is_file():
        return []
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED_EXTS:
        logger.warning(f"跳过不支持的类型: {filename}")
        return []
    try:
        if ext == ".md":
            documents = TextDocumentLoader(file_path).load()
            raw_text = "\n\n".join(document.page_content for document in documents)
        else:
            result = parse_document(file_path)
            raw_text = result.markdown or ""
        text = clean_markdown_text(raw_text).strip()
        if not text:
            return []
        meta: dict = {"source": filename}
        if doc_meta:
            meta.update(doc_meta)
        meta["content_hash"] = hashlib.md5(text.encode("utf-8")).hexdigest()
        return [Document(page_content=text, metadata=meta)]
    except Exception as e:
        logger.error(f"文档解析失败 {filename}: {e}")
        raise


def ingest_file(kb, kb_name: str, filename: str, doc_meta: Optional[Dict] = None) -> None:
    """解析文件并写入双索引：子块 → Milvus + ES，父块 → ES。"""
    kb_file = KnowledgeFile(filename=filename, knowledge_base_name=kb_name)
    file_path = get_file_path(knowledge_base_name=kb_name, doc_name=filename)
    docs = load_document(file_path, filename, doc_meta=doc_meta)
    if not docs:
        raise RuntimeError(f"文件无可解析文本: {filename}")

    merged = "\n\n".join(d.page_content for d in docs if d.page_content)
    base_meta = dict(docs[0].metadata or {})
    parent_docs, child_docs = split_text_to_parent_child_docs(merged, metadata=base_meta)
    if not child_docs:
        raise RuntimeError(f"文件无可切分文本: {filename}")
    kb.update_doc(
        kb_file,
        docs=child_docs,
        parent_docs=parent_docs,
        not_refresh_vs_cache=False,
    )


def delete_kb_files(kb_name: str, filenames: List[str], kb) -> None:
    """从知识库删除文件（文件 + 向量 + 记录）。"""
    for filename in filenames:
        try:
            kb_file = KnowledgeFile(filename=filename, knowledge_base_name=kb_name)
            kb.delete_doc(kb_file, delete_content=True, not_refresh_vs_cache=False)
        except Exception as e:
            logger.warning(f"删除本地知识库文件失败 {filename}: {e}")
