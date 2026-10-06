"""知识库文件对象与文档加载器。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Generator, List, Optional, Tuple, Union

from kylin.platform.runtime.documents import Document

from kylin.platform.runtime.async_utils import run_in_thread_pool
from kylin.platform.config.settings import Settings
from kylin.platform.observability.logging import build_logger
from kylin.adapters.outbound.knowledge_base.clean import clean_markdown_text
from kylin.adapters.outbound.knowledge_base.kb_service.paths import get_file_path
from kylin.adapters.outbound.knowledge_base.parse import DoclingDocumentLoader, SUPPORTED_PARSE_EXTS, TextDocumentLoader
from kylin.adapters.outbound.knowledge_base.split import split_text_to_parent_child_docs, split_text_to_documents

logger = build_logger()

# PDF / DOCX -> Docling；工单反哺等 Markdown 走项目内部文本 loader
LOADER_DICT = {
    "DoclingDocumentLoader": sorted(SUPPORTED_PARSE_EXTS),
    "TextDocumentLoader": [".md"],
}
SUPPORTED_EXTS = [ext for exts in LOADER_DICT.values() for ext in exts]


def get_LoaderClass(file_extension):
    for LoaderClass, extensions in LOADER_DICT.items():
        if file_extension in extensions:
            return LoaderClass


def get_loader(
    loader_name: str,
    file_path: str,
    loader_kwargs: Optional[Dict] = None,
):
    """根据 loader_name 返回项目内部文档加载器。"""
    loader_kwargs = loader_kwargs or {}
    if not loader_name:
        raise ValueError(f"没有为文件找到加载器: {file_path}")
    loader_classes = {
        "DoclingDocumentLoader": DoclingDocumentLoader,
        "TextDocumentLoader": TextDocumentLoader,
    }
    document_loader = loader_classes.get(loader_name)
    if document_loader is None:
        raise ValueError(f"不支持的加载器 {loader_name}: {file_path}")
    return document_loader(file_path, **loader_kwargs)


class KnowledgeFile:
    def __init__(
        self,
        filename: str,
        knowledge_base_name: str,
        loader_kwargs: Optional[Dict] = None,
    ):
        """
        对应知识库目录中的文件，必须是磁盘上存在的才能进行向量化等操作。
        """
        self.kb_name = knowledge_base_name
        self.filename = str(Path(filename).as_posix())
        self.ext = os.path.splitext(filename)[-1].lower()
        if self.ext not in SUPPORTED_EXTS:
            raise ValueError(f"暂未支持的文件格式 {self.filename}")
        self.loader_kwargs = loader_kwargs or {}
        self.filepath = get_file_path(knowledge_base_name, filename)
        self.docs = None
        self.splited_docs = None
        self.parent_docs = None  # List[Tuple[str, Document]]，双索引父块
        self.document_loader_name = get_LoaderClass(self.ext)
        self.text_splitter_name = Settings.kb_settings.TEXT_SPLITTER_NAME

    def file2docs(self, refresh: bool = False):
        if self.docs is None or refresh:
            logger.info(f"{self.document_loader_name} used for {self.filepath}")
            loader = get_loader(
                loader_name=self.document_loader_name,
                file_path=self.filepath,
                loader_kwargs=self.loader_kwargs,
            )
            self.docs = loader.load()
        return self.docs

    def docs2texts(
        self,
        docs: List[Document] = None,
        refresh: bool = False,
        chunk_size: int = None,
        chunk_overlap: int = None,
        text_splitter=None,
        **_ignored,
    ):
        # 缺省值延迟到调用时解析：避免 import 即读配置
        if chunk_size is None:
            chunk_size = Settings.kb_settings.CHILD_CHUNK_SIZE
        if chunk_overlap is None:
            chunk_overlap = Settings.kb_settings.CHILD_OVERLAP_SIZE
        docs = docs or self.file2docs(refresh=refresh)
        if not docs:
            return []
        # 切分前清洗：叠字/空表行/水印/空图占位等（不丢正文）
        cleaned_docs = []
        for d in docs:
            cleaned = clean_markdown_text(d.page_content or "").strip()
            if cleaned:
                cleaned_docs.append(
                    Document(
                        page_content=cleaned,
                        metadata=dict(d.metadata or {}),
                    )
                )
        docs = cleaned_docs
        if not docs:
            return []
        if self.ext not in [".csv"]:
            # 多 Document 时合并后切分（Docling 通常一篇一文）
            merged = "\n\n".join(d.page_content for d in docs)
            base_meta = dict(docs[0].metadata or {})
            parent_docs, child_docs = split_text_to_parent_child_docs(
                merged,
                metadata=base_meta,
                parent_chunk_size=Settings.kb_settings.PARENT_CHUNK_SIZE,
                child_chunk_size=chunk_size,
                child_overlap=chunk_overlap,
            )
            self.parent_docs = parent_docs
            docs = child_docs
        else:
            self.parent_docs = []

        if not docs:
            return []

        print(f"文档切分示例：{docs[0]}")
        self.splited_docs = docs
        return self.splited_docs

    def file2text(
        self,
        refresh: bool = False,
        chunk_size: int = None,
        chunk_overlap: int = None,
        text_splitter=None,
        **_ignored,
    ):
        if self.splited_docs is None or refresh:
            docs = self.file2docs()
            self.splited_docs = self.docs2texts(
                docs=docs,
                refresh=refresh,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                text_splitter=text_splitter,
            )
        return self.splited_docs

    def file_exist(self):
        return os.path.isfile(self.filepath)

    def get_mtime(self):
        return os.path.getmtime(self.filepath)

    def get_size(self):
        return os.path.getsize(self.filepath)


def files2docs_in_thread_file2docs(
    *, file: KnowledgeFile, **kwargs
) -> Tuple[bool, Tuple]:
    try:
        child_docs = file.file2text(**kwargs)
        parent_docs = list(file.parent_docs or [])
        return True, (file.kb_name, file.filename, child_docs, parent_docs)
    except Exception as e:
        msg = f"从文件 {file.kb_name}/{file.filename} 加载文档时出错：{e}"
        logger.error(f"{e.__class__.__name__}: {msg}")
        return False, (file.kb_name, file.filename, msg)


def files2docs_in_thread(
    files: List[Union[KnowledgeFile, Tuple[str, str], Dict]],
    chunk_size: int = None,
    chunk_overlap: int = None,
    **_ignored,
) -> Generator:
    """
    利用多线程批量将磁盘文件转化成 langchain Document。
    成功时返回 status, (kb_name, file_name, child_docs, parent_docs)
    失败时返回 status, (kb_name, file_name, error)
    """
    if chunk_size is None:
        chunk_size = Settings.kb_settings.CHILD_CHUNK_SIZE
    if chunk_overlap is None:
        chunk_overlap = Settings.kb_settings.CHILD_OVERLAP_SIZE

    kwargs_list = []
    for i, file in enumerate(files):
        kwargs = {}
        try:
            if isinstance(file, tuple) and len(file) >= 2:
                filename = file[0]
                kb_name = file[1]
                file = KnowledgeFile(filename=filename, knowledge_base_name=kb_name)
            elif isinstance(file, dict):
                filename = file.pop("filename")
                kb_name = file.pop("kb_name")
                kwargs.update(file)
                file = KnowledgeFile(filename=filename, knowledge_base_name=kb_name)
            kwargs["file"] = file
            kwargs["chunk_size"] = chunk_size
            kwargs["chunk_overlap"] = chunk_overlap
            kwargs_list.append(kwargs)
        except Exception as e:
            yield False, (kb_name, filename, str(e))

    for result in run_in_thread_pool(
        func=files2docs_in_thread_file2docs, params=kwargs_list
    ):
        yield result
