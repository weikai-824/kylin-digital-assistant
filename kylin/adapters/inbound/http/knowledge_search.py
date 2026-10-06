"""知识库检索 HTTP 适配器：逻辑仍由检索服务提供。"""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import Body, HTTPException

from kylin.ports.data.knowledge_search import (
    KnowledgeBaseNotFoundError,
    KnowledgeBaseUnavailableError,
    KnowledgeDocument,
    KnowledgeSearchError,
    KnowledgeSearchPort,
)
from kylin.platform.config.settings import Settings


class KnowledgeSearchHttpAdapter:
    """把知识检索请求转换为端口调用和兼容响应。"""

    def __init__(self, search_port: KnowledgeSearchPort):
        self._search_port = search_port

    def search_docs(
        self,
        query: str = Body("", description="用户输入", examples=["你好"]),
        knowledge_base_name: str = Body(
            ..., description="知识库名称", examples=["oa_manuals"]
        ),
        top_k: int = Body(None, description="返回文档数；缺省由检索层解析默认"),
        score_threshold: float = Body(
            None,
            description="向量距离阈值，越小越严；取 2 表示不筛",
            ge=0.0,
            le=2.0,
        ),
        file_name: str = Body("", description="文件名称，支持 sql 通配符"),
        metadata: dict = Body({}, description="根据 metadata 过滤，仅支持一级键"),
    ) -> List[Dict[str, Any]]:
        """在指定知识库检索或按文件名和 metadata 列出文档。"""
        resolved_top_k = (
            top_k
            if top_k is not None
            else Settings.kb_settings.VECTOR_SEARCH_TOP_K
        )
        resolved_score_threshold = (
            score_threshold
            if score_threshold is not None
            else Settings.kb_settings.SCORE_THRESHOLD
        )
        try:
            documents = self._search_port.search(
                query=query,
                knowledge_base_name=knowledge_base_name,
                top_k=resolved_top_k,
                score_threshold=resolved_score_threshold,
                file_name=file_name,
                metadata=metadata or {},
            )
        except KnowledgeBaseNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except (KnowledgeBaseUnavailableError, KnowledgeSearchError) as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        return [self._to_response(document) for document in documents]

    @staticmethod
    def _to_response(document: KnowledgeDocument) -> Dict[str, Any]:
        return {
            "page_content": document.page_content,
            "metadata": dict(document.metadata),
            "id": document.document_id,
            "score": document.score,
        }
